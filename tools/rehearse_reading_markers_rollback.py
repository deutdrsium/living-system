"""Reconstruct reading runtime markers into a new, isolated host copy."""

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
from good_life.metadata_import import LIFE_PREFIXES, _snapshot
from good_life.prepared_commands import _hash
from rehearse_activity_rollback import _creation_evidence
from rehearse_calendar_rollback import _open


def _confirmed_start(host, row):
    request = {name: row[name] for name in ("command_id", "owner_key", "context_key", "kind", "expected_revision")}
    request["payload"] = json.loads(row["payload_json"])
    payload = request["payload"]
    identity = digest([request["context_key"], request["command_id"]])
    decision_id, command_id = "life-decision-" + identity, "life-command-" + identity
    decision = {"command_id": request["command_id"], "request_hash": row["request_hash"],
                "decision": "commit", "decision_id": decision_id}
    stored = host.execute("SELECT value FROM runtime_metadata WHERE key=?",
                          ("life.command.decision.v1:" + decision_id,)).fetchone()
    handoff = host.execute("SELECT value FROM runtime_metadata WHERE key=?",
                           ("life.activity.reading-start.v1:" + decision_id,)).fetchone()
    result = json.loads(row["result_json"])
    activity = json.loads(result["record_json"])
    scope = activity["scope"]
    owner = {"installation_id": scope["installation_id"], "bot_id": scope["bot_id"], "persona_id": activity["role_id"]}
    parameter_hash = digest([owner, payload["activity_id"], request["expected_revision"], payload["state"],
                             payload["checkpoint"], payload["work"]])
    expected_handoff = {"request_hash": row["request_hash"], "scope_hash": digest(scope),
                        "role_operation_id": payload["operation_id"], "role_parameter_hash": parameter_hash}
    if (row["state"] != "acknowledged" or row["request_hash"] != _hash(request)
            or row["decision_id"] != decision_id or row["decision_hash"] != _hash(decision)
            or stored is None or json.loads(stored[0]) != {"decision": decision, "operation_id": command_id, "state": "completed"}
            or handoff is None or json.loads(handoff[0]) != expected_handoff
            or digest(owner) != request["owner_key"] or activity["activity_id"] != payload["activity_id"]
            or activity["revision"] != request["expected_revision"] + 1
            or activity["work"] != payload["work"] or activity["state"] != "active"):
        raise ValueError("reading start evidence is invalid")
    for operation_id, owner_name, parameters in ((command_id, "life.command", row["request_hash"]),
                                                   (payload["operation_id"], "role.life", parameter_hash)):
        operation = host.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        scope_names = ("installation_id", "platform", "scope_kind", "native_scope_id", "bot_id", "account_id", "conversation_id", "session_id")
        expected_scope = tuple((scope["kind"] if name == "scope_kind" else scope[name]) or "" for name in scope_names)
        if (operation is None or (operation["owner"], operation["state"], operation["commit_state"], operation["parameter_hash"])
                != (owner_name, "completed", "committed", parameters)
                or tuple(operation[name] for name in scope_names) != expected_scope):
            raise ValueError("reading start lacks its host audit")
    return request


