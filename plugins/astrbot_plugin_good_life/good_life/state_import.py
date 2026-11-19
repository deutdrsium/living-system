"""Copy the existing role-state data without changing its representation.

Operation audit and source assets remain in the companion database.  This is
an offline staging step, not an execution-ownership switch.
"""

import json
from pathlib import Path
import sqlite3

from .calendar_import import _fingerprint
from .storage import LifeStorage


STATE_TABLES = {
    "role_state_records": ("owner_key", "record_id", "revision", "updated_at", "record_json"),
    "role_state_history": ("owner_key", "record_id", "revision", "operation_id", "record_json"),
    "role_state_receipts": ("operation_id", "owner_key", "record_id", "result_json"),
    "role_state_schedules": ("context_key", "context_json", "settings_json", "next_at", "revision", "enabled", "result_json"),
    "state_metadata": ("key", "value"),
}
CONFIG_TABLES = {
    "state_configuration_targets": ("target_key", "target_json", "revision", "updated_at"),
    "state_configuration_values": ("target_key", "field_id", "value_json", "revision"),
}
ORDER = {"role_state_records": "owner_key,record_id", "role_state_history": "owner_key,record_id,revision",
         "role_state_receipts": "operation_id", "role_state_schedules": "context_key", "state_metadata": "key",
         "state_configuration_targets": "target_key", "state_configuration_values": "target_key,field_id"}
DDL = {
    "role_state_records": "(owner_key TEXT NOT NULL,record_id TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(owner_key,record_id))",
    "role_state_history": "(owner_key TEXT NOT NULL,record_id TEXT NOT NULL,revision INTEGER NOT NULL,operation_id TEXT NOT NULL,record_json TEXT NOT NULL,PRIMARY KEY(owner_key,record_id,revision),FOREIGN KEY(owner_key,record_id) REFERENCES staged_role_state_records(owner_key,record_id))",
    "role_state_receipts": "(operation_id TEXT PRIMARY KEY NOT NULL,owner_key TEXT NOT NULL,record_id TEXT NOT NULL,result_json TEXT NOT NULL)",
    "role_state_schedules": "(context_key TEXT PRIMARY KEY NOT NULL,context_json TEXT NOT NULL,settings_json TEXT NOT NULL,next_at TEXT NOT NULL,revision INTEGER NOT NULL,enabled INTEGER NOT NULL,result_json TEXT)",
    "state_metadata": "(key TEXT PRIMARY KEY NOT NULL,value TEXT NOT NULL)",
    "state_configuration_targets": "(target_key TEXT PRIMARY KEY NOT NULL,target_json TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL)",
    "state_configuration_values": "(target_key TEXT NOT NULL,field_id TEXT NOT NULL,value_json TEXT NOT NULL,revision INTEGER NOT NULL,PRIMARY KEY(target_key,field_id),FOREIGN KEY(target_key) REFERENCES staged_state_configuration_targets(target_key))",
}
METADATA_PREFIXES = ("role.state.dialogue-cursor.v1:", "role.state.patrol.v1:")


def _snapshot(source, expected_version):
    version = source.execute("SELECT value FROM runtime_metadata WHERE key='schema_version'").fetchone()
    if not version or version[0] != expected_version:
        raise ValueError("source schema version differs from the expected version")
    if source.execute("PRAGMA quick_check").fetchone()[0] != "ok" or source.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("source database integrity check failed")


def _check_shapes(source, tables):
    for name, fields in tables.items():
        if tuple(row[1] for row in source.execute(f"PRAGMA table_info({name})")) != fields:
            raise ValueError("state source table shape changed: " + name)


def _summarize(rows):
    return {name: {"rows": len(values), "sha256": _fingerprint(values)} for name, values in rows.items()}


def _state_rows(source):
    _check_shapes(source, {name: cols for name, cols in STATE_TABLES.items() if name != "state_metadata"})
    metadata = tuple(source.execute("SELECT key,value FROM runtime_metadata WHERE key LIKE 'role.state.%' ORDER BY key"))
    if any(not key.startswith(METADATA_PREFIXES) for key, _ in metadata):
        raise ValueError("unclassified role-state metadata requires review")
    rows = {name: tuple(source.execute(f"SELECT * FROM {name} ORDER BY " + ORDER[name]))
            for name, fields in STATE_TABLES.items() if name != "state_metadata"}
    rows["state_metadata"] = metadata
    invalid = (
        """SELECT 1 FROM role_state_records r LEFT JOIN role_state_history h
           ON h.owner_key=r.owner_key AND h.record_id=r.record_id AND h.revision=r.revision
           WHERE h.record_json IS NOT r.record_json LIMIT 1""",
        """SELECT 1 FROM role_state_receipts x LEFT JOIN role_state_records r
           ON r.owner_key=x.owner_key AND r.record_id=x.record_id WHERE r.record_id IS NULL LIMIT 1""",
        *[f"""SELECT 1 FROM {name} h LEFT JOIN operations o ON o.operation_id=h.operation_id
             WHERE o.operation_id IS NULL OR o.owner IS NOT 'role_state' OR o.state IS NOT 'completed'
             OR o.commit_state IS NOT 'committed' LIMIT 1""" for name in ("role_state_history", "role_state_receipts")],
    )
    if any(source.execute(query).fetchone() for query in invalid):
        raise ValueError("state history or operation audit is inconsistent")
    json_fields = {"role_state_records": (4,), "role_state_history": (4,), "role_state_receipts": (3,),
                   "role_state_schedules": (1, 2, 6), "state_metadata": (1,)}
    try:
        for name, positions in json_fields.items():
            for row in rows[name]:
                for index in positions:
                    if row[index] is not None:
                        json.loads(row[index])
    except (TypeError, ValueError) as error:
        raise ValueError("state source contains invalid JSON") from error
    operations = tuple(source.execute("""SELECT * FROM operations WHERE operation_id IN
        (SELECT operation_id FROM role_state_history UNION SELECT operation_id FROM role_state_receipts)
        ORDER BY operation_id"""))
    return rows, {"host_operations": {"rows": len(operations), "sha256": _fingerprint(operations)}}


