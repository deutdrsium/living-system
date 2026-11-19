"""Create browsing or sleep activities on the isolated staged life ledger.

The companion host must authorize the exact context and recheck its original
calendar, sources and role before deciding; this is not a live scheduler.
"""

from datetime import timedelta, timezone
import json

from .prepared_commands import PreparedCommands, _hash, _stored_request
from .reading_queue import next_span, queue_key
from .staged_activity_commands import ACTIVITY_FIELDS, _digest_json, _encode, _opaque, _sleep, _time, _work, kernel_foreground_busy
from .storage import StorageReader
from .sleep_physiology import recommended_sleep_minutes, sleep_quality_range


def _activity(request):
    payload = request["payload"]
    if type(payload) is not dict or set(payload) not in ({"owner", "activity"}, {"owner", "activity", "management"},
        {"owner", "activity", "schedule"}, {"owner", "activity", "management", "reading_import"}):
        raise ValueError("activity creation fields are invalid")
    if "management" in payload:
        management = payload["management"]
        if (type(management) is not dict or set(management) != {"operation_id", "parameter_hash"}
                or not _opaque(management["operation_id"]) or type(management["parameter_hash"]) is not str
                or len(management["parameter_hash"]) != 64
                or any(c not in "0123456789abcdef" for c in management["parameter_hash"])):
            raise ValueError("activity creation management identity is invalid")
    if "schedule" in payload:
        schedule = payload["schedule"]
        if (type(schedule) is not dict or set(schedule) != {"kind", "planned_at", "config_hash"}
                or schedule["kind"] not in ("night", "nap")
                or (schedule["kind"] == "nap" and schedule["planned_at"] is not None)
                or (schedule["kind"] == "night" and type(schedule["planned_at"]) is not str)
                or type(schedule["config_hash"]) is not str or len(schedule["config_hash"]) != 64
                or any(c not in "0123456789abcdef" for c in schedule["config_hash"])):
            raise ValueError("activity creation schedule identity is invalid")
        if schedule["planned_at"] is not None:
            _time(schedule["planned_at"])
    owner, activity = payload["owner"], payload["activity"]
    reading_import = payload.get("reading_import")
    if (type(owner) is not dict or set(owner) != {"installation_id", "bot_id", "persona_id"}
            or any(not _opaque(owner[key]) for key in owner)
            or _digest_json(owner) != request["owner_key"] or type(activity) is not dict
            or set(activity) != ACTIVITY_FIELDS or request["expected_revision"] != 0):
        raise ValueError("activity creation owner is invalid")
    scope, work = activity["scope"], activity["work"]
    scope_fields = {"installation_id", "platform", "kind", "native_scope_id", "bot_id", "account_id", "conversation_id", "session_id"}
    if (type(scope) is not dict or set(scope) != scope_fields or scope.get("kind") != "private"
            or any(not _opaque(scope[key]) for key in scope_fields - {"kind"})
            or scope.get("installation_id") != owner["installation_id"] or scope.get("bot_id") != owner["bot_id"]
            or activity["role_id"] != owner["persona_id"] or type(activity["revision"]) is not int or activity["revision"] != 1
            or activity["state"] != ("planned" if reading_import is not None else "active") or activity["reading"] is not None
            or type(work) is not dict or work.get("kind") not in (("reading",) if reading_import is not None else ("social", "sleep"))
            or work.get("context_key") != request["context_key"]):
        raise ValueError("activity creation crosses its role or context")
    for field in ("description", "checkpoint"):
        if type(activity[field]) is not str or len(activity[field]) > 16384:
            raise ValueError("activity creation text is invalid")
    started, updated = _time(activity["started_at"]), _time(activity["updated_at"])
    if started != updated:
        raise ValueError("activity creation timestamp is invalid")
    _work(work, scope)
    kind = work["kind"]
    expected = ("book-part-" + _digest_json([work["source"]["version"]["lineage_id"], work["start_offset"]])
                if kind == "reading" else
                ("sleep-" if kind == "sleep" else "social-activity-") + _digest_json([request["context_key"], work["request_id"]]))
    if activity["activity_id"] != expected:
        raise ValueError("activity creation identifier is invalid")
    if kind == "sleep":
        _sleep(activity["sleep"], work)
        if _time(work["due_at"]) <= started:
            raise ValueError("sleep must have a future wake time")
    elif activity["sleep"] is not None:
        raise ValueError("browsing cannot create a sleep receipt")
    if reading_import is not None:
        if (type(reading_import) is not dict or set(reading_import) != {"artifact", "queue_key", "text_length"}
                or type(reading_import["text_length"]) is not int or reading_import["text_length"] < work["end_offset"]
                or work["request_id"] != activity["activity_id"]
                or reading_import["queue_key"] != queue_key(request["context_key"], work["source"])):
            raise ValueError("reading import metadata is invalid")
        artifact = reading_import["artifact"]
        operation_id = payload["management"]["operation_id"]
        if (type(artifact) is not dict or set(artifact) != {"artifact_id", "activity_id", "kind", "source", "created_at", "asset", "owner", "journal_meaning"}
                or artifact != {"artifact_id": "book-" + _digest_json(operation_id), "activity_id": activity["activity_id"],
                    "kind": "text", "source": work["source"], "created_at": activity["started_at"],
                    "asset": None, "owner": owner, "journal_meaning": "reading"}
                or work["source"]["source_id"] != artifact["artifact_id"]
                or work["source"]["version"]["lineage_id"] != operation_id):
            raise ValueError("reading import source or artifact changed")
    return owner, activity


