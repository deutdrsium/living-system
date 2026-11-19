"""Execute the explicit triage list and report each result in the job summary."""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
TRIAGE = {}
for line in (ROOT / "tests/ci-needs-triage.txt").read_text(encoding="utf-8").splitlines():
    if line and not line.startswith("#"):
        nodeid, reason = line.split("\t", 1)
        if nodeid in TRIAGE or not reason:
            raise ValueError(f"Invalid duplicate or empty triage entry: {nodeid}")
        TRIAGE[nodeid] = reason

results = {}


def pytest_collection_modifyitems(items):
    for item in items:
        if item.nodeid in TRIAGE:
            item.add_marker(pytest.mark.xfail(reason=TRIAGE[item.nodeid], strict=False))


def pytest_runtest_logreport(report):
    if report.nodeid not in TRIAGE:
        return
    if report.when == "call" or (report.when == "setup" and report.outcome != "passed"):
        if hasattr(report, "wasxfail"):
            results[report.nodeid] = "XPASS" if report.passed else "XFAIL"
        else:
            results[report.nodeid] = report.outcome.upper()
    if report.when == "teardown" and report.failed:
        results[report.nodeid] = "TEARDOWN ERROR"


def pytest_sessionfinish(session, exitstatus):
    # xdist sends test reports to the controller. Only it owns the summary.
    if hasattr(session.config, "workerinput"):
        return
    lines = ["## Tests awaiting triage", "", "These tests run as non-blocking xfail; XPASS means the test passed.", "",
             "| Test | Result | Previously observed reason |", "| --- | --- | --- |"]
    for nodeid, reason in TRIAGE.items():
        lines.append(f"| `{nodeid}` | {results.get(nodeid, 'NOT RUN')} | {reason} |")
    summary = "\n".join(lines) + "\n"
    print("\n" + summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as output:
            output.write(summary)
