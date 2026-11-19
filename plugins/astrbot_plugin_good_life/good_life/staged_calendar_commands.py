"""Prepare and persist isolated calendar changes; not a live AstrBot handler.

The host must authorize the original role and conversation, normalize the
actor and sources, and fence reminder delivery before committing its decision.
"""

from datetime import datetime, timezone
import hashlib
import json

from .calendar_changes import change
from .prepared_commands import PreparedCommands, _hash, _stored_request
from .storage import StorageReader


FIELDS = {"storage_key", "lookup_owner_keys", "record_id", "operation_id", "previous_hash", "action",
          "data", "sources", "actor", "operator_authenticated", "planning", "observed_at"}


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _timestamp(value):
    if type(value) is not str:
        raise ValueError("calendar observation time is invalid")
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("calendar observation time must be timezone-aware")
    return result.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _proposal(reader, request, *, prior=None, inspect_store=True):
    payload = request["payload"]
    if type(payload) is not dict or set(payload) != FIELDS:
        raise ValueError("calendar command fields are invalid")
    for name in ("storage_key", "record_id", "operation_id"):
        value = payload[name]
        if type(value) is not str or not value.strip() or len(value) > 512:
            raise ValueError("calendar identity is invalid")
    keys = payload["lookup_owner_keys"]
    if (type(keys) is not list or not keys or len(keys) != len(set(keys))
            or any(type(key) is not str or not key.strip() or len(key) > 512 for key in keys)
            or payload["storage_key"] not in keys):
        raise ValueError("calendar lookup owners are invalid")
    if type(payload["sources"]) is not list or type(payload["actor"]) is not dict or type(payload["operator_authenticated"]) is not bool:
        raise ValueError("calendar authority documents are invalid")
    timestamp = _timestamp(payload["observed_at"])
    if inspect_store:
        row = reader.one("""SELECT revision, updated_at, record_json FROM staged_calendar_records
            WHERE owner_key=? AND record_id=?""", (payload["storage_key"], payload["record_id"]))
        raw = row["record_json"] if row else None
        current = json.loads(raw) if raw is not None else None
        if row is not None and (current.get("id") != payload["record_id"] or current.get("revision") != row["revision"]
                                or current.get("updated_at") != row["updated_at"]):
            raise ValueError("calendar record index is inconsistent")
        exists = any(reader.one("SELECT 1 FROM staged_calendar_records WHERE owner_key=? AND record_id=?",
                                (key, payload["record_id"])) is not None for key in keys)
    else:
        raw, current = prior, json.loads(prior) if prior is not None else None
        exists = current is not None
    if (current["revision"] if current else 0) != request["expected_revision"]:
        raise ValueError("calendar revision changed")
    expected_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw is not None else None
    if expected_hash != payload["previous_hash"]:
        raise ValueError("calendar original content changed")
    if current and current["updated_at"] > timestamp:
        raise ValueError("calendar update cannot move backwards in time")
    computed = change(current, action=payload["action"], data=payload["data"], sources=payload["sources"],
                      actor=payload["actor"], operator_authenticated=payload["operator_authenticated"],
                      planning=payload["planning"], identifier_exists=exists,
                      observed_at=datetime.fromisoformat(timestamp))
    record = {**computed["record"], "id": payload["record_id"], "revision": request["expected_revision"] + 1,
              "updated_at": timestamp}
    if len(_encode(record).encode("utf-8")) > 131072:
        raise ValueError("calendar record exceeds its storage contract")
    receipt = {"operation_id": payload["operation_id"], "id": payload["record_id"],
               "revision": record["revision"], "state": record.get("state", "saved"), "replayed": False}
    return {"previous": current, "previous_raw": raw, "record": record, "receipt": receipt, "cancel_reminder": computed["cancel_reminder"],
            "evidence": computed["evidence"]}


