"""Persist pytest reports incrementally, including before a step timeout."""
import json
import os
from pathlib import Path


def record(report):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    destination = Path(__file__).resolve().parent / "reports" / f"shard-{os.environ['CI_SHARD_INDEX']}.jsonl"
    with destination.open("a", encoding="utf-8") as output:
        output.write(json.dumps({"nodeid": report.nodeid, "outcome": report.outcome,
                                 "phase": getattr(report, "when", "collection"),
                                 "duration": getattr(report, "duration", 0)}, ensure_ascii=False) + "\n")


def pytest_runtest_logreport(report):
    if report.failed or report.when == "call":
        record(report)


def pytest_collectreport(report):
    if report.failed:
        record(report)
