"""Read-only inventory of candidate life data in one companion runtime database.

This is an inventory, not an export or an authorization to cut over. In
particular, shared operations, configuration, sources and consumers need their
own migration work before a destination can be considered complete.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


LIFE_TABLES = (
    "calendar_records", "calendar_history", "calendar_receipts",
    "role_activities", "role_activity_revisions", "role_artifacts", "role_life_operations",
    "reading_archive_entries", "reading_archive_progress", "reading_archive_reviews",
    "reading_archive_consents", "reading_archive_triggers",
)
SHARED_TABLES = (
    "agenda_memos", "agenda_reminders", "agenda_occurrences", "agenda_results",
    "agenda_dispatches", "agenda_open_loop_activity", "role_exposure_sessions", "role_exposures",
    "role_exposure_topics", "role_exposure_feedback",
)


def _identifier(name):
    return '"' + name.replace('"', '""') + '"'


def inspect_database(database, *, expected_version=None):
    path = Path(database).resolve(strict=True)
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        tables = {name: sql for name, sql in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'table'")}
        missing = sorted(set(LIFE_TABLES) - tables.keys())
        if missing:
            raise ValueError("required life tables are missing: " + ", ".join(missing))
        if "runtime_metadata" not in tables:
            raise ValueError("runtime metadata table is missing")
        row = connection.execute("SELECT value FROM runtime_metadata WHERE key = ?", ("schema_version",)).fetchone()
        if row is None or not isinstance(row[0], str) or not row[0]:
            raise ValueError("runtime schema version is missing")
        version = row[0]
        if expected_version is not None and version != str(expected_version):
            raise ValueError("runtime schema version does not match the expected version")
        checks = [row[0] for row in connection.execute("PRAGMA quick_check")]
        if checks != ["ok"]:
            raise ValueError("runtime database failed SQLite quick_check")
        foreign_key_errors = sum(1 for _ in connection.execute("PRAGMA foreign_key_check"))
        if foreign_key_errors:
            raise ValueError("runtime database has foreign key violations")

        results = {}
        for name in LIFE_TABLES + SHARED_TABLES:
            if name not in tables:
                continue
            quoted = _identifier(name)
            references = sorted({item[2] for item in connection.execute("PRAGMA foreign_key_list(" + quoted + ")")})
            results[name] = {
                "rows": connection.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0],
                "schema_sha256": hashlib.sha256((tables[name] or "").encode("utf-8")).hexdigest(),
                "references": references,
                "classification": "candidate" if name in LIFE_TABLES else "shared_needs_review",
            }
        return {"schema_version": version, "integrity": "ok", "migration_ready": False,
                "tables": results}
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("database", help="explicit path to a runtime database or consistent backup")
    parser.add_argument("--expected-version", help="reject a different runtime schema version")
    args = parser.parse_args()
    try:
        result = inspect_database(args.database, expected_version=args.expected_version)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f"preflight failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
