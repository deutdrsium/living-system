"""Prepare an auditable deletion in the isolated activity ledger.

The host owns management authorization, source withdrawal and operation
audits. This writer can cancel planned related reading in the same local
transaction and stop any corresponding life scheduler worker.
"""

import hashlib
import json

from .activity_import import TABLES
from .prepared_commands import PreparedCommands, _hash, _stored_request, _synchronous
from .staged_activity_commands import _build, _digest_json, _encode, _time
from .storage import StorageReader


DELETED_TABLES = ("role_activities", "role_activity_revisions", "role_artifacts")


def row_hash(table, row):
    """Identify a removed row without retaining its private contents."""
    return _hash([table, list(row)])


def _rows(view, owner, activity):
    rows = {}
    for table in DELETED_TABLES:
        columns = TABLES[table]
        rows[table] = tuple(tuple(row) for row in view.all(
            f"SELECT {', '.join(columns)} FROM staged_{table} WHERE owner_key=? AND activity_id=? "
            f"ORDER BY {', '.join(columns)}", (owner, activity)))
    return rows


def _hashes(rows):
    return {name: [row_hash(name, row) for row in rows[name]] for name in DELETED_TABLES}


def _hex(value):
    return (type(value) is str and len(value) == 64
            and all(character in "0123456789abcdef" for character in value))


def _related(view, owner_key, identifier, target, *, include_target=False, target_raw=None):
    rows = view.all("""SELECT activity_id, revision, state, updated_at, record_json FROM staged_role_activities
        WHERE owner_key=? AND activity_id<>? AND state NOT IN ('completed','cancelled') ORDER BY activity_id""",
                    (owner_key, identifier))
    result = []
    for identifier, revision, state, updated, raw in rows:
        candidate = json.loads(raw)
        work = candidate.get("work")
        if (candidate.get("scope") != target["scope"] or type(work) is not dict
                or work.get("source") != target["work"]["source"]):
            continue
        if (candidate.get("activity_id") != identifier or candidate.get("revision") != revision
                or candidate.get("state") != state or candidate.get("updated_at") != updated):
            raise ValueError("related reading index changed")
        result.append(raw)
    if include_target and target["state"] not in ("completed", "cancelled"):
        if target_raw is None:
            raise ValueError("withdrawal target record is unavailable")
        result.append(target_raw)
        result.sort(key=lambda raw: json.loads(raw)["activity_id"])
    return result


def _cancelled(raw, observed_at):
    payload = {"state": "cancelled", "observed_at": observed_at, "checkpoint": None,
               "reading": None, "work": None, "sleep": None}
    return _encode(_build(json.loads(raw), payload))


