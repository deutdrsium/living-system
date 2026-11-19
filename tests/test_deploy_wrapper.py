"""Network-free forced-command rehearsal, with a fake Docker and fake data root."""
from contextlib import closing, contextmanager
import gzip
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import select
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import time
from types import SimpleNamespace
from uuid import UUID, uuid4
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("deploy_host", ROOT / "tools/deploy/deploy_host.py")
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)

NO_CONSOLE = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
SOURCE_COMMIT = "a" * 40
NEW_VERSIONS = {host.HOST: "7.1.18", host.LIFE: "0.1.1"}

DOCKER = '''import json, os, pathlib, sqlite3, sys, time
if os.name == 'nt':
    import ctypes
    assert not ctypes.windll.kernel32.GetConsoleWindow(), 'fake Docker received a console window'
root = pathlib.Path(__file__).parent
state = root / 'running'
args = sys.argv[1:]
if (root / 'record-docker-env').exists():
    keys = ('SSH_ORIGINAL_COMMAND', 'LSR_DEPLOY_TEST_ROOT', 'LSR_DEPLOY_REHEARSAL_SENTINEL')
    (root / 'docker-env.json').write_text(json.dumps([key for key in keys if key in os.environ]))
with (root / 'docker-calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
if args[0] == 'inspect':
    if (root / 'slow-inspect').exists():
        time.sleep(20)
    if (root / 'large-inspect-stdout').exists():
        sys.stdout.write('x' * (256 * 1024))
    if (root / 'large-inspect-stderr').exists():
        sys.stderr.write('x' * (256 * 1024))
    running = 'true' if state.exists() else 'false'
    if 'RestartCount' in args[2]:
        health = 'unhealthy' if (root / 'unhealthy-after-start').exists() else 'none'
        restart_count, started_at = '0', '2026-11-18T03:00:00.000000000Z'
        restart = root / 'restart-between-checks'
        if restart.is_file():
            calls = (root / 'docker-calls.jsonl').read_text().count('RestartCount')
            if calls >= 3:
                if restart.read_text() == 'restart-count':
                    restart_count = '1'
                else:
                    started_at = '2026-11-18T03:00:01.000000000Z'
        print(f'{running} {restart_count} {started_at} {health}')
    else:
        print(running)
elif args[0] == 'stop':
    pending = root / 'good-life-deploy/pending.json'
    first_stop = root / 'first-stop-journal.json'
    if pending.is_file() and not first_stop.exists():
        first_stop.write_bytes(pending.read_bytes())
        journal = json.loads(first_stop.read_bytes())
        key = journal['bundle'] if journal['action'] == 'release' else 'rollback-' + journal['backup_id']
        marker = root / 'etc/companion-deploy/approved-bundles' / ('used-' + key)
        if marker.is_file():
            (root / 'first-stop-approval.json').write_bytes(marker.read_bytes())
    state.unlink(missing_ok=True)
    gate = root / 'hold-first-stop'
    entered = root / 'first-stop-held'
    if gate.exists() and not entered.exists():
        entered.touch()
        deadline = time.monotonic() + 20
        while gate.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
elif args[0] == 'start':
    state.touch()
    life = 'astrbot_plugin_good_life'
    metadata = (root / 'plugins' / life / 'metadata.yaml').read_text(encoding='utf-8')
    if (root / 'create-new-domain-on-start').exists() and 'version: 0.1.1' in metadata.splitlines():
        db = sqlite3.connect(root / 'plugin_data' / life / 'good-life.sqlite3')
        with db:
            db.execute('CREATE TABLE IF NOT EXISTS life_kernel_new_history(id TEXT PRIMARY KEY)')
            db.execute("INSERT OR IGNORE INTO life_kernel_new_history VALUES('new-fact')")
        db.close()
        gate = root / 'hold-incompatible-start'
        if gate.exists():
            (root / 'incompatible-start-held').touch()
            deadline = time.monotonic() + 20
            while gate.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
elif args[0] == 'logs':
    if (root / 'large-logs').exists():
        for _ in range(500):
            print('ordinary rehearsal log ' + 'routine event ' * 72)
    if not (root / 'fail-postcheck').exists():
        print('Loading plugin astrbot_plugin_private_companion ...')
        print('Loading plugin astrbot_plugin_good_life ...')
        print('AstrBot started.')
        if not (root / 'missing-life-binding').exists():
            print('[GoodLife] host binding ready')
    print('Authorization: Bearer private-token api_key=secret-value password="hidden"')
else:
    sys.exit(2)
'''


def make_code(root, plugin, version, marker="old"):
    root.mkdir(parents=True)
    (root / "metadata.yaml").write_text(f"name: {plugin}\nversion: {version}\n", encoding="utf-8")
    (root / "main.py").write_text(marker, encoding="utf-8")
    host.write_json(root / "RELEASE_FILES.json", {"plugin_id": plugin, "version": version,
        "source_commit": SOURCE_COMMIT, "files": host.files(root)})


def database(path, table, version):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(f"CREATE TABLE {table}(key TEXT PRIMARY KEY, value TEXT)")
    db.execute(f"INSERT INTO {table} VALUES('schema_version', ?)", (version,))
    return db


@pytest.fixture
def fake_root(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / ".lsr-rehearsal").touch()
    (root / "docker.py").write_text(DOCKER, encoding="utf-8")
    (root / "running").touch()
    (root / "etc/companion-deploy").mkdir(parents=True, mode=0o700)
    (root / "etc/companion-deploy/approved-bundles").mkdir(mode=0o700)
    (root / "config").mkdir()
    (root / "config" / (host.LIFE + "_config.json")).write_text('{"academics_enabled": false}', encoding="utf-8")
    for plugin, ver in ((host.HOST, "7.1.17"), (host.LIFE, "0.1.0")):
        make_code(root / "plugins" / plugin, plugin, ver)
    deploy = host.Deployer(root, rehearsal=True)
    runtime, config, life = deploy.database_paths()
    with closing(database(runtime, "runtime_metadata", "companion-v2.storage.v71")) as db:
        for key in host.OWNER_KEYS:
            db.execute("INSERT INTO runtime_metadata VALUES(?, 'good_life')", (key,))
        sys.path.insert(0, str(ROOT / "tools"))
        from life_migration_preflight import LIFE_TABLES
        for table in LIFE_TABLES:
            db.execute(f"CREATE TABLE {table}(id TEXT)")
        db.execute("CREATE TABLE execution_tasks(task_id TEXT, kind TEXT, state TEXT, attempt_id TEXT, generation INTEGER)")
        db.execute("CREATE TABLE execution_attempts(attempt_id TEXT, task_id TEXT, generation INTEGER, record_json TEXT)")
        db.commit()
    with closing(database(config, "runtime_metadata", "configuration-store.v1")) as db:
        db.execute("INSERT INTO runtime_metadata VALUES(?, 'good_life')", (host.OWNER_KEYS[-1],))
        db.execute("CREATE TABLE operations(owner TEXT, state TEXT)")
        db.commit()
    with closing(database(life, "life_metadata", "good-life.storage.v1")) as db:
        db.execute("INSERT INTO life_metadata VALUES('life.phase1.cutover_ready.v1', 'ready')")
        db.execute("CREATE TABLE life_events(id TEXT PRIMARY KEY)")
        db.commit()
    return root


def wrapper_env(root, command):
    return dict(os.environ, SSH_ORIGINAL_COMMAND=command, LSR_DEPLOY_TEST_ROOT=root.as_posix(),
                LSR_DEPLOY_TEST_PYTHON=Path(sys.executable).as_posix())


def wrapper(root, command, data=None):
    if os.name == "nt":
        # Exercise the same dispatcher without asking Git Bash to launch console programs.
        argv = [sys.executable, "-I", "-X", "utf8", str(ROOT / "tools/deploy/deploy_host.py"),
                "--test-root", str(root)]
    else:
        argv = [shutil.which("bash"), str(ROOT / "tools/deploy/companion-deploy")]
    return subprocess.run(argv, input=data, capture_output=True, env=wrapper_env(root, command),
                          timeout=30, creationflags=NO_CONSOLE)


def invoke(root, command, data=None, ok=True):
    result = wrapper(root, command, data)
    if ok:
        assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
        return json.loads(result.stdout)
    assert result.returncode != 0, result.stdout
    return result


