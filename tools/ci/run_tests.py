"""Run a CI suite with the explicitly recorded baseline exclusions."""
import glob
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
os.chdir(ROOT)
known = (ROOT / "tests/ci-known-failures.txt").read_text(encoding="utf-8").splitlines()
entries = [line for line in known if line and not line.startswith("#")]
if any(not line.startswith(("pytest:", "node:")) for line in entries):
    raise SystemExit("Invalid entry in ci-known-failures.txt")

suite = sys.argv[1]
if suite == "node":
    names = [line.removeprefix("node:") for line in entries if line.startswith("node:")]
    command = ["node", "--test"]
    for name in names:
        command += ["--test-skip-pattern", f"^{re.escape(name)}$"]
    command += sorted(glob.glob("plugins/astrbot_plugin_private_companion/tests/*.cjs"))
    if not command[-1].endswith(".cjs"):
        raise SystemExit("No Node tests found")
elif suite in {"good-life", "contracts", "companion-python"}:
    paths = {"good-life": ["tests"], "contracts": sorted(glob.glob("tests/test_contracts_*.py")),
             "companion-python": ["plugins/astrbot_plugin_private_companion/tests"]}[suite]
    if suite == "contracts" and not paths:
        print("No contract tests yet; nothing to run.", flush=True)
        raise SystemExit(0)
    command = [sys.executable, "-m", "pytest", "-q"]
    if suite == "companion-python":
        test_dir = Path(paths[0])
        files = sorted(set(test_dir.rglob("test_*.py")) | set(test_dir.rglob("*_test.py")))
        shard = int(os.environ["CI_SHARD_INDEX"])
        count = int(os.environ["CI_SHARD_COUNT"])
        if not 0 <= shard < count or not files:
            raise SystemExit("Invalid shard or no Python tests found")
        paths = [str(path) for path in files[shard::count]]
        if not paths:
            raise SystemExit("Empty Python test shard")
        report_dir = ROOT / "tools/ci/reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"shard-{shard}.jsonl").write_text("", encoding="utf-8")
        command += ["-n", "2", "--dist", "loadfile", "-p", "ci.failure_report"]
    if suite == "good-life":
        command += ["-p", "ci.triage", "-rxX", "--ignore-glob=tests/test_contracts_*.py"]
        if workers := os.environ.get("CI_XDIST_WORKERS"):
            command += ["-n", workers]
        if os.environ.get("CI_SKIP_TIMING") == "1":
            command += ["-m", "not timing"]
    command += ["--deselect=" + line.removeprefix("pytest:") for line in entries if line.startswith("pytest:")]
    command += paths
else:
    raise SystemExit(f"Unknown suite: {suite}")
print("Running:", repr(command), flush=True)
raise SystemExit(subprocess.call(command))
