"""Stage scoped life settings from a separate companion configuration snapshot."""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .calendar_import import _fingerprint
from .storage import LifeStorage


PREFIX = "role.life."
LIFE_FIELDS = frozenset({
    "reading_chunk_chars", "reading_daily_segments", "reading_enabled", "reading_speed_chars_per_minute",
    "sleep_enabled", "sleep_minutes", "sleep_time", "sleep_timezone", "sleep_variation_minutes", "sleep_window_minutes",
    "social_existing_percent", "social_interval_minutes", "social_new_percent", "social_random_read_percent",
    "social_recommendation_enabled", "social_related_percent",
})
SHARED_FIELDS = frozenset({"enabled", "timezone"})
HOST_FIELDS = frozenset({
    "derive_enabled", "diary_enabled", "diary_style_prompt", "diary_time", "dream_afterglow_mode",
    "dream_intimate_theme_enabled", "dream_mixed_themes_enabled", "dream_theme_candidates",
    "enable_rest_backlog_reply", "enable_rest_reply_simulation", "rest_backlog_max_messages",
    "rest_reply_active_windows", "rest_reply_awake_grace_minutes", "rest_reply_llm_threshold",
    "rest_reply_mode", "rest_reply_probability",
})
TABLES = {
    "configuration_targets": ("target_key", "target_json", "revision", "updated_at"),
    "configuration_values": ("target_key", "field_id", "value_json", "revision"),
}
ORDER = {"configuration_targets": "target_key", "configuration_values": "target_key, field_id"}
DDL = (
    """CREATE TABLE staged_configuration_targets (
        target_key TEXT PRIMARY KEY NOT NULL, target_json TEXT NOT NULL,
        revision INTEGER NOT NULL, updated_at TEXT NOT NULL)""",
    """CREATE TABLE staged_configuration_values (
        target_key TEXT NOT NULL REFERENCES staged_configuration_targets(target_key), field_id TEXT NOT NULL,
        value_json TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(target_key, field_id))""",
    """CREATE TABLE staged_configuration_manifest (
        migration_id TEXT PRIMARY KEY, source_version TEXT NOT NULL, summary_json TEXT NOT NULL)""",
)


def _summary(rows):
    return {name: {"rows": len(items), "sha256": _fingerprint(items)} for name, items in rows.items()}


