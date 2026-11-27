from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tools.ci.check_companion_python_report import PLUGIN, check_shard, main

TEST = "tests/test_example.py::test_example"
TOOLS = Path(__file__).resolve().parents[1] / "tools" / "ci"


def report(nodeid, phase="call", outcome="passed"):
    return {"nodeid": nodeid, "phase": phase, "outcome": outcome, "duration": 0.0}


def write_shard(folder, shard, records, *, exit_code=0, collected=1, worker_crashed=False):
    artifact = folder / f"python-shard-{shard}"
    artifact.mkdir(exist_ok=True)
    prefix = artifact / f"shard-{shard}"
    payload = "".join(json.dumps(record) + "\n" for record in records).encode("utf-8")
    prefix.with_suffix(".jsonl").write_bytes(payload)
    Path(f"{prefix}-status.txt").write_text("success", encoding="utf-8")
    Path(f"{prefix}-exit-code.txt").write_text(str(exit_code), encoding="utf-8")
    Path(f"{prefix}-session.json").write_text(json.dumps({
        "collected": collected,
        "exit_code": exit_code,
        "sha256": sha256(payload).hexdigest(),
        "worker_crashed": worker_crashed,
    }), encoding="utf-8")


@pytest.fixture
def reports(tmp_path):
    for shard in range(8):
        nodeid = f"{TEST}_{shard}"
        write_shard(tmp_path, shard, [report(nodeid), report(nodeid, "teardown")])
    (tmp_path / "baseline.txt").write_text("", encoding="utf-8")
    return tmp_path


def check(folder):
    return main([str(folder), "--baseline", str(folder / "baseline.txt")])


def test_all_shards_pass_and_confirm_baseline_recovery(reports, capsys):
    (reports / "baseline.txt").write_text(f"{PLUGIN}{TEST}_3\n{PLUGIN}{TEST}_absent\n", encoding="utf-8")
    assert check(reports) == 0
    output = capsys.readouterr().out
    assert "Baseline tests now passing (1)" in output
    assert f"{PLUGIN}{TEST}_3" in output
    assert f"{PLUGIN}{TEST}_absent" not in output


@pytest.mark.parametrize("phase", ["setup", "call", "subtest", "teardown"])
def test_known_failure_is_allowed_and_new_failure_is_listed(reports, capsys, phase):
    nodeid = f"{TEST}[case]"
    baseline = reports / "baseline.txt"
    baseline.write_text(f"{PLUGIN}{nodeid}\n", encoding="utf-8")
    windows_nodeid = nodeid.replace("/", "\\")
    records = [report(windows_nodeid, phase, "failed")]
    if phase != "teardown":
        records.append(report(windows_nodeid, "teardown"))
    if phase == "subtest":
        records.insert(1, report(windows_nodeid))
    write_shard(reports, 2, records, exit_code=1)

    assert check(reports) == 0
    assert "Baseline tests now passing" not in capsys.readouterr().out
    baseline.write_text(f"{PLUGIN}{TEST}[other]\n", encoding="utf-8")
    assert check(reports) == 1
    assert f"New companion Python failures (1):\n  {PLUGIN}{nodeid}\n" in capsys.readouterr().out


@pytest.mark.parametrize("missing", [".jsonl", "-session.json", "-status.txt", "-exit-code.txt"])
def test_missing_artifact_file_fails(reports, capsys, missing):
    (reports / f"python-shard-6/shard-6{missing}").unlink()
    assert check(reports) == 1
    assert "Shard 6:" in capsys.readouterr().out


def test_missing_shard_artifact_fails(reports, capsys):
    (reports / "python-shard-6").rename(reports / "unexpected-artifact")
    assert check(reports) == 1
    assert "Shard 6: missing" in capsys.readouterr().out


@pytest.mark.parametrize(("suffix", "content", "diagnostic"), [
    ("-status.txt", "cancelled\n", "cancellation"),
    ("-status.txt", "failure\n", "timeout"),
    ("-exit-code.txt", "124\n", "abnormally"),
    ("-exit-code.txt", "3\n", "abnormally"),
    ("-exit-code.txt", "-9\n", "abnormally"),
    ("-session.json", "{", "property name"),
    (".jsonl", "", "empty, incomplete, or corrupted"),
    (".jsonl", "{", "empty, incomplete, or corrupted"),
])
def test_invalid_artifact_fails(reports, capsys, suffix, content, diagnostic):
    (reports / f"python-shard-6/shard-6{suffix}").write_text(content, encoding="utf-8")
    assert check(reports) == 1
    assert diagnostic in capsys.readouterr().out


def test_incomplete_results_fail_even_with_valid_checksum_and_exit_code(reports, capsys):
    write_shard(reports, 6, [report(TEST), report(TEST, "teardown")], collected=2)
    assert check(reports) == 1
    assert "1/2 completed tests" in capsys.readouterr().out


@pytest.mark.parametrize("crash", [False, True])
def test_real_xdist_report(tmp_path, monkeypatch, crash):
    tools = tmp_path / "tools" / "ci"
    tools.mkdir(parents=True)
    for name in ("run_tests.py", "failure_report.py", "summarize.py"):
        shutil.copyfile(TOOLS / name, tools / name)
    tests = tmp_path / PLUGIN / "tests"
    tests.mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "ci-known-failures.txt").write_text("", encoding="utf-8")
    (tests / "test_example.py").write_text(
        "import os\nimport unittest\n\nimport pytest\n\n"
        "class TestExample(unittest.TestCase):\n"
        "    def test_subtest(self):\n"
        "        with self.subTest(case='example'):\n"
        "            self.assertEqual(1, 2)\n\n"
        "@pytest.mark.skip\n"
        "def test_skipped():\n"
        "    pass\n\n"
        "def test_other():\n"
        f"    {'os._exit(17)' if crash else 'assert True'}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CI_SHARD_INDEX", "0")
    monkeypatch.setenv("CI_SHARD_COUNT", "8")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "tools"))
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_PLUGINS", "xdist.plugin")
    monkeypatch.delenv("PYTEST_XDIST_WORKER", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    result = subprocess.run([sys.executable, str(tools / "run_tests.py"), "companion-python"],
                            env=os.environ, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    monkeypatch.setenv("TEST_OUTCOME", "success")
    subprocess.run([sys.executable, str(tools / "summarize.py"), str(tools / "reports")],
                   capture_output=True, check=True, timeout=15)
    (tools / "reports").rename(tmp_path / "python-shard-0")

    if crash:
        with pytest.raises(ValueError, match="worker crashed"):
            check_shard(tmp_path, 0)
    else:
        failed, passed = check_shard(tmp_path, 0)
        assert failed == {f"{PLUGIN}tests/test_example.py::TestExample::test_subtest"}
        assert f"{PLUGIN}tests/test_example.py::test_other" in passed
        records = (tmp_path / "python-shard-0" / "shard-0.jsonl").read_text(encoding="utf-8")
        assert any(row["phase"] == "subtest" and row["outcome"] == "failed"
                   for row in map(json.loads, records.splitlines()))
