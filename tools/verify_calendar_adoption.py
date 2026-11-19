"""Verify a calendar adoption against a consistent host snapshot, read-only."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from audit_calendar_ownership import adoption_plan_in_snapshot
from good_life.calendar_adoption import _stored_adoption, adoption_certificate, verify_staged_adoption
from good_life.storage import VERSION


def _snapshot(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def verify(source_backup, life_backup, *, expected_version):
    source, life = Path(source_backup).resolve(strict=True), Path(life_backup).resolve(strict=True)
    if source == life:
        raise ValueError("host and life snapshots must be separate")
    with closing(_snapshot(source)) as host, closing(_snapshot(life)) as target:
        version = target.execute("SELECT value FROM life_metadata WHERE key='schema_version'").fetchone()
        if (version is None or version[0] != VERSION or [row[0] for row in target.execute("PRAGMA quick_check")] != ["ok"]
                or target.execute("PRAGMA foreign_key_check").fetchone()):
            raise ValueError("life snapshot failed integrity checks")
        journal = target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'").fetchone()
        if journal and target.execute("SELECT 1 FROM life_prepared_commands WHERE state IN ('prepared','committed') LIMIT 1").fetchone():
            raise ValueError("life commands are not yet resolved")
        plan = adoption_plan_in_snapshot(host, expected_version=expected_version)
        saved, _ = _stored_adoption(target)
        if saved != plan:
            raise ValueError("calendar adoption no longer matches the verified host ownership")
        verify_staged_adoption(host, target, expected_version=expected_version, source_summary=plan["source_summary"])
        return {"state": "verified", "certificate": adoption_certificate(target)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host_backup")
    parser.add_argument("life_backup")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.host_backup, args.life_backup, expected_version=args.expected_version)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "calendar adoption verification failed; source snapshots were not changed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
