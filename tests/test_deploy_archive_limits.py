"""Exercise deploy parsing budgets with ordinary files and small test limits."""
import json
import os
import subprocess
import zipfile

import pytest

from test_deploy_wrapper import NEW_VERSIONS, SOURCE_COMMIT, bundle, host, make_code


@pytest.fixture(autouse=True)
def hide_windows_console(monkeypatch):
    if os.name != "nt":
        return
    popen = subprocess.Popen

    def quiet_popen(*args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | subprocess.CREATE_NO_WINDOW
        return popen(*args, **kwargs)

    monkeypatch.setattr(host.subprocess, "Popen", quiet_popen)


def test_bundle_parser_returns_approval_fields(tmp_path):
    payload, digest = bundle(tmp_path)
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(payload)
    destination = tmp_path / "destination"
    destination.mkdir()

    result = host.parse_untrusted("bundle", archive, destination)

    assert set(result) == {"schema", "source_commit", "plugins"}
    assert all(set(item) == {"id", "version", "archive", "sha256"} for item in result["plugins"])
    assert host.approval_record(digest, result) == {
        "sha256": digest, "source_commit": SOURCE_COMMIT, "versions": NEW_VERSIONS,
    }
    assert (destination / "plugins" / host.LIFE / "metadata.yaml").is_file()


def test_verify_code_parser_checks_expected_version(tmp_path):
    root = tmp_path / host.LIFE
    make_code(root, host.LIFE, "0.1.1")
    assert host.parse_untrusted("verify-code", root, host.LIFE, "0.1.1") == {
        "version": "0.1.1", "release_files_sha256": host.sha(root / "RELEASE_FILES.json"),
    }
    with pytest.raises(ValueError, match="identity/version differs"):
        host.parse_untrusted("verify-code", root, host.LIFE, "0.1.2")


def test_json_parser_honors_explicit_byte_cap(tmp_path):
    document = {"flag": False, "label": "中文"}
    payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
    path = tmp_path / "settings.json"
    path.write_bytes(payload)
    assert host.parse_untrusted("json", path) == document
    assert host.parse_untrusted("json", path, len(payload)) == document
    with pytest.raises(ValueError, match="exceeds byte limit"):
        host.parse_untrusted("json", path, len(payload) - 1)


def test_parser_uses_isolated_python_and_explicit_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LSR_DEPLOY_PARSER_TEST_SENTINEL", "ordinary value")
    monkeypatch.setattr(host, "_PARSER_SCRIPT", """
import json, os, sys
print(json.dumps({'isolated': sys.flags.isolated, 'inherited': 'LSR_DEPLOY_PARSER_TEST_SENTINEL' in os.environ}))
""")
    assert host.parse_untrusted("json", tmp_path / "settings.json") == {"isolated": 1, "inherited": False}


@pytest.mark.parametrize("action", ["bundle", "json", "verify-code"])
def test_ssh_command_rejects_internal_parser_actions(action):
    with pytest.raises(ValueError, match="unsupported command"):
        host.command_args(action)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_parser_capture_is_bounded(tmp_path, monkeypatch, stream):
    monkeypatch.setattr(host, "_PARSER_SCRIPT", f"import sys; print('x' * 8192, file=sys.{stream})")
    with pytest.raises(ValueError, match="output exceeds byte limit"):
        host.parse_untrusted("json", tmp_path / "settings.json", 64)


@pytest.mark.skipif(os.name != "posix", reason="POSIX worker rlimits")
def test_parser_sets_cpu_and_address_space_limits(tmp_path, monkeypatch):
    probe = """
import json, resource
print(json.dumps([resource.getrlimit(resource.RLIMIT_AS), resource.getrlimit(resource.RLIMIT_CPU)]))
raise SystemExit(0)
"""
    monkeypatch.setattr(host, "_PARSER_SCRIPT", host._PARSER_SCRIPT.replace("import runpy", probe))
    assert host.parse_untrusted("json", tmp_path / "settings.json") == [
        [host.PARSER_MAX_MEMORY] * 2, [host.PARSER_CPU_TIMEOUT] * 2]


def test_parser_enforces_wall_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(host, "PARSER_WALL_TIMEOUT", 0.05)
    monkeypatch.setattr(host, "_PARSER_SCRIPT", "import time; time.sleep(10)")
    with pytest.raises(ValueError, match="wall timeout"):
        host.parse_untrusted("json", tmp_path / "settings.json")


@pytest.mark.parametrize("limit", ["MAX_ZIP_ENTRIES", "MAX_ZIP_DIRECTORY"])
def test_zip_central_budget_precedes_zipfile(tmp_path, monkeypatch, limit):
    payload, _ = bundle(tmp_path)
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(payload)
    destination = tmp_path / "destination"
    destination.mkdir()
    monkeypatch.setattr(host, limit, 1)
    monkeypatch.setattr(host.zipfile, "ZipFile", lambda *args: pytest.fail("opened ZIP before budget check"))
    with pytest.raises(ValueError, match="central directory exceeds limits"):
        host.unpack_bundle(archive, destination)


@pytest.mark.parametrize("name", ["RELEASE_FILES.json", "metadata.yaml"])
def test_zip_control_file_budget_precedes_extraction(tmp_path, monkeypatch, name):
    payload, _ = bundle(tmp_path)
    archive = tmp_path / "bundle.tar.gz"
    archive.write_bytes(payload)
    destination = tmp_path / "destination"
    destination.mkdir()
    # Change only the ZIP checks. The paired tar manifest has its own budget.
    entry_size = (tmp_path / "code" / host.HOST / name).stat().st_size
    real = host.paired_manifest

    def lower_after_tar(path):
        result = real(path)
        monkeypatch.setattr(host, "MAX_CONTROL_BYTES", entry_size - 1)
        return result

    monkeypatch.setattr(host, "paired_manifest", lower_after_tar)
    with pytest.raises(ValueError, match="plugin control file exceeds byte limit"):
        host.unpack_bundle(archive, destination)
    assert not (destination / "plugins" / host.HOST / name).exists()


def test_zip64_local_header_refused(tmp_path):
    path = tmp_path / "sample.zip"
    with zipfile.ZipFile(path, "w") as output, output.open("sample.txt", "w", force_zip64=True) as entry:
        entry.write(b"ordinary text")
    with path.open("rb") as raw:
        directory = host.check_zip_directory(raw)
        with zipfile.ZipFile(raw) as source:
            with pytest.raises(ValueError, match="ZIP64 is forbidden"):
                host.check_zip_local(raw, source.getinfo("sample.txt"), directory)


@pytest.mark.skipif(os.name != "nt", reason="Windows reader share mode")
def test_atomic_journal_write_retries_transient_windows_sharing_error(tmp_path, monkeypatch):
    path = tmp_path / "pending.json"
    host.write_json(path, {"state": "queued"})
    replace = os.replace
    attempts = 0

    def transient(source, target):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(13, "another process has the journal open", str(target), 32)
        return replace(source, target)

    monkeypatch.setattr(os, "replace", transient)
    host.write_json(path, {"state": "running"})
    assert attempts == 2
    assert json.loads(path.read_text(encoding="utf-8")) == {"state": "running"}


def test_write_json_uses_unique_temp_and_cleans_failed_write(tmp_path):
    path = tmp_path / "journal.json"
    path.with_name("journal.json.tmp").touch()
    host.write_json(path, {"id": "a"})
    before = set(tmp_path.iterdir())
    with pytest.raises(TypeError):
        host.write_json(path, {"id": object()})
    assert set(tmp_path.iterdir()) == before
    assert json.loads(path.read_text(encoding="utf-8")) == {"id": "a"}
