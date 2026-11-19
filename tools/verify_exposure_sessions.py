"""Compare staged browsing session lookups with an isolated source snapshot."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.exposure_import import _snapshot
from good_life.exposure_records import StagedExposureRecords
from verify_exposure_authority import verify_contexts


def verify(source_path, staged_path, *, expected_version):
    source_path, staged_path = Path(source_path).resolve(strict=True), Path(staged_path).resolve(strict=True)
    with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as source, \
         closing(sqlite3.connect(staged_path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as staged:
        for db in (source, staged):
            db.execute("PRAGMA query_only = ON")
            db.execute("BEGIN")
        rows, summary = _snapshot(source, expected_version)
        summary["context_provenance"] = verify_contexts(source)
        manifest = staged.execute("SELECT source_version, summary_json FROM staged_exposure_manifest WHERE migration_id=?",
                                  ("exposures.v2",)).fetchone()
        if manifest is None or manifest[0] != expected_version or json.loads(manifest[1]) != summary:
            raise ValueError("browsing snapshot does not match its manifest")
        records = StagedExposureRecords(staged)
        sessions = rows["role_exposure_sessions"]
        for scan_id, scope, activity, platform, _, _ in sessions:
            if records.session(scope_key=scope, activity_id=activity) != {
                    "scan_id": scan_id, "scope_key": scope, "activity_id": activity, "platform": platform}:
                raise ValueError("browsing session lookup differs")
            previous = next((item for item in reversed(sessions) if item[1] == scope
                             and item[2] != activity and item[3] != "unavailable"), None)
            expected = ({"scan_id": previous[0], "scope_key": previous[1], "activity_id": previous[2], "platform": previous[3]}
                        if previous else None)
            if records.previous_session(scope_key=scope, activity_id=activity) != expected:
                raise ValueError("previous browsing session lookup differs")
        return {"state": "matched", "sessions": len(sessions), "scopes": len({row[1] for row in sessions})}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_backup")
    parser.add_argument("staged_database")
    parser.add_argument("--expected-version", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.source_backup, args.staged_database, expected_version=args.expected_version)
    except (OSError, sqlite3.Error, ValueError, KeyError):
        parser.exit(1, "browsing session verification failed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