def _config_rows(source):
    _check_shapes(source, {"configuration_targets": CONFIG_TABLES["state_configuration_targets"],
                           "configuration_values": CONFIG_TABLES["state_configuration_values"]})
    values = tuple(source.execute("""SELECT * FROM configuration_values WHERE field_id LIKE 'role.state.%'
                                     ORDER BY target_key,field_id"""))
    targets = tuple(source.execute("""SELECT * FROM configuration_targets WHERE target_key IN
        (SELECT target_key FROM configuration_values WHERE field_id LIKE 'role.state.%') ORDER BY target_key"""))
    if {row[0] for row in values} != {row[0] for row in targets}:
        raise ValueError("state configuration target is missing")
    try:
        for row in targets:
            json.loads(row[1])
        for row in values:
            json.loads(row[2])
    except (TypeError, ValueError) as error:
        raise ValueError("state configuration contains invalid JSON") from error
    audit = tuple(source.execute("""SELECT o.* FROM operations o WHERE o.operation_id IN
        (SELECT operation_id FROM configuration_changes WHERE group_id='role.state') ORDER BY o.operation_id"""))
    if source.execute("""SELECT 1 FROM configuration_changes c LEFT JOIN operations o USING(operation_id)
        WHERE c.group_id='role.state' AND (o.operation_id IS NULL OR o.owner IS NOT 'role.state.settings'
        OR o.state IS NOT 'completed' OR o.commit_state IS NOT 'committed') LIMIT 1""").fetchone():
        raise ValueError("state configuration change audit is inconsistent")
    changes = tuple(source.execute("SELECT * FROM configuration_changes WHERE group_id='role.state' ORDER BY sequence"))
    applications = tuple(source.execute("SELECT * FROM configuration_applications WHERE group_id='role.state' ORDER BY target_key,consumer_id"))
    consumers = tuple(source.execute("SELECT * FROM configuration_consumers WHERE consumer_id='role.state' ORDER BY consumer_id"))
    return {"state_configuration_targets": targets, "state_configuration_values": values}, {
        name: {"rows": len(items), "sha256": _fingerprint(items)}
        for name, items in (("host_operations", audit), ("host_changes", changes), ("host_applications", applications),
                            ("host_consumers", consumers))}


def _stage(source_path, *, expected_version, destination, config):
    if type(expected_version) is not str or not expected_version:
        raise ValueError("the expected source schema version is required")
    path = Path(source_path).resolve(strict=True)
    if destination is not None and path == (Path(destination).resolve() / "good-life.sqlite3"):
        raise ValueError("source and destination must be different databases")
    source = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("BEGIN")
        _snapshot(source, expected_version)
        rows, audit = (_config_rows if config else _state_rows)(source)
        summary = {**_summarize(rows), **audit}
        if destination is None:
            return {"state": "dry_run", "summary": summary}
        storage = LifeStorage.open(destination)
        try:
            domain = "state-config" if config else "state"
            with storage.transaction() as target:
                manifest = "staged_" + domain.replace("-", "_") + "_manifest"
                existing = target.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (manifest,)).fetchone()
                if existing:
                    previous = target.execute(f"SELECT source_version,summary_json FROM {manifest}").fetchall()
                    if len(previous) != 1 or previous[0][0] != expected_version or json.loads(previous[0][1]) != summary:
                        raise ValueError("state source changed after staging")
                    actual = {name: tuple(target.execute(f"SELECT * FROM staged_{name} ORDER BY " + ORDER[name]))
                              for name, fields in (CONFIG_TABLES if config else STATE_TABLES).items()}
                    if _summarize(actual) != _summarize(rows):
                        raise ValueError("staged state changed after import")
                    state = "unchanged"
                else:
                    for name, fields in (CONFIG_TABLES if config else STATE_TABLES).items():
                        target.execute(f"CREATE TABLE staged_{name} " + DDL[name])
                        target.executemany(f"INSERT INTO staged_{name} VALUES ({','.join('?' for _ in fields)})", rows[name])
                    target.execute(f"CREATE TABLE {manifest} (source_version TEXT NOT NULL, summary_json TEXT NOT NULL)")
                    target.execute(f"INSERT INTO {manifest} VALUES (?,?)", (expected_version, json.dumps(summary, sort_keys=True)))
                    state = "staged"
            return {"state": state, "summary": summary}
        finally:
            storage.close()
    finally:
        source.rollback()
        source.close()


def stage_state(source_path, *, expected_version, destination=None):
    return _stage(source_path, expected_version=expected_version, destination=destination, config=False)


def stage_state_configuration(source_path, *, expected_version, destination=None):
    return _stage(source_path, expected_version=expected_version, destination=destination, config=True)
