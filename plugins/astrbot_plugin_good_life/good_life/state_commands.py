"""Commit a host-authorized state result into the life database once."""

from datetime import datetime, timezone
import hashlib
import json

from .prepared_commands import PreparedCommands, _hash, _stored_request, _synchronous
from .storage import StorageReader


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _previous(reader, request):
    payload = request["payload"]
    fields = {"storage_key", "legacy_key", "record_id", "operation_id", "previous_hash", "observed_at", "command_hash", "dialogue_through"}
    if type(payload) is not dict or set(payload) != fields or payload["record_id"] != "current":
        raise ValueError("state command fields are invalid")
    if any(type(payload[key]) is not str or not payload[key].strip() or len(payload[key]) > 512
           for key in ("storage_key", "legacy_key", "operation_id")):
        raise ValueError("state command identity is invalid")
    if (type(payload["command_hash"]) is not str or len(payload["command_hash"]) != 64
            or payload["storage_key"] == payload["legacy_key"]):
        raise ValueError("state command digest is invalid")
    at = datetime.fromisoformat(payload["observed_at"])
    if at.utcoffset() is None:
        raise ValueError("state observation time must be aware")
    if payload["dialogue_through"] is not None:
        through = datetime.fromisoformat(payload["dialogue_through"])
        if through.utcoffset() is None or through > at:
            raise ValueError("state dialogue cursor is invalid")
    primary = reader.one("SELECT record_json FROM staged_role_state_records WHERE owner_key=? AND record_id='current'",
                         (payload["storage_key"],))
    legacy = reader.one("SELECT record_json FROM staged_role_state_records WHERE owner_key=? AND record_id='current'",
                        (payload["legacy_key"],))
    raw = primary[0] if primary else legacy[0] if legacy else None
    previous = json.loads(raw) if raw is not None else None
    expected = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw is not None else None
    if ((previous["revision"] if previous else 0) != request["expected_revision"] or expected != payload["previous_hash"]
            or previous and (previous.get("id") != "current" or previous["updated_at"] > at.astimezone(timezone.utc).isoformat(timespec="microseconds"))):
        raise ValueError("state original record changed")
    return raw, previous, primary is None and legacy is not None


