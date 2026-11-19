"""Copy life-owned settings back into an isolated companion configuration backup."""

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
from companion_v2.configuration.life_handoff import APPLIED_PREFIX, EFFECT_PREFIX
from companion_v2.configuration.life_provider import OWNER_KEY
from companion_v2.execution.models import digest
from good_life.configuration_import import HOST_FIELDS, LIFE_FIELDS, SHARED_FIELDS
from good_life.prepared_commands import _hash, _stored_request
from stage_life_migration import _fingerprint


def _open(path):
    connection = sqlite3.connect(Path(path).resolve(strict=True).as_uri() + "?mode=ro", uri=True, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    connection.execute("BEGIN")
    return connection


def _owned(field):
    if field.startswith("role.state."):
        return True
    if not field.startswith("role.life."):
        return False
    name = field.removeprefix("role.life.")
    if name not in LIFE_FIELDS | SHARED_FIELDS | HOST_FIELDS:
        raise ValueError("unclassified life configuration field")
    return name in LIFE_FIELDS | SHARED_FIELDS


def _verify(config, life, runtime, expected_version):
    if config.execute("SELECT value FROM runtime_metadata WHERE key='schema_version'").fetchone()[0] != expected_version:
        raise ValueError("configuration backup has a different schema version")
    owner = config.execute("SELECT value FROM runtime_metadata WHERE key=?", (OWNER_KEY,)).fetchone()
    if owner is None or owner[0] != "good_life":
        raise ValueError("configuration was not owned by the life plugin")
    for connection in (config, life, runtime):
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchone():
            raise ValueError("rollback backup failed integrity checks")
    installation = runtime.execute("SELECT value FROM runtime_metadata WHERE key='installation_id'").fetchone()
    if installation is None:
        raise ValueError("runtime backup has no installation identity")
    journal = life.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'").fetchone()
    if journal and life.execute("SELECT 1 FROM life_prepared_commands WHERE state IN ('prepared','committed') LIMIT 1").fetchone():
        raise ValueError("life commands are not yet resolved")
    if journal:
        for item in life.execute("SELECT * FROM life_prepared_commands WHERE kind='configuration.patch'"):
            if item["state"] == "aborted":
                continue
            request = _stored_request(item)
            decision_id = "life-decision-" + digest([request["context_key"], request["command_id"]])
            operation_id = "life-command-" + digest([request["context_key"], request["command_id"]])
            decision = {"command_id": request["command_id"], "request_hash": item["request_hash"],
                        "decision": "commit", "decision_id": decision_id}
            saved = config.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                   ("life.command.decision.v1:" + decision_id,)).fetchone()
            effect_row = config.execute("SELECT value FROM runtime_metadata WHERE key=?", (EFFECT_PREFIX + decision_id,)).fetchone()
            receipt = life.execute("SELECT result_json FROM life_configuration_receipts WHERE command_id=?", (item["command_id"],)).fetchone()
            command_op = config.execute("SELECT owner,state,commit_state,parameter_hash FROM operations WHERE operation_id=?",
                                        (operation_id,)).fetchone()
            original_op = config.execute("SELECT owner,state,commit_state,parameter_hash FROM operations WHERE operation_id=?",
                                         (request["payload"]["operation_id"],)).fetchone()
            change = config.execute("SELECT group_id,result_json FROM configuration_changes WHERE operation_id=?",
                                    (request["payload"]["operation_id"],)).fetchone()
            effect = json.loads(effect_row[0]) if effect_row else None
            expected_owner = "role." + request["payload"]["domain"] + ".settings"
            if (item["state"] != "acknowledged" or item["request_hash"] != _hash(request)
                    or item["decision_id"] != decision_id or item["decision_hash"] != _hash(decision)
                    or saved is None or json.loads(saved[0]) != {"decision": decision, "operation_id": operation_id, "state": "completed"}
                    or receipt is None or json.loads(receipt[0]) != json.loads(item["result_json"])
                    or command_op is None or tuple(command_op) != ("life.command", "completed", "committed", item["request_hash"])
                    or original_op is None or tuple(original_op) != (expected_owner, "completed", "committed", request["payload"]["parameter_hash"])
                    or change is None or change["group_id"] != "role." + request["payload"]["domain"]
                    or type(effect) is not dict or effect.get("request_hash") != item["request_hash"]
                    or effect.get("operation_id") != request["payload"]["operation_id"]
                    or effect.get("parameter_hash") != request["payload"]["parameter_hash"]):
                raise ValueError("configuration command lacks its completed host audit")
            state_effect = effect.get("state_effect")
            if state_effect is not None:
                applied = runtime.execute("SELECT value FROM runtime_metadata WHERE key=?", (APPLIED_PREFIX + decision_id,)).fetchone()
                if applied is None or applied[0] != digest(state_effect):
                    raise ValueError("state configuration effects are not confirmed")
    targets, values = {}, {}
    for domain in ("configuration", "state_configuration"):
        for row in life.execute(f"SELECT * FROM staged_{domain}_targets"):
            document = json.loads(row["target_json"])
            if document.get("installation_id") != installation[0]:
                raise ValueError("life configuration belongs to another installation")
            current = targets.setdefault(row["target_key"], {"document": document, "revisions": [], "updated": []})
            if current["document"] != document:
                raise ValueError("life configuration target identity changed")
            current["revisions"].append(row["revision"])
            current["updated"].append(row["updated_at"])
        for row in life.execute(f"SELECT * FROM staged_{domain}_values"):
            key = (row["target_key"], row["field_id"])
            if (row["target_key"] not in targets or not _owned(row["field_id"]) or key in values
                    or (domain == "configuration") != row["field_id"].startswith("role.life.")):
                raise ValueError("life configuration contains an invalid field")
            json.loads(row["value_json"])
            values[key] = row["value_json"]
    for row in config.execute("SELECT target_key,field_id FROM configuration_values WHERE field_id LIKE 'role.life.%' OR field_id LIKE 'role.state.%'"):
        if _owned(row["field_id"]) and row["target_key"] not in targets:
            raise ValueError("life configuration omitted an original target")
    return targets, values


