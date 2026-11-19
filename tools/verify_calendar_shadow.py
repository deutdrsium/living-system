"""Compare staged calendar read queries with the original snapshot, without writes."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.calendar_import import _source_summary
from good_life.calendar_records import StagedCalendarRecords


def verify(source_path, staged_path, *, expected_version):
    source_path = Path(source_path).resolve(strict=True)
    staged_path = Path(staged_path).resolve(strict=True)
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as source:
        with closing(sqlite3.connect(staged_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as staged:
            for connection in (source, staged):
                connection.execute("PRAGMA query_only = ON")
                connection.execute("BEGIN")
            summary = _source_summary(source, expected_version)
            manifest = staged.execute("SELECT source_version, summary_json FROM staged_calendar_manifest WHERE migration_id=?",
                                      ("calendar.v1",)).fetchone()
            if manifest is None or manifest[0] != expected_version or json.loads(manifest[1]) != summary:
                raise ValueError("calendar snapshot does not match the staged manifest")
            records = StagedCalendarRecords(staged)
            heads = tuple(source.execute("SELECT owner_key, record_id, record_json FROM calendar_records ORDER BY owner_key, record_id"))
            histories = tuple(source.execute("SELECT owner_key, record_id, revision, record_json FROM calendar_history ORDER BY owner_key, record_id, revision"))
            receipts = tuple(source.execute("SELECT owner_key, operation_id, result_json FROM calendar_receipts ORDER BY operation_id"))
            for owner, identifier, raw in heads:
                if records.get(owner_key=owner, identifier=identifier) != json.loads(raw):
                    raise ValueError("staged calendar query differs from its original")
            for owner, identifier, revision, raw in histories:
                if records.version(owner_key=owner, identifier=identifier, revision=revision) != json.loads(raw):
                    raise ValueError("staged calendar version differs from its original")
            for owner, operation, raw in receipts:
                if records.receipt(owner_key=owner, operation_id=operation) != json.loads(raw):
                    raise ValueError("staged calendar receipt differs from its original")
            owners = {row[0] for row in heads}
            for owner in owners:
                expected = [(identifier, json.loads(raw)) for key, identifier, raw in heads if key == owner]
                actual, cursor = [], ""
                while True:
                    page = records.list(owner_key=owner, after_id=cursor, limit=100)
                    actual.extend(page)
                    if len(page) < 100:
                        break
                    cursor = page[-1]["id"]
                if actual != [item for _, item in expected]:
                    raise ValueError("staged calendar pagination differs from its original")
                kinds = {item.get("kind") for _, item in expected if isinstance(item.get("kind"), str) and item["kind"]}
                for kind in kinds:
                    filtered = records.list(owner_key=owner, kinds=(kind,), limit=100)
                    if filtered != tuple(item for _, item in expected if item.get("kind") == kind)[:100]:
                        raise ValueError("staged calendar kind filter differs from its original")
                reminders = {item.get("reminder_id") for _, item in expected if item.get("reminder_id")}
                for reminder in reminders:
                    expected_reminders = [item for _, item in expected if item.get("reminder_id") == reminder]
                    actual_reminders = records.by_reminder(owner_keys=(owner,), reminder_id=reminder)
                    canonical = lambda items: sorted(json.dumps(item, sort_keys=True) for item in items)
                    if canonical(actual_reminders) != canonical(expected_reminders):
                        raise ValueError("staged calendar reminder query differs from its original")
            return {"state": "matched", "owners": len(owners), "records": len(heads),
                    "revisions": len(histories), "receipts": len(receipts)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("staged_database")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.source_backup, args.staged_database, expected_version=args.expected_version)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "calendar shadow verification failed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
