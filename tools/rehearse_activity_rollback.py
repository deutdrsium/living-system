"""Reconstruct the life activity ledger into a new isolated host database copy."""

import argparse
from contextlib import closing
import hashlib
import json
from datetime import datetime
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import digest
from good_life.activity_import import MANIFEST, SCAN_FLOORS, TABLES, _snapshot, _staged_rows, _summary
from good_life.prepared_commands import _hash
from good_life.staged_activity_creations import StagedActivityCreations
from good_life.staged_activity_deletions import DELETED_TABLES, StagedActivityDeletions, row_hash
from rehearse_calendar_rollback import _open


def _events_confirmed(host, life):
    version = host.execute("SELECT value FROM runtime_metadata WHERE key='life.event.inbox.version'").fetchone()
    if version is not None and version["value"] != "1":
        raise ValueError("host life event inbox version is incompatible")
    events = life.execute("SELECT * FROM life_events ORDER BY sequence").fetchall()
    for event in events:
        deliveries = life.execute("SELECT consumer, acknowledged_at FROM life_event_deliveries WHERE sequence=? ORDER BY consumer",
                                  (event["sequence"],)).fetchall()
        if not deliveries or any(row["acknowledged_at"] is None for row in deliveries):
            raise ValueError("life events are not yet acknowledged")
        payload = json.loads(event["payload_json"])
        if type(payload) is not dict or json.dumps(payload, ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False) != event["payload_json"]:
            raise ValueError("life event payload is invalid")
        consumers = [row["consumer"] for row in deliveries]
        identity = json.dumps([event["event_id"], event["owner_key"], event["context_key"], event["kind"],
                               event["payload_json"], event["occurred_at"], consumers], ensure_ascii=False,
                              separators=(",", ":"))
        if hashlib.sha256(identity.encode("utf-8")).hexdigest() != event["content_hash"]:
            raise ValueError("life event evidence changed")
        for consumer in consumers:
            operation_id = "life-event-" + digest([event["context_key"], consumer, event["event_id"]])
            receipt = host.execute("SELECT owner, state, commit_state, parameter_hash FROM operations WHERE operation_id=?",
                                   (operation_id,)).fetchone()
            if (receipt is None or receipt["owner"] != "life.event" or receipt["state"] != "completed"
                    or receipt["commit_state"] != "committed" or receipt["parameter_hash"] != event["content_hash"]):
                raise ValueError("life event has no matching host receipt")
            if version is not None:
                key = "life.event.inbox.v1:" + digest([event["context_key"], consumer, event["event_id"]])
                row = host.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
                document = json.loads(row["value"]) if row is not None else None
                expected = {"sequence": event["sequence"], "event_id": event["event_id"],
                            "owner_key": event["owner_key"], "context_key": event["context_key"],
                            "kind": event["kind"], "payload": payload, "content_hash": event["content_hash"],
                            "occurred_at": event["occurred_at"], "consumers": consumers}
                if (type(document) is not dict or document.get("schema") != "companion.life-event-inbox.v1"
                        or document.get("consumer") != consumer or document.get("event") != expected
                        or document.get("state") != "completed"):
                    raise ValueError("host life event inbox is not fully settled")
    return len(events)


def _sequence(life, table, rows):
    row = life.execute("SELECT seq FROM sqlite_sequence WHERE name=?", ("staged_" + table,)).fetchone()
    sequence = row[0] if row else 0
    if type(sequence) is not int or sequence < max((item[0] for item in rows), default=0):
        raise ValueError("life activity sequence is inconsistent")
    return sequence