class StagedActivityCreations:
    OPERATIONS = frozenset({"propose_manual_sleep", "propose_scheduled_sleep", "propose_social", "propose_reading_import", "prepare", "verify_prepared", "resolve",
                            "verify_commit", "acknowledge", "request_for_replay"})

    def __init__(self, storage):
        self.storage = storage
        self.reader = StorageReader(storage)
        self.commands = PreparedCommands(storage)

    def dispatch(self, operation, *args, **kwargs):
        prefix = "activity_creation."
        if type(operation) is not str or not operation.startswith(prefix) or operation[len(prefix):] not in self.OPERATIONS:
            raise ValueError("unknown activity creation operation")
        return getattr(self, operation[len(prefix):])(*args, **kwargs)

    def verify_prepared(self, request):
        return self.commands.verify_prepared(request)

    def propose_manual_sleep(self, *, owner, scope, context_key, operation_id, observed_at,
                             minutes, base_need_minutes, workload_score, sleep_debt_minutes):
        """Build a manual sleep intent from explicitly authorized inputs."""
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError("sleep duration must be 1..1440 minutes")
        if (type(base_need_minutes) is not int or not 1 <= base_need_minutes <= 1440
                or type(workload_score) is not int or not 0 <= workload_score <= 100
                or type(sleep_debt_minutes) is not int or not 0 <= sleep_debt_minutes <= 720):
            raise ValueError("sleep physiology is invalid")
        at = _time(observed_at).astimezone(timezone.utc)
        started = at.isoformat(timespec="microseconds")
        lower, upper = sleep_quality_range(workload_score, sleep_debt_minutes)
        activity = {"activity_id": "sleep-" + _digest_json([context_key, operation_id]),
            "role_id": owner["persona_id"], "scope": scope, "started_at": started, "state": "active", "revision": 1,
            "description": "角色开始一段睡眠", "checkpoint": "", "updated_at": started, "reading": None,
            "work": {"kind": "sleep", "context_key": context_key, "request_id": operation_id,
                     "source": None, "content_hash": None, "start_offset": None, "end_offset": None,
                     "due_at": (at + timedelta(minutes=minutes)).isoformat(timespec="microseconds")},
            "sleep": {"kind": "manual", "base_need_minutes": base_need_minutes, "target_minutes": minutes,
                      "slept_seconds": 0, "interruptions": 0, "early_interruptions": 0, "workload_score": workload_score,
                      "quality_min": lower, "quality_max": upper, "wake_until": None, "oversleep_minutes": 0}}
        probe = {"owner_key": _digest_json(owner), "context_key": context_key, "expected_revision": 0,
                 "payload": {"owner": owner, "activity": activity}}
        _activity(probe)
        return activity

    def propose_scheduled_sleep(self, *, owner, scope, context_key, request_id, observed_at, kind,
                                base_need_minutes, workload_score, sleep_debt_minutes):
        if kind not in ("night", "nap"):
            raise ValueError("scheduled sleep kind is invalid")
        minutes = (25 if kind == "nap" else
                   recommended_sleep_minutes(base_need_minutes, sleep_debt_minutes, workload_score))
        activity = self.propose_manual_sleep(owner=owner, scope=scope, context_key=context_key, operation_id=request_id,
            observed_at=observed_at, minutes=minutes, base_need_minutes=base_need_minutes,
            workload_score=workload_score, sleep_debt_minutes=sleep_debt_minutes)
        activity["sleep"]["kind"] = kind
        if kind == "nap":
            activity["description"] = "角色短暂午休"
        return activity

    def propose_social(self, *, owner, scope, context_key, request_id, observed_at):
        started = _time(observed_at).astimezone(timezone.utc).isoformat(timespec="microseconds")
        activity = {"activity_id": "social-activity-" + _digest_json([context_key, request_id]),
            "role_id": owner["persona_id"], "scope": scope, "started_at": started, "state": "active", "revision": 1,
            "description": "自主浏览外部内容", "checkpoint": "", "updated_at": started, "reading": None, "sleep": None,
            "work": {"kind": "social", "context_key": context_key, "request_id": request_id, "source": None,
                     "content_hash": None, "start_offset": None, "end_offset": None, "due_at": None}}
        _activity({"owner_key": _digest_json(owner), "context_key": context_key, "expected_revision": 0,
                   "payload": {"owner": owner, "activity": activity}})
        return activity

    def propose_reading_import(self, *, owner, scope, context_key, operation_id, source, text, title, observed_at):
        if (type(text) is not str or not text.strip() or type(title) is not str or not title.strip()
                or not _opaque(operation_id) or _time(observed_at) < _time(source["issued_at"])):
            raise ValueError("reading import input is invalid")
        span = next_span(enabled=True, lineage_id=operation_id, text=text, title=title, start=0)
        if span is None:
            raise ValueError("reading import has no readable text")
        activity = {"activity_id": span["activity_id"], "role_id": owner["persona_id"], "scope": scope,
            "started_at": observed_at, "state": "planned", "revision": 1, "description": span["description"],
            "checkpoint": "", "updated_at": observed_at, "reading": None, "sleep": None,
            "work": {"kind": "reading", "context_key": context_key, "request_id": span["activity_id"],
                     "source": source, "content_hash": _digest_json(text), "start_offset": span["start_offset"],
                     "end_offset": span["end_offset"], "due_at": None}}
        artifact = {"artifact_id": "book-" + _digest_json(operation_id), "activity_id": span["activity_id"],
            "kind": "text", "source": source, "created_at": observed_at, "asset": None,
            "owner": owner, "journal_meaning": "reading"}
        return {"activity": activity, "reading_import": {"artifact": artifact,
                "queue_key": queue_key(context_key, source), "text_length": len(text)}}

    def request_for_replay(self, *, command_id, owner_key, context_key):
        row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,))
        if row is None:
            return None
        request = _stored_request(row)
        if (request["kind"] != "activity.create" or request["owner_key"] != owner_key
                or request["context_key"] != context_key or _hash(request) != row["request_hash"]):
            raise ValueError("stored creation request conflicts with its original identity")
        return request

    def prepare(self, request, *, prepared_at):
        if type(request) is not dict or request.get("kind") != "activity.create":
            raise ValueError("expected an activity creation command")
        return self.commands.prepare(**request, prepared_at=prepared_at, validate=self._validate)

    @staticmethod
    def _validate(view, request):
        _, activity = _activity(request)
        key, work = request["owner_key"], activity["work"]
        if view.one("SELECT 1 FROM staged_role_activities WHERE owner_key=? AND activity_id=?", (key, activity["activity_id"])):
            raise ValueError("activity creation identifier already exists")
        if work["kind"] == "reading":
            metadata = request["payload"]["reading_import"]
            if (view.one("SELECT 1 FROM staged_role_artifacts WHERE owner_key=? AND artifact_id=?",
                         (key, metadata["artifact"]["artifact_id"]))
                    or view.one("SELECT 1 FROM staged_life_runtime_metadata WHERE key=?", (metadata["queue_key"],))):
                raise ValueError("reading import identity already exists")
            return True
        if kernel_foreground_busy(view, key):
            raise ValueError("the role already has a kernel foreground activity")
        if view.one("""SELECT 1 FROM staged_role_activities WHERE owner_key=? AND state IN ('active','paused')
            AND json_extract(record_json, '$.work.kind')='sleep' LIMIT 1""", (key,)):
            raise ValueError("the role already has pending sleep")
        if work["kind"] == "sleep":
            if view.one("""SELECT 1 FROM staged_role_activities WHERE owner_key=? AND state='active'
                AND json_extract(record_json, '$.work.kind')='reading' LIMIT 1""", (key,)):
                raise ValueError("the role already has active reading")
        elif view.one("""SELECT 1 FROM staged_role_activities WHERE owner_key=? AND state='active' LIMIT 1""", (key,)):
            raise ValueError("the role already has an active activity")
        return True

    def resolve(self, decision, *, verify_host_decision):
        return self.commands.resolve(decision, verify_host_decision=verify_host_decision, apply=self.apply)

    def apply(self, connection, request):
        self._validate(self.reader, request)
        owner, activity = _activity(request)
        raw = _encode(activity)
        connection.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
            (request["owner_key"], activity["activity_id"], _encode(owner), 1, activity["state"], activity["updated_at"], raw))
        connection.execute("""INSERT INTO staged_role_activity_revisions
            (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
            VALUES (?, ?, 1, ?, 'recorded', NULL, ?)""",
            (request["owner_key"], activity["activity_id"], activity["updated_at"], raw))
        if "reading_import" in request["payload"]:
            metadata = request["payload"]["reading_import"]
            artifact = metadata["artifact"]
            connection.execute("INSERT INTO staged_role_artifacts VALUES (?,?,?,?,?)",
                (request["owner_key"], artifact["artifact_id"], activity["activity_id"], _digest_json(activity["scope"]), _encode(artifact)))
            connection.execute("INSERT INTO staged_life_runtime_metadata VALUES (?, '1')", (metadata["queue_key"],))
        return {"activity_id": activity["activity_id"], "revision": 1, "record_json": raw}

    def verify_commit(self, decision):
        if not self.commands.verify_commit(decision):
            return False
        try:
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
            request = {name: row[name] for name in ("command_id", "owner_key", "context_key", "kind", "expected_revision")}
            request["payload"] = json.loads(row["payload_json"])
            return self._matches_result(request, json.loads(row["result_json"]), require_current=row["state"] != "acknowledged")
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _matches_result(self, request, result, *, require_current):
        owner, activity = _activity(request)
        raw = _encode(activity)
        saved = self.reader.one("""SELECT a.owner_json, a.revision, a.state, a.updated_at, a.record_json,
                h.record_json, h.provenance, h.operation_id, h.updated_at FROM staged_role_activities a
            JOIN staged_role_activity_revisions h ON h.owner_key=a.owner_key AND h.activity_id=a.activity_id AND h.revision=1
            WHERE a.owner_key=? AND a.activity_id=?""", (request["owner_key"], activity["activity_id"]))
        if saved is None:
            return False
        if "reading_import" in request["payload"] and require_current:
            metadata = request["payload"]["reading_import"]
            artifact = self.reader.one("SELECT record_json FROM staged_role_artifacts WHERE owner_key=? AND artifact_id=?",
                                       (request["owner_key"], metadata["artifact"]["artifact_id"]))
            marker = self.reader.one("SELECT value FROM staged_life_runtime_metadata WHERE key=?", (metadata["queue_key"],))
            if artifact is None or artifact[0] != _encode(metadata["artifact"]) or marker is None or marker[0] != "1":
                return False
        head = json.loads(saved[4])
        return (result == {"activity_id": activity["activity_id"], "revision": 1, "record_json": raw}
                and type(head) is dict and head.get("activity_id") == activity["activity_id"]
                and head.get("revision") == saved[1] and head.get("state") == saved[2] and head.get("updated_at") == saved[3]
                and saved[0] == _encode(owner) and saved[1] >= 1
                and (not require_current or saved[1] == 1)
                and (saved[1] != 1 or saved[4] == raw) and saved[5] == raw
                and saved[6] == "recorded" and saved[7] is None and saved[8] == activity["updated_at"])

    def _before_ack(self, _connection, request, result):
        if not self._matches_result(request, result, require_current=True):
            raise ValueError("created activity changed before host acknowledgement")

    def acknowledge(self, command_id, *, verify_host_completion):
        return self.commands.acknowledge(command_id, verify_host_completion=verify_host_completion, after_ack=self._before_ack)
