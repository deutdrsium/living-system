"""Stage browsing sessions and exposures while preserving host interest links."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .calendar_import import _fingerprint
from .storage import LifeStorage


TABLES = {
    "role_exposure_sessions": ("scan_id", "scope_key", "activity_id", "platform", "selected_id", "selection_made"),
    "role_exposures": ("scope_key", "activity_id", "platform", "content_id", "title", "summary", "source_url",
                       "fetched_at", "exposed_at", "attempted_at", "coverage", "random_attempt"),
}
ORDER = {"role_exposure_sessions": "scan_id", "role_exposures": "scope_key, activity_id, platform, content_id"}
DDL = (
    """CREATE TABLE staged_role_exposure_sessions (
        scan_id INTEGER PRIMARY KEY AUTOINCREMENT, scope_key TEXT NOT NULL, activity_id TEXT NOT NULL,
        platform TEXT NOT NULL, selected_id TEXT NOT NULL, selection_made INTEGER NOT NULL,
        UNIQUE(scope_key, activity_id))""",
    """CREATE TABLE staged_role_exposures (
        scope_key TEXT NOT NULL, activity_id TEXT NOT NULL, platform TEXT NOT NULL, content_id TEXT NOT NULL,
        title TEXT NOT NULL, summary TEXT NOT NULL, source_url TEXT NOT NULL, fetched_at TEXT NOT NULL,
        exposed_at TEXT, attempted_at TEXT, coverage TEXT NOT NULL, random_attempt INTEGER NOT NULL,
        PRIMARY KEY(scope_key, activity_id, platform, content_id))""",
    """CREATE TABLE staged_exposure_manifest (
        migration_id TEXT PRIMARY KEY, source_version TEXT NOT NULL, summary_json TEXT NOT NULL)""",
)


def _summary(rows):
    return {name: {"rows": len(items), "sha256": _fingerprint(items)} for name, items in rows.items()}


def _source_rows(connection):
    rows = {}
    for table, columns in TABLES.items():
        expected = columns[1:] if table == "role_exposure_sessions" else columns
        actual = tuple(item[1] for item in connection.execute(f"PRAGMA table_info({table})"))
        if actual != expected:
            raise ValueError("browsing table shape differs from the supported schema")
        select = "rowid AS scan_id, " + ", ".join(columns[1:]) if table == "role_exposure_sessions" else ", ".join(columns)
        rows[table] = tuple(connection.execute(f"SELECT {select} FROM {table} ORDER BY {ORDER[table]}"))
    return rows


def _snapshot(connection, expected_version):
    row = connection.execute("SELECT value FROM runtime_metadata WHERE key='schema_version'").fetchone()
    if row is None or row[0] != expected_version:
        raise ValueError("source schema version differs from the expected version")
    if [item[0] for item in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise ValueError("source failed SQLite quick_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("source contains foreign key violations")
    if connection.execute("SELECT 1 FROM role_activities WHERE NOT json_valid(record_json) LIMIT 1").fetchone():
        raise ValueError("activity source contains invalid record JSON")
    rows = _source_rows(connection)
    checks = (
        """SELECT COUNT(*) FROM role_exposures e LEFT JOIN role_exposure_sessions s
           ON s.scope_key=e.scope_key AND s.activity_id=e.activity_id WHERE s.activity_id IS NULL""",
        *[f"""SELECT COUNT(*) FROM {table} e WHERE (SELECT COUNT(*) FROM role_activities a
             WHERE a.activity_id=e.activity_id AND json_extract(a.record_json,'$.work.kind')='social') != 1"""
          for table in TABLES],
    )
    if any(connection.execute(query).fetchone()[0] for query in checks):
        raise ValueError("browsing records cannot be matched to one original social activity")
    if any(row[0] < 1 for row in rows["role_exposure_sessions"]):
        raise ValueError("browsing session scan IDs must be positive")
    summary = _summary(rows)
    summary["session_scan_ceiling"] = connection.execute(
        "SELECT COALESCE(MAX(rowid), 0) FROM role_exposure_sessions").fetchone()[0]
    if summary["session_scan_ceiling"] >= 9223372036854775807:
        raise ValueError("browsing session sequence has no remaining SQLite IDs")
    for name, query in (
        ("host_topics", "SELECT * FROM role_exposure_topics ORDER BY scope_key, activity_id, platform, content_id, interest_id"),
        ("host_feedback", "SELECT rowid, * FROM role_exposure_feedback ORDER BY rowid"),
        ("host_interests", "SELECT * FROM role_interests ORDER BY scope_key, interest_id"),
        ("host_coefficients", "SELECT * FROM role_interest_coefficients ORDER BY scope_key, interest_id"),
        ("social_activities", "SELECT owner_key, activity_id, record_json FROM role_activities WHERE json_extract(record_json,'$.work.kind')='social' ORDER BY owner_key, activity_id"),
    ):
        items = tuple(connection.execute(query))
        summary[name] = {"rows": len(items), "sha256": _fingerprint(items)}
    return rows, summary


def _staged_rows(connection):
    result = {}
    for table, columns in TABLES.items():
        name = "staged_" + table
        if tuple(item[1] for item in connection.execute(f"PRAGMA table_info({name})")) != columns:
            raise ValueError("staged browsing table shape differs from the supported schema")
        result[table] = tuple(connection.execute(f"SELECT {', '.join(columns)} FROM {name} ORDER BY {ORDER[table]}"))
    return result


def stage_exposures(source_path, *, expected_version, validate_source, destination=None):
    """Inspect or stage browsing records without activating the new plugin."""
    if type(expected_version) is not str or not expected_version.strip():
        raise ValueError("the expected source schema version is required")
    if not callable(validate_source):
        raise ValueError("browsing provenance validator must be callable")
    source = Path(source_path).resolve(strict=True)
    if destination is not None and source == (Path(destination).resolve() / "good-life.sqlite3"):
        raise ValueError("source and destination must be different databases")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        rows, summary = _snapshot(connection, expected_version)
        provenance = validate_source(connection)
        if type(provenance) is not dict or provenance.get("state") != "matched":
            raise ValueError("browsing provenance could not be verified")
        summary["context_provenance"] = provenance
        if destination is None:
            return {"state": "dry_run", "source_version": expected_version, "summary": summary}
        storage = LifeStorage.open(destination)
        try:
            with storage.transaction() as target:
                exists = target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                        ("staged_exposure_manifest",)).fetchone()
                if exists:
                    previous = target.execute("SELECT source_version, summary_json FROM staged_exposure_manifest WHERE migration_id=?",
                                              ("exposures.v2",)).fetchone()
                    if previous is None or previous[0] != expected_version or json.loads(previous[1]) != summary:
                        raise ValueError("browsing source changed after the initial staging import")
                    if _summary(_staged_rows(target)) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged browsing data changed after the initial import")
                    sequence = target.execute("SELECT seq FROM sqlite_sequence WHERE name=?",
                                              ("staged_role_exposure_sessions",)).fetchone()
                    if sequence is None or sequence[0] != summary["session_scan_ceiling"]:
                        raise ValueError("staged browsing session sequence changed after import")
                    state = "unchanged"
                else:
                    present = {item[0] for item in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if any("staged_" + name in present for name in TABLES):
                        raise ValueError("unclaimed staged browsing tables already exist")
                    for statement in DDL:
                        target.execute(statement)
                    for table, columns in TABLES.items():
                        target.executemany(f"INSERT INTO staged_{table} VALUES ({','.join('?' for _ in columns)})", rows[table])
                    if _summary(_staged_rows(target)) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged browsing data does not match its source")
                    target.execute("DELETE FROM sqlite_sequence WHERE name=?", ("staged_role_exposure_sessions",))
                    target.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)",
                                   ("staged_role_exposure_sessions", summary["session_scan_ceiling"]))
                    target.execute("INSERT INTO staged_exposure_manifest VALUES (?, ?, ?)",
                        ("exposures.v2", expected_version, json.dumps(summary, sort_keys=True, separators=(",", ":"))))
                    state = "staged"
            return {"state": state, "source_version": expected_version, "summary": summary}
        finally:
            storage.close()
    finally:
        connection.close()
