"""Reconstruct life-owned state into a new host database copy for rehearsal."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import digest
from good_life.prepared_commands import _hash, _stored_request
from good_life.state_import import METADATA_PREFIXES, ORDER, STATE_TABLES, _snapshot, _state_rows, _summarize


def _open(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _verify(host, life, expected_version):
    prefix = "life.state.intervention.v1:"
    if host.execute("SELECT 1 FROM runtime_metadata WHERE key>=? AND key<? LIMIT 1", (prefix, prefix + "\uffff")).fetchone():
        raise ValueError("state intervention requests are still pending")
    _snapshot(host, expected_version)
    original, audit = _state_rows(host)
    manifest = life.execute("SELECT source_version,summary_json FROM staged_state_manifest").fetchall()
    if (len(manifest) != 1 or manifest[0][0] != expected_version
            or json.loads(manifest[0][1]) != {**_summarize(original), **audit}):
        raise ValueError("sealed host state differs from the imported snapshot")
    if [row[0] for row in life.execute("PRAGMA quick_check")] != ["ok"] or life.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("life snapshot failed integrity checks")
    journal = life.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'").fetchone()
    if journal and life.execute("SELECT 1 FROM life_prepared_commands WHERE state IN ('prepared','committed') LIMIT 1").fetchone():
        raise ValueError("life commands are not yet resolved")
    if host.execute("SELECT 1 FROM runtime_metadata WHERE key LIKE 'life.state.manual.v1:%' LIMIT 1").fetchone():
        raise ValueError("manual state maintenance requests are not yet resolved")
    manual = life.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_state_manual_requests'").fetchone()
    if manual:
        for item in life.execute("SELECT request_id FROM life_state_manual_requests"):
            operation = host.execute("SELECT state FROM operations WHERE operation_id=?", (item[0],)).fetchone()
            if operation is None or operation[0] != "completed":
                raise ValueError("manual state maintenance request lacks a completed host receipt")
    rows = {name: tuple(tuple(row) for row in life.execute(f"SELECT * FROM staged_{name} ORDER BY " + ORDER[name]))
            for name in STATE_TABLES}
    for name in ("role_state_history", "role_state_receipts"):
        if not {tuple(row) for row in original[name]} <= set(rows[name]):
            raise ValueError("life state omitted original history or receipts")
    heads = {(row[0], row[1]): row for row in rows["role_state_records"]}
    history, versions = {}, {}
    for owner, identifier, revision, operation_id, raw in rows["role_state_history"]:
        record = json.loads(raw)
        key = (owner, identifier)
        operation = host.execute("SELECT owner,state,commit_state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if (key not in heads or type(record) is not dict or record.get("id") != identifier or record.get("revision") != revision
                or operation is None or tuple(operation) != ("role_state", "completed", "committed")):
            raise ValueError("life state history is inconsistent or unaudited")
        history[(owner, identifier, revision, operation_id)] = record
        versions.setdefault(key, {})[revision] = raw
    for key, (_owner, identifier, revision, updated_at, raw) in heads.items():
        record, items = json.loads(raw), versions.get(key, {})
        if (record.get("id") != identifier or record.get("revision") != revision or record.get("updated_at") != updated_at
                or items.get(revision) != raw or sorted(items) != list(range(1, revision + 1))):
            raise ValueError("life state head or revision sequence is inconsistent")
    for owner, identifier, revision, _updated, raw in original["role_state_records"]:
        if (owner, identifier) not in heads or versions.get((owner, identifier), {}).get(revision) != raw:
            raise ValueError("life state omitted an original record")
    for operation_id, owner, identifier, raw in rows["role_state_receipts"]:
        receipt = json.loads(raw)
        key = (owner, identifier, receipt.get("revision"), operation_id)
        if (key not in history or receipt.get("operation_id") != operation_id or receipt.get("id") != identifier
                or receipt.get("state") != history[key].get("state", "saved") or receipt.get("replayed") is not False):
            raise ValueError("life state receipt is inconsistent")
    if journal:
        for item in life.execute("SELECT * FROM life_prepared_commands WHERE kind='state.change' ORDER BY command_id"):
            if item["state"] == "aborted":
                continue
            request, result = _stored_request(item), json.loads(item["result_json"])
            identity = digest([request["context_key"], request["command_id"]])
            decision_id, operation_id = "life-decision-" + identity, "life-command-" + identity
            decision = {"command_id": request["command_id"], "request_hash": item["request_hash"],
                        "decision": "commit", "decision_id": decision_id}
            saved = host.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                 ("life.command.decision.v1:" + decision_id,)).fetchone()
            effect_row = host.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                      ("life.state.handoff.v1:" + decision_id,)).fetchone()
            effect = json.loads(effect_row[0]) if effect_row else None
            state_operation = host.execute("SELECT * FROM operations WHERE operation_id=?",
                                           (request["payload"]["operation_id"],)).fetchone()
            command_operation = host.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
            source_scope = result["record"]["sources"][0]["scope"]
            scope_names = ("installation_id", "platform", "scope_kind", "native_scope_id", "bot_id", "account_id", "conversation_id", "session_id")
            expected_scope = tuple((source_scope["kind"] if name == "scope_kind" else source_scope[name]) or "" for name in scope_names)
            if (item["state"] != "acknowledged" or item["request_hash"] != _hash(request)
                    or item["decision_id"] != decision_id or item["decision_hash"] != _hash(decision)
                    or saved is None or json.loads(saved[0]) != {"decision": decision, "operation_id": operation_id, "state": "completed"}
                    or type(effect) is not dict or set(effect) != {"request_hash", "scope_hash", "operation_id", "parameter_hash", "effect"}
                    or effect.get("request_hash") != item["request_hash"]
                    or effect.get("scope_hash") != digest(source_scope) or effect.get("operation_id") != request["payload"]["operation_id"]
                    or effect.get("effect") != result or state_operation is None or command_operation is None
                    or state_operation["parameter_hash"] != effect.get("parameter_hash")
                    or command_operation["owner"] != "life.command" or command_operation["parameter_hash"] != item["request_hash"]
                    or command_operation["state"] != "completed" or command_operation["commit_state"] != "committed"
                    or any(tuple(operation[name] for name in scope_names) != expected_scope
                           or operation["idempotency_key"] != operation["operation_id"]
                           or operation["lineage_id"] != operation["operation_id"] or operation["operation_revision"] != 1
                           for operation in (state_operation, command_operation))
                    or (request["payload"]["storage_key"], "current", result["record"]["revision"],
                        request["payload"]["operation_id"]) not in history):
                raise ValueError("life state command lacks its matching host decision")
    return rows


def rehearse(host_backup, life_backup, *, expected_version, destination=None):
    host_path, life_path = Path(host_backup).resolve(strict=True), Path(life_backup).resolve(strict=True)
    target_path = Path(destination).resolve() if destination is not None else None
    if host_path == life_path or target_path in (host_path, life_path):
        raise ValueError("rollback destination and source snapshots must be separate")
    with closing(_open(host_path)) as host, closing(_open(life_path)) as life:
        rows = _verify(host, life, expected_version)
        result = {"state": "dry_run", "records": len(rows["role_state_records"]),
                  "revisions": len(rows["role_state_history"]), "receipts": len(rows["role_state_receipts"])}
        if target_path is None:
            return result
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(target_path, isolation_level=None)) as target:
                target.execute("PRAGMA foreign_keys=ON")
                host.backup(target)
                target.execute("BEGIN IMMEDIATE")
                for name in ("role_state_history", "role_state_receipts", "role_state_records", "role_state_schedules"):
                    target.execute(f"DELETE FROM {name}")
                for prefix in METADATA_PREFIXES:
                    target.execute("DELETE FROM runtime_metadata WHERE substr(key,1,?)=?", (len(prefix), prefix))
                for name in ("role_state_records", "role_state_history", "role_state_receipts", "role_state_schedules"):
                    target.executemany(f"INSERT INTO {name} VALUES ({','.join('?' for _ in STATE_TABLES[name])})", rows[name])
                target.executemany("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", rows["state_metadata"])
                copied, _ = _state_rows(target)
                if _summarize(copied) != _summarize(rows) or target.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("rollback copy does not match the life state")
                target.commit()
        except BaseException:
            target_path.unlink(missing_ok=True)
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
        parser.exit(1, "state rollback rehearsal failed; source snapshots were not changed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