def bundle(tmp_path, *, malicious=None, zip_malicious=None, versions=None):
    release = tmp_path / "release"
    release.mkdir()
    items = []
    for plugin, ver in (versions or NEW_VERSIONS.items()):
        code = tmp_path / "code" / plugin
        make_code(code, plugin, ver, marker="new")
        archive = release / (plugin + ".zip")
        with zipfile.ZipFile(archive, "w") as output:
            for path in code.iterdir():
                output.write(path, plugin + "/" + path.name)
            if zip_malicious and plugin == host.HOST:
                info = zipfile.ZipInfo(zip_malicious)
                info.external_attr = 0o120777 << 16
                output.writestr(info, "/etc/passwd")
        items.append({"id": plugin, "version": ver, "archive": archive.name, "sha256": host.sha(archive)})
    host.write_json(release / "release-manifest.json", {"schema": "good-life.paired-release.v1",
        "source_commit": SOURCE_COMMIT, "plugins": items})
    sums = "".join(f"{host.sha(path)}  {path.name}\n" for path in sorted(release.iterdir()))
    (release / "SHA256SUMS.txt").write_text(sums, encoding="utf-8", newline="\n")
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tarfile.USTAR_FORMAT) as archive:
        for path in sorted(release.iterdir()):
            archive.add(path, arcname=path.name)
        if malicious:
            info = tarfile.TarInfo(malicious[0])
            info.type = malicious[1]
            info.linkname = "/etc/passwd"
            archive.addfile(info, io.BytesIO())
    payload = output.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def upload(root, tmp_path, **kwargs):
    payload, digest = bundle(tmp_path, **kwargs)
    invoke(root, "upload " + digest, payload)
    return digest


def approve(deploy, digest, *, reapprove=False, versions=NEW_VERSIONS):
    deploy.approve(digest, SOURCE_COMMIT, versions[host.HOST], versions[host.LIFE], reapprove=reapprove)
    return deploy.approval_dir / digest


def approve_rollback(deploy, identifier):
    deploy.approve_rollback(identifier, NEW_VERSIONS[host.HOST], NEW_VERSIONS[host.LIFE])
    return deploy.approval_dir / ("rollback-" + identifier)


def submit(root, command):
    queued = invoke(root, command)
    assert queued["state"] == "queued"
    operation_id = queued["operation_id"]
    assert UUID(operation_id).version == 4
    return operation_id


def wait_until(predicate, description, *, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {description}")


def wait_operation(root, operation_id):
    deploy = host.Deployer(root, rehearsal=True)

    def terminal():
        # Poll the journal in-process on Windows to avoid repeatedly spawning
        # the venv launcher while the supervisor and worker are active.
        response = (deploy.operation(operation_id) if os.name == "nt"
                    else invoke(root, "operation " + operation_id))
        assert response["state"] in {"queued", "running", "recovering", "succeeded", "failed", "manual-recovery"}
        return response if response["state"] not in {"queued", "running", "recovering"} else None

    result = wait_until(terminal, f"terminal operation {operation_id}")
    if os.name == "nt":
        assert invoke(root, "operation " + operation_id) == result
    return result


def run_job(root, command):
    result = wait_operation(root, submit(root, command))
    assert result["state"] == "succeeded", result
    return result["result"]


def refused_job(root, command):
    # Pre-claim validation can reject the SSH request immediately. A guard
    # evaluated by the detached job is reported by its terminal operation.
    result = wrapper(root, command)
    if result.returncode:
        return result.stderr.decode("utf-8", errors="replace")
    queued = json.loads(result.stdout)
    assert queued["state"] == "queued"
    terminal = wait_operation(root, queued["operation_id"])
    assert terminal["state"] == "failed", terminal
    return terminal["error"]


def release_pair(root, tmp_path):
    deploy = host.Deployer(root, rehearsal=True)
    digest = upload(root, tmp_path)
    approve(deploy, digest)
    return deploy, run_job(root, "release " + digest)["backup_id"]


def docker_calls(root):
    path = root / "docker-calls.jsonl"
    return [json.loads(line)[0] for line in path.read_text().splitlines()] if path.exists() else []


def stopped_backup(deploy):
    identifier = "20000101t000000z-" + uuid4().hex[:12]
    assert not (deploy.root / "running").exists()
    deploy.backup(identifier)
    return identifier


def stage_payload(deploy, directory, monkeypatch, payload, digest):
    # Exercise the real reader with a binary file descriptor and ordinary EOF.
    stdin = directory / "stdin.tar.gz"
    stdin.write_bytes(payload)
    with stdin.open(encoding="utf-8") as stream, monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", stream)
        deploy.upload(digest)
    return digest


def test_status_read_only_and_logs_redaction(fake_root):
    before = [host.sha(path) for path in host.Deployer(fake_root, rehearsal=True).database_paths()]
    status = invoke(fake_root, "status")
    assert status["container_running"] is True
    assert status["plugins"][host.LIFE]["version"] == "0.1.0"
    assert all(value is False for value in status["flags"].values())
    result = wrapper(fake_root, "logs 500")
    assert result.returncode == 0, result.stderr
    assert b"private-token" not in result.stdout and b"secret-value" not in result.stdout and b"hidden" not in result.stdout
    assert [host.sha(path) for path in host.Deployer(fake_root, rehearsal=True).database_paths()] == before


@pytest.mark.parametrize("command", ["", "status extra", "logs 0", "logs 501", "logs -1", "logs 1000",
    "set-flag academics true", "status;id", "status\nbackup", "upload ../../x", "rollback ../../x", "$(id)",
    f"approve {'0' * 64} {SOURCE_COMMIT} 7.1.18 0.1.1"])
def test_strict_arguments(fake_root, command):
    invoke(fake_root, command, ok=False)
    assert not (fake_root / "docker-calls.jsonl").exists()
    assert "request=" in (fake_root / "companion-deploy.log").read_text(encoding="utf-8")


def test_upload_bad_sha(fake_root, tmp_path):
    payload, _ = bundle(tmp_path)
    invoke(fake_root, "upload " + "0" * 64, payload, ok=False)
    assert not list((fake_root / "good-life-deploy/staging").iterdir())


@pytest.mark.parametrize("name,kind", [("../escape", tarfile.REGTYPE), ("/absolute", tarfile.REGTYPE),
    ("symlink", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE), ("a/../../escape", tarfile.REGTYPE)])
def test_upload_rejects_tar_paths_and_links(fake_root, tmp_path, name, kind):
    payload, digest = bundle(tmp_path, malicious=(name, kind))
    invoke(fake_root, "upload " + digest, payload, ok=False)
    assert not (fake_root / "good-life-deploy/staging" / digest).exists()


@pytest.mark.parametrize("name", [host.HOST + "/link", host.HOST + "/../escape", "/absolute", "C:/absolute"])
def test_upload_rejects_zip_paths_and_symlinks(fake_root, tmp_path, name):
    payload, digest = bundle(tmp_path, zip_malicious=name)
    invoke(fake_root, "upload " + digest, payload, ok=False)


@pytest.mark.parametrize("tar_format,header", [
    (tarfile.PAX_FORMAT, tarfile.XHDTYPE), (tarfile.GNU_FORMAT, tarfile.GNUTYPE_LONGNAME),
])
def test_upload_rejects_ordinary_extended_tar_metadata(fake_root, tar_format, header):
    contents = b"ordinary archive member\n"
    name = "release-notes.txt" if tar_format == tarfile.PAX_FORMAT else "release-notes-" + "2026-" * 20 + ".txt"
    member = tarfile.TarInfo(name)
    member.size = len(contents)
    if tar_format == tarfile.PAX_FORMAT:
        member.pax_headers = {"comment": "ordinary release metadata"}
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz", format=tar_format) as archive:
        archive.addfile(member, io.BytesIO(contents))
    payload = output.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        assert archive.getnames() == [name]
        assert archive.extractfile(name).read() == contents
    raw = gzip.decompress(payload)
    assert len(raw) == tarfile.RECORDSIZE
    assert raw[156:157] == header
    result = invoke(fake_root, "upload " + digest, payload, ok=False)
    assert b"extended tar headers" in result.stderr
    assert not list((fake_root / "good-life-deploy/staging").iterdir())


def test_upload_bounds_raw_tar_size(tmp_path, monkeypatch):
    payload, _ = bundle(tmp_path)
    archive = tmp_path / "ordinary.tar.gz"
    archive.write_bytes(payload)
    assert len(gzip.decompress(payload)) == tarfile.RECORDSIZE
    bounded = tmp_path / "bounded"
    exact = tmp_path / "exact"
    bounded.mkdir()
    exact.mkdir()
    # The ordinary record padding alone crosses this reduced limit.
    monkeypatch.setattr(host, "MAX_TAR_BYTES", tarfile.RECORDSIZE - 1)
    with pytest.raises(ValueError, match="decompressed tar exceeds byte limit"):
        host.unpack_bundle(archive, bounded)
    monkeypatch.setattr(host, "MAX_TAR_BYTES", tarfile.RECORDSIZE)
    assert host.unpack_bundle(archive, exact)["source_commit"] == SOURCE_COMMIT


def test_approval_checks_exact_release_when_already_uploaded(fake_root, tmp_path):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = upload(fake_root, tmp_path)
    cases = [("b" * 40, NEW_VERSIONS[host.HOST], NEW_VERSIONS[host.LIFE]),
             (SOURCE_COMMIT, "7.1.19", NEW_VERSIONS[host.LIFE]),
             (SOURCE_COMMIT, NEW_VERSIONS[host.HOST], "0.1.2")]
    for fields in cases:
        with pytest.raises(ValueError, match="does not match"):
            deploy.approve(digest, *fields)
        assert not (deploy.approval_dir / digest).exists()
    entry = approve(deploy, digest)
    record = json.loads(entry.read_text(encoding="utf-8"))
    assert UUID(record.pop("approval_id")).version == 4
    assert record == {"sha256": digest, "source_commit": SOURCE_COMMIT, "versions": NEW_VERSIONS}
    with pytest.raises(ValueError, match="already approved"):
        approve(deploy, digest)


def test_release_without_approval_refused_before_docker(fake_root, tmp_path):
    digest = upload(fake_root, tmp_path)
    result = invoke(fake_root, "release " + digest, ok=False)
    assert b"not approved" in result.stderr
    assert not (fake_root / "docker-calls.jsonl").exists()
    assert (fake_root / "good-life-deploy/staging" / digest / "verified.json").is_file()


@pytest.mark.parametrize("approve_before_edit", [True, False], ids=["verified-record", "bundle-manifest"])
def test_release_checks_approval_against_bundle_manifest(fake_root, tmp_path, approve_before_edit):
    digest = upload(fake_root, tmp_path)
    deploy = host.Deployer(fake_root, rehearsal=True)
    if approve_before_edit:
        approve(deploy, digest)
    # Change only staged metadata. Depending on when approval was granted,
    # it disagrees with the verified record or with the actual archive.
    path = deploy.base / "staging" / digest / "verified.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["source_commit"] = "b" * 40
    host.write_json(path, record)
    if not approve_before_edit:
        deploy.approve(digest, record["source_commit"], NEW_VERSIONS[host.HOST], NEW_VERSIONS[host.LIFE])
    assert "does not match" in refused_job(fake_root, "release " + digest)
    assert set(docker_calls(fake_root)) == {"inspect"}  # Read-only settle checks, no service change.
    assert (deploy.approval_dir / digest).is_file()


@pytest.mark.skipif(os.name == "nt", reason="POSIX approval ownership and permissions")
@pytest.mark.parametrize("component", ["entry", "directory", "parent"])
def test_release_rejects_writable_allowlist(fake_root, tmp_path, component):
    digest = upload(fake_root, tmp_path)
    deploy = host.Deployer(fake_root, rehearsal=True)
    entry = approve(deploy, digest)
    paths = {"entry": entry, "directory": entry.parent, "parent": entry.parent.parent}
    path = paths[component]
    mode = path.stat().st_mode & 0o777
    try:
        path.chmod(mode | 0o020)
        result = invoke(fake_root, "release " + digest, ok=False)
        assert b"unsafe ownership, permissions or file type" in result.stderr
    finally:
        path.chmod(mode)
    assert not (fake_root / "docker-calls.jsonl").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX approval ownership")
def test_release_rejects_allowlist_entry_with_wrong_owner(fake_root, tmp_path, monkeypatch):
    digest = upload(fake_root, tmp_path)
    deploy = host.Deployer(fake_root, rehearsal=True)
    entry = approve(deploy, digest)
    lstat = Path.lstat

    def wrong_owner(path, *args, **kwargs):
        info = lstat(path, *args, **kwargs)
        if path == entry:
            return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid + 1, st_gid=info.st_gid)
        return info

    monkeypatch.setattr(Path, "lstat", wrong_owner)
    with pytest.raises(ValueError, match="unsafe ownership, permissions or file type"):
        deploy.approval(digest)
    assert not (fake_root / "docker-calls.jsonl").exists()


