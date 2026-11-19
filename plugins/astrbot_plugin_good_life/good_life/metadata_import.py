"""Stage reading and sleep runtime markers from a companion snapshot."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .calendar_import import _fingerprint
from .storage import LifeStorage


LIFE_PREFIXES = ("role.reading.lazy:", "role.reading.choice:", "role.life.reading_pace:",
                 "role.life.retry_interrupted:", "role.sleep.pressure-correction.v1:")
HOST_PREFIXES = ("role.life.rest_gate.v1:",)


def _snapshot(connection, expected_version):
    row = connection.execute("SELECT value FROM runtime_metadata WHERE key='schema_version'").fetchone()
    if row is None or row[0] != expected_version:
        raise ValueError("source schema version differs from the expected version")
    if tuple(item[1] for item in connection.execute("PRAGMA table_info(runtime_metadata)")) != ("key", "value"):
        raise ValueError("runtime metadata shape differs from the supported schema")
    if [item[0] for item in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise ValueError("source failed SQLite quick_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("source contains foreign key violations")
    all_rows = tuple(connection.execute("""SELECT key, value FROM runtime_metadata
        WHERE key LIKE 'role.life.%' OR key LIKE 'role.reading.%' OR key LIKE 'role.sleep.%' ORDER BY key"""))
    if any(not isinstance(key, str) or not key.startswith(LIFE_PREFIXES + HOST_PREFIXES) for key, _ in all_rows):
        raise ValueError("source contains an unclassified life runtime marker")
    rows = tuple(row for row in all_rows if row[0].startswith(LIFE_PREFIXES))
    try:
        for _, value in rows:
            json.loads(value)
    except (TypeError, ValueError) as error:
        raise ValueError("source contains invalid life runtime marker JSON") from error
    summary = {"life_markers": {"rows": len(rows), "sha256": _fingerprint(rows)},
               "all_related_markers": {"rows": len(all_rows), "sha256": _fingerprint(all_rows)}}
    return rows, summary


def stage_metadata(source_path, *, expected_version, destination=None):
    """Inspect or stage explicit markers without executing any retry or activity."""
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
                                        ("staged_life_metadata_manifest",)).fetchone()
                if exists:
                    previous = target.execute("SELECT source_version, summary_json FROM staged_life_metadata_manifest WHERE migration_id=?",
                                              ("metadata.v1",)).fetchone()
                    if previous is None or previous[0] != expected_version or json.loads(previous[1]) != summary:
                        raise ValueError("runtime markers changed after the initial staging import")
                    staged = tuple(target.execute("SELECT key, value FROM staged_life_runtime_metadata ORDER BY key"))
                    if {"rows": len(staged), "sha256": _fingerprint(staged)} != summary["life_markers"]:
                        raise ValueError("staged runtime markers changed after the initial import")
                    state = "unchanged"
                else:
                    if target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                      ("staged_life_runtime_metadata",)).fetchone():
                        raise ValueError("unclaimed staged runtime markers already exist")
                    target.execute("CREATE TABLE staged_life_runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
                    target.execute("CREATE TABLE staged_life_metadata_manifest (migration_id TEXT PRIMARY KEY, source_version TEXT NOT NULL, summary_json TEXT NOT NULL)")
                    target.executemany("INSERT INTO staged_life_runtime_metadata VALUES (?, ?)", rows)
                    staged = tuple(target.execute("SELECT key, value FROM staged_life_runtime_metadata ORDER BY key"))
                    if {"rows": len(staged), "sha256": _fingerprint(staged)} != summary["life_markers"]:
                        raise ValueError("staged runtime markers do not match the source")
                    target.execute("INSERT INTO staged_life_metadata_manifest VALUES (?, ?, ?)",
                        ("metadata.v1", expected_version, json.dumps(summary, sort_keys=True, separators=(",", ":"))))
                    state = "staged"
            return {"state": state, "source_version": expected_version, "summary": summary}
        finally:
            storage.close()
    finally:
        connection.close()
