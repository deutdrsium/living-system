"""Persist pytest reports incrementally, including before a step timeout."""
from hashlib import sha256
import json
import os
from pathlib import Path

import pytest

REPORT_DIR = Path(__file__).resolve().parent / "reports"
worker_crashed = False


def shard_file(suffix):
    return REPORT_DIR / f"shard-{os.environ['CI_SHARD_INDEX']}{suffix}"


def record(report):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    phase = ("subtest" if isinstance(report, getattr(pytest, "SubtestReport", ()))
             else getattr(report, "when", "collection"))
    with shard_file(".jsonl").open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps({"nodeid": report.nodeid, "outcome": report.outcome,
                                 "phase": phase,
                                 "duration": getattr(report, "duration", 0)}, ensure_ascii=False) + "\n")


def pytest_runtest_logreport(report):
    if report.failed or report.when in {"call", "teardown"}:
        record(report)


def pytest_collectreport(report):
    if report.failed:
        record(report)


@pytest.hookimpl(optionalhook=True)
def pytest_testnodedown(node, error):
    global worker_crashed
    if error is not None or node.workeroutput["exitstatus"] not in {0, 1}:
        worker_crashed = True


def pytest_internalerror():
    global worker_crashed
    worker_crashed = True


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return
    data = {
        "collected": session.testscollected,
        "exit_code": int(exitstatus),
        "sha256": sha256(shard_file(".jsonl").read_bytes()).hexdigest(),
        "worker_crashed": worker_crashed,
    }
    shard_file("-session.json").write_text(json.dumps(data) + "\n", encoding="utf-8", newline="\n")