def _verify(host, life, expected_version):
    original, summary = _snapshot(host, expected_version)
    manifest = life.execute("SELECT source_version,summary_json FROM staged_life_metadata_manifest WHERE migration_id='metadata.v1'").fetchone()
    if (manifest is None or manifest[0] != expected_version
            or json.loads(manifest[1])["life_markers"] != summary["life_markers"]):
        raise ValueError("sealed host reading markers differ from the import")
    if [row[0] for row in life.execute("PRAGMA quick_check")] != ["ok"] or life.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("life snapshot failed integrity checks")
    if host.execute("SELECT 1 FROM runtime_metadata WHERE key LIKE 'life.reading.manual.v1:%' LIMIT 1").fetchone():
        raise ValueError("manual reading requests are still pending")
    if life.execute("SELECT 1 FROM life_prepared_commands WHERE state IN ('prepared','committed') LIMIT 1").fetchone():
        raise ValueError("life commands are not yet resolved")
    _creation_evidence(host, life, True)
    expected = {key: json.loads(value) for key, value in original}
    for row in life.execute("SELECT * FROM life_prepared_commands WHERE state='acknowledged' ORDER BY rowid"):
        payload = json.loads(row["payload_json"])
        if row["kind"] == "activity.create" and "reading_import" in payload:
            expected[payload["reading_import"]["queue_key"]] = 1
        elif row["kind"] == "activity.transition" and payload.get("reading_control", {}).get("mode") == "start":
            request = _confirmed_start(host, row)
            control = request["payload"]["reading_control"]
            pace_key = "role.life.reading_pace:" + request["context_key"]
            if expected.get(pace_key) != control["prior_pace"]:
                raise ValueError("reading pace history is inconsistent")
            expected[pace_key] = control["pace"]
            if control["retry_task_id"] is not None:
                retry_key = "role.life.retry_interrupted:" + control["retry_task_id"]
                if expected.get(retry_key) != control["retry_marker"]:
                    raise ValueError("reading retry authority changed")
                expected.pop(retry_key, None)
    for row in host.execute("SELECT key,value FROM runtime_metadata WHERE key LIKE 'life.reading.choice.evidence.v1:%'"):
        evidence = json.loads(row["value"])
        if type(evidence) is not dict or set(evidence) != {"context_key", "slot", "value"}:
            raise ValueError("reading choice evidence is invalid")
        task_id = "reading-choice-" + digest([evidence["context_key"], evidence["slot"]])
        operation = host.execute("SELECT owner,state,commit_state FROM operations WHERE operation_id=?", (task_id,)).fetchone()
        if (row["key"] != "life.reading.choice.evidence.v1:" + task_id or operation is None
                or tuple(operation) != ("role.reading.choice", "completed", "committed")):
            raise ValueError("reading choice lacks its host audit")
        key = "role.reading.choice:" + digest([evidence["context_key"], evidence["slot"]])
        if key in expected and expected[key] != evidence["value"]:
            raise ValueError("reading choice conflicts with an existing result")
        expected[key] = evidence["value"]
    rows = tuple(tuple(row) for row in life.execute("SELECT key,value FROM staged_life_runtime_metadata ORDER BY key"))
    if any(not key.startswith(LIFE_PREFIXES) for key, _ in rows) or {key: json.loads(value) for key, value in rows} != expected:
        raise ValueError("life reading markers do not match their imported or audited history")
    return rows


def rehearse(host_backup, life_backup, *, expected_version, destination=None):
    host_path, life_path = Path(host_backup).resolve(strict=True), Path(life_backup).resolve(strict=True)
    target_path = Path(destination).resolve() if destination is not None else None
    if host_path == life_path or target_path in (host_path, life_path):
        raise ValueError("rollback destination and source snapshots must be separate")
    with closing(_open(host_path)) as host, closing(_open(life_path)) as life:
        rows = _verify(host, life, expected_version)
        result = {"state": "dry_run", "markers": len(rows)}
        if target_path is None:
            return result
        target_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(target_path, isolation_level=None)) as target:
                host.backup(target)
                target.execute("BEGIN IMMEDIATE")
                for prefix in LIFE_PREFIXES:
                    target.execute("DELETE FROM runtime_metadata WHERE substr(key,1,?)=?", (len(prefix), prefix))
                target.executemany("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", rows)
                copied = tuple(target.execute("""SELECT key,value FROM runtime_metadata
                    WHERE key LIKE 'role.life.%' OR key LIKE 'role.reading.%' OR key LIKE 'role.sleep.%' ORDER BY key"""))
                if tuple(row for row in copied if row[0].startswith(LIFE_PREFIXES)) != rows:
                    raise ValueError("rollback copy does not match the life reading markers")
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
        parser.exit(1, "reading marker rollback rehearsal failed; source snapshots were not changed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