def test_release_rejects_symlink_allowlist_entry(fake_root, tmp_path):
    digest = upload(fake_root, tmp_path)
    deploy = host.Deployer(fake_root, rehearsal=True)
    entry = approve(deploy, digest)
    original = tmp_path / "approval.json"
    entry.rename(original)
    try:
        entry.symlink_to(original)
    except OSError:
        if os.name == "nt":
            pytest.skip("symlink creation is unavailable")
        raise
    result = invoke(fake_root, "release " + digest, ok=False)
    assert b"unsafe ownership, permissions or file type" in result.stderr
    assert not (fake_root / "docker-calls.jsonl").exists()


def versioned_bundle(tmp_path, name, life_version):
    directory = tmp_path / name
    directory.mkdir()
    payload, digest = bundle(directory, versions=((host.HOST, NEW_VERSIONS[host.HOST]), (host.LIFE, life_version)))
    return directory, payload, digest


def test_upload_count_quota_prunes_only_expired_unprotected_entries(fake_root, tmp_path, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    staging = deploy.base / "staging"
    monkeypatch.setattr(host, "UPLOAD_QUOTA_COUNT", 4)
    monkeypatch.setattr(host, "UPLOAD_EXPIRY", 3600)
    directory, payload, digest = versioned_bundle(tmp_path, "approved", "0.1.1")
    approved = stage_payload(deploy, directory, monkeypatch, payload, digest)
    entry = approve(deploy, approved)
    existing = {}
    for role, version in (("pending", "0.1.2"), ("expired", "0.1.3"), ("fresh", "0.1.4")):
        directory, payload, digest = versioned_bundle(tmp_path, role, version)
        existing[role] = stage_payload(deploy, directory, monkeypatch, payload, digest)
    host.write_json(deploy.base / "pending.json", {"bundle": existing["pending"], "state": "installing"})
    expired_at = time.time() - host.UPLOAD_EXPIRY - 60
    for digest in (approved, existing["pending"], existing["expired"]):
        os.utime(staging / digest / "verified.json", (expired_at, expired_at))

    directory, payload, digest = versioned_bundle(tmp_path, "replacement", "0.1.5")
    replacement = stage_payload(deploy, directory, monkeypatch, payload, digest)
    expected = {approved, existing["pending"], existing["fresh"], replacement}
    assert {path.name for path in staging.iterdir()} == expected
    assert entry.is_file()

    directory, payload, digest = versioned_bundle(tmp_path, "overflow", "0.1.6")
    with pytest.raises(ValueError, match="quota"):
        stage_payload(deploy, directory, monkeypatch, payload, digest)
    assert {path.name for path in staging.iterdir()} == expected


@pytest.mark.parametrize("remaining_delta", [-1, 0], ids=["archive-bytes", "archive-and-metadata-bytes"])
def test_upload_byte_quota_is_aggregate(fake_root, tmp_path, monkeypatch, remaining_delta):
    deploy = host.Deployer(fake_root, rehearsal=True)
    staging = deploy.base / "staging"
    directory, payload, digest = versioned_bundle(tmp_path, "kept", "0.1.1")
    kept = stage_payload(deploy, directory, monkeypatch, payload, digest)
    used = sum(path.stat().st_size for path in (staging / kept).rglob("*") if path.is_file())
    directory, payload, digest = versioned_bundle(tmp_path, "overflow", "0.1.2")
    monkeypatch.setattr(host, "UPLOAD_QUOTA_BYTES", used + len(payload) + remaining_delta
                        + host.MAX_UPLOAD + host.UPLOAD_METADATA_BUDGET)
    with pytest.raises(ValueError, match="aggregate byte quota"):
        stage_payload(deploy, directory, monkeypatch, payload, digest)
    assert {path.name for path in staging.iterdir()} == {kept}


def test_disk_reserve_refuses_upload_and_backup_before_data_writes(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    calls = []

    def disk_usage(path):
        calls.append(path)
        return SimpleNamespace(free=0)

    monkeypatch.setattr(host.shutil, "disk_usage", disk_usage)
    with pytest.raises(ValueError, match="insufficient free space"):
        deploy.upload("0" * 64)
    assert calls == [deploy.base]
    assert not list((deploy.base / "staging").glob("*"))
    with pytest.raises(ValueError, match="insufficient free space"):
        deploy.backup("20000101t000000z-0123456789ab")
    assert calls == [deploy.base, deploy.base]
    assert not (deploy.base / "backups").exists()
    assert (fake_root / "running").exists()
    assert docker_calls(fake_root) == []


UPLOAD_HELPER = """
import runpy
import sys

helper, idle, total, root = sys.argv[1:]
main = runpy.run_path(helper)["main"]
main.__globals__.update(UPLOAD_IDLE_TIMEOUT=float(idle), UPLOAD_TOTAL_TIMEOUT=float(total))
sys.argv = [helper, "--test-root", root]
main()
"""


@contextmanager
def open_upload(root, digest, *, idle, total):
    helper = ROOT / "tools/deploy/deploy_host.py"
    env = dict(os.environ, SSH_ORIGINAL_COMMAND="upload " + digest)
    with subprocess.Popen([sys.executable, "-X", "utf8", "-I", "-c", UPLOAD_HELPER,
                           str(helper), str(idle), str(total), str(root)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          bufsize=0, env=env, creationflags=NO_CONSOLE) as process:
        try:
            staging = root / "good-life-deploy/staging"
            until = time.monotonic() + 5
            while not list(staging.glob("incoming-*/bundle.tar.gz")):
                if process.poll() is not None:
                    pytest.fail(f"upload exited before accepting input: {process.stderr.read()!r}")
                assert time.monotonic() < until, "upload did not start reading stdin"
                time.sleep(0.01)
            yield process
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_upload_idle_timeout_with_open_stdin(fake_root, tmp_path):
    payload, digest = bundle(tmp_path)
    with open_upload(fake_root, digest, idle=1, total=10) as process:
        process.stdin.write(payload[:1])
        assert process.wait(timeout=5) != 0
        assert b"upload idle timeout" in process.stderr.read()
    assert not list((fake_root / "good-life-deploy/staging").iterdir())


def test_upload_total_timeout_while_input_keeps_arriving(fake_root, tmp_path):
    payload, digest = bundle(tmp_path)
    with open_upload(fake_root, digest, idle=1, total=2.5) as process:
        delivered = 0
        until = time.monotonic() + 5
        while process.poll() is None and time.monotonic() < until:
            try:
                process.stdin.write(payload[delivered:delivered + 1])
            except BrokenPipeError:
                break
            delivered += 1
            time.sleep(0.05)
        assert 10 <= delivered < len(payload)
        assert process.wait(timeout=1) != 0
        assert b"upload total timeout" in process.stderr.read()
    assert not list((fake_root / "good-life-deploy/staging").iterdir())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux flock regression")
def test_stalled_upload_allows_wrapper_status(fake_root, tmp_path):
    payload, digest = bundle(tmp_path)
    with open_upload(fake_root, digest, idle=10, total=15) as process:
        deploy = host.Deployer(fake_root, rehearsal=True)
        with host.exclusive(deploy.base / "action.lock"):
            assert invoke(fake_root, "status")["container_running"] is True
        assert process.poll() is None
        assert docker_calls(fake_root) == ["inspect"]
        stdout, stderr = process.communicate(input=payload, timeout=5)
        assert process.returncode == 0, stderr
        assert json.loads(stdout)["verified"] == digest
    assert invoke(fake_root, "status")["container_running"] is True


def test_audit_rotation_keeps_bounded_ordered_generations(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    monkeypatch.setattr(host, "AUDIT_MAX_BYTES", 100)
    monkeypatch.setattr(host, "AUDIT_GENERATIONS", 2)
    messages = [f"audit event {index}: " + "x" * 20 for index in range(6)]
    for message in messages:
        deploy.log(message)
    for generation, message in enumerate(reversed(messages[-3:])):
        path = deploy.log_path if generation == 0 else deploy.log_path.with_suffix(f".log.{generation}")
        data = path.read_bytes()
        assert len(data) <= host.AUDIT_MAX_BYTES
        assert len(data.splitlines()) == 1
        assert data.endswith(f" {message}\n".encode())
    assert not deploy.log_path.with_suffix(".log.3").exists()


def test_release_creates_fresh_backup_under_one_stop(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    manifest = deploy.verify_backup(deploy.base / "backups" / identifier)
    assert len(manifest["files"]) >= 13
    assert manifest["plugins"][host.HOST]["version"] == "7.1.17"
    assert len(list((deploy.base / "backups" / identifier / "sqlite").glob("*.sqlite3"))) == 3
    assert [call for call in docker_calls(fake_root) if call in {"stop", "start"}] == ["stop", "start"]
    assert (fake_root / "running").exists()


def test_backup_retention_preserves_active_pending_and_last_operation(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    (fake_root / "running").unlink()
    protected = {}
    for journal in ("active.json", "pending.json", "last-operation.json"):
        identifier = stopped_backup(deploy)
        host.write_json(deploy.base / journal, {"backup_id": identifier})
        protected[journal] = identifier
    oldest = stopped_backup(deploy)
    for identifier in (*protected.values(), oldest):
        # These directories are managed by ID and are retained together.
        (deploy.base / "previous" / identifier).mkdir(parents=True)
        (deploy.base / "failed" / (identifier + "-01234567")).mkdir(parents=True)

    monkeypatch.setattr(host, "BACKUPS_TO_KEEP", 1)
    newest = stopped_backup(deploy)
    deploy.prune_backups()

    backups = deploy.base / "backups"
    assert {path.name for path in backups.iterdir()} == {*protected.values(), newest}
    for identifier in protected.values():
        assert deploy.verify_backup(backups / identifier)["id"] == identifier
    assert {path.name for path in (deploy.base / "previous").iterdir()} == set(protected.values())
    assert {path.name for path in (deploy.base / "failed").iterdir()} == {
        identifier + "-01234567" for identifier in protected.values()
    }


def test_failed_backup_does_not_run_retention(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    (fake_root / "running").unlink()
    old = {stopped_backup(deploy) for _ in range(2)}
    monkeypatch.setattr(host, "BACKUPS_TO_KEEP", 1)
    def fail_copy(*args, **kwargs):
        raise OSError("fixture copy failure")

    monkeypatch.setattr(host.shutil, "copytree", fail_copy)
    with pytest.raises(OSError, match="fixture copy failure"):
        stopped_backup(deploy)
    assert {path.parent.name for path in (deploy.base / "backups").glob("*/manifest.json")} == old


def test_backup_retention_logs_deletion_failure_and_reports_success(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    (fake_root / "running").unlink()
    backups = deploy.base / "backups"
    old = backups / stopped_backup(deploy)
    rmtree = host.shutil.rmtree

    def refuse_old(path, *args, **kwargs):
        if path == old:
            raise PermissionError("simulated retention failure")
        return rmtree(path, *args, **kwargs)

    monkeypatch.setattr(host, "BACKUPS_TO_KEEP", 1)
    monkeypatch.setattr(host.shutil, "rmtree", refuse_old)
    newest = stopped_backup(deploy)
    deploy.prune_backups()
    assert deploy.verify_backup(backups / newest)["id"] == newest
    assert old.exists()
    assert any(old.name in line and "prune failed" in line
               for line in deploy.log_path.read_text(encoding="utf-8").splitlines())


def test_release_and_code_rollback_keep_new_data(fake_root, tmp_path, monkeypatch):
    payload, digest = bundle(tmp_path)
    invoke(fake_root, "upload " + digest, payload)
    before_config = host.sha(fake_root / "config" / (host.LIFE + "_config.json"))
    deploy = host.Deployer(fake_root, rehearsal=True)
    approve(deploy, digest)
    result = run_job(fake_root, "release " + digest)
    identifier = result["backup_id"]
    assert result["installed"] == digest
    assert result["versions"] == NEW_VERSIONS
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.1"
    assert not (deploy.base / "staging" / digest).exists()
    assert "approv" in refused_job(fake_root, "release " + digest).lower()
    invoke(fake_root, "upload " + digest, payload)
    with pytest.raises(ValueError, match="reapprove"):
        approve(deploy, digest)
    with closing(sqlite3.connect(deploy.database_paths()[2])) as db:
        db.execute("INSERT INTO life_events VALUES('new-after-upgrade')")
        db.commit()
    hashes = [host.sha(path) for path in deploy.database_paths()]
    approve_rollback(deploy, identifier)
    result = run_job(fake_root, "rollback " + identifier)
    assert result["rolled_back_code"] == identifier
    assert result["data_restored"] is False
    assert host.version(fake_root / "plugins" / host.HOST) == "7.1.17"
    assert [host.sha(path) for path in deploy.database_paths()] == hashes
    assert host.sha(fake_root / "config" / (host.LIFE + "_config.json")) == before_config
    assert "approv" in refused_job(fake_root, "rollback " + identifier).lower()
    last_operation = json.loads((deploy.base / "last-operation.json").read_text(encoding="utf-8"))
    assert last_operation["backup_id"] == identifier
    assert last_operation["action"] == "rollback"
    monkeypatch.setattr(host, "BACKUPS_TO_KEEP", 1)
    (fake_root / "running").unlink()
    newer = stopped_backup(deploy)
    deploy.prune_backups()
    assert {path.name for path in (deploy.base / "backups").iterdir()} == {identifier, newer}


def test_postcheck_failure_restores_code_only_and_requires_reapproval(fake_root, tmp_path):
    digest = upload(fake_root, tmp_path)
    deploy = host.Deployer(fake_root, rehearsal=True)
    entry = approve(deploy, digest)
    first_approval = json.loads(entry.read_text(encoding="utf-8"))["approval_id"]
    hashes = [host.sha(path) for path in deploy.database_paths()]
    (fake_root / "fail-postcheck").touch()
    operation = submit(fake_root, "release " + digest)
    terminal = wait_operation(fake_root, operation)
    assert terminal["state"] == "failed", terminal
    assert "previous paired code restored" in terminal["error"]
    assert "databases/configuration retained" in terminal["error"]
    assert host.version(fake_root / "plugins" / host.HOST) == "7.1.17"
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.0"
    assert (fake_root / "running").exists()
    assert [host.sha(path) for path in deploy.database_paths()] == hashes
    assert (deploy.base / "staging" / digest / "bundle.tar.gz").is_file()

    # Simulate root archiving a journal after inspecting the restored pair.
    pending = deploy.base / "pending.json"
    if pending.exists():
        pending.rename(deploy.base / "reviewed-journal.json")
    stops = docker_calls(fake_root).count("stop")
    assert "approv" in refused_job(fake_root, "release " + digest).lower()
    assert docker_calls(fake_root).count("stop") == stops
    with pytest.raises(ValueError, match="reapprove"):
        approve(deploy, digest)
    assert wait_operation(fake_root, operation) == terminal

    (fake_root / "fail-postcheck").unlink()
    entry = approve(deploy, digest, reapprove=True)
    assert json.loads(entry.read_text(encoding="utf-8"))["approval_id"] != first_approval
    retry = submit(fake_root, "release " + digest)
    assert retry != operation
    assert wait_operation(fake_root, retry)["state"] == "succeeded"


def test_incompatible_postcheck_leaves_new_code_and_data_stopped(fake_root, tmp_path):
    digest = upload(fake_root, tmp_path)
    deploy = host.Deployer(fake_root, rehearsal=True)
    approve(deploy, digest)
    config = fake_root / "config" / (host.LIFE + "_config.json")
    before_config = host.sha(config)
    (fake_root / "create-new-domain-on-start").touch()

    terminal = wait_operation(fake_root, submit(fake_root, "release " + digest))

    assert terminal["state"] == "manual-recovery", terminal
    assert "automatic restore refused" in terminal["error"]
    assert not (fake_root / "running").exists()
    assert host.sha(config) == before_config
    assert {plugin: host.version(fake_root / "plugins" / plugin) for plugin in host.IDS} == NEW_VERSIONS
    pending = json.loads((deploy.base / "pending.json").read_text(encoding="utf-8"))
    assert pending["state"] == "manual-recovery"
    identifier = pending["backup_id"]
    previous = deploy.base / "previous" / identifier
    assert host.version(previous / host.HOST) == "7.1.17"
    assert host.version(previous / host.LIFE) == "0.1.0"
    with closing(sqlite3.connect(deploy.database_paths()[2])) as db:
        assert db.execute("SELECT id FROM life_kernel_new_history").fetchall() == [("new-fact",)]
    assert (deploy.base / "staging" / digest / "bundle.tar.gz").is_file()
    assert "manual inspection" in refused_job(fake_root, "release " + digest)
    assert "manual inspection" in refused_job(fake_root, "rollback " + identifier)


def test_release_uses_own_backup_even_if_an_old_backup_exists(fake_root, tmp_path):
    deploy = host.Deployer(fake_root, rehearsal=True)
    (fake_root / "running").unlink()
    old = stopped_backup(deploy)
    (fake_root / "running").touch()
    path = deploy.base / "backups" / old / "manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["created_at"] = time.time() - 3600
    host.write_json(path, manifest)
    _, identifier = release_pair(fake_root, tmp_path)
    assert identifier != old
    assert deploy.verify_backup(deploy.base / "backups" / identifier)["id"] == identifier


@pytest.mark.parametrize("failure", ["tampered-code", "domain-history", "flag-enabled", "ownership"])
def test_rollback_refusal(fake_root, tmp_path, failure):
    deploy, identifier = release_pair(fake_root, tmp_path)
    approve_rollback(deploy, identifier)
    if failure == "tampered-code":
        (deploy.base / "previous" / identifier / host.HOST / "main.py").write_text("changed")
    elif failure == "flag-enabled":
        (fake_root / "config" / (host.LIFE + "_config.json")).write_text('{"academics_enabled": true}')
    else:
        index = 2 if failure == "domain-history" else 0
        with closing(sqlite3.connect(deploy.database_paths()[index])) as db:
            if failure == "domain-history":
                db.execute("CREATE TABLE life_kernel_instances(id TEXT)")
            else:
                db.execute("UPDATE runtime_metadata SET value='legacy' WHERE key=?", (host.OWNER_KEYS[0],))
            db.commit()
    hashes = [host.sha(path) for path in deploy.database_paths()]
    refused_job(fake_root, "rollback " + identifier)
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.1"
    assert [host.sha(path) for path in deploy.database_paths()] == hashes


@pytest.mark.parametrize("command", ["backup", "install " + "0" * 64])
def test_obsolete_restricted_commands_do_not_touch_docker(fake_root, command):
    invoke(fake_root, command, ok=False)
    assert docker_calls(fake_root) == []
    assert (fake_root / "running").is_file()


def test_unapproved_or_wrong_rollback_cannot_stop_container(fake_root, tmp_path):
    deploy, identifier = release_pair(fake_root, tmp_path)
    before = docker_calls(fake_root).count("stop")
    assert "approv" in refused_job(fake_root, "rollback " + identifier).lower()
    approve_rollback(deploy, identifier)
    wrong = "20000101t000000z-" + "0" * 12
    refused_job(fake_root, "rollback " + wrong)
    assert docker_calls(fake_root).count("stop") == before
    assert (fake_root / "running").is_file()


@pytest.mark.parametrize("action", ["release", "rollback"])
def test_approval_claim_exists_in_durable_journal_before_first_stop(fake_root, tmp_path, action):
    if action == "release":
        deploy = host.Deployer(fake_root, rehearsal=True)
        argument = upload(fake_root, tmp_path)
        entry = approve(deploy, argument)
    else:
        deploy, argument = release_pair(fake_root, tmp_path)
        (fake_root / "first-stop-journal.json").unlink()
        (fake_root / "first-stop-approval.json").unlink()
        entry = approve_rollback(deploy, argument)
    expected = json.loads(entry.read_text(encoding="utf-8"))
    identifier = submit(fake_root, f"{action} {argument}")
    terminal = wait_operation(fake_root, identifier)
    assert terminal["state"] == "succeeded", terminal
    journal = json.loads((fake_root / "first-stop-journal.json").read_text(encoding="utf-8"))
    claim = json.loads((fake_root / "first-stop-approval.json").read_text(encoding="utf-8"))
    assert journal["operation_id"] == identifier
    assert journal["approval"]["approval_id"] == expected["approval_id"]
    assert claim == {**expected, "operation_id": identifier}
    assert not entry.exists()


def test_root_purge_uploads_frees_slots_but_keeps_protected_uploads(fake_root, tmp_path, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    monkeypatch.setattr(host, "UPLOAD_QUOTA_COUNT", 3)
    digests = {}
    for role, version in (("approved", "0.1.1"), ("pending", "0.1.2"), ("unapproved", "0.1.3")):
        directory, payload, digest = versioned_bundle(tmp_path, role, version)
        digests[role] = stage_payload(deploy, directory, monkeypatch, payload, digest)
    approve(deploy, digests["approved"])
    host.write_json(deploy.base / "pending.json", {"bundle": digests["pending"], "state": "running"})
    directory, payload, digest = versioned_bundle(tmp_path, "replacement", "0.1.4")
    with pytest.raises(ValueError, match="quota"):
        stage_payload(deploy, directory, monkeypatch, payload, digest)
    assert deploy.purge_uploads() == {"purged_uploads": 1}
    assert stage_payload(deploy, directory, monkeypatch, payload, digest) == digest
    assert {path.name for path in (deploy.base / "staging").iterdir()} == {
        digests["approved"], digests["pending"], digest,
    }


def test_root_cleanup_removes_only_unreferenced_partial_backups(fake_root):
    deploy = host.Deployer(fake_root, rehearsal=True)
    root = deploy.base / "backups"
    orphan, protected, complete = (root / ("20000101t000000z-" + char * 12) for char in "abc")
    for path in (orphan, protected, complete):
        path.mkdir(parents=True)
        (path / "ordinary.txt").write_text("rehearsal data")
    host.write_json(deploy.base / "pending.json", {"backup_id": protected.name})
    host.write_json(complete / "manifest.json", {"id": complete.name})
    assert deploy.cleanup_backups() == {"removed_partial_backups": 1}
    assert set(root.iterdir()) == {protected, complete}


def test_backup_byte_quota_prunes_only_after_completed_backup(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    (fake_root / "running").unlink()
    root = deploy.base / "backups"
    older = [root / stopped_backup(deploy) for _ in range(3)]
    host.write_json(deploy.base / "active.json", {"backup_id": older[0].name})
    used = host.tree_bytes(older[0], allocated=True)
    monkeypatch.setattr(host, "BACKUP_QUOTA_BYTES", 3 * used)
    monkeypatch.setattr(deploy, "backup_space", lambda: 32 * 1024)
    with pytest.raises(ValueError, match="while writing"):
        stopped_backup(deploy)
    assert set(root.iterdir()) == set(older)
    monkeypatch.undo()
    monkeypatch.setattr(host, "BACKUP_QUOTA_BYTES", 3 * used)
    newest = root / stopped_backup(deploy)
    assert set(root.iterdir()) == {older[0], older[2], newest}
    assert deploy.verify_backup(newest)["id"] == newest.name


def test_logs_response_is_capped_by_bytes(fake_root):
    (fake_root / "large-logs").touch()
    result = wrapper(fake_root, "logs 500")
    assert result.returncode == 0, result.stderr
    assert len(result.stdout) <= 256 * 1024
    assert b"ordinary rehearsal log" in result.stdout
    assert b"logs truncated" in result.stdout


@pytest.mark.parametrize("command", ["status", "logs 10"])
def test_docker_subprocess_drops_forced_command_environment(fake_root, monkeypatch, command):
    (fake_root / "record-docker-env").touch()
    monkeypatch.setenv("LSR_DEPLOY_REHEARSAL_SENTINEL", "ordinary-fixture")
    result = wrapper(fake_root, command)
    assert result.returncode == 0, result.stderr
    assert json.loads((fake_root / "docker-env.json").read_text(encoding="utf-8")) == []


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pipe capacity and flock regression")
def test_blocked_log_stdout_does_not_hold_service_lock(fake_root):
    (fake_root / "large-logs").touch()
    command = [shutil.which("bash"), str(ROOT / "tools/deploy/companion-deploy")]
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          env=wrapper_env(fake_root, "logs 500"), pipesize=4096) as process:
        try:
            assert select.select([process.stdout], [], [], 5)[0], "no log response reached the pipe"
            # Leave the pipe unread while checking the service lock and status.
            deploy = host.Deployer(fake_root, rehearsal=True)
            with host.exclusive(deploy.base / "action.lock"):
                assert invoke(fake_root, "status")["container_running"] is True
            assert process.poll() is None
            stdout, stderr = process.communicate(timeout=5)
            assert process.returncode == 0, stderr
            assert 4096 < len(stdout) <= 256 * 1024
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


PARK_RELEASE_CLIENT = r"""
import runpy
import sys
import time

helper, root = sys.argv[1:]
main = runpy.run_path(helper)["main"]
sys.argv = [helper, "--test-root", root]

class PressuredOutput:
    def __init__(self, stream):
        self.stream = stream

    def write(self, text):
        self.stream.write(text)
        self.stream.flush()
        if text.endswith("\n"):
            time.sleep(30)

    def __getattr__(self, name):
        return getattr(self.stream, name)

sys.stdout = PressuredOutput(sys.stdout)
main()
"""


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux client SIGKILL and detached supervisor")
def test_job_continues_when_client_is_killed_while_stopped(fake_root, tmp_path):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = upload(fake_root, tmp_path)
    approve(deploy, digest)
    gate = fake_root / "hold-first-stop"
    gate.touch()
    helper = ROOT / "tools/deploy/deploy_host.py"
    command = [sys.executable, "-X", "utf8", "-I", "-c", PARK_RELEASE_CLIENT, str(helper), str(fake_root)]
    response = tmp_path / "queue-response.json"
    with response.open("wb") as output, subprocess.Popen(
            command, stdout=output, stderr=subprocess.PIPE,
            env=wrapper_env(fake_root, "release " + digest)) as process:
        try:
            wait_until(lambda: (fake_root / "first-stop-held").exists(), "fake Docker to stop under backup job")
            wait_until(lambda: response.read_bytes().endswith(b"\n"), "queued response")
            queued = json.loads(response.read_text(encoding="utf-8"))
            assert queued["state"] == "queued"
            operation = invoke(fake_root, "operation " + queued["operation_id"])
            assert operation["state"] == "running"
            assert operation["phase"] == "backup"
            assert not (fake_root / "running").exists()
            assert process.poll() is None
            process.kill()
            assert process.wait(timeout=5) == -signal.SIGKILL
        finally:
            gate.unlink(missing_ok=True)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert wait_operation(fake_root, queued["operation_id"])["state"] == "succeeded"
    assert (fake_root / "running").is_file()


@pytest.mark.timing
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux watchdog and child process-group handling")
def test_watchdog_times_out_stopped_job_and_restarts_container(fake_root, tmp_path):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = upload(fake_root, tmp_path)
    entry = approve(deploy, digest)
    approval = json.loads(entry.read_text(encoding="utf-8"))
    (fake_root / ".job-timeout").write_text("3", encoding="utf-8")
    gate = fake_root / "hold-first-stop"
    gate.touch()
    try:
        identifier = submit(fake_root, "release " + digest)
        wait_until(lambda: (fake_root / "first-stop-held").exists(), "fake Docker stop before watchdog timeout")
        assert not (fake_root / "running").exists()
        terminal = wait_operation(fake_root, identifier)
    finally:
        gate.unlink(missing_ok=True)
    assert terminal["state"] == "failed", terminal
    assert "tim" in terminal["error"].lower()
    assert docker_calls(fake_root).count("start") == 1
    assert (fake_root / "running").is_file()
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.0"
    used = json.loads((deploy.approval_dir / ("used-" + digest)).read_text(encoding="utf-8"))
    assert used["approval_id"] == approval["approval_id"]
    assert used["operation_id"] == identifier
    assert not list((deploy.base / "backups").glob("*/manifest.json"))


@pytest.mark.timing
@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux watchdog and child process-group handling")
def test_watchdog_preserves_incompatible_data_for_manual_recovery(fake_root, tmp_path):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = upload(fake_root, tmp_path)
    entry = approve(deploy, digest)
    approval = json.loads(entry.read_text(encoding="utf-8"))
    (fake_root / ".job-timeout").write_text("5", encoding="utf-8")
    (fake_root / "create-new-domain-on-start").touch()
    gate = fake_root / "hold-incompatible-start"
    gate.touch()
    try:
        identifier = submit(fake_root, "release " + digest)
        wait_until(lambda: (fake_root / "incompatible-start-held").exists(), "new code to write incompatible data")
        terminal = wait_operation(fake_root, identifier)
    finally:
        gate.unlink(missing_ok=True)
    assert terminal["state"] == "manual-recovery", terminal
    assert not (fake_root / "running").exists()
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.1"
    with closing(sqlite3.connect(deploy.database_paths()[2])) as db:
        assert db.execute("SELECT id FROM life_kernel_new_history").fetchall() == [("new-fact",)]
    used = json.loads((deploy.approval_dir / ("used-" + digest)).read_text(encoding="utf-8"))
    assert used == {**approval, "operation_id": identifier}


def test_preapproved_upload_has_one_reserved_slot_and_still_validates(fake_root, tmp_path, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    monkeypatch.setattr(host, "UPLOAD_QUOTA_COUNT", 1)
    directory, payload, digest = versioned_bundle(tmp_path, "ordinary", "0.1.2")
    stage_payload(deploy, directory, monkeypatch, payload, digest)
    directory, payload, digest = versioned_bundle(tmp_path, "binding", "0.1.3")
    approve(deploy, digest)
    with pytest.raises(ValueError, match="approval does not match"):
        stage_payload(deploy, directory, monkeypatch, payload, digest)
    directory, payload, digest = versioned_bundle(tmp_path, "reserved", "0.1.1")
    _, used = deploy.staging_usage(deploy.base / "staging")
    monkeypatch.setattr(host, "MAX_UPLOAD", len(payload))
    monkeypatch.setattr(host, "UPLOAD_QUOTA_BYTES", used + len(payload) + host.UPLOAD_METADATA_BUDGET)
    # Exercise the ordinary byte limit before filling the ordinary slot limit.
    monkeypatch.setattr(host, "UPLOAD_QUOTA_COUNT", 2)
    with pytest.raises(ValueError, match="byte quota"):
        stage_payload(deploy, directory, monkeypatch, payload, digest)
    monkeypatch.setattr(host, "UPLOAD_QUOTA_COUNT", 1)
    approve(deploy, digest)
    assert stage_payload(deploy, directory, monkeypatch, payload, digest) == digest
    directory, payload, digest = versioned_bundle(tmp_path, "overflow", "0.1.4")
    approve(deploy, digest, versions={host.HOST: "7.1.18", host.LIFE: "0.1.4"})
    with pytest.raises(ValueError, match="quota"):
        stage_payload(deploy, directory, monkeypatch, payload, digest)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux flock and pidfd")
def test_purge_interrupts_only_the_verified_upload_holder(fake_root):
    deploy = host.Deployer(fake_root, rehearsal=True)
    with host.exclusive(deploy.base / "upload.lock") as lock:
        lock.write(f"{os.getpid()} {lock.fileno()}\n")
        lock.flush()
        with pytest.raises(ValueError, match="not the deployment wrapper"):
            deploy.purge_uploads()
    with open_upload(fake_root, "a" * 64, idle=15, total=30) as process:
        assert deploy.purge_uploads() == {"purged_uploads": 0}
        assert process.wait(timeout=5) == 1
        assert b"upload canceled by root purge" in process.stderr.read()
    assert not list((deploy.base / "staging").iterdir())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux flock")
def test_request_slots_reserve_status_and_share_disk_budget(fake_root):
    from contextlib import ExitStack

    deploy = host.Deployer(fake_root, rehearsal=True)
    with ExitStack() as locks:
        for index in range(1, host.REQUEST_SLOTS):
            locks.enter_context(host.exclusive(deploy.base / f"request-{index}.lock"))
        assert b"slots are busy" in wrapper(fake_root, "logs 1").stderr
        assert invoke(fake_root, "status")["container_running"] is True
        locks.enter_context(host.exclusive(deploy.base / "request-0.lock"))
        assert b"slots are busy" in wrapper(fake_root, "status").stderr
    with host.exclusive(deploy.base / "space.lock"):
        assert wrapper(fake_root, "upload " + "a" * 64, b"").returncode == 1
        assert not (deploy.base / "staging").exists()


def test_known_supervisor_start_failure_keeps_approval(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = "a" * 64
    entry = approve(deploy, digest).read_bytes()

    def missing(*args, **kwargs):
        raise FileNotFoundError("fixture supervisor unavailable")

    monkeypatch.setattr(host.subprocess, "Popen", missing)
    with pytest.raises(ValueError, match="supervisor did not start"):
        deploy.launch("release", digest)
    assert not (deploy.base / "pending.json").exists()
    assert (deploy.approval_dir / digest).read_bytes() == entry
    assert not (deploy.approval_dir / ("used-" + digest)).exists()


def test_takeover_sync_failure_is_unacknowledged_and_preclaim(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = "a" * 64
    entry = approve(deploy, digest)
    approval = entry.read_bytes()
    sync_directory = host.sync_directory
    failed_syncs = []

    def sync(path):
        if path == deploy.base and not failed_syncs:
            pending = deploy.saved_state("pending.json")
            if pending.get("phase") == "preparing":
                identifier = pending["operation_id"]
                assert pending["state"] == "running"  # The rename is visible already.
                with pytest.raises(ValueError, match="submission unknown"):
                    deploy.wait_for_takeover(identifier, SimpleNamespace(poll=lambda: None))
                failed_syncs.append(identifier)
                raise OSError("fixture takeover directory sync failed")
        sync_directory(path)

    def spawn(identifier):
        deploy.supervise(identifier)
        return SimpleNamespace(poll=lambda: 0)

    def unexpected_worker(*args, **kwargs):
        pytest.fail("worker launched after takeover sync failure")

    monkeypatch.setattr(host, "sync_directory", sync)
    monkeypatch.setattr(host, "SUPERVISOR_TAKEOVER_TIMEOUT", 0.02)
    monkeypatch.setattr(deploy, "spawn_supervisor", spawn)
    monkeypatch.setattr(host.subprocess, "Popen", unexpected_worker)
    with pytest.raises(ValueError, match="could not take over.*approval remains available"):
        deploy.launch("release", digest)

    identifier, = failed_syncs
    assert not deploy.saved_state("pending.json")
    terminal = deploy.operation(identifier)
    assert terminal["state"] == "failed" and terminal["phase"] == "queued"
    assert "fixture takeover directory sync failed" in terminal["error"]
    assert not (deploy.base / "takeover.json").exists()
    assert entry.read_bytes() == approval
    assert not (deploy.approval_dir / ("used-" + digest)).exists()
    assert not (fake_root / "docker-calls.jsonl").exists()


@pytest.mark.parametrize("state", ["active", "failed", "inactive", "unknown", "took-over"])
def test_production_waits_for_takeover_or_checks_fixed_unit(fake_root, monkeypatch, state):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = "a" * 64
    approval = approve(deploy, digest).read_bytes()
    # Keep fixture ownership checks while exercising production submission.
    check_path = deploy.check_approval_path

    def check_fixture(path, **kwargs):
        with monkeypatch.context() as fixture:
            fixture.setattr(deploy, "rehearsal", True)
            check_path(path, **kwargs)

    monkeypatch.setattr(deploy, "check_approval_path", check_fixture)
    deploy.rehearsal = False
    identifier = "a" * 32
    unit = f"companion-deploy-{identifier}.service"
    monkeypatch.setattr(host.uuid, "uuid4", lambda: SimpleNamespace(hex=identifier))
    monkeypatch.setattr(host, "SUPERVISOR_TAKEOVER_TIMEOUT", 0.02)
    checked = []
    host.write_json(deploy.base / "takeover.json", {"operation_id": "b" * 32})

    def submit(command, **kwargs):
        assert command[0] == "/usr/bin/systemd-run" and "--unit=" + unit in command
        assert "--property=Type=exec" in command
        runtime = host.OPERATION_TIMEOUT + host.RECOVERY_TIMEOUT + host.SYSTEMD_FINALIZATION_MARGIN
        assert f"--property=RuntimeMaxSec={runtime}" in command
        if state == "took-over":
            deploy.take_over(identifier)
            # A short job can finish while systemd-run returns its receipt.
            deploy.finish_operation("failed", error="fixture")
            raise subprocess.TimeoutExpired(command, 1)
        return SimpleNamespace(returncode=1 if state == "failed" else 0)

    def inspect(command, *args, **kwargs):
        assert command[0] == "/usr/bin/systemctl" and command[-1] == unit
        checked.append(unit)
        if state == "unknown":
            return 1, b"", b"", False
        return 0, f"LoadState=loaded\nActiveState={state}\n".encode(), b"", False

    monkeypatch.setattr(host.subprocess, "run", submit)
    monkeypatch.setattr(host, "bounded_capture", inspect)
    if state == "took-over":
        assert deploy.launch("release", digest)["operation_id"] == identifier
        assert deploy.operation(identifier)["state"] == "failed"
        assert not checked
    elif state in ("failed", "inactive"):
        with pytest.raises(ValueError, match="supervisor did not start"):
            deploy.launch("release", digest)
        assert not deploy.saved_state("pending.json")
    else:
        with pytest.raises(ValueError, match="submission unknown"):
            deploy.launch("release", digest)
        assert deploy.saved_state("pending.json")["state"] == "queued"
    if state != "took-over":
        assert checked == [unit]
    assert (deploy.approval_dir / digest).read_bytes() == approval
    assert not (deploy.approval_dir / ("used-" + digest)).exists()


@pytest.mark.parametrize("phase", ["committed", "restored"])
def test_unhealthy_container_preserves_pending_for_manual_recovery(fake_root, phase):
    deploy = host.Deployer(fake_root, rehearsal=True)
    identifier = "a" * 32
    (fake_root / "unhealthy-after-start").touch()
    deploy.update_pending(operation_id=identifier, action="release", phase=phase, state="running")
    deploy.recover_operation(identifier, "fixture")
    assert deploy.operation(identifier)["state"] == "manual-recovery"
    assert "settle window" in deploy.saved_state("pending.json")["error"]


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_status_rejects_oversized_docker_output(fake_root, stream):
    (fake_root / f"large-inspect-{stream}").touch()
    assert b"Docker output exceeds byte limit" in wrapper(fake_root, "status").stderr


def test_w6_feature_flags_default_off_and_block_deploy_when_enabled(fake_root):
    flags = {"kernel_enabled", "campus_enabled", "academics_enabled", "class_enabled", "attendance_enabled",
             "study_enabled", "wallet_enabled", "city_enabled", "amap_navigation_enabled",
             "amap_poi_search_enabled", "state_model_enabled"}
    deploy = host.Deployer(fake_root, rehearsal=True)
    assert {key: deploy.flags()[key] for key in flags} == dict.fromkeys(flags, False)
    config = fake_root / "config" / (host.LIFE + "_config.json")
    for key in sorted(flags):
        config.write_text(json.dumps({key: True, "home_place_id": "home"}), encoding="utf-8")
        assert deploy.flags() == dict.fromkeys(deploy.flags(), False) | {key: True}
        with pytest.raises(ValueError, match="new features must remain off"):
            deploy.preflight()


def test_status_has_bounded_typed_markers_and_flags(fake_root):
    deploy = host.Deployer(fake_root, rehearsal=True)
    with closing(sqlite3.connect(deploy.database_paths()[0])) as db, db:
        db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", ("z" * 65, host.OWNER_KEYS[0]))
        db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (b"blob", host.OWNER_KEYS[1]))
    markers = invoke(fake_root, "status")["markers"]["host"]
    assert markers[host.OWNER_KEYS[0]] is None and markers[host.OWNER_KEYS[1]] is None
    assert markers[host.OWNER_KEYS[2]] == "good_life"
    (fake_root / "config" / (host.LIFE + "_config.json")).write_text('{"academics_enabled": []}', encoding="utf-8")
    assert b"flags must be booleans" in wrapper(fake_root, "status").stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows CRT sharing error")
def test_pending_read_retries_transient_permission_error(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    deploy.update_pending(state="queued")
    read = host.read_control_json
    attempts = 0

    def transient(path):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(13, "sharing conflict without WinError metadata", str(path))
        return read(path)

    monkeypatch.setattr(host, "read_control_json", transient)
    assert deploy.saved_state("pending.json") == {"state": "queued"}
    assert attempts == 2


def test_preclaim_retries_replace_records_and_retention_keeps_unfinished(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = "a" * 64
    approve(deploy, digest)
    approval = deploy.approval(digest)
    operations = deploy.base / "operations"
    operations.mkdir()
    unfinished, oldest, first, retry = [char * 32 for char in "1234"]
    host.write_json(operations / f"{unfinished}.json", {"state": "running"})
    host.write_json(operations / f"{oldest}.json", {"state": "succeeded"})
    monkeypatch.setattr(host, "OPERATION_RECORD_COUNT", 2)
    for identifier in (first, retry):
        deploy.update_pending(operation_id=identifier, action="release", phase="preparing", state="running",
                              approval=approval, bundle=digest, backup_id="fixture")
        deploy.finish_operation("failed", error="fixture")
    assert {p.stem for p in operations.glob("*.json")} == {unfinished, retry}
    assert deploy.operation(retry)["error"] == "fixture"
    assert "preclaim_approval" not in deploy.operation(retry)
    assert deploy.approval(digest) == approval
    used = sum(p.stat().st_size for p in operations.glob("*.json"))
    monkeypatch.setattr(host, "OPERATION_RECORD_BYTES", used + 100)
    monkeypatch.setattr(host, "OPERATION_RECORD_COUNT", 10)
    deploy.update_pending(operation_id=oldest, action="release", phase="committed", state="running")
    deploy.finish_operation("succeeded", result={"installed": digest})
    assert {p.stem for p in operations.glob("*.json")} == {unfinished, oldest}


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux wrapper response timer")
@pytest.mark.parametrize("action", ["status", "logs 500"])
def test_readonly_response_deadline_includes_blocked_output(fake_root, action):
    helper = ROOT / "tools/deploy/deploy_host.py"
    if action == "status":
        (fake_root / "slow-inspect").touch()
    else:
        (fake_root / "large-logs").touch()
    script = (
        'import runpy,sys; helper,root=sys.argv[1:]; main=runpy.run_path(helper)["main"]; '
        'main.__globals__["RESPONSE_TIMEOUT"]=2; sys.argv=[helper,"--test-root",root]; main()'
    )
    with subprocess.Popen([sys.executable, "-I", "-c", script, str(helper), str(fake_root)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, pipesize=4096,
                          env=wrapper_env(fake_root, action), creationflags=NO_CONSOLE) as process:
        try:
            assert process.wait(timeout=6) == 1
            assert b"deadline exceeded" in process.stderr.read()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


def test_deploy_cli_parsers_reject_abbreviations(fake_root, monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["deploy_host.py", "--test-r", str(fake_root)])
    with pytest.raises(SystemExit) as failure:
        host.main()
    assert failure.value.code == 2 and "unrecognized arguments" in capsys.readouterr().err
    monkeypatch.setattr(sys, "argv", ["companion-approve", "release", "--rea", "a" * 64,
                                     SOURCE_COMMIT, "7.1.18", "0.1.1"])
    with pytest.raises(SystemExit) as failure:
        host.approve_main()
    assert failure.value.code == 2 and "unrecognized arguments: --rea" in capsys.readouterr().err


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux wrapper and POSIX lock types")
def test_wrapper_ignores_bash_startup_environment_and_rejects_symlink_lock(fake_root, monkeypatch):
    env_script = fake_root / "startup-env.sh"
    env_script.write_text("exit 97\n", encoding="ascii")
    monkeypatch.setenv("BASH_ENV", str(env_script))
    script = ROOT / "tools/deploy/companion-deploy"
    arguments = ["/bin/bash", "-p", str(script)]
    result = subprocess.run(arguments, env=wrapper_env(fake_root, "status"), capture_output=True,
                            timeout=10, creationflags=NO_CONSOLE)
    assert json.loads(result.stdout)["container_running"] is True
    deploy = host.Deployer(fake_root, rehearsal=True)
    request_lock = deploy.base / "request-0.lock"
    request_lock.unlink()
    request_lock.symlink_to(env_script)
    result = subprocess.run(arguments, env=wrapper_env(fake_root, "status"), capture_output=True,
                            timeout=10, creationflags=NO_CONSOLE)
    assert result.returncode != 0
    assert env_script.read_text(encoding="ascii") == "exit 97\n"


def test_release_without_life_host_binding_uses_guarded_restore(fake_root, tmp_path):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = upload(fake_root, tmp_path)
    approve(deploy, digest)
    (fake_root / "missing-life-binding").touch()
    database_hashes = [host.sha(path) for path in deploy.database_paths()]
    result = wait_operation(fake_root, submit(fake_root, "release " + digest))
    assert result["state"] == "failed", result
    assert "GoodLife host binding not confirmed" in result["error"]
    assert "previous paired code restored" in result["error"]
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.0"
    assert [host.sha(path) for path in deploy.database_paths()] == database_hashes
    assert not (deploy.base / "pending.json").exists()
    docker_commands = [json.loads(line) for line in (fake_root / "docker-calls.jsonl").read_text().splitlines()]
    assert ["logs", "--since", "2026-11-18T03:00:00.000000000Z", "--tail", "500", "astrbot"] in docker_commands


@pytest.mark.parametrize("change", ["started-at", "restart-count"])
def test_start_identity_change_between_postcheck_and_settle_restores_code(fake_root, tmp_path, change):
    deploy = host.Deployer(fake_root, rehearsal=True)
    digest = upload(fake_root, tmp_path)
    approve(deploy, digest)
    (fake_root / "restart-between-checks").write_text(change, encoding="ascii")
    result = wait_operation(fake_root, submit(fake_root, "release " + digest))
    assert result["state"] == "failed", result
    assert "previous paired code restored" in result["error"]
    assert "settle window" in result["error"]
    assert host.version(fake_root / "plugins" / host.LIFE) == "0.1.0"
    commands = [json.loads(line) for line in (fake_root / "docker-calls.jsonl").read_text().splitlines()]
    assert [command[2] for command in commands if command[:2] == ["logs", "--since"]] == [
        "2026-11-18T03:00:00.000000000Z",
    ]


@pytest.mark.timing
def test_slow_recovery_persists_manual_outcome_with_last_known_state(fake_root, monkeypatch):
    deploy = host.Deployer(fake_root, rehearsal=True)
    identifier = "a" * 32
    deploy.update_pending(operation_id=identifier, action="release", phase="committed", state="running")
    started = time.monotonic()
    deadlines = []

    def status(*, timeout):
        assert deploy.saved_state("pending.json")["state"] == "recovering"
        deadlines.append(timeout)
        if len(deadlines) == 1:
            return True, ("0", "2026-11-18T03:00:00.000000000Z"), "starting"
        # The unstable container never produces a second reliable observation.
        time.sleep(timeout + 0.05)  # sleep can wake slightly early; make sure the deadline has passed
        raise subprocess.TimeoutExpired("fixture inspect", timeout)

    monkeypatch.setattr(deploy, "container_status", status)
    deploy.recover_operation(identifier, "fixture watchdog timeout", deadline=started + 0.3)
    assert 0 < deadlines[1] < deadlines[0] <= 0.3 + 1e-6  # monotonic float sums can overshoot by ~1e-13
    record = deploy.operation(identifier)
    assert record["state"] == "manual-recovery"
    assert record["last_known_container_state"] == "running"
    assert "recovery deadline exceeded" in record["error"]
    assert deploy.saved_state(Path("operations") / (identifier + ".json")) == record


def test_life_migration_preflight_rejects_abbreviated_option(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location("preflight_cli", ROOT / "tools/life_migration_preflight.py")
    preflight = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(preflight)
    monkeypatch.setattr(sys, "argv", ["life_migration_preflight.py", "fixture.sqlite3", "--expected-v", "fixture"])
    with pytest.raises(SystemExit) as outcome:
        preflight.main()
    assert outcome.value.code == 2
    assert "unrecognized arguments: --expected-v" in capsys.readouterr().err
