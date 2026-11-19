"""Prepare and commit life-owned configuration changes after host approval."""

import hashlib
import json

from .configuration_import import LIFE_FIELDS, SHARED_FIELDS
from .prepared_commands import PreparedCommands, _hash, _stored_request
from .storage import StorageReader


def _target_key(document):
    encoded = json.dumps(document, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _tables(domain):
    if domain == "life":
        return "staged_configuration_targets", "staged_configuration_values"
    if domain == "state":
        return "staged_state_configuration_targets", "staged_state_configuration_values"
    raise ValueError("unknown configuration domain")


def _allowed(domain, name):
    return (name in {"role.life." + item for item in LIFE_FIELDS | SHARED_FIELDS} if domain == "life"
            else name.startswith("role.state."))


class StagedConfigurationCommands:
    def __init__(self, storage):
        self.storage, self.reader, self.commands = storage, StorageReader(storage), PreparedCommands(storage)

    def install(self):
        with self.storage.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS life_configuration_receipts (
                command_id TEXT PRIMARY KEY, result_json TEXT NOT NULL)""")

    def dispatch(self, operation, *args, **kwargs):
        if operation not in ("prepare", "proposal", "verify_prepared", "resolve", "verify_commit", "acknowledge", "request_for_replay"):
            raise ValueError("unknown life configuration command")
        return getattr(self, operation)(*args, **kwargs)

    def _validate(self, view, request):
        payload = request["payload"]
        if (request["kind"] != "configuration.patch" or request["expected_revision"] != 0
                or type(payload) is not dict or set(payload) != {"domain", "targets", "operation_id", "parameter_hash", "observed_at", "identity"}
                or type(payload["operation_id"]) is not str or not payload["operation_id"]
                or type(payload["parameter_hash"]) is not str or len(payload["parameter_hash"]) != 64
                or type(payload["observed_at"]) is not str or not payload["observed_at"]
                or type(payload["targets"]) is not list or not 1 <= len(payload["targets"]) <= 4):
            raise ValueError("invalid life configuration command")
        identity = payload["identity"]
        if (type(identity) is not dict or set(identity) != {"installation_id", "bot_id", "role_id"}
                or any(type(value) is not str or not value for value in identity.values())):
            raise ValueError("invalid life configuration identity")
        domain = payload["domain"]
        target_table, value_table = _tables(domain)
        keys = []
        for target in payload["targets"]:
            if (type(target) is not dict or set(target) != {"key", "document", "revision", "changes"}
                    or type(target["document"]) is not dict or target["key"] != _target_key(target["document"])
                    or type(target["revision"]) is not int or target["revision"] < 0
                    or type(target["changes"]) is not list or len(target["changes"]) > 512):
                raise ValueError("invalid life configuration target")
            keys.append(target["key"])
            row = view.one(f"SELECT target_json,revision FROM {target_table} WHERE target_key=?", (target["key"],))
            if ((row["revision"] if row else 0) != target["revision"]
                    or row is not None and json.loads(row["target_json"]) != target["document"]):
                raise ValueError("life configuration target changed")
            names = []
            for item in target["changes"]:
                if (type(item) is not dict or set(item) != {"field_id", "value_json"}
                        or type(item["field_id"]) is not str or not _allowed(domain, item["field_id"])
                        or item["value_json"] is not None and type(item["value_json"]) is not str):
                    raise ValueError("invalid life configuration value")
                if item["value_json"] is not None:
                    value = json.loads(item["value_json"], parse_constant=lambda _x: (_ for _ in ()).throw(ValueError("nonfinite")))
                    if type(value) not in (str, bool, int, float):
                        raise ValueError("configuration value must be scalar")
                names.append(item["field_id"])
            if names != sorted(set(names)):
                raise ValueError("configuration fields must be unique and sorted")
            view.one(f"SELECT 1 FROM {value_table} WHERE target_key=? LIMIT 1", (target["key"],))
        if keys != sorted(set(keys)):
            raise ValueError("configuration targets must be unique and sorted")
        return True

    def prepare(self, request, *, prepared_at):
        return self.commands.prepare(**request, prepared_at=prepared_at, validate=self._validate)

    def verify_prepared(self, request):
        return self.commands.verify_prepared(request)

    def proposal(self, request):
        with self.reader.locked():
            if not self.commands.verify_prepared(request):
                raise ValueError("configuration command is not prepared")
            self._validate(self.reader, request)
            return self._result(self.reader, request)

    def _result(self, view, request):
        _target_table, value_table = _tables(request["payload"]["domain"])
        result = []
        for target in request["payload"]["targets"]:
            changed = []
            for item in target["changes"]:
                row = view.one(f"SELECT value_json FROM {value_table} WHERE target_key=? AND field_id=?",
                               (target["key"], item["field_id"]))
                if (row is None and item["value_json"] is not None or row is not None and item["value_json"] != row["value_json"]):
                    changed.append(item["field_id"])
            result.append({"key": target["key"], "revision": target["revision"] + bool(changed), "changed_fields": changed})
        return {"targets": result}

    def apply(self, connection, request):
        self._validate(self.reader, request)
        target_table, value_table = _tables(request["payload"]["domain"])
        result = self._result(self.reader, request)
        for target, saved in zip(request["payload"]["targets"], result["targets"]):
            connection.execute(f"INSERT OR IGNORE INTO {target_table} VALUES (?,?,0,?)",
                (target["key"], json.dumps(target["document"], ensure_ascii=True, sort_keys=True, separators=(",", ":")),
                 request["payload"]["observed_at"]))
            for item in target["changes"]:
                if item["field_id"] not in saved["changed_fields"]:
                    continue
                if item["value_json"] is None:
                    connection.execute(f"DELETE FROM {value_table} WHERE target_key=? AND field_id=?", (target["key"], item["field_id"]))
                else:
                    connection.execute(f"""INSERT INTO {value_table} VALUES (?,?,?,?) ON CONFLICT(target_key,field_id)
                        DO UPDATE SET value_json=excluded.value_json,revision=excluded.revision""",
                        (target["key"], item["field_id"], item["value_json"], saved["revision"]))
            connection.execute(f"UPDATE {target_table} SET revision=?,updated_at=? WHERE target_key=?",
                               (saved["revision"], request["payload"]["observed_at"], target["key"]))
        connection.execute("INSERT INTO life_configuration_receipts VALUES (?,?)",
                           (request["command_id"], json.dumps(result, sort_keys=True, separators=(",", ":"))))
        return result

    def resolve(self, decision, *, verify_host_decision):
        return self.commands.resolve(decision, verify_host_decision=verify_host_decision, apply=self.apply)

    def verify_commit(self, decision):
        if not self.commands.verify_commit(decision):
            return False
        row = self.reader.one("SELECT result_json FROM life_configuration_receipts WHERE command_id=?", (decision["command_id"],))
        command = self.reader.one("SELECT result_json FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
        return row is not None and command is not None and json.loads(row["result_json"]) == json.loads(command["result_json"])

    def acknowledge(self, command_id, *, verify_host_completion):
        return self.commands.acknowledge(command_id, verify_host_completion=verify_host_completion)

    def request_for_replay(self, *, command_id, owner_key, context_key):
        row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,))
        if row is None:
            return None
        request = _stored_request(row)
        if (request["kind"] != "configuration.patch" or request["owner_key"] != owner_key
                or request["context_key"] != context_key or _hash(request) != row["request_hash"]):
            raise ValueError("configuration command identity changed")
        return request