def _deletion_evidence(host, life, journal):
    """Accept only deletion hashes tied to both completed host operations."""
    if not journal:
        return {}
    covered = {}
    checker = StagedActivityDeletions(life)
    rows = life.execute("SELECT * FROM life_prepared_commands WHERE kind='activity.delete' ORDER BY command_id")
    for row in rows:
        if row["state"] == "aborted":
            continue
        try:
            request = {name: row[name] for name in ("command_id", "owner_key", "context_key", "kind", "expected_revision")}
            request["payload"] = json.loads(row["payload_json"])
            result = json.loads(row["result_json"])
            payload = request["payload"]
            identity = digest([request["context_key"], request["command_id"]])
            decision_id, operation_id = "life-decision-" + identity, "life-command-" + identity
            decision = {"command_id": request["command_id"], "request_hash": row["request_hash"],
                        "decision": "commit", "decision_id": decision_id}
            if (row["state"] != "acknowledged" or row["request_hash"] != _hash(request)
                    or row["decision_id"] != decision_id or row["decision_hash"] != _hash(decision)
                    or not checker._matches_result(request, result, require_absent=False)):
                raise ValueError("life deletion evidence changed")
            stored = host.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                  ("life.command.decision.v1:" + decision_id,)).fetchone()
            expected = {"decision": decision, "operation_id": operation_id, "state": "completed"}
            if stored is None or json.loads(stored[0]) != expected:
                raise ValueError("life deletion has no matching host decision")
            handoff = host.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                   ("life.activity.deletion.v1:" + decision_id,)).fetchone()
            evidence = json.loads(handoff[0]) if handoff is not None else None
            if (type(evidence) is not dict or set(evidence) != {"request_hash", "operation_id", "parameter_hash", "scope_hash", "source"}
                    or evidence["request_hash"] != row["request_hash"]
                    or evidence["operation_id"] != payload["management_operation_id"]
                    or evidence["parameter_hash"] != payload["management_parameter_hash"]
                    or type(evidence["source"]) is not dict or digest(evidence["source"]) != payload["source_hash"]
                    or digest(evidence["source"]["scope"]) != evidence["scope_hash"]):
                raise ValueError("life deletion has no matching source withdrawal evidence")
            source = evidence["source"]
            source_row = host.execute("""SELECT state FROM sources WHERE source_id=? AND lineage_id=? AND source_revision=?""",
                (source["source_id"], source["version"]["lineage_id"], source["version"]["revision"])).fetchone()
            if source_row is not None and source_row[0] != "revoked":
                raise ValueError("life deletion source is still active")
            scope = source["scope"]
            scope_names = ("installation_id", "platform", "scope_kind", "native_scope_id", "bot_id", "account_id", "conversation_id", "session_id")
            expected_scope = tuple((scope["kind"] if name == "scope_kind" else scope[name]) or "" for name in scope_names)
            for identifier, owner, parameters in (
                (operation_id, "life.command", row["request_hash"]),
                (payload["management_operation_id"], "role.life.management", payload["management_parameter_hash"]),
            ):
                receipt = host.execute("SELECT * FROM operations WHERE operation_id=?", (identifier,)).fetchone()
                if (receipt is None or receipt["owner"] != owner or receipt["state"] != "completed"
                        or receipt["commit_state"] != "committed" or receipt["parameter_hash"] != parameters
                        or receipt["idempotency_key"] != identifier or receipt["lineage_id"] != identifier
                        or receipt["operation_revision"] != 1
                        or tuple(receipt[name] for name in scope_names) != expected_scope):
                    raise ValueError("life deletion has no matching host receipt")
            if payload.get("mode") != "withdraw":
                for name in DELETED_TABLES:
                    covered.setdefault((name, request["owner_key"], payload["activity_id"]), set()).update(result["row_hashes"][name])
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("life deletion evidence is invalid or incomplete") from error
    return covered


