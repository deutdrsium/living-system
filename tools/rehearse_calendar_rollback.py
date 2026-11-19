"""Reconstruct calendar data into a new host database copy for rollback rehearsal."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.calendar_adoption import _project, _stored_adoption
from good_life.calendar_import import TABLES, _rows, _source_summary, _summaries


def _open(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _verify(host, life, expected_version):
    prefix = "life.calendar.manual.v1:"
    if host.execute("SELECT 1 FROM runtime_metadata WHERE key>=? AND key<? LIMIT 1", (prefix, prefix + "\uffff")).fetchone():
        raise ValueError("manual calendar planning requests are still pending")
    host_summary = _source_summary(host, expected_version)
    if [row[0] for row in life.execute("PRAGMA quick_check")] != ["ok"] or life.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("life snapshot failed integrity checks")
    manifest = life.execute("SELECT source_version, summary_json FROM staged_calendar_manifest WHERE migration_id=?",
                            ("calendar.v1",)).fetchone()
    plan, _ = _stored_adoption(life)
    if (manifest is None or manifest[0] != expected_version or json.loads(manifest[1]) != host_summary
            or plan["source_version"] != expected_version or plan["source_summary"] != host_summary):
        raise ValueError("sealed host calendar differs from the imported snapshot")
    journal = life.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'").fetchone()
    if journal and life.execute("SELECT 1 FROM life_prepared_commands WHERE state IN ('prepared', 'committed') LIMIT 1").fetchone():
        raise ValueError("life commands are not yet resolved")
    staged = {name: tuple(tuple(row) for row in _rows(life, name, staged=True)) for name in TABLES}
    summary = _summaries(life, staged=True)
    for name in ("calendar_history", "calendar_receipts"):
        if not set(_project(_rows(host, name), name, plan["moves"])) <= set(staged[name]):
            raise ValueError("life calendar omitted original history or receipts")
    heads = {(row[0], row[1]): row for row in staged["calendar_records"]}
    histories, revisions = {}, {}
    for owner, identifier, revision, operation_id, raw in staged["calendar_history"]:
        record = json.loads(raw)
        key = (owner, identifier)
        if (key not in heads or type(record) is not dict or record.get("id") != identifier
                or record.get("revision") != revision or operation_id in histories):
            raise ValueError("life calendar history is inconsistent")
        operation = host.execute("SELECT owner, state, commit_state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if (operation is None or operation["owner"] != "calendar" or operation["state"] != "completed"
                or operation["commit_state"] != "committed"):
            raise ValueError("life calendar operation is not confirmed in the host")
        histories[operation_id] = (key, revision, record)
        revisions.setdefault(key, {})[revision] = raw
    for key, (owner, identifier, revision, updated_at, raw) in heads.items():
        record = json.loads(raw)
        versions = revisions.get(key, {})
        if (type(record) is not dict or record.get("id") != identifier or record.get("revision") != revision
                or record.get("updated_at") != updated_at or versions.get(revision) != raw
                or revision != len(versions) or sorted(versions) != list(range(1, len(versions) + 1))):
            raise ValueError("life calendar head or revision sequence is inconsistent")
    for owner, identifier, revision, _updated_at, raw in _project(_rows(host, "calendar_records"), "calendar_records", plan["moves"]):
        key = (owner, identifier)
        if key not in heads or heads[key][2] < revision or revisions.get(key, {}).get(revision) != raw:
            raise ValueError("life calendar omitted an original record")
    receipt_operations = set()
    for operation_id, owner, identifier, raw in staged["calendar_receipts"]:
        receipt = json.loads(raw)
        history = histories.get(operation_id)
        if (type(receipt) is not dict or history is None or history[0] != (owner, identifier)
                or receipt.get("operation_id") != operation_id or receipt.get("id") != identifier
                or receipt.get("revision") != history[1] or receipt.get("state") != history[2].get("state", "saved")
                or receipt.get("replayed") is not False):
            raise ValueError("life calendar receipt is inconsistent")
        receipt_operations.add(operation_id)
    if receipt_operations != set(histories):
        raise ValueError("life calendar is missing operation receipts")
    return staged, summary


def rehearse(host_backup, life_backup, *, expected_version, destination=None):
    host_path, life_path = Path(host_backup).resolve(strict=True), Path(life_backup).resolve(strict=True)
    if host_path == life_path:
        raise ValueError("host and life snapshots must be separate")
    destination_path = Path(destination).resolve() if destination is not None else None
    if destination_path in (host_path, life_path):
        raise ValueError("rollback destination must be a new copy")
    with closing(_open(host_path)) as host, closing(_open(life_path)) as life:
        staged, summary = _verify(host, life, expected_version)
        result = {"state": "dry_run", "records": len(staged["calendar_records"]),
                  "revisions": len(staged["calendar_history"]), "receipts": len(staged["calendar_receipts"])}
        if destination_path is None:
            return result
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(destination_path, isolation_level=None)) as target:
                target.execute("PRAGMA foreign_keys=ON")
                host.backup(target)
                target.execute("BEGIN IMMEDIATE")
                for table in ("calendar_history", "calendar_receipts", "calendar_records"):
                    target.execute(f"DELETE FROM {table}")
                for table, columns in TABLES.items():
                    target.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in columns)})", staged[table])
                if _summaries(target) != summary or target.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("rollback copy does not match life calendar")
                _source_summary(target, expected_version)
                target.commit()
        except BaseException:
            destination_path.unlink(missing_ok=True)
            raise
        return {**result, "state": "rehearsed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host_backup")
    parser.add_argument("life_backup")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--destination", help="new isolated host copy; omit for read-only preflight")
    args = parser.parse_args()
    try:
        result = rehearse(args.host_backup, args.life_backup, expected_version=args.expected_version, destination=args.destination)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "calendar rollback rehearsal failed; source snapshots were not changed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
