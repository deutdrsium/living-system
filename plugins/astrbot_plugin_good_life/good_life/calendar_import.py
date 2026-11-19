"""Stage a verified calendar snapshot without cutting over either plugin."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from .storage import LifeStorage


TABLES = {
    "calendar_records": ("owner_key", "record_id", "revision", "updated_at", "record_json"),
    "calendar_history": ("owner_key", "record_id", "revision", "operation_id", "record_json"),
    "calendar_receipts": ("operation_id", "owner_key", "record_id", "result_json"),
}
ORDER = {
    "calendar_records": "owner_key, record_id",
    "calendar_history": "owner_key, record_id, revision",
    "calendar_receipts": "operation_id",
}
DDL = (
    """CREATE TABLE staged_calendar_records (
        owner_key TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision > 0),
        updated_at TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner_key, record_id))""",
    """CREATE TABLE staged_calendar_history (
        owner_key TEXT NOT NULL, record_id TEXT NOT NULL, revision INTEGER NOT NULL,
        operation_id TEXT NOT NULL, record_json TEXT NOT NULL, PRIMARY KEY(owner_key, record_id, revision),
        FOREIGN KEY(owner_key, record_id) REFERENCES staged_calendar_records(owner_key, record_id))""",
    """CREATE TABLE staged_calendar_receipts (
        operation_id TEXT PRIMARY KEY NOT NULL, owner_key TEXT NOT NULL,
        record_id TEXT NOT NULL, result_json TEXT NOT NULL)""",
    """CREATE TABLE staged_calendar_manifest (
        migration_id TEXT PRIMARY KEY, source_version TEXT NOT NULL, summary_json TEXT NOT NULL)""",
)


def _fingerprint(rows):
    value = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(list(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        value.update(len(encoded).to_bytes(8, "big"))
        value.update(encoded)
    return value.hexdigest()


def _rows(connection, table, *, staged=False):
    columns = ", ".join(TABLES[table])
    name = ("staged_" if staged else "") + table
    return tuple(connection.execute(f"SELECT {columns} FROM {name} ORDER BY {ORDER[table]}"))


def _summaries(connection, *, staged=False):
    result = {}
    for table, columns in TABLES.items():
        name = ("staged_" if staged else "") + table
        actual = tuple(row[1] for row in connection.execute(f"PRAGMA table_info({name})"))
        if actual != columns:
            raise ValueError("calendar table shape differs from the supported schema")
        nulls = " OR ".join(column + " IS NULL" for column in columns)
        if connection.execute(f"SELECT 1 FROM {name} WHERE {nulls} LIMIT 1").fetchone() is not None:
            raise ValueError("calendar table contains incomplete rows")
        rows = _rows(connection, table, staged=staged)
        result[table] = {"rows": len(rows), "sha256": _fingerprint(rows)}
    return result


def _identity_fingerprint(connection):
    installation = connection.execute("SELECT value FROM runtime_metadata WHERE key='installation_id'").fetchone()
    if installation is None or not installation[0]:
        raise ValueError("source installation is unavailable")
    identity = {table: _fingerprint(connection.execute(f"SELECT * FROM {table} ORDER BY rowid"))
                for table in ("conversation_routes", "persona_binding_events", "role_bindings")}
    identity["installation"] = installation[0]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _source_summary(connection, expected_version):
    row = connection.execute("SELECT value FROM runtime_metadata WHERE key = 'schema_version'").fetchone()
    if row is None or row[0] != expected_version:
        raise ValueError("source schema version differs from the expected version")
    if [row[0] for row in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise ValueError("source failed SQLite quick_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("source contains foreign key violations")
    checks = (
        """SELECT COUNT(*) FROM calendar_records r LEFT JOIN calendar_history h
            ON h.owner_key=r.owner_key AND h.record_id=r.record_id AND h.revision=r.revision
            WHERE h.record_id IS NULL OR h.record_json IS NOT r.record_json""",
        """SELECT COUNT(*) FROM calendar_receipts x LEFT JOIN calendar_records r
            ON r.owner_key=x.owner_key AND r.record_id=x.record_id WHERE r.record_id IS NULL""",
        *[f"""SELECT COUNT(*) FROM {table} x LEFT JOIN operations o ON o.operation_id=x.operation_id
             WHERE o.operation_id IS NULL OR o.owner IS NOT 'calendar' OR o.state IS NOT 'completed'
             OR o.commit_state IS NOT 'committed'"""
          for table in ("calendar_history", "calendar_receipts")],
    )
    if any(connection.execute(query).fetchone()[0] for query in checks):
        raise ValueError("calendar history or operation receipts are inconsistent")
    result = _summaries(connection)
    operations = tuple(connection.execute("""SELECT o.* FROM operations o WHERE o.operation_id IN (
        SELECT operation_id FROM calendar_history UNION SELECT operation_id FROM calendar_receipts
        ) ORDER BY o.operation_id"""))
    result["host_operations"] = {"rows": len(operations), "sha256": _fingerprint(operations)}
    return result


def stage_calendar(source_path, *, expected_version, destination=None):
    """Inspect a consistent snapshot, optionally copying it into staging tables.

    Only the calendar's rows are copied. Original operation audit rows stay in
    the host database; their IDs and a fingerprint are retained for validation.
    No production execution owner is changed by this function.
    """
    if type(expected_version) is not str or not expected_version.strip():
        raise ValueError("the expected source schema version is required")
    source = Path(source_path).resolve(strict=True)
    if destination is not None and source == (Path(destination).resolve() / "good-life.sqlite3"):
        raise ValueError("source and destination must be different databases")
    connection = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        summary = _source_summary(connection, expected_version)
        if destination is None:
            return {"state": "dry_run", "source_version": expected_version, "summary": summary}
        storage = LifeStorage.open(destination)
        try:
            with storage.transaction() as target:
                exists = target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                        ("staged_calendar_manifest",)).fetchone()
                if exists:
                    previous = target.execute("SELECT source_version, summary_json FROM staged_calendar_manifest WHERE migration_id=?",
                                              ("calendar.v1",)).fetchone()
                    if previous is None or previous[0] != expected_version or json.loads(previous[1]) != summary:
                        raise ValueError("calendar source changed after the initial staging import")
                    adoption = target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                              ("staged_calendar_adoption",)).fetchone()
                    if adoption:
                        from .calendar_adoption import verify_staged_adoption
                        verify_staged_adoption(connection, target, expected_version=expected_version, source_summary=summary)
                    elif _summaries(target, staged=True) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged calendar changed after the initial import")
                    state = "unchanged"
                else:
                    present = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if any("staged_" + name in present for name in TABLES):
                        raise ValueError("unclaimed staged calendar tables already exist")
                    for statement in DDL:
                        target.execute(statement)
                    for table, columns in TABLES.items():
                        placeholders = ",".join("?" for _ in columns)
                        target.executemany(f"INSERT INTO staged_{table} VALUES ({placeholders})", _rows(connection, table))
                    if _summaries(target, staged=True) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged calendar does not match its source")
                    target.execute("INSERT INTO staged_calendar_manifest VALUES (?, ?, ?)",
                        ("calendar.v1", expected_version, json.dumps(summary, sort_keys=True, separators=(",", ":"))))
                    state = "staged"
            return {"state": state, "source_version": expected_version, "summary": summary}
        finally:
            storage.close()
    finally:
        connection.close()