def rehearse(configuration_backup, life_backup, runtime_backup, *, expected_version, destination=None):
    sources = [Path(item).resolve(strict=True) for item in (configuration_backup, life_backup, runtime_backup)]
    target = Path(destination).resolve() if destination is not None else None
    if len(set(sources)) != 3 or target in sources or target is not None and target.exists():
        raise ValueError("rollback sources and destination must be separate")
    fingerprints = [_fingerprint(path) for path in sources]
    with closing(_open(sources[0])) as config, closing(_open(sources[1])) as life, closing(_open(sources[2])) as runtime:
        targets, values = _verify(config, life, runtime, expected_version)
        result = {"state": "dry_run", "targets": len(targets), "values": len(values)}
        if target is None:
            return result
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(target, isolation_level=None)) as copy:
                copy.execute("PRAGMA foreign_keys=ON")
                config.backup(copy)
                copy.execute("BEGIN IMMEDIATE")
                existing = tuple(copy.execute("SELECT target_key,field_id FROM configuration_values WHERE field_id LIKE 'role.life.%' OR field_id LIKE 'role.state.%'"))
                copy.executemany("DELETE FROM configuration_values WHERE target_key=? AND field_id=?", [row for row in existing if _owned(row[1])])
                revisions = {}
                for key, data in targets.items():
                    old = copy.execute("SELECT target_json,revision,updated_at FROM configuration_targets WHERE target_key=?", (key,)).fetchone()
                    if old is not None and json.loads(old[0]) != data["document"]:
                        raise ValueError("host configuration target identity changed")
                    revision = max([old[1] if old else 0, *data["revisions"]]) + 1
                    updated = max([old[2] if old else "", *data["updated"]])
                    copy.execute("INSERT OR IGNORE INTO configuration_targets VALUES (?,?,0,?)",
                                 (key, json.dumps(data["document"], ensure_ascii=True, sort_keys=True, separators=(",", ":")), updated))
                    copy.execute("UPDATE configuration_targets SET revision=?,updated_at=? WHERE target_key=?", (revision, updated, key))
                    revisions[key] = revision
                copy.executemany("INSERT INTO configuration_values VALUES (?,?,?,?)",
                    [(key, field, value, revisions[key]) for (key, field), value in sorted(values.items())])
                actual = {(row[0], row[1]): row[2] for row in copy.execute(
                    "SELECT target_key,field_id,value_json FROM configuration_values WHERE field_id LIKE 'role.life.%' OR field_id LIKE 'role.state.%'") if _owned(row[1])}
                if actual != values or copy.execute("PRAGMA foreign_key_check").fetchone():
                    raise ValueError("rollback configuration copy differs from life settings")
                copy.commit()
            if [_fingerprint(path) for path in sources] != fingerprints:
                raise ValueError("a backup changed during configuration rollback")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return {**result, "state": "rehearsed"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("configuration_backup")
    parser.add_argument("life_backup")
    parser.add_argument("runtime_backup")
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--destination")
    args = parser.parse_args()
    try:
        result = rehearse(args.configuration_backup, args.life_backup, args.runtime_backup,
                          expected_version=args.expected_version, destination=args.destination)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as error:
        parser.exit(1, f"configuration rollback failed: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