class StagedActivityDeletions:
    OPERATIONS = frozenset({"describe", "prepare", "proposal", "verify_prepared", "resolve", "verify_commit",
                            "acknowledge", "request_for_replay"})

    def __init__(self, storage):
        self.storage = storage
        self.reader = StorageReader(storage)
        self.commands = PreparedCommands(storage)

    def dispatch(self, operation, *args, **kwargs):
        prefix = "reading_deletion."
        if type(operation) is not str or not operation.startswith(prefix) or operation[len(prefix):] not in self.OPERATIONS:
            raise ValueError("unknown reading deletion operation")
        return getattr(self, operation[len(prefix):])(*args, **kwargs)

    def describe(self, *, owner_key, activity_id, include_target=False):
        """Return the exact current record and hashes for a host-authorized request."""
        with self.reader.locked():
            rows = _rows(self.reader, owner_key, activity_id)
            if len(rows["role_activities"]) != 1:
                raise ValueError("activity to remove is unavailable")
            raw = rows["role_activities"][0][-1]
            return {"previous_json": raw, "rows_hash": _hash(_hashes(rows)),
                    "related_json": _related(self.reader, owner_key, activity_id, json.loads(raw),
                                             include_target=include_target, target_raw=raw)}

    def prepare(self, request, *, prepared_at):
        if type(request) is not dict or request.get("kind") != "activity.delete":
            raise ValueError("expected an activity deletion command")
        return self.commands.prepare(**request, prepared_at=prepared_at, validate=self._validate)

    @staticmethod
    def _validate(view, request):
        payload = request["payload"]
        fields = {"activity_id", "management_operation_id", "management_parameter_hash", "previous_hash", "rows_hash", "source_hash"}
        if type(payload) is not dict or set(payload) not in (fields, fields | {"observed_at", "related"},
                                                               fields | {"observed_at", "related", "mode"}):
            raise ValueError("activity deletion fields are invalid")
        withdraw = payload.get("mode") == "withdraw"
        if "mode" in payload and not withdraw:
            raise ValueError("reading withdrawal mode is invalid")
        if any(type(payload[name]) is not str or not payload[name].strip() or payload[name] != payload[name].strip()
               or len(payload[name]) > 512 for name in ("activity_id", "management_operation_id")):
            raise ValueError("activity deletion identity is invalid")
        if any(not _hex(payload[name]) for name in ("management_parameter_hash", "previous_hash", "rows_hash", "source_hash")):
            raise ValueError("activity deletion evidence is invalid")
        rows = _rows(view, request["owner_key"], payload["activity_id"])
        if len(rows["role_activities"]) != 1 or _hash(_hashes(rows)) != payload["rows_hash"]:
            raise ValueError("activity deletion records changed")
        owner_key, identifier, owner_json, revision, state, updated, raw = rows["role_activities"][0]
        try:
            owner, activity = json.loads(owner_json), json.loads(raw)
            if (_digest_json(owner) != owner_key or owner_key != request["owner_key"]
                    or identifier != payload["activity_id"] or revision != request["expected_revision"]
                    or activity["activity_id"] != identifier or activity["revision"] != revision
                    or activity["state"] != state or activity["updated_at"] != updated
                    or activity["role_id"] != owner["persona_id"]
                    or activity["scope"]["installation_id"] != owner["installation_id"]
                    or activity["scope"]["bot_id"] != owner["bot_id"]
                    or (not withdraw and state not in ("completed", "cancelled")) or activity["work"]["kind"] != "reading"
                    or (withdraw and activity["work"]["source"] is None)
                    or _digest_json(activity["work"]["source"]) != payload["source_hash"]
                    or hashlib.sha256(raw.encode("utf-8")).hexdigest() != payload["previous_hash"]
                    or not any(row[3] == revision and row[-1] == raw for row in rows["role_activity_revisions"])):
                raise ValueError("activity deletion does not match its original record")
        except (KeyError, TypeError, AttributeError) as error:
            raise ValueError("activity deletion record is invalid") from error
        if "observed_at" in payload:
            _time(payload["observed_at"])
        related = _related(view, owner_key, identifier, activity, include_target=withdraw, target_raw=raw)
        items = payload.get("related", [])
        if type(items) is not list or len(items) != len(related):
            raise ValueError("related reading set changed")
        for raw, item in zip(related, items):
            candidate = json.loads(raw)
            if candidate["state"] not in ("planned", "active", "paused"):
                raise ValueError("related reading cannot be cancelled")
            expected = {"activity_id": candidate["activity_id"], "revision": candidate["revision"],
                        "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        "operation_id": payload["management_operation_id"] + (
                            ":cancel" if withdraw and candidate["activity_id"] == identifier else ":" + _digest_json(candidate["activity_id"])),
                        "result_hash": hashlib.sha256(_cancelled(raw, payload["observed_at"]).encode("utf-8")).hexdigest()}
            if item != expected:
                raise ValueError("related reading evidence changed")
            original = view.one("""SELECT record_json FROM staged_role_activity_revisions
                WHERE owner_key=? AND activity_id=? AND revision=?""",
                (owner_key, item["activity_id"], item["revision"]))
            if original is None or original[0] != raw:
                raise ValueError("related reading history changed")
            if view.one("SELECT 1 FROM staged_role_life_operations WHERE operation_id=?", (item["operation_id"],)):
                raise ValueError("related reading operation already exists")
            if view.one("""SELECT 1 FROM staged_role_activity_revisions WHERE owner_key=? AND activity_id=? AND revision=?""",
                        (owner_key, item["activity_id"], item["revision"] + 1)):
                raise ValueError("related reading revision already exists")
        return True

    def proposal(self, request):
        with self.reader.locked():
            if not self.commands.verify_prepared(request):
                raise ValueError("activity deletion is not prepared")
            self._validate(self.reader, request)
            return self.describe(owner_key=request["owner_key"], activity_id=request["payload"]["activity_id"],
                                 include_target=request["payload"].get("mode") == "withdraw")

    def apply(self, connection, request):
        self._validate(self.reader, request)
        owner, identifier = request["owner_key"], request["payload"]["activity_id"]
        evidence = _hashes(_rows(self.reader, owner, identifier))
        target_raw = _rows(self.reader, owner, identifier)["role_activities"][0][-1]
        target = json.loads(target_raw)
        withdraw = request["payload"].get("mode") == "withdraw"
        related = _related(self.reader, owner, identifier, target, include_target=withdraw, target_raw=target_raw)
        for raw, item in zip(related, request["payload"].get("related", [])):
            result_json = _cancelled(raw, request["payload"]["observed_at"])
            result = json.loads(result_json)
            changed = connection.execute("""UPDATE staged_role_activities SET revision=?, state=?, updated_at=?, record_json=?
                WHERE owner_key=? AND activity_id=? AND revision=? AND record_json=?""",
                (result["revision"], result["state"], result["updated_at"], result_json, owner,
                 item["activity_id"], item["revision"], raw))
            if changed.rowcount != 1:
                raise ValueError("related reading changed during commit")
            connection.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES (?, ?, ?, ?, 'recorded', ?, ?)""",
                (owner, item["activity_id"], result["revision"], result["updated_at"], item["operation_id"], result_json))
            connection.execute("INSERT INTO staged_role_life_operations(operation_id, owner_key, result_json) VALUES (?, ?, ?)",
                               (item["operation_id"], owner, result_json))
        if not withdraw:
            for table in ("role_artifacts", "role_activity_revisions", "role_activities"):
                connection.execute(f"DELETE FROM staged_{table} WHERE owner_key=? AND activity_id=?", (owner, identifier))
        result = {"activity_id": identifier, "row_hashes": evidence}
        if "related" in request["payload"]:
            result["related"] = request["payload"]["related"]
        if withdraw:
            result["mode"] = "withdraw"
        return result

    def resolve(self, decision, *, verify_host_decision):
        return self.commands.resolve(decision, verify_host_decision=verify_host_decision, apply=self.apply)

    def verify_prepared(self, request):
        return self.commands.verify_prepared(request)

    def request_for_replay(self, *, command_id, owner_key, context_key):
        row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,))
        if row is None:
            return None
        request = _stored_request(row)
        if (request["kind"] != "activity.delete" or request["owner_key"] != owner_key
                or request["context_key"] != context_key or _hash(request) != row["request_hash"]):
            raise ValueError("stored deletion request conflicts with its original identity")
        return request

    def cancelled_readings(self, *, command_id, owner_key, context_key, verify_host_completion):
        request = self.request_for_replay(command_id=command_id, owner_key=owner_key, context_key=context_key)
        row = self.reader.one("SELECT state,result_json,request_hash,decision_id FROM life_prepared_commands WHERE command_id=?",
                              (command_id,))
        evidence = {"command_id": command_id, "request_hash": row["request_hash"], "decision_id": row["decision_id"]} if row else None
        if (request is None or row is None or row["state"] not in ("committed", "acknowledged")
                or not command_id.startswith("activity-delete-") or not callable(verify_host_completion)
                or _synchronous(verify_host_completion(evidence)) is not True
                or not self._matches_result(request, json.loads(row["result_json"]), require_absent=False)):
            raise ValueError("reading deletion is not confirmed")
        result = []
        for item in request["payload"].get("related", []):
            previous = self.reader.one("""SELECT record_json FROM staged_role_activity_revisions
                WHERE owner_key=? AND activity_id=? AND revision=?""", (owner_key, item["activity_id"], item["revision"]))
            if previous is None:
                if row["state"] == "acknowledged":
                    continue
                raise ValueError("cancelled reading history is unavailable")
            activity = json.loads(previous[0])
            result.append((activity["work"]["context_key"], item["activity_id"]))
        return tuple(result)

    def _matches_result(self, request, result, *, require_absent):
        payload = request["payload"]
        withdraw = payload.get("mode") == "withdraw"
        expected_fields = ({"activity_id", "row_hashes"} | ({"related"} if "related" in payload else set())
                           | ({"mode"} if withdraw else set()))
        if (type(result) is not dict or set(result) != expected_fields
                or result["activity_id"] != payload["activity_id"]
                or (withdraw and result.get("mode") != "withdraw")
                or result.get("related", []) != payload.get("related", [])
                or type(result["row_hashes"]) is not dict or set(result["row_hashes"]) != set(DELETED_TABLES)
                or any(type(items) is not list or any(not _hex(item) for item in items)
                       or len(items) != len(set(items)) for items in result["row_hashes"].values())
                or len(result["row_hashes"]["role_activities"]) != 1
                or not result["row_hashes"]["role_activity_revisions"]
                or _hash(result["row_hashes"]) != payload["rows_hash"]):
            return False
        if require_absent:
            rows = _rows(self.reader, request["owner_key"], payload["activity_id"])
            if not withdraw and any(rows.values()):
                return False
            if withdraw and payload["activity_id"] not in {item["activity_id"] for item in payload["related"]}:
                if _hash(_hashes(rows)) != payload["rows_hash"]:
                    return False
        for item in result.get("related", []):
            receipt = self.reader.one("SELECT result_json FROM staged_role_life_operations WHERE owner_key=? AND operation_id=?",
                                      (request["owner_key"], item["operation_id"]))
            if receipt is None or hashlib.sha256(receipt[0].encode("utf-8")).hexdigest() != item["result_hash"]:
                return False
            if require_absent:
                head = self.reader.one("""SELECT revision, state, record_json FROM staged_role_activities
                    WHERE owner_key=? AND activity_id=?""", (request["owner_key"], item["activity_id"]))
                history = self.reader.one("""SELECT record_json, operation_id FROM staged_role_activity_revisions
                    WHERE owner_key=? AND activity_id=? AND revision=?""",
                    (request["owner_key"], item["activity_id"], item["revision"] + 1))
                if (head is None or head["revision"] != item["revision"] + 1 or head["state"] != "cancelled"
                        or head["record_json"] != receipt[0] or history is None or history[0] != receipt[0]
                        or history[1] != item["operation_id"]):
                    return False
        return True

    def verify_commit(self, decision):
        with self.reader.locked():
            if not self.commands.verify_commit(decision):
                return False
            try:
                row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
                request = {name: row[name] for name in ("command_id", "owner_key", "context_key", "kind", "expected_revision")}
                request["payload"] = json.loads(row["payload_json"])
                return self._matches_result(request, json.loads(row["result_json"]), require_absent=row["state"] != "acknowledged")
            except (AttributeError, KeyError, TypeError, ValueError):
                return False

    def _before_ack(self, _connection, request, result):
        if not self._matches_result(request, result, require_absent=True):
            raise ValueError("deleted activity changed before host acknowledgement")

    def acknowledge(self, command_id, *, verify_host_completion):
        return self.commands.acknowledge(command_id, verify_host_completion=verify_host_completion, after_ack=self._before_ack)