def _creation_evidence(host, life, journal):
    """Do not return a newly created activity without its exact host audit."""
    if not journal:
        return
    checker = StagedActivityCreations(life)
    for row in life.execute("SELECT * FROM life_prepared_commands WHERE kind='activity.create' ORDER BY command_id"):
        if row["state"] == "aborted":
            continue
        try:
            request = {name: row[name] for name in ("command_id", "owner_key", "context_key", "kind", "expected_revision")}
            request["payload"] = json.loads(row["payload_json"])
            result = json.loads(row["result_json"])
            payload, activity = request["payload"], request["payload"]["activity"]
            management, schedule, scope = payload.get("management"), payload.get("schedule"), activity["scope"]
            social = (management is None and schedule is None and activity["work"]["kind"] == "social"
                      and request["command_id"].startswith("activity-social-start-"))
            if not social and (management is None) == (schedule is None):
                raise ValueError("life creation must have one original authority")
            identity = digest([request["context_key"], request["command_id"]])
            decision_id, operation_id = "life-decision-" + identity, "life-command-" + identity
            decision = {"command_id": request["command_id"], "request_hash": row["request_hash"],
                        "decision": "commit", "decision_id": decision_id}
            if (row["state"] != "acknowledged" or row["request_hash"] != _hash(request)
                    or row["decision_id"] != decision_id or row["decision_hash"] != _hash(decision)
                    or not checker._matches_result(request, result, require_current=False)):
                raise ValueError("life creation evidence changed")
            stored = host.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                  ("life.command.decision.v1:" + decision_id,)).fetchone()
            if stored is None or json.loads(stored[0]) != {"decision": decision, "operation_id": operation_id, "state": "completed"}:
                raise ValueError("life creation has no matching host decision")
            receipts = [(operation_id, "life.command", row["request_hash"])]
            if management is not None:
                reading = payload.get("reading_import")
                if reading is not None:
                    expected_id = "activity-reading-import-" + digest([request["context_key"], management["operation_id"]])
                    source_ref = activity["work"]["source"]
                    source = host.execute("SELECT content_hash FROM sources WHERE source_id=? AND lineage_id=? AND source_revision=?",
                        (source_ref["source_id"], source_ref["version"]["lineage_id"], source_ref["version"]["revision"])).fetchone()
                    if (request["command_id"] != expected_id or activity["work"]["kind"] != "reading"
                            or source is None or source[0] != activity["work"]["content_hash"]):
                        raise ValueError("reading import source or operation changed")
                    prefix = "life.activity.reading-import.v1:"
                    expected = {"request_hash": row["request_hash"], "operation_id": management["operation_id"],
                        "parameter_hash": management["parameter_hash"], "scope_hash": digest(scope),
                        "source_hash": activity["work"]["content_hash"]}
                else:
                    if activity["work"]["request_id"] != management["operation_id"]:
                        raise ValueError("life management request changed")
                    prefix = "life.activity.creation.v1:"
                    expected = {"request_hash": row["request_hash"], "operation_id": management["operation_id"],
                                "parameter_hash": management["parameter_hash"], "scope_hash": digest(scope)}
                handoff = host.execute("SELECT value FROM runtime_metadata WHERE key=?", (prefix + decision_id,)).fetchone()
                if handoff is None or json.loads(handoff[0]) != expected:
                    raise ValueError("life creation has no matching management evidence")
                receipts.append((management["operation_id"], "role.life.management", management["parameter_hash"]))
            elif social:
                expected_id = "activity-social-start-" + digest([request["context_key"], activity["work"]["request_id"],
                                                                 datetime.fromisoformat(activity["started_at"])])
                if request["command_id"] != expected_id:
                    raise ValueError("scheduled browsing creation identity changed")
            else:
                expected_id = "activity-scheduled-" + digest([request["context_key"], activity["work"]["request_id"],
                                                              datetime.fromisoformat(activity["started_at"])])
                if (request["command_id"] != expected_id or activity["work"]["kind"] != "sleep"
                        or activity["sleep"]["kind"] != schedule["kind"]):
                    raise ValueError("scheduled sleep creation identity changed")
            scope_names = ("installation_id", "platform", "scope_kind", "native_scope_id", "bot_id", "account_id", "conversation_id", "session_id")
            expected_scope = tuple((scope["kind"] if name == "scope_kind" else scope[name]) or "" for name in scope_names)
            for identifier, owner, parameters in receipts:
                receipt = host.execute("SELECT * FROM operations WHERE operation_id=?", (identifier,)).fetchone()
                if (receipt is None or receipt["owner"] != owner or receipt["state"] != "completed"
                        or receipt["commit_state"] != "committed" or receipt["parameter_hash"] != parameters
                        or receipt["idempotency_key"] != identifier or receipt["lineage_id"] != identifier
                        or receipt["operation_revision"] != 1
                        or tuple(receipt[name] for name in scope_names) != expected_scope):
                    raise ValueError("life creation has no matching host receipt")
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("life creation evidence is invalid or incomplete") from error


