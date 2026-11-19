"""Stage the role-life activity ledger from a companion host snapshot."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .calendar_import import _fingerprint
from .storage import LifeStorage


TABLES = {
    "role_activities": ("owner_key", "activity_id", "owner_json", "revision", "state", "updated_at", "record_json"),
    "role_activity_revisions": ("scan_id", "owner_key", "activity_id", "revision", "updated_at", "provenance", "operation_id", "record_json"),
    "role_artifacts": ("owner_key", "artifact_id", "activity_id", "scope_key", "record_json"),
    "role_life_operations": ("scan_id", "operation_id", "owner_key", "result_json"),
}
ORDER = {"role_activities": "owner_key, activity_id", "role_activity_revisions": "owner_key, activity_id, revision",
         "role_artifacts": "owner_key, artifact_id", "role_life_operations": "scan_id"}
SELECTED = "SELECT owner_key, activity_id FROM role_activities"
CURSOR_PREFIXES = ("memory.activity-ingestion.v1:", "role.growth.progress.cursor.v1:")
MANIFEST = "activities.v4"
SCAN_FLOORS = {
    "role_life_operations": "life.rollback.activity_receipt_floor.v1",
    "role_activity_revisions": "life.rollback.activity_revision_floor.v1",
}
DDL = (
    """CREATE TABLE staged_role_activities (
        owner_key TEXT NOT NULL, activity_id TEXT NOT NULL, owner_json TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0), state TEXT NOT NULL,
        updated_at TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner_key, activity_id))""",
    """CREATE TABLE staged_role_activity_revisions (
        scan_id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_key TEXT NOT NULL, activity_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
        updated_at TEXT NOT NULL, provenance TEXT NOT NULL, operation_id TEXT, record_json TEXT NOT NULL,
        UNIQUE(owner_key, activity_id, revision),
        FOREIGN KEY(owner_key, activity_id) REFERENCES staged_role_activities(owner_key, activity_id))""",
    """CREATE TABLE staged_role_artifacts (
        owner_key TEXT NOT NULL, artifact_id TEXT NOT NULL, activity_id TEXT NOT NULL,
        scope_key TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner_key, artifact_id),
        FOREIGN KEY(owner_key, activity_id) REFERENCES staged_role_activities(owner_key, activity_id))""",
    """CREATE TABLE staged_role_life_operations (
        scan_id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT NOT NULL UNIQUE,
        owner_key TEXT NOT NULL, result_json TEXT NOT NULL)""",
    """CREATE TABLE staged_activity_manifest (
        migration_id TEXT PRIMARY KEY, source_version TEXT NOT NULL, summary_json TEXT NOT NULL)""",
)


def _source_rows(connection):
    rows = {}
    for table, columns in TABLES.items():
        expected = columns[1:] if table in ("role_activity_revisions", "role_life_operations") else columns
        actual = tuple(item[1] for item in connection.execute(f"PRAGMA table_info({table})"))
        if actual != expected:
            raise ValueError("activity table shape differs from the supported schema")
        json_column = "result_json" if table == "role_life_operations" else "record_json"
        if connection.execute(f"SELECT 1 FROM {table} WHERE NOT json_valid({json_column}) LIMIT 1").fetchone():
            raise ValueError("activity source contains invalid record JSON")
        if table == "role_activities":
            select = ", ".join("a." + name for name in columns)
            sql = f"SELECT {select} FROM {table} a JOIN selected s USING(owner_key, activity_id) ORDER BY {ORDER[table]}"
        elif table == "role_life_operations":
            sql = """SELECT a.rowid AS scan_id, a.operation_id, a.owner_key, a.result_json
                     FROM role_life_operations a ORDER BY scan_id"""
        else:
            if table == "role_activity_revisions":
                select = "a.rowid AS scan_id, " + ", ".join("a." + name for name in columns[1:])
            else:
                select = ", ".join("a." + name for name in columns)
            order = ", ".join("a." + name.strip() for name in ORDER[table].split(","))
            sql = f"SELECT {select} FROM {table} a JOIN selected s USING(owner_key, activity_id) ORDER BY {order}"
        rows[table] = tuple(connection.execute("WITH selected AS (" + SELECTED + ") " + sql))
    return rows


def _summary(rows):
    return {name: {"rows": len(items), "sha256": _fingerprint(items)} for name, items in rows.items()}


def _snapshot(connection, expected_version):
    row = connection.execute("SELECT value FROM runtime_metadata WHERE key='schema_version'").fetchone()
    if row is None or row[0] != expected_version:
        raise ValueError("source schema version differs from the expected version")
    if [item[0] for item in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise ValueError("source failed SQLite quick_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("source contains foreign key violations")
    rows = _source_rows(connection)
    checks = (
        """SELECT COUNT(*) FROM role_activities a LEFT JOIN role_activity_revisions h
           ON h.owner_key=a.owner_key AND h.activity_id=a.activity_id AND h.revision=a.revision
           WHERE h.activity_id IS NULL OR h.record_json IS NOT a.record_json""",
        """SELECT COUNT(*) FROM role_life_operations l WHERE
           COALESCE(json_type(l.result_json, '$.activity_id'), '') IS NOT 'text'
           OR length(json_extract(l.result_json, '$.activity_id')) = 0
           OR COALESCE(json_type(l.result_json, '$.revision'), '') IS NOT 'integer'
           OR json_extract(l.result_json, '$.revision') < 1""",
        """SELECT COUNT(*) FROM role_life_operations l LEFT JOIN operations o ON o.operation_id=l.operation_id
           WHERE o.operation_id IS NULL OR o.owner IS NOT 'role.life' OR o.state IS NOT 'completed'
                 OR o.commit_state IS NOT 'committed'""",
        """SELECT COUNT(*) FROM role_activity_revisions h LEFT JOIN operations o ON o.operation_id=h.operation_id
           WHERE h.operation_id IS NOT NULL AND (o.operation_id IS NULL OR o.owner IS NOT 'role.life'
                                                  OR o.state IS NOT 'completed' OR o.commit_state IS NOT 'committed')""",
        """SELECT COUNT(*) FROM role_life_operations l JOIN role_activity_revisions h
           ON h.owner_key=l.owner_key AND h.activity_id=json_extract(l.result_json,'$.activity_id')
           AND h.revision=json_extract(l.result_json,'$.revision') AND h.operation_id=l.operation_id
           WHERE h.record_json IS NOT l.result_json""",
    )
    if any(connection.execute(query).fetchone()[0] for query in checks):
        raise ValueError("activity history or operation receipts are inconsistent")
    operation_rows = tuple(connection.execute("""WITH selected AS (""" + SELECTED + """), ids AS (
        SELECT l.operation_id FROM role_life_operations l
        UNION SELECT h.operation_id FROM role_activity_revisions h JOIN selected s USING(owner_key, activity_id)
            WHERE h.operation_id IS NOT NULL
        ) SELECT o.* FROM operations o JOIN ids ON ids.operation_id=o.operation_id ORDER BY o.operation_id"""))
    summary = _summary(rows)
    summary["host_operations"] = {"rows": len(operation_rows), "sha256": _fingerprint(operation_rows)}
    detached = tuple(connection.execute("""SELECT l.rowid, l.operation_id, l.owner_key, l.result_json
        FROM role_life_operations l WHERE NOT EXISTS (
            SELECT 1 FROM role_activity_revisions h WHERE h.owner_key=l.owner_key
            AND h.activity_id=json_extract(l.result_json, '$.activity_id')
            AND h.revision=json_extract(l.result_json, '$.revision') AND h.operation_id=l.operation_id
        ) ORDER BY l.rowid"""))
    summary["detached_receipts"] = {"rows": len(detached), "sha256": _fingerprint(detached)}
    cursors = tuple(connection.execute("""SELECT key, value FROM runtime_metadata
        WHERE key LIKE ? OR key LIKE ? ORDER BY key""", tuple(prefix + "%" for prefix in CURSOR_PREFIXES)))
    cursor_values = []
    for _, raw in cursors:
        try:
            cursor = json.loads(raw)
        except (TypeError, ValueError) as error:
            raise ValueError("saved activity cursor is invalid") from error
        if (type(cursor) is not dict or set(cursor) != {"activity", "journal"}
                or any(type(value) is not int or value < 0 for value in cursor.values())):
            raise ValueError("saved activity cursor is invalid")
        cursor_values.append(cursor["activity"])
    maximum = connection.execute("SELECT COALESCE(MAX(rowid), 0) FROM role_life_operations").fetchone()[0]
    if any(row[0] < 1 for row in rows["role_life_operations"]):
        raise ValueError("activity operation scan IDs must be positive")
    if any(row[0] < 1 for row in rows["role_activity_revisions"]):
        raise ValueError("activity revision scan IDs must be positive")
    floor_rows = tuple(connection.execute("SELECT key, value FROM runtime_metadata WHERE key IN (?, ?) ORDER BY key",
                                          tuple(SCAN_FLOORS.values())))
    floors = {}
    for key, raw in floor_rows:
        if (type(raw) is not str or len(raw) > 19 or not raw.isascii() or not raw.isdecimal()
                or str(int(raw)) != raw):
            raise ValueError("saved activity scan floor is invalid")
        floors[key] = int(raw)
    revision_ceiling = max(max((row[0] for row in rows["role_activity_revisions"]), default=0),
                           floors.get(SCAN_FLOORS["role_activity_revisions"], 0))
    if revision_ceiling >= 9223372036854775807:
        raise ValueError("activity revision sequence has no remaining SQLite IDs")
    ceiling = max([maximum, floors.get(SCAN_FLOORS["role_life_operations"], 0), *cursor_values])
    if ceiling >= 9223372036854775807:
        raise ValueError("activity scan sequence has no remaining SQLite IDs")
    summary["host_cursors"] = {"rows": len(cursors), "sha256": _fingerprint(cursors), "scan_ceiling": ceiling,
                               "revision_ceiling": revision_ceiling, "floor_rows": len(floor_rows),
                               "floor_sha256": _fingerprint(floor_rows)}
    return rows, summary


def _staged_rows(connection):
    result = {}
    for table, columns in TABLES.items():
        name = "staged_" + table
        actual = tuple(item[1] for item in connection.execute(f"PRAGMA table_info({name})"))
        if actual != columns:
            raise ValueError("staged activity table shape differs from the supported schema")
        result[table] = tuple(connection.execute(
            f"SELECT {', '.join(columns)} FROM {name} ORDER BY {ORDER[table]}"))
    return result


def stage_activities(source_path, *, expected_version, destination=None):
    """Inspect or stage a snapshot; never switch production ownership."""
    if type(expected_version) is not str or not expected_version.strip():
        raise ValueError("the expected source schema version is required")
    source = Path(source_path).resolve(strict=True)
    if destination is not None and source == (Path(destination).resolve() / "good-life.sqlite3"):
        raise ValueError("source and destination must be different databases")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        rows, summary = _snapshot(connection, expected_version)
        if destination is None:
            return {"state": "dry_run", "source_version": expected_version, "summary": summary}
        storage = LifeStorage.open(destination)
        try:
            with storage.transaction() as target:
                exists = target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                        ("staged_activity_manifest",)).fetchone()
                if exists:
                    previous = target.execute("SELECT source_version, summary_json FROM staged_activity_manifest WHERE migration_id=?",
                                              (MANIFEST,)).fetchone()
                    if previous is None or previous[0] != expected_version or json.loads(previous[1]) != summary:
                        raise ValueError("activity source changed after the initial staging import")
                    if _summary(_staged_rows(target)) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged activity data changed after the initial import")
                    sequence = target.execute("SELECT seq FROM sqlite_sequence WHERE name=?",
                                              ("staged_role_life_operations",)).fetchone()
                    if sequence is None or sequence[0] != summary["host_cursors"]["scan_ceiling"]:
                        raise ValueError("staged activity scan sequence changed after import")
                    revision_sequence = target.execute("SELECT seq FROM sqlite_sequence WHERE name=?",
                        ("staged_role_activity_revisions",)).fetchone()
                    revision_ceiling = summary["host_cursors"]["revision_ceiling"]
                    if (revision_sequence[0] if revision_sequence else 0) != revision_ceiling:
                        raise ValueError("staged activity revision sequence changed after import")
                    state = "unchanged"
                else:
                    present = {item[0] for item in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if any("staged_" + name in present for name in TABLES):
                        raise ValueError("unclaimed staged activity tables already exist")
                    for statement in DDL:
                        target.execute(statement)
                    for table, columns in TABLES.items():
                        placeholders = ",".join("?" for _ in columns)
                        target.executemany(f"INSERT INTO staged_{table} VALUES ({placeholders})", rows[table])
                    if _summary(_staged_rows(target)) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged activity data does not match its source")
                    ceiling = summary["host_cursors"]["scan_ceiling"]
                    target.execute("DELETE FROM sqlite_sequence WHERE name=?", ("staged_role_life_operations",))
                    target.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                                   ("staged_role_life_operations", ceiling))
                    target.execute("DELETE FROM sqlite_sequence WHERE name=?", ("staged_role_activity_revisions",))
                    target.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                                   ("staged_role_activity_revisions", summary["host_cursors"]["revision_ceiling"]))
                    target.execute("INSERT INTO staged_activity_manifest VALUES (?, ?, ?)",
                        (MANIFEST, expected_version, json.dumps(summary, sort_keys=True, separators=(",", ":"))))
                    state = "staged"
            return {"state": state, "source_version": expected_version, "summary": summary}
        finally:
            storage.close()
    finally:
        connection.close()