class StagedStateCommands:
    OPERATIONS = frozenset({"prepare", "status", "find_request", "verify_prepared", "resolve", "verify_commit", "acknowledge"})

    def __init__(self, storage):
        self.storage, self.reader = storage, StorageReader(storage)
        self.commands = PreparedCommands(storage)

    def dispatch(self, operation, *args, **kwargs):
        if operation not in self.OPERATIONS:
            raise ValueError("unknown state command operation")
        return getattr(self, operation)(*args, **kwargs)

    def prepare(self, request, *, prepared_at):
        if type(request) is not dict or request.get("kind") != "state.change":
            raise ValueError("expected a state command")
        return self.commands.prepare(**request, prepared_at=prepared_at, validate=self._validate)

    @staticmethod
    def _validate(reader, request):
        _previous(reader, request)
        p = request["payload"]
        if reader.one("SELECT 1 FROM staged_role_state_receipts WHERE operation_id=?", (p["operation_id"],)):
            raise ValueError("state operation already exists")
        if reader.one("SELECT 1 FROM staged_role_state_history WHERE owner_key=? AND record_id='current' AND revision=?",
                      (p["storage_key"], request["expected_revision"] + 1)):
            raise ValueError("state revision already exists")
        return True

    def find_request(self, *, command_id, owner_key, context_key):
        row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,))
        if row is None:
            return None
        request = _stored_request(row)
        if (request["kind"] != "state.change" or request["owner_key"] != owner_key or request["context_key"] != context_key
                or _hash(request) != row["request_hash"]):
            raise ValueError("stored state request changed")
        return request

    def status(self, request):
        row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (request["command_id"],))
        if row is None or _hash(request) != row["request_hash"]:
            raise ValueError("state command is unavailable")
        return {"state": row["state"], "request_hash": row["request_hash"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None}

    def verify_prepared(self, request):
        return self.commands.verify_prepared(request)

    def resolve(self, decision, *, verify_host_decision, read_host_effect):
        effect = _synchronous(read_host_effect(dict(decision))) if decision.get("decision") == "commit" else None
        return self.commands.resolve(decision, verify_host_decision=verify_host_decision,
                                     apply=lambda connection, request: self._apply(connection, request, effect))

    def _apply(self, connection, request, effect):
        self._validate(self.reader, request)
        p = request["payload"]
        _raw, _current, adopt = _previous(self.reader, request)
        if type(effect) is not dict or set(effect) != {"record", "receipt"}:
            raise ValueError("host state effect is invalid")
        record, receipt = effect["record"], effect["receipt"]
        timestamp = datetime.fromisoformat(p["observed_at"]).astimezone(timezone.utc).isoformat(timespec="microseconds")
        expected_receipt = {"operation_id": p["operation_id"], "id": "current", "revision": request["expected_revision"] + 1,
                            "state": "saved", "replayed": False}
        if (type(record) is not dict or record.get("id") != "current" or record.get("state") != "saved"
                or record.get("revision") != expected_receipt["revision"] or record.get("updated_at") != timestamp
                or type(record.get("values")) is not dict or type(record.get("sources")) is not list or receipt != expected_receipt
                or len(_encode(record).encode("utf-8")) > 131072):
            raise ValueError("host state effect does not match the prepared revision")
        if adopt:
            connection.execute("""INSERT INTO staged_role_state_records
                SELECT ?, record_id, revision, updated_at, record_json FROM staged_role_state_records
                WHERE owner_key=? AND record_id='current'""", (p["storage_key"], p["legacy_key"]))
            connection.execute("""INSERT INTO staged_role_state_history
                SELECT ?, record_id, revision, operation_id, record_json FROM staged_role_state_history
                WHERE owner_key=? AND record_id='current'""", (p["storage_key"], p["legacy_key"]))
        raw = _encode(record)
        connection.execute("""INSERT INTO staged_role_state_records VALUES (?, 'current', ?, ?, ?)
            ON CONFLICT(owner_key,record_id) DO UPDATE SET revision=excluded.revision,
                updated_at=excluded.updated_at,record_json=excluded.record_json""",
            (p["storage_key"], record["revision"], timestamp, raw))
        connection.execute("INSERT INTO staged_role_state_history VALUES (?, 'current', ?, ?, ?)",
                           (p["storage_key"], record["revision"], p["operation_id"], raw))
        connection.execute("INSERT INTO staged_role_state_receipts VALUES (?, ?, 'current', ?)",
                           (p["operation_id"], p["storage_key"], _encode(receipt)))
        if p["dialogue_through"] is not None:
            key, through = "role.state.dialogue-cursor.v1:" + request["context_key"], p["dialogue_through"]
            row = connection.execute("SELECT value FROM staged_state_metadata WHERE key=?", (key,)).fetchone()
            if row is None or json.loads(row[0])["through"] < through:
                connection.execute("""INSERT INTO staged_state_metadata(key,value) VALUES (?,?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, _encode({"through": through})))
        return {"record": record, "receipt": receipt}

    def verify_commit(self, decision):
        if not self.commands.verify_commit(decision):
            return False
        try:
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
            if row is None or not row["result_json"]:
                return False
            request, effect = _stored_request(row), json.loads(row["result_json"])
            p, record, receipt = request["payload"], effect["record"], effect["receipt"]
            history = self.reader.one("""SELECT record_json FROM staged_role_state_history
                WHERE owner_key=? AND record_id='current' AND revision=? AND operation_id=?""",
                (p["storage_key"], record["revision"], p["operation_id"]))
            saved = self.reader.one("SELECT result_json FROM staged_role_state_receipts WHERE operation_id=? AND owner_key=?",
                                    (p["operation_id"], p["storage_key"]))
            head = self.reader.one("""SELECT revision,record_json FROM staged_role_state_records
                WHERE owner_key=? AND record_id='current'""", (p["storage_key"],))
            return (history is not None and json.loads(history[0]) == record and saved is not None
                    and json.loads(saved[0]) == receipt and head is not None and head[0] >= record["revision"]
                    and (row["state"] == "acknowledged" or head[0] == record["revision"])
                    and (head[0] != record["revision"] or json.loads(head[1]) == record))
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def acknowledge(self, command_id, *, verify_host_completion):
        return self.commands.acknowledge(command_id, verify_host_completion=verify_host_completion)
