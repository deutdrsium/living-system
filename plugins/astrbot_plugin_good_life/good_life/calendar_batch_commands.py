"""Prepare and commit one atomic batch of calendar changes."""

import json

from .prepared_commands import PreparedCommands, _hash, _stored_request
from .staged_calendar_commands import StagedCalendarCommands, _encode, _proposal
from .storage import StorageReader


class CalendarBatchCommands:
    OPERATIONS = frozenset({"prepare", "status", "verify_prepared", "resolve", "verify_commit", "acknowledge"})

    def __init__(self, storage, *, spending=None):
        self.reader = StorageReader(storage)
        self.commands = PreparedCommands(storage)
        self.spending = spending
        self.single = StagedCalendarCommands(storage, spending=spending)

    def dispatch(self, operation, *args, **kwargs):
        name = operation.removeprefix("batch.")
        if not operation.startswith("batch.") or name not in self.OPERATIONS:
            raise ValueError("unknown calendar batch operation")
        return getattr(self, name)(*args, **kwargs)

    @staticmethod
    def _items(request):
        if (type(request) is not dict or request.get("kind") != "calendar.batch" or request.get("expected_revision") != 0
                or type(request.get("payload")) is not dict
                or set(request["payload"]) - {"spending"} != {"commands", "storage_keys"}):
            raise ValueError("invalid calendar batch")
        items = request["payload"]["commands"]
        if type(items) is not list or len(items) > 200:
            raise ValueError("invalid calendar batch size")
        ids, operations, commands, keys = set(), set(), set(), set()
        fields = {"command_id", "owner_key", "context_key", "kind", "expected_revision", "payload"}
        for item in items:
            if (type(item) is not dict or set(item) != fields or item["kind"] != "calendar.change"
                    or item["owner_key"] != request["owner_key"] or item["context_key"] != request["context_key"]
                    or type(item["expected_revision"]) is not int or item["expected_revision"] < 0
                    or type(item["payload"]) is not dict):
                raise ValueError("calendar batch crosses its original role or context")
            record = item["payload"].get("record_id")
            operation = item["payload"].get("operation_id")
            command = item["command_id"]
            key = item["payload"].get("storage_key")
            if (any(type(value) is not str or not value for value in (record, operation, command, key))
                    or record in ids or operation in operations or command in commands):
                raise ValueError("calendar batch repeats a record or operation")
            ids.add(record)
            operations.add(operation)
            commands.add(command)
            keys.add(key)
        if request["payload"]["storage_keys"] != sorted(keys):
            raise ValueError("calendar batch read boundary is invalid")
        return items

    def _validate(self, view, request):
        for item in self._items(request):
            self.single._validate(view, item)
        return True

    def prepare(self, request, *, prepared_at):
        self._items(request)
        return self.commands.prepare(**request, prepared_at=prepared_at, validate=self._validate)

    def verify_prepared(self, request):
        return self.commands.verify_prepared(request)

    def status(self, request):
        with self.reader.locked():
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (request["command_id"],))
            if row is None or _stored_request(row) != request or _hash(request) != row["request_hash"]:
                raise ValueError("calendar batch does not match its stored request")
            if row["state"] == "prepared":
                result = {"proposals": [_proposal(self.reader, item) for item in self._items(request)]}
            elif row["state"] in ("committed", "acknowledged"):
                decision = {"command_id": row["command_id"], "request_hash": row["request_hash"],
                            "decision": "commit", "decision_id": row["decision_id"]}
                if not self._verify_commit_locked(decision):
                    raise ValueError("calendar batch result is inconsistent")
                result = json.loads(row["result_json"])
            else:
                result = None
            return {"state": row["state"], "request_hash": row["request_hash"], "proposal": result}

    def resolve(self, decision, *, verify_host_decision):
        return self.commands.resolve(decision, verify_host_decision=verify_host_decision, apply=self.apply)

    def apply(self, connection, request):
        self._validate(self.reader, request)
        proposals = [self.single.apply(connection, item, batch_command_id=request["command_id"])
                     for item in self._items(request)]
        if self.spending is not None:
            self.spending.add(connection, request)
        return {"proposals": proposals}

    def verify_commit(self, decision):
        with self.reader.locked():
            return self._verify_commit_locked(decision)

    def _verify_commit_locked(self, decision):
        if not self.commands.verify_commit(decision):
            return False
        try:
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
            request = _stored_request(row)
            items = self._items(request)
            result = json.loads(row["result_json"])
            if type(result) is not dict or set(result) != {"proposals"} or len(result["proposals"]) != len(items):
                return False
            for item, saved in zip(items, result["proposals"]):
                payload = item["payload"]
                revision = item["expected_revision"] + 1
                current = self.reader.one("SELECT revision,record_json FROM staged_calendar_records WHERE owner_key=? AND record_id=?",
                                          (payload["storage_key"], payload["record_id"]))
                history = self.reader.one("""SELECT operation_id,record_json FROM staged_calendar_history
                    WHERE owner_key=? AND record_id=? AND revision=?""", (payload["storage_key"], payload["record_id"], revision))
                previous = self.reader.one("""SELECT record_json FROM staged_calendar_history
                    WHERE owner_key=? AND record_id=? AND revision=?""", (payload["storage_key"], payload["record_id"], revision - 1)) if revision > 1 else None
                receipt = self.reader.one("""SELECT result_json FROM staged_calendar_receipts
                    WHERE operation_id=? AND owner_key=? AND record_id=?""",
                    (payload["operation_id"], payload["storage_key"], payload["record_id"]))
                if (not current or not history or not receipt or (revision > 1 and not previous)
                        or current["revision"] < revision or (row["state"] != "acknowledged" and current["revision"] != revision)):
                    return False
                expected = _proposal(self.reader, item, prior=previous["record_json"] if previous else None, inspect_store=False)
                encoded = _encode(expected["record"])
                if (history["operation_id"] != payload["operation_id"] or history["record_json"] != encoded
                        or (current["revision"] == revision and current["record_json"] != encoded)
                        or receipt["result_json"] != _encode(expected["receipt"]) or saved != expected):
                    return False
            return True
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def acknowledge(self, command_id, *, verify_host_completion):
        return self.commands.acknowledge(command_id, verify_host_completion=verify_host_completion)