class StagedCalendarCommands:
    OPERATIONS = frozenset({"prepare", "status", "recover", "find_request", "verify_prepared", "resolve",
                            "verify_commit", "acknowledge", "adoption_certificate"})

    def __init__(self, storage):
        self.storage = storage
        self.reader = StorageReader(storage)
        self.commands = PreparedCommands(storage)

    def dispatch(self, operation, *args, **kwargs):
        if type(operation) is str and operation.startswith("batch."):
            from .calendar_batch_commands import CalendarBatchCommands
            return CalendarBatchCommands(self.storage).dispatch(operation, *args, **kwargs)
        if type(operation) is not str or operation not in self.OPERATIONS:
            raise ValueError("unknown calendar command operation")
        return getattr(self, operation)(*args, **kwargs)

    def adoption_certificate(self):
        from .calendar_adoption import adoption_certificate
        with self.reader.locked():
            return adoption_certificate(self.reader._connection)

    def prepare(self, request, *, prepared_at):
        if type(request) is not dict or request.get("kind") != "calendar.change":
            raise ValueError("expected a calendar change command")
        return self.commands.prepare(**request, prepared_at=prepared_at, validate=self._validate)

    @staticmethod
    def _validate(view, request):
        _proposal(view, request)
        payload = request["payload"]
        if view.one("SELECT 1 FROM staged_calendar_receipts WHERE operation_id=?", (payload["operation_id"],)):
            raise ValueError("calendar operation already exists")
        if view.one("""SELECT 1 FROM staged_calendar_history WHERE owner_key=? AND record_id=? AND revision=?""",
                    (payload["storage_key"], payload["record_id"], request["expected_revision"] + 1)):
            raise ValueError("calendar revision already exists")
        return True

    def proposal(self, request):
        """Expose the host effects to authorize before it persists a decision."""
        with self.reader.locked():
            if not self.commands.verify_prepared(request):
                raise ValueError("calendar command is not prepared")
            return _proposal(self.reader, request)

    def status(self, request):
        """Return the exact proposal or result for an already stored request."""
        with self.reader.locked():
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (request["command_id"],))
            if row is None or _stored_request(row) != request or _hash(request) != row["request_hash"]:
                raise ValueError("calendar command does not match its stored request")
            if row["state"] == "prepared":
                result = _proposal(self.reader, request)
            elif row["state"] in ("committed", "acknowledged"):
                decision = {"command_id": row["command_id"], "request_hash": row["request_hash"],
                            "decision": "commit", "decision_id": row["decision_id"]}
                if not self._verify_commit_locked(decision):
                    raise ValueError("calendar command result is inconsistent")
                result = json.loads(row["result_json"])
            else:
                result = None
            return {"state": row["state"], "request_hash": row["request_hash"], "proposal": result}

    def recover(self, *, command_id, owner_key, context_key, request_hash):
        return self.commands.recovery_request(command_id=command_id, owner_key=owner_key,
                                              context_key=context_key, request_hash=request_hash)

    def find_request(self, *, command_id, owner_key, context_key):
        """Find the exact original command for a host-authorized retry."""
        with self.reader.locked():
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,))
            if row is None or row["owner_key"] != owner_key or row["context_key"] != context_key or row["kind"] != "calendar.change":
                return None
            request = _stored_request(row)
            if _hash(request) != row["request_hash"]:
                raise ValueError("calendar stored request changed")
            return request

    def verify_prepared(self, request):
        return self.commands.verify_prepared(request)

    def resolve(self, decision, *, verify_host_decision):
        return self.commands.resolve(decision, verify_host_decision=verify_host_decision, apply=self.apply)

    def apply(self, connection, request):
        self._validate(self.reader, request)
        result = _proposal(self.reader, request)
        payload, record = request["payload"], result["record"]
        encoded = _encode(record)
        if request["expected_revision"] == 0:
            connection.execute("INSERT INTO staged_calendar_records VALUES (?, ?, ?, ?, ?)",
                               (payload["storage_key"], payload["record_id"], record["revision"], record["updated_at"], encoded))
        else:
            changed = connection.execute("""UPDATE staged_calendar_records SET revision=?, updated_at=?, record_json=?
                WHERE owner_key=? AND record_id=? AND revision=?""", (record["revision"], record["updated_at"], encoded,
                payload["storage_key"], payload["record_id"], request["expected_revision"]))
            if changed.rowcount != 1:
                raise ValueError("calendar revision changed during commit")
        connection.execute("INSERT INTO staged_calendar_history VALUES (?, ?, ?, ?, ?)",
                           (payload["storage_key"], payload["record_id"], record["revision"], payload["operation_id"], encoded))
        connection.execute("INSERT INTO staged_calendar_receipts VALUES (?, ?, ?, ?)",
                           (payload["operation_id"], payload["storage_key"], payload["record_id"], _encode(result["receipt"])))
        return result

    def verify_commit(self, decision):
        with self.reader.locked():
            return self._verify_commit_locked(decision)

    def _verify_commit_locked(self, decision):
        if not self.commands.verify_commit(decision):
            return False
        try:
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
            request = {name: row[name] for name in ("command_id", "owner_key", "context_key", "kind", "expected_revision")}
            request["payload"] = json.loads(row["payload_json"])
            payload = request["payload"]
            current = self.reader.one("""SELECT revision, record_json FROM staged_calendar_records
                WHERE owner_key=? AND record_id=?""", (payload["storage_key"], payload["record_id"]))
            revision = request["expected_revision"] + 1
            historical = self.reader.one("""SELECT operation_id, record_json FROM staged_calendar_history
                WHERE owner_key=? AND record_id=? AND revision=?""", (payload["storage_key"], payload["record_id"], revision))
            previous = self.reader.one("""SELECT record_json FROM staged_calendar_history
                WHERE owner_key=? AND record_id=? AND revision=?""", (payload["storage_key"], payload["record_id"], revision - 1)) if revision > 1 else None
            receipt = self.reader.one("""SELECT result_json FROM staged_calendar_receipts
                WHERE operation_id=? AND owner_key=? AND record_id=?""",
                (payload["operation_id"], payload["storage_key"], payload["record_id"]))
            if not current or not historical or not receipt or (revision > 1 and not previous):
                return False
            if current["revision"] < revision or (row["state"] != "acknowledged" and current["revision"] != revision):
                return False
            expected = _proposal(self.reader, request, prior=previous["record_json"] if previous else None, inspect_store=False)
            encoded = _encode(expected["record"])
            return (historical["operation_id"] == payload["operation_id"] and historical["record_json"] == encoded
                    and (current["revision"] != revision or current["record_json"] == encoded)
                    and receipt["result_json"] == _encode(expected["receipt"]) and json.loads(row["result_json"]) == expected)
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def acknowledge(self, command_id, *, verify_host_completion):
        return self.commands.acknowledge(command_id, verify_host_completion=verify_host_completion)
