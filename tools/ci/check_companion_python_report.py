"""Require complete companion Python reports and reject unlisted failures."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

BASELINE = Path(__file__).with_name("companion_python_known_failures.txt")
PLUGIN = "plugins/astrbot_plugin_private_companion/"


def normalize_nodeid(nodeid):
    if not isinstance(nodeid, str) or not nodeid.isprintable():
        raise ValueError("invalid pytest nodeid")
    path, sep, test = nodeid.partition("::")
    path = path.replace("\\", "/").removeprefix("./").removeprefix(PLUGIN)
    if not path.startswith("tests/") or not path.endswith(".py") or not sep or not test:
        raise ValueError("expected a companion test nodeid")
    if any(part in {"", ".", ".."} or ":" in part for part in path.split("/")):
        raise ValueError("invalid path in pytest nodeid")
    return f"{PLUGIN}{path}::{test}"


def check_shard(folder, shard):
    prefix = f"shard-{shard}"
    artifact = folder / f"python-shard-{shard}"
    status = (artifact / f"{prefix}-status.txt").read_text(encoding="utf-8").strip()
    if status != "success":
        raise ValueError("test step did not complete successfully (failure, timeout, cancellation, or invalid status)")
    exit_file = artifact / f"{prefix}-exit-code.txt"
    if not exit_file.is_file():
        raise ValueError("pytest exit record missing (timeout, cancellation, or interrupted runner)")
    exit_code = int(exit_file.read_text(encoding="utf-8"))
    if exit_code not in {0, 1}:
        raise ValueError(f"pytest exited abnormally with code {exit_code}")
    session = json.loads((artifact / f"{prefix}-session.json").read_text(encoding="utf-8"))
    if not isinstance(session, dict) or session.get("worker_crashed") is not False:
        raise ValueError("pytest worker crashed or session metadata is invalid")
    if type(session.get("exit_code")) is not int or session["exit_code"] != exit_code:
        raise ValueError("pytest session and process exit codes disagree")
    collected = session.get("collected")
    if type(collected) is not int or collected <= 0:
        raise ValueError("missing or empty pytest collection")

    records = (artifact / f"{prefix}.jsonl").read_bytes()
    if not records or sha256(records).hexdigest() != session.get("sha256"):
        raise ValueError("empty, incomplete, or corrupted pytest report")
    failed = set()
    passed = set()
    completed = set()
    for number, line in enumerate(records.decode("utf-8").split("\n")[:-1], 1):
        try:
            report = json.loads(line)
            nodeid = normalize_nodeid(report["nodeid"])
            phase = report["phase"]
            outcome = report["outcome"]
            if phase not in ("setup", "call", "subtest", "teardown") or outcome not in ("passed", "failed", "skipped"):
                raise ValueError
            if phase == "teardown":
                if nodeid in completed:
                    raise ValueError
                completed.add(nodeid)
            if outcome == "failed":
                failed.add(nodeid)
            if phase == "call" and outcome == "passed":
                passed.add(nodeid)
        except (ValueError, KeyError, TypeError):
            raise ValueError(f"invalid pytest report record at line {number}") from None
    if not records.endswith(b"\n") or len(completed) != collected or (failed | passed) - completed:
        raise ValueError(f"incomplete pytest report ({len(completed)}/{collected} completed tests)")
    if bool(failed) != bool(exit_code):
        raise ValueError("pytest exit code disagrees with the recorded failures")
    return failed, passed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", type=Path)
    parser.add_argument("--baseline", type=Path, default=BASELINE)
    args = parser.parse_args(argv)

    try:
        known = {normalize_nodeid(line) for line in args.baseline.read_text(encoding="utf-8").splitlines()
                 if line and not line.startswith("#")}
    except (OSError, ValueError):
        print("Missing or invalid companion Python failure baseline.")
        return 1

    invalid = False
    failures = set()
    passes = set()
    for shard in range(8):
        try:
            failed, passed = check_shard(args.reports, shard)
        except OSError as exc:
            name = Path(exc.filename).name if exc.filename else "unknown"
            print(f"Shard {shard}: missing or unreadable artifact file {name}")
            invalid = True
        except ValueError as exc:
            print(f"Shard {shard}: {exc}")
            invalid = True
        else:
            failures.update(failed)
            passes.update(passed)

    new_failures = failures - known
    if new_failures:
        print(f"New companion Python failures ({len(new_failures)}):")
        for nodeid in sorted(new_failures):
            print(f"  {nodeid}")
    if invalid or new_failures:
        return 1

    recovered = (known & passes) - failures
    if recovered:
        print(f"Baseline tests now passing ({len(recovered)}):")
        for nodeid in sorted(recovered):
            print(f"  {nodeid}")
    print(f"All 8 shards complete; {len(failures)} known failures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