def _snapshot(connection, expected_version):
    row = connection.execute("SELECT value FROM runtime_metadata WHERE key='schema_version'").fetchone()
    if row is None or row[0] != expected_version:
        raise ValueError("configuration schema version differs from the expected version")
    if [item[0] for item in connection.execute("PRAGMA quick_check")] != ["ok"]:
        raise ValueError("configuration source failed SQLite quick_check")
    if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("configuration source contains foreign key violations")
    for table, columns in TABLES.items():
        actual = tuple(item[1] for item in connection.execute(f"PRAGMA table_info({table})"))
        if actual != columns:
            raise ValueError("configuration table shape differs from the supported schema")
    all_values = tuple(connection.execute("""SELECT target_key, field_id, value_json, revision
        FROM configuration_values WHERE field_id LIKE 'role.life.%' ORDER BY target_key, field_id"""))
    known = LIFE_FIELDS | SHARED_FIELDS | HOST_FIELDS
    if any(not field.startswith(PREFIX) or field[len(PREFIX):] not in known for _, field, _, _ in all_values):
        raise ValueError("configuration contains an unclassified role.life field")
    try:
        for _, _, value, _ in all_values:
            json.loads(value)
    except (ValueError, TypeError) as error:
        raise ValueError("configuration contains invalid value JSON") from error
    selected_fields = LIFE_FIELDS | SHARED_FIELDS
    selected = tuple(row for row in all_values if row[1][len(PREFIX):] in selected_fields)
    target_keys = {row[0] for row in selected}
    all_targets = tuple(connection.execute("SELECT target_key, target_json, revision, updated_at FROM configuration_targets ORDER BY target_key"))
    targets = tuple(row for row in all_targets if row[0] in target_keys)
    if {row[0] for row in targets} != target_keys:
        raise ValueError("configuration target is missing")
    try:
        for _, target_json, _, _ in targets:
            json.loads(target_json)
    except (ValueError, TypeError) as error:
        raise ValueError("configuration contains invalid target JSON") from error
    checks = tuple(connection.execute("""SELECT c.* FROM configuration_changes c
        WHERE c.group_id = 'role.life' ORDER BY c.sequence"""))
    if connection.execute("""SELECT 1 FROM configuration_changes c LEFT JOIN operations o ON o.operation_id=c.operation_id
        WHERE c.group_id='role.life' AND (o.operation_id IS NULL OR o.owner IS NOT 'role.life.settings'
                                         OR o.state IS NOT 'completed' OR o.commit_state IS NOT 'committed') LIMIT 1""").fetchone():
        raise ValueError("configuration change operation is unconfirmed")
    applications = tuple(connection.execute("""SELECT * FROM configuration_applications
        WHERE group_id='role.life' ORDER BY target_key, consumer_id"""))
    consumers = tuple(connection.execute("""SELECT * FROM configuration_consumers
        WHERE consumer_id='role.life' ORDER BY consumer_id"""))
    operations = tuple(connection.execute("""SELECT o.* FROM operations o WHERE o.operation_id IN
        (SELECT operation_id FROM configuration_changes WHERE group_id='role.life') ORDER BY o.operation_id"""))
    rows = {"configuration_targets": targets, "configuration_values": selected}
    summary = _summary(rows)
    for name, items in (("all_life_values", all_values), ("host_changes", checks),
                        ("host_applications", applications), ("host_consumers", consumers),
                        ("host_operations", operations)):
        summary[name] = {"rows": len(items), "sha256": _fingerprint(items)}
    return rows, summary


def _staged_rows(connection):
    result = {}
    for table, columns in TABLES.items():
        name = "staged_" + table
        actual = tuple(item[1] for item in connection.execute(f"PRAGMA table_info({name})"))
        if actual != columns:
            raise ValueError("staged configuration table shape differs from the supported schema")
        result[table] = tuple(connection.execute(f"SELECT {', '.join(columns)} FROM {name} ORDER BY {ORDER[table]}"))
    return result


def stage_configuration(source_path, *, expected_version, destination=None):
    """Inspect or stage explicit life settings; no configuration is activated."""
    if type(expected_version) is not str or not expected_version.strip():
        raise ValueError("the expected configuration schema version is required")
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
                                        ("staged_configuration_manifest",)).fetchone()
                if exists:
                    previous = target.execute("SELECT source_version, summary_json FROM staged_configuration_manifest WHERE migration_id=?",
                                              ("configuration.v1",)).fetchone()
                    if previous is None or previous[0] != expected_version or json.loads(previous[1]) != summary:
                        raise ValueError("configuration source changed after the initial staging import")
                    if _summary(_staged_rows(target)) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged configuration changed after the initial import")
                    state = "unchanged"
                else:
                    present = {item[0] for item in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    if any("staged_" + name in present for name in TABLES):
                        raise ValueError("unclaimed staged configuration tables already exist")
                    for statement in DDL:
                        target.execute(statement)
                    for table, columns in TABLES.items():
                        target.executemany(f"INSERT INTO staged_{table} VALUES ({','.join('?' for _ in columns)})", rows[table])
                    if _summary(_staged_rows(target)) != {name: summary[name] for name in TABLES}:
                        raise ValueError("staged configuration does not match its source")
                    target.execute("INSERT INTO staged_configuration_manifest VALUES (?, ?, ?)",
                        ("configuration.v1", expected_version, json.dumps(summary, sort_keys=True, separators=(",", ":"))))
                    state = "staged"
            return {"state": state, "source_version": expected_version, "summary": summary}
        finally:
            storage.close()
    finally:
        connection.close()
