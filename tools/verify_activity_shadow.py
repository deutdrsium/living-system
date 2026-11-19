"""Compare staged activity read queries with the original snapshot, without writes."""

import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.activity_import import MANIFEST, TABLES, _snapshot
from good_life.activity_records import StagedActivityRecords


def verify(source_path, staged_path, *, expected_version):
    source_path = Path(source_path).resolve(strict=True)
    staged_path = Path(staged_path).resolve(strict=True)
    source = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        staged = sqlite3.connect(staged_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    except BaseException:
        source.close()
        raise
    try:
        for connection in (source, staged):
            connection.execute("PRAGMA query_only = ON")
            connection.execute("BEGIN")
        rows, summary = _snapshot(source, expected_version)
        manifest = staged.execute("SELECT source_version, summary_json FROM staged_activity_manifest WHERE migration_id=?",
                                  (MANIFEST,)).fetchone()
        if manifest is None or manifest[0] != expected_version or json.loads(manifest[1]) != summary:
            raise ValueError("activity snapshot does not match the staged manifest")
        records = StagedActivityRecords(staged)
        for owner, identifier, _, _, _, _, raw in rows["role_activities"]:
            if records.activity(owner_key=owner, activity_id=identifier) != raw:
                raise ValueError("staged activity query differs from its original")
        for _, owner, identifier, revision, _, provenance, operation_id, raw in rows["role_activity_revisions"]:
            if (records.activity_revision(owner_key=owner, activity_id=identifier, revision=revision) != raw
                    or records.activity_revision_evidence(owner_key=owner, activity_id=identifier, revision=revision)
                    != {"provenance": provenance, "operation_id": operation_id}):
                raise ValueError("staged revision query differs from its original")
        for _, operation_id, owner, raw in rows["role_life_operations"]:
            if records.activity_receipt(owner_key=owner, operation_id=operation_id) != raw:
                raise ValueError("staged receipt query differs from its original")

        expected_scan = [{"scan_id": scan_id, "operation_id": op, "owner_key": owner}
                         for scan_id, op, owner, _ in rows["role_life_operations"]]
        scanned, cursor = [], 0
        while True:
            page = records.scan_activity_receipts(after=cursor, limit=100)
            scanned.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1]["scan_id"]
        if scanned != expected_scan:
            raise ValueError("staged activity receipt cursor differs from the original")

        owners = {row[0] for row in rows["role_activities"]} | {row[2] for row in rows["role_life_operations"]}
        names = TABLES["role_activity_revisions"][1:]
        for owner in owners:
            reference = [dict(zip(names, row[1:])) for row in rows["role_activity_revisions"] if row[1] == owner]
            reference.sort(key=lambda row: (row["updated_at"], row["activity_id"], row["revision"]), reverse=True)
            actual, cursor = [], None
            while True:
                page = records.activity_revisions(owner_key=owner, after="", at="\uffff", before=cursor, limit=100)
                actual.extend(page)
                if len(page) < 100:
                    break
                last = page[-1]
                cursor = (last["updated_at"], last["activity_id"], last["revision"])
            if actual != reference:
                raise ValueError("staged activity revision pagination differs from the original")
            recent = [{"operation_id": row[1]} for row in reversed(rows["role_life_operations"]) if row[2] == owner][:100]
            if list(records.recent_activity_receipts(owner_key=owner, limit=100)) != recent:
                raise ValueError("staged recent receipt query differs from the original")
        return {"state": "matched", "roles": len(owners), "activities": len(rows["role_activities"]),
                "revisions": len(rows["role_activity_revisions"]), "receipts": len(rows["role_life_operations"])}
    finally:
        staged.close()
        source.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("staged_database")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.source_backup, args.staged_database, expected_version=args.expected_version)
    except (OSError, sqlite3.Error, ValueError, KeyError):
        parser.exit(1, "activity shadow verification failed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