def _verify(host, life, expected_version):
    original, host_summary = _snapshot(host, expected_version)
    prefix = "life.sleep-interruption.v1:"
    if host.execute("SELECT 1 FROM runtime_metadata WHERE key>=? AND key<? LIMIT 1", (prefix, prefix + "\uffff")).fetchone():
        raise ValueError("message-triggered sleep interruptions are still pending")
    prefix = "life.reading.manual.v1:"
    if host.execute("SELECT 1 FROM runtime_metadata WHERE key>=? AND key<? LIMIT 1", (prefix, prefix + "\uffff")).fetchone():
        raise ValueError("manual reading requests are still pending")
    if [row[0] for row in life.execute("PRAGMA quick_check")] != ["ok"] or life.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("life snapshot failed integrity checks")
    manifest = life.execute("SELECT source_version, summary_json FROM staged_activity_manifest WHERE migration_id=?",
                            (MANIFEST,)).fetchone()
    if manifest is None or manifest[0] != expected_version:
        raise ValueError("activity migration manifest is unavailable")
    source_summary = json.loads(manifest[1])
    for name in (*TABLES, "host_operations", "detached_receipts"):
        if source_summary.get(name) != host_summary.get(name):
            raise ValueError("sealed host activity ledger differs from the imported snapshot")
    journal = life.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'").fetchone()
    if journal and life.execute("SELECT 1 FROM life_prepared_commands WHERE state IN ('prepared', 'committed') LIMIT 1").fetchone():
        raise ValueError("life commands are not yet resolved")
    _creation_evidence(host, life, journal)
    staged = {name: tuple(tuple(row) for row in values) for name, values in _staged_rows(life).items()}
    deleted = _deletion_evidence(host, life, journal)
    operation_ids = {row[6] for row in staged["role_activity_revisions"] if row[6] is not None}
    operation_ids.update(row[1] for row in staged["role_life_operations"])
    for operation_id in operation_ids:
        receipt = host.execute("SELECT owner, state, commit_state FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
        if (receipt is None or receipt["owner"] != "role.life" or receipt["state"] != "completed"
                or receipt["commit_state"] != "committed"):
            raise ValueError("life activity operation is not confirmed in the host")
    for name in ("role_activity_revisions", "role_artifacts"):
        present = set(staged[name])
        for source in original[name]:
            source = tuple(source)
            owner, identifier = (source[1], source[2]) if name == "role_activity_revisions" else (source[0], source[2])
            if source not in present and row_hash(name, source) not in deleted.get((name, owner, identifier), set()):
                raise ValueError("life ledger omitted an original history or artifact")
    if not {tuple(row) for row in original["role_life_operations"]} <= set(staged["role_life_operations"]):
        raise ValueError("life ledger omitted an original receipt")
    heads = {(row[0], row[1]): row for row in staged["role_activities"]}
    history = {(row[1], row[2], row[3]): row[7] for row in staged["role_activity_revisions"]}
    original_history = {(row[1], row[2], row[3]): tuple(row) for row in original["role_activity_revisions"]}
    for source in original["role_activities"]:
        owner, identifier, owner_json, revision, _state, _updated, raw = source
        current = heads.get((owner, identifier))
        if (current is None or current[2] != owner_json or current[3] < revision
                or history.get((owner, identifier, revision)) != raw):
            original_revision = original_history[(owner, identifier, revision)]
            if row_hash("role_activity_revisions", original_revision) not in deleted.get(
                    ("role_activity_revisions", owner, identifier), set()):
                raise ValueError("life ledger omitted an original activity")
    events = _events_confirmed(host, life)
    ceilings = {
        "role_life_operations": max(_sequence(life, "role_life_operations", staged["role_life_operations"]),
                                     host_summary["host_cursors"]["scan_ceiling"]),
        "role_activity_revisions": max(_sequence(life, "role_activity_revisions", staged["role_activity_revisions"]),
                                        host_summary["host_cursors"]["revision_ceiling"]),
    }
    if any(value >= 9223372036854775807 for value in ceilings.values()):
        raise ValueError("activity scan sequence is exhausted")
    return staged, ceilings, events


def _apply(host, target, staged, ceilings, expected_version):
    host.backup(target)
    target.execute("PRAGMA foreign_keys=ON")
    target.execute("BEGIN IMMEDIATE")
    for table in ("role_artifacts", "role_activity_revisions", "role_activities", "role_life_operations"):
        target.execute(f"DELETE FROM {table}")
    for table in ("role_activities", "role_activity_revisions", "role_artifacts", "role_life_operations"):
        columns = TABLES[table]
        names = ("rowid", *columns[1:]) if columns[0] == "scan_id" else columns
        target.executemany(f"INSERT INTO {table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", staged[table])
    for table, ceiling in ceilings.items():
        target.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (SCAN_FLOORS[table], str(ceiling)))
    rows, _ = _snapshot(target, expected_version)
    if _summary(rows) != _summary(staged) or target.execute("PRAGMA foreign_key_check").fetchone():
        raise ValueError("activity rollback copy does not match the life ledger")
    target.commit()


def rehearse(host_backup, life_backup, *, expected_version, destination=None):
    host_path, life_path = Path(host_backup).resolve(strict=True), Path(life_backup).resolve(strict=True)
    if host_path == life_path:
        raise ValueError("host and life snapshots must be separate")
    destination_path = Path(destination).resolve() if destination is not None else None
    if destination_path in (host_path, life_path):
        raise ValueError("rollback destination must be a new copy")
    with closing(_open(host_path)) as host, closing(_open(life_path)) as life:
        staged, ceilings, events = _verify(host, life, expected_version)
        result = {"state": "dry_run", "activities": len(staged["role_activities"]),
                  "revisions": len(staged["role_activity_revisions"]), "artifacts": len(staged["role_artifacts"]),
                  "receipts": len(staged["role_life_operations"]), "events": events}
        if destination_path is None:
            with closing(sqlite3.connect(":memory:", isolation_level=None)) as target:
                _apply(host, target, staged, ceilings, expected_version)
            return result
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(destination_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(destination_path, isolation_level=None)) as target:
                _apply(host, target, staged, ceilings, expected_version)
        except BaseException:
            destination_path.unlink(missing_ok=True)
            raise
        return {**result, "state": "rehearsed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("host_backup")
    parser.add_argument("life_backup")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--destination", help="new isolated host copy; omit for in-memory rehearsal")
    args = parser.parse_args()
    try:
        result = rehearse(args.host_backup, args.life_backup, expected_version=args.expected_version, destination=args.destination)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        parser.exit(1, "activity rollback rehearsal failed; source snapshots were not changed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
