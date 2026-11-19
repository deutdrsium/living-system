"""Exercise a versioned activity transition on the isolated staged ledger.

The host must validate sources, authorize the original scope and reserve its
operation before deciding to commit. This staged writer is not registered as
an AstrBot handler and does not perform model, social, or message actions.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from .prepared_commands import PreparedCommands, _hash, _stored_request, _synchronous
from .activity_records import ActivityTransitionPending
from .reading_queue import next_span, queue_key
from .sleep_physiology import actual_sleep_seconds, oversleep_extension, sleep_profile, sleep_quality_range
from .storage import StorageReader


TRANSITIONS = {
    "planned": {"planned", "active", "cancelled"},
    "active": {"active", "paused", "completed", "cancelled"},
    "paused": {"paused", "active", "completed", "cancelled"},
    "completed": set(), "cancelled": set(),
}
ACTIVITY_FIELDS = {"activity_id", "role_id", "scope", "started_at", "state", "revision", "description",
                   "checkpoint", "updated_at", "reading", "work", "sleep"}


def kernel_foreground_busy(view, owner_key):
    # A flags-off database has no A1 tables. Previously persisted claims
    # still count until their original result is settled.
    return (view.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_kernel_instances'", ()) is not None
            and view.one("""SELECT 1 FROM life_kernel_instances WHERE owner_key=?
                 AND status IN ('preparing','running','unknown') LIMIT 1""", (owner_key,)) is not None)


def staged_foreground_busy(view, owner_key):
    if (view.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='staged_role_activities'", ()) is not None
            and view.one("""SELECT 1 FROM staged_role_activities WHERE owner_key=? AND (
        state='active' OR (state='paused' AND json_extract(record_json,'$.work.kind')='sleep')) LIMIT 1""",
        (owner_key,)) is not None):
        return True
    return (view.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='life_prepared_commands'", ()) is not None
            and view.one("""SELECT 1 FROM life_prepared_commands WHERE owner_key=?
                AND state IN ('prepared','committed') LIMIT 1""", (owner_key,)) is not None)


def _time(value):
    if type(value) is not str:
        raise ValueError("activity timestamp is invalid")
    moment = datetime.fromisoformat(value)
    if moment.utcoffset() is None:
        raise ValueError("activity timestamp must be timezone-aware")
    return moment


def _digest_json(value):
    raw = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _build(old, payload):
    if type(old) is not dict or set(old) != ACTIVITY_FIELDS:
        raise ValueError("original activity record shape changed")
    state = payload["state"]
    if type(state) is not str or state not in TRANSITIONS.get(old["state"], set()):
        raise ValueError("activity transition is not valid for its current state")
    observed = _time(payload["observed_at"]).astimezone(timezone.utc)
    if observed < _time(old["updated_at"]):
        raise ValueError("activity update cannot move backwards in time")
    return {**old, "state": state, "revision": old["revision"] + 1,
            "updated_at": observed.isoformat(timespec="microseconds"),
            "checkpoint": old["checkpoint"] if payload["checkpoint"] is None else payload["checkpoint"],
            "reading": payload["reading"], "work": old["work"] if payload["work"] is None else payload["work"],
            "sleep": old["sleep"] if payload["sleep"] is None else payload["sleep"]}


def _encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _opaque(value, maximum=512):
    return type(value) is str and bool(value.strip()) and value == value.strip() and len(value) <= maximum


def _source(source, scope):
    if (type(source) is not dict or set(source) != {"source_id", "owner", "scope", "version", "issued_at", "expires_at"}
            or not _opaque(source["source_id"]) or not _opaque(source["owner"]) or source["scope"] != scope
            or type(source["version"]) is not dict or set(source["version"]) != {"lineage_id", "revision"}
            or not _opaque(source["version"]["lineage_id"]) or type(source["version"]["revision"]) is not int
            or source["version"]["revision"] < 1):
        raise ValueError("source reference is invalid")
    issued = _time(source["issued_at"])
    if source["expires_at"] is not None and _time(source["expires_at"]) <= issued:
        raise ValueError("source expiry is invalid")
    return issued


def _work(work, scope):
    fields = {"kind", "context_key", "request_id", "source", "content_hash", "start_offset", "end_offset", "due_at"}
    if (type(work) is not dict or set(work) != fields or work["kind"] not in ("reading", "sleep", "social")
            or any(type(work[name]) is not str or not work[name].strip() or len(work[name]) > 256
                   for name in ("context_key", "request_id"))):
        raise ValueError("work record is invalid")
    if work["kind"] == "reading":
        _source(work["source"], scope)
        if (not work["content_hash"] or work["due_at"] is not None
                or type(work["start_offset"]) is not int or type(work["end_offset"]) is not int
                or not 0 <= work["start_offset"] < work["end_offset"]):
            raise ValueError("reading work span is invalid")
    elif work["kind"] == "social":
        if any(work[name] is not None for name in ("source", "content_hash", "start_offset", "end_offset", "due_at")):
            raise ValueError("social work has unexpected source fields")
    else:
        if any(work[name] is not None for name in ("source", "content_hash", "start_offset", "end_offset")):
            raise ValueError("sleep work has unexpected source fields")
        _time(work["due_at"])


def _reading(reading, scope, state, updated_at):
    fields = {"source", "content_hash", "text_length", "start_offset", "end_offset", "completed_at", "reflection"}
    if type(reading) is not dict or set(reading) != fields:
        raise ValueError("reading receipt is invalid")
    issued = _source(reading["source"], scope)
    completed = _time(reading["completed_at"])
    if (state != "completed" or completed != _time(updated_at) or completed < issued
            or not _opaque(reading["content_hash"]) or type(reading["reflection"]) is not str
            or len(reading["reflection"]) > 8192
            or any(type(reading[name]) is not int for name in ("start_offset", "end_offset", "text_length"))
            or not 0 <= reading["start_offset"] < reading["end_offset"] <= reading["text_length"]):
        raise ValueError("reading receipt does not match the completed activity")


def _sleep(sleep, work):
    fields = {"kind", "base_need_minutes", "target_minutes", "slept_seconds", "interruptions", "early_interruptions",
              "workload_score", "quality_min", "quality_max", "wake_until", "oversleep_minutes"}
    if type(sleep) is not dict or set(sleep) != fields or work is None or work.get("kind") != "sleep":
        raise ValueError("sleep receipt is invalid")
    if sleep["kind"] not in ("night", "nap", "manual"):
        raise ValueError("sleep kind is invalid")
    for name in ("base_need_minutes", "target_minutes"):
        if type(sleep[name]) is not int or not 1 <= sleep[name] <= 1440:
            raise ValueError("sleep duration is invalid")
    for name, maximum in (("slept_seconds", 172800), ("interruptions", 1000), ("early_interruptions", 1000),
                          ("workload_score", 100), ("quality_min", 100), ("quality_max", 100), ("oversleep_minutes", 60)):
        if type(sleep[name]) is not int or not 0 <= sleep[name] <= maximum:
            raise ValueError("sleep receipt value is invalid")
    if sleep["quality_min"] > sleep["quality_max"]:
        raise ValueError("sleep quality range is invalid")
    if sleep["wake_until"] is not None:
        _time(sleep["wake_until"])


def _reading_control(view, request, old, new):
    control = request["payload"].get("reading_control")
    if control is None:
        return
    payload, work = request["payload"], old["work"]
    if (type(control) is not dict or work is None or work.get("kind") != "reading"
            or work["context_key"] != request["context_key"] or type(control.get("lazy")) is not bool):
        raise ValueError("reading control is invalid")
    lazy = view.one("SELECT 1 FROM staged_life_runtime_metadata WHERE key=?",
                    (queue_key(request["context_key"], work["source"]),)) is not None
    if lazy != control["lazy"]:
        raise ValueError("reading queue ownership changed")
    if control.get("mode") == "start":
        if (set(control) != {"mode", "lazy", "prior_pace", "pace", "retry_task_id", "retry_marker"}
                or old["state"] not in ("planned", "paused")
                or new["state"] != "active" or new["reading"] is not None
                or new["work"]["source"] != work["source"] or new["work"]["content_hash"] != work["content_hash"]
                or new["work"]["start_offset"] < work["start_offset"]):
            raise ValueError("reading start changed the original work")
        retry_id = ("reading-task-" + _digest_json([request["context_key"], old["activity_id"], work["request_id"]])
                    if old["state"] == "paused" else None)
        retry = (view.one("SELECT value FROM staged_life_runtime_metadata WHERE key=?",
                          ("role.life.retry_interrupted:" + retry_id,)) if retry_id else None)
        if (control["retry_task_id"] != retry_id or control["retry_marker"] != (json.loads(retry[0]) if retry else None)
                or old["state"] == "paused" and (new["work"]["start_offset"] != work["start_offset"]
                                                    or new["work"]["end_offset"] != work["end_offset"])):
            raise ValueError("reading retry authority or original span changed")
        pace = view.one("SELECT value FROM staged_life_runtime_metadata WHERE key=?",
                        ("role.life.reading_pace:" + request["context_key"],))
        if ((json.loads(pace[0]) if pace else None) != control["prior_pace"]
                or control["pace"] != {"started_at": payload["observed_at"],
                    "characters": new["work"]["end_offset"] - new["work"]["start_offset"]}):
            raise ValueError("reading pace changed")
        if view.one("""SELECT 1 FROM staged_role_activities WHERE owner_key=? AND activity_id!=? AND
            ((state IN ('active','paused') AND json_extract(record_json,'$.work.kind')='sleep') OR
             (state='active' AND json_extract(record_json,'$.work.kind')='reading')) LIMIT 1""",
            (request["owner_key"], old["activity_id"])):
            raise ValueError("the role is already sleeping or reading")
    elif control.get("mode") in ("complete", "cancel"):
        cancelled = control["mode"] == "cancel"
        if cancelled:
            if (set(control) != {"mode", "lazy", "next_activity", "text_length"}
                    or old["state"] not in ("planned", "active", "paused") or new["state"] != "cancelled"
                    or new["reading"] is not None or (control["text_length"] is not None
                    and (type(control["text_length"]) is not int or control["text_length"] < 0))):
                raise ValueError("reading cancellation changed the original work")
        elif (set(control) != {"mode", "lazy", "next_activity"} or old["state"] != "active"
              or new["state"] != "completed" or new["reading"] is None):
            raise ValueError("reading completion changed the original work")
        following = control["next_activity"]
        if following is not None:
            if (not lazy or type(following) is not dict or set(following) != ACTIVITY_FIELDS
                    or following["role_id"] != old["role_id"] or following["scope"] != old["scope"]
                    or following["state"] != "planned" or following["revision"] != 1
                    or following["started_at"] != payload["observed_at"] or following["updated_at"] != payload["observed_at"]
                    or following["reading"] is not None or following["sleep"] is not None or following["checkpoint"] != ""):
                raise ValueError("next reading activity is invalid")
            following_work = following["work"]
            _work(following_work, old["scope"])
            if (following_work["kind"] != "reading" or following_work["source"] != work["source"]
                    or following_work["content_hash"] != work["content_hash"]
                    or following_work["context_key"] != request["context_key"]
                    or following_work["start_offset"] < (new["work"]["end_offset"] if cancelled else work["end_offset"])
                    or following_work["end_offset"] > (control["text_length"] if cancelled else new["reading"]["text_length"])
                    or following["activity_id"] != "book-part-" + _digest_json([work["source"]["version"]["lineage_id"], following_work["start_offset"]])
                    or following_work["request_id"] != following["activity_id"]):
                raise ValueError("next reading activity changed its original source")
            existing = view.one("SELECT record_json FROM staged_role_activities WHERE owner_key=? AND activity_id=?",
                                (request["owner_key"], following["activity_id"]))
            if existing is not None and existing[0] != _encode(following):
                raise ValueError("next reading activity already exists")
    else:
        raise ValueError("reading control mode is invalid")


class StagedActivityTransitions:
    """Prepare, apply and acknowledge a single staged activity revision."""

    OPERATIONS = frozenset({"sleep_profile", "actual_sleep_seconds", "propose_manual_wake", "propose_manual_cancel_sleep", "propose_due_sleep", "propose_resume_sleep", "propose_interrupt_sleep", "propose_interrupted_work", "reading_metadata", "reading_retry", "reading_choice", "propose_reading_start", "propose_reading_completion", "propose_reading_cancel", "prepare", "proposal", "verify_prepared", "resolve",
                            "verify_commit", "acknowledge", "request_for_replay"})

    def __init__(self, storage):
        self.storage = storage
        self.reader = StorageReader(storage)
        self.commands = PreparedCommands(storage)

    def sleep_profile(self, *, episodes, workload_score, observed_at):
        if type(episodes) not in (list, tuple) or len(episodes) > 200:
            raise ValueError("invalid sleep episodes")
        return sleep_profile(episodes, workload_score=workload_score, now=observed_at)

    def actual_sleep_seconds(self, *, activity, observed_at):
        return actual_sleep_seconds(activity, observed_at)

    def dispatch(self, operation, *args, **kwargs):
        prefix = "activity_transition."
        if type(operation) is not str or not operation.startswith(prefix) or operation[len(prefix):] not in self.OPERATIONS:
            raise ValueError("unknown activity transition operation")
        return getattr(self, operation[len(prefix):])(*args, **kwargs)

    def verify_prepared(self, request):
        return self.commands.verify_prepared(request)

    def resolve(self, decision, *, verify_host_decision):
        return self.commands.resolve(decision, verify_host_decision=verify_host_decision, apply=self.apply)

    def request_for_replay(self, *, command_id, owner_key, context_key):
        row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (command_id,))
        if row is None:
            return None
        request = _stored_request(row)
        if (request["kind"] != "activity.transition" or request["owner_key"] != owner_key
                or request["context_key"] != context_key or _hash(request) != row["request_hash"]):
            raise ValueError("stored transition request conflicts with its original identity")
        return request

    def cancelled_reading(self, *, command_id, owner_key, context_key, verify_host_completion):
        request = self.request_for_replay(command_id=command_id, owner_key=owner_key, context_key=context_key)
        row = self.reader.one("SELECT state,result_json,request_hash,decision_id FROM life_prepared_commands WHERE command_id=?", (command_id,))
        evidence = {"command_id": command_id, "request_hash": row["request_hash"], "decision_id": row["decision_id"]} if row else None
        if (request is None or row is None or row["state"] not in ("committed", "acknowledged")
                or not command_id.startswith("activity-cancel-reading-")
                or request["payload"].get("state") != "cancelled"
                or not callable(verify_host_completion) or _synchronous(verify_host_completion(evidence)) is not True
                or not self._matches_result(request, json.loads(row["result_json"]), require_current=False)):
            raise ValueError("reading cancellation is not confirmed")
        result = json.loads(row["result_json"])
        activity = json.loads(result["record_json"])
        work = activity.get("work")
        if (type(work) is not dict or work.get("kind") != "reading" or activity.get("state") != "cancelled"
                or type(work.get("context_key")) is not str or not work["context_key"]
                or (work["context_key"] == context_key and request["payload"].get("reading_control", {}).get("mode") != "cancel")):
            raise ValueError("cancelled reading identity changed")
        return work["context_key"], request["payload"]["activity_id"]

    def propose_manual_wake(self, *, activity, observed_at, sleep_debt_minutes):
        """Calculate only the observed sleep receipt, with no host database access."""
        return self._wake_receipt(activity, observed_at, sleep_debt_minutes, allow_start=False)

    def propose_manual_cancel_sleep(self, *, activity, observed_at, sleep_debt_minutes):
        return self._wake_receipt(activity, observed_at, sleep_debt_minutes, allow_start=True)

    def propose_interrupted_work(self, *, activity, observed_at):
        """Pause an unfinished reading or browsing activity after its worker stopped."""
        if (type(activity) is not dict or set(activity) != ACTIVITY_FIELDS or activity.get("state") != "active"
                or type(activity.get("work")) is not dict or activity["work"].get("kind") not in ("reading", "social")
                or _time(observed_at) < _time(activity["updated_at"])):
            raise ValueError("interrupted activity cannot be paused")
        kind = activity["work"]["kind"]
        checkpoint = ("interrupted; inspect the original model result before retrying" if kind == "reading"
                      else "浏览中断；不会重复结果不明的互动")
        return {"state": "paused", "checkpoint": checkpoint, "work": None, "sleep": None,
                "operation_id": "recover-" + kind + "-" + _digest_json([activity["activity_id"], activity["revision"]]),
                "event_consumers": []}

    def reading_metadata(self, *, context_key, source=None):
        with self.reader.locked():
            self._reading_fence(context_key)
            lazy = (self.reader.one("SELECT 1 FROM staged_life_runtime_metadata WHERE key=?", (queue_key(context_key, source),))
                    if source is not None else None)
            pace = self.reader.one("SELECT value FROM staged_life_runtime_metadata WHERE key=?",
                                   ("role.life.reading_pace:" + context_key,))
            return {"lazy": lazy is not None, "pace": json.loads(pace[0]) if pace else None}

    def reading_choice(self, *, context_key, slot):
        key = "role.reading.choice:" + _digest_json([context_key, slot])
        with self.reader.locked():
            row = self.reader.one("SELECT value FROM staged_life_runtime_metadata WHERE key=?", (key,))
            return json.loads(row[0]) if row else None

    def _reading_fence(self, context_key):
        if self.reader.one("""SELECT 1 FROM life_prepared_commands WHERE kind='activity.transition' AND state='committed'
            AND context_key=? AND json_extract(payload_json,'$.reading_control') IS NOT NULL LIMIT 1""", (context_key,)):
            raise ActivityTransitionPending("reading change awaits host confirmation")

    def reading_retry(self, *, context_key, task_id):
        with self.reader.locked():
            self._reading_fence(context_key)
            row = self.reader.one("SELECT value FROM staged_life_runtime_metadata WHERE key=?",
                                  ("role.life.retry_interrupted:" + task_id,))
            return json.loads(row[0]) if row else None

    def propose_reading_start(self, *, activity, text, request_id, chunk_size, observed_at):
        if (type(activity) is not dict or activity.get("state") not in ("planned", "paused") or type(activity.get("work")) is not dict
                or activity["work"].get("kind") != "reading" or not _opaque(request_id, 256)
                or type(text) is not str or _digest_json(text) != activity["work"]["content_hash"]
                or type(chunk_size) is not int or not 200 <= chunk_size <= 6000
                or _time(observed_at) < _time(activity["updated_at"])):
            raise ValueError("reading cannot start with these inputs")
        work = dict(activity["work"])
        metadata = self.reading_metadata(context_key=work["context_key"], source=work["source"])
        retry_id = ("reading-task-" + _digest_json([work["context_key"], activity["activity_id"], work["request_id"]])
                    if activity["state"] == "paused" else None)
        retry_marker = self.reading_retry(context_key=work["context_key"], task_id=retry_id) if retry_id else None
        if metadata["lazy"] and activity["state"] == "planned":
            start = work["start_offset"]
            while start < len(text) and not text[start:start + chunk_size].strip():
                start += chunk_size
            if start >= len(text):
                raise ValueError("reading original has no remaining text")
            work.update(start_offset=start, end_offset=min(len(text), start + chunk_size))
        work["request_id"] = request_id
        return {"state": "active", "checkpoint": "正在阅读实际提供的片段", "work": work, "sleep": None,
                "operation_id": "read-start-" + _digest_json([work["context_key"], request_id]), "event_consumers": [],
                "reading_control": {"mode": "start", "lazy": metadata["lazy"], "prior_pace": metadata["pace"],
                    "pace": {"started_at": observed_at, "characters": work["end_offset"] - work["start_offset"]},
                    "retry_task_id": retry_id, "retry_marker": retry_marker}}

    def propose_reading_completion(self, *, activity, text, reflection, task_id, observed_at):
        if (type(activity) is not dict or activity.get("state") != "active" or type(activity.get("work")) is not dict
                or activity["work"].get("kind") != "reading" or not _opaque(task_id)
                or type(text) is not str or _digest_json(text) != activity["work"]["content_hash"]
                or type(reflection) is not str or not reflection.strip() or len(reflection) > 8192
                or _time(observed_at) < _time(activity["updated_at"])):
            raise ValueError("reading completion inputs are invalid")
        work = activity["work"]
        if work["end_offset"] > len(text) or not text[work["start_offset"]:work["end_offset"]].strip():
            raise ValueError("reading completion has no original span")
        metadata = self.reading_metadata(context_key=work["context_key"], source=work["source"])
        title = activity["description"].rsplit(" · ", 1)[0]
        span = next_span(enabled=metadata["lazy"], lineage_id=work["source"]["version"]["lineage_id"],
                         text=text, title=title, start=work["end_offset"])
        following = None
        if span:
            following = {"activity_id": span["activity_id"], "role_id": activity["role_id"], "scope": activity["scope"],
                "started_at": observed_at, "state": "planned", "revision": 1, "description": span["description"],
                "checkpoint": "", "updated_at": observed_at, "reading": None, "sleep": None,
                "work": {**work, "request_id": span["activity_id"], "start_offset": span["start_offset"],
                         "end_offset": span["end_offset"]}}
        receipt = {"source": work["source"], "content_hash": work["content_hash"], "text_length": len(text),
                   "start_offset": work["start_offset"], "end_offset": work["end_offset"],
                   "completed_at": observed_at, "reflection": reflection}
        return {"state": "completed", "checkpoint": None, "work": None, "sleep": None, "reading": receipt,
                "operation_id": "reading-complete-" + _digest_json(task_id), "event_consumers": ["journal", "interest"],
                "reading_control": {"mode": "complete", "lazy": metadata["lazy"], "next_activity": following}}

    def propose_reading_cancel(self, *, activity, text, chunk_size, operation_id, observed_at):
        if (type(activity) is not dict or activity.get("state") not in ("planned", "active", "paused")
                or type(activity.get("work")) is not dict or activity["work"].get("kind") != "reading"
                or not _opaque(operation_id) or type(chunk_size) is not int or chunk_size < 1
                or (text is not None and (type(text) is not str or _digest_json(text) != activity["work"]["content_hash"]))
                or _time(observed_at) < _time(activity["updated_at"])):
            raise ValueError("reading cancellation inputs are invalid")
        original = activity["work"]
        metadata = self.reading_metadata(context_key=original["context_key"], source=original["source"])
        work = dict(original)
        if text is not None and metadata["lazy"] and activity["state"] == "planned":
            work["end_offset"] = min(len(text), work["start_offset"] + chunk_size)
        span = (next_span(enabled=metadata["lazy"], lineage_id=work["source"]["version"]["lineage_id"],
                          text=text, title=activity["description"].rsplit(" · ", 1)[0], start=work["end_offset"])
                if text is not None else None)
        following = None
        if span:
            following = {"activity_id": span["activity_id"], "role_id": activity["role_id"], "scope": activity["scope"],
                "started_at": observed_at, "state": "planned", "revision": 1, "description": span["description"],
                "checkpoint": "", "updated_at": observed_at, "reading": None, "sleep": None,
                "work": {**work, "request_id": span["activity_id"], "start_offset": span["start_offset"],
                         "end_offset": span["end_offset"]}}
        return {"state": "cancelled", "checkpoint": None, "work": work if work != original else None,
                "sleep": None, "reading": None, "operation_id": operation_id, "event_consumers": [],
                "reading_control": {"mode": "cancel", "lazy": metadata["lazy"], "next_activity": following,
                                    "text_length": len(text) if text is not None else None}}

    def _wake_receipt(self, activity, observed_at, sleep_debt_minutes, *, allow_start):
        if (type(activity) is not dict or set(activity) != ACTIVITY_FIELDS
                or activity.get("state") not in ("active", "paused") or type(activity.get("work")) is not dict
                or activity["work"].get("kind") != "sleep"
                or type(sleep_debt_minutes) is not int or not 0 <= sleep_debt_minutes <= 720):
            raise ValueError("sleep cannot be woken")
        at, started = _time(observed_at), _time(activity["started_at"])
        if (at < started or (not allow_start and at == started) or at < _time(activity["updated_at"])):
            raise ValueError("sleep has not actually started")
        receipt = activity["sleep"]
        if receipt is None:
            target = max(1, min(1440, round((_time(activity["work"]["due_at"]) - started).total_seconds() / 60)))
            low, high = sleep_quality_range(0, 0)
            receipt = {"kind": "night", "base_need_minutes": target, "target_minutes": target, "slept_seconds": 0,
                       "interruptions": 0, "early_interruptions": 0, "workload_score": 0,
                       "quality_min": low, "quality_max": high, "wake_until": None, "oversleep_minutes": 0}
        else:
            _sleep(receipt, activity["work"])
            receipt = dict(receipt)
        elapsed = max(0, round((at - _time(activity["updated_at"])).total_seconds())) if activity["state"] == "active" else 0
        low, high = sleep_quality_range(receipt["workload_score"], sleep_debt_minutes,
                                         receipt["interruptions"], receipt["early_interruptions"])
        return {**receipt, "slept_seconds": min(172800, receipt["slept_seconds"] + elapsed),
                "wake_until": None, "quality_min": low, "quality_max": high}

    def propose_due_sleep(self, *, activity, observed_at, sleep_debt_minutes):
        """Calculate a due wake or one bounded oversleep from the saved episode."""
        if (type(activity) is not dict or activity.get("state") != "active"
                or type(activity.get("work")) is not dict or activity["work"].get("kind") != "sleep"):
            raise ValueError("no active sleep is due")
        at = _time(observed_at)
        if _time(activity["work"]["due_at"]) > at:
            raise ValueError("sleep is not due")
        finished = self.propose_manual_wake(activity=activity, observed_at=observed_at,
                                            sleep_debt_minutes=sleep_debt_minutes)
        extension = oversleep_extension(activity, sleep_debt_minutes=sleep_debt_minutes)
        identity = _digest_json([activity["activity_id"], activity["revision"]])
        if extension:
            original = activity["sleep"]
            sleep = {**original, "slept_seconds": finished["slept_seconds"], "oversleep_minutes": extension}
            due = (_time(activity["work"]["due_at"]) + timedelta(minutes=extension)).astimezone(timezone.utc)
            return {"state": "active", "checkpoint": "睡眠延长片刻，稍后自然醒来",
                    "work": {**activity["work"], "due_at": due.isoformat(timespec="microseconds")},
                    "sleep": sleep, "operation_id": "sleep-oversleep-" + identity, "event_consumers": []}
        consumers = ["journal", "dream"]
        if finished["kind"] == "night":
            consumers.append("wardrobe")
        return {"state": "completed", "checkpoint": "睡眠结束；记录实际恢复与睡眠质量范围",
                "work": None, "sleep": finished, "operation_id": "body-wake-" + identity,
                "event_consumers": consumers}

    def propose_interrupt_sleep(self, *, activity, observed_at, sleep_debt_minutes, grace_minutes):
        if (type(activity) is not dict or activity.get("state") != "active"
                or type(grace_minutes) is not int or not 0 <= grace_minutes <= 1440):
            raise ValueError("sleep cannot be interrupted")
        at = _time(observed_at).astimezone(timezone.utc)
        if at < _time(activity["updated_at"]):
            raise ValueError("sleep interruption time moved backwards")
        sleep = self._wake_receipt(activity, observed_at, sleep_debt_minutes, allow_start=True)
        early = int((at - _time(activity["updated_at"])).total_seconds() < 20 * 60)
        sleep["interruptions"] += 1
        sleep["early_interruptions"] += early
        sleep["quality_min"], sleep["quality_max"] = sleep_quality_range(
            sleep["workload_score"], sleep_debt_minutes, sleep["interruptions"], sleep["early_interruptions"])
        sleep["wake_until"] = (at + timedelta(minutes=grace_minutes)).isoformat(timespec="microseconds")
        return {"state": "paused", "checkpoint": "用户消息打断睡眠；进入清醒宽限后再评估是否续睡", "work": None,
                "sleep": sleep, "operation_id": "sleep-interrupt-" + _digest_json([activity["activity_id"], activity["revision"]]),
                "event_consumers": []}

    def propose_resume_sleep(self, *, activity, observed_at):
        """Resume an interrupted sleep after its existing wake grace ends."""
        if (type(activity) is not dict or activity.get("state") != "paused"
                or type(activity.get("work")) is not dict or activity["work"].get("kind") != "sleep"
                or type(activity.get("sleep")) is not dict):
            raise ValueError("no interrupted sleep can resume")
        receipt = activity["sleep"]
        _sleep(receipt, activity["work"])
        at = _time(observed_at).astimezone(timezone.utc)
        if receipt["wake_until"] is None or _time(receipt["wake_until"]) > at:
            raise ValueError("sleep wake grace has not ended")
        if at < _time(activity["updated_at"]):
            raise ValueError("sleep observation moved backwards")
        remaining = receipt["target_minutes"] * 60 - receipt["slept_seconds"]
        sleep = {**receipt, "wake_until": None}
        identity = _digest_json([activity["activity_id"], activity["revision"]])
        if remaining <= 0:
            consumers = ["journal", "dream"]
            if sleep["kind"] == "night":
                consumers.append("wardrobe")
            return {"state": "completed", "checkpoint": "中断后已达到本段睡眠目标", "work": None, "sleep": sleep,
                    "operation_id": "sleep-finish-short-" + identity, "event_consumers": consumers}
        return {"state": "active", "checkpoint": "清醒间隔结束，继续补足本段睡眠",
                "work": {**activity["work"], "due_at": (at + timedelta(seconds=remaining)).isoformat(timespec="microseconds")},
                "sleep": sleep, "operation_id": "sleep-resume-" + identity, "event_consumers": []}

    def prepare(self, request, *, prepared_at):
        if type(request) is not dict or request.get("kind") != "activity.transition":
            raise ValueError("expected an activity transition command")
        return self.commands.prepare(**request, prepared_at=prepared_at, validate=self._validate)

    def proposal(self, request):
        """Return the exact prior and proposed versions for host authorization."""
        with self.reader.locked():
            if not self.commands.verify_prepared(request):
                raise ValueError("activity transition is not prepared")
            self._validate(self.reader, request)
            row = self.reader.one("SELECT record_json FROM staged_role_activities WHERE owner_key=? AND activity_id=?",
                                  (request["owner_key"], request["payload"]["activity_id"]))
            return {"previous_json": row[0], "result_json": _encode(_build(json.loads(row[0]), request["payload"]))}

    @staticmethod
    def _validate(view, request):
        payload = request["payload"]
        fields = {"activity_id", "operation_id", "previous_hash", "state",
                  "observed_at", "checkpoint", "reading", "work", "sleep", "event_consumers"}
        if type(payload) is not dict or set(payload) not in (fields, fields | {"management"}, fields | {"reading_control"},
                                                               fields | {"management", "reading_control"}):
            raise ValueError("activity transition fields are invalid")
        if ("management" in payload and "reading_control" in payload
                and (type(payload["reading_control"]) is not dict or payload["reading_control"].get("mode") != "cancel")):
            raise ValueError("management reading transition is invalid")
        if "management" in payload:
            management = payload["management"]
            if (type(management) is not dict or set(management) != {"operation_id", "parameter_hash"}
                    or not _opaque(management["operation_id"]) or type(management["parameter_hash"]) is not str
                    or len(management["parameter_hash"]) != 64
                    or any(c not in "0123456789abcdef" for c in management["parameter_hash"])):
                raise ValueError("activity transition management identity is invalid")
        activity_id, operation_id = payload["activity_id"], payload["operation_id"]
        if any(type(value) is not str or not value.strip() or value != value.strip() or len(value) > 512
               for value in (activity_id, operation_id)):
            raise ValueError("activity transition identifiers are invalid")
        if type(payload["previous_hash"]) is not str or len(payload["previous_hash"]) != 64:
            raise ValueError("activity transition evidence is invalid")
        consumers = payload["event_consumers"]
        if (type(consumers) is not list
                or any(type(item) is not str or not item.strip() or item != item.strip() or len(item) > 256 for item in consumers)
                or len(consumers) != len(set(consumers))):
            raise ValueError("activity event consumers are invalid")
        row = view.one("""SELECT owner_json, revision, state, updated_at, record_json FROM staged_role_activities
            WHERE owner_key=? AND activity_id=?""", (request["owner_key"], activity_id))
        if row is None:
            raise ValueError("original activity is unavailable")
        old_raw = row["record_json"]
        if hashlib.sha256(old_raw.encode("utf-8")).hexdigest() != payload["previous_hash"]:
            raise ValueError("original activity content changed")
        try:
            owner, old = json.loads(row["owner_json"]), json.loads(old_raw)
            if type(owner) is not dict or type(old) is not dict:
                raise ValueError("activity record shape changed")
            if (_digest_json(owner) != request["owner_key"] or old["activity_id"] != activity_id
                    or type(old["revision"]) is not int or old["revision"] != row["revision"] or old["state"] != row["state"]
                    or old["updated_at"] != row["updated_at"] or row["revision"] != request["expected_revision"]):
                raise ValueError("original activity index or revision changed")
            new = _build(old, payload)
            if (new["scope"]["installation_id"] != owner["installation_id"] or new["scope"]["bot_id"] != owner["bot_id"]
                    or new["role_id"] != owner["persona_id"] or type(new["revision"]) is not int
                    or new["revision"] != row["revision"] + 1):
                raise ValueError("activity transition is not valid for its original owner")
            if (_time(new["updated_at"]) < _time(old["updated_at"]) or _time(new["updated_at"]) < _time(new["started_at"])
                    or type(new["checkpoint"]) is not str or len(new["checkpoint"]) > 16384):
                raise ValueError("activity transition timestamp or checkpoint is invalid")
            if new["reading"] is not None:
                _reading(new["reading"], new["scope"], new["state"], new["updated_at"])
            if new["work"] is not None:
                _work(new["work"], new["scope"])
            if new["sleep"] is not None:
                _sleep(new["sleep"], new["work"])
            if consumers and new["state"] != "completed":
                raise ValueError("only completed activities can publish completion events")
            if new["state"] == "active" and old["state"] != "active" and kernel_foreground_busy(view, request["owner_key"]):
                raise ValueError("the role already has a kernel foreground activity")
            _reading_control(view, request, old, new)
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise ValueError("activity transition record is invalid") from error
        if view.one("SELECT 1 FROM staged_role_life_operations WHERE operation_id=?", (operation_id,)) is not None:
            raise ValueError("activity operation already exists")
        if view.one("""SELECT 1 FROM staged_role_activity_revisions WHERE owner_key=? AND activity_id=? AND revision=?""",
                    (request["owner_key"], activity_id, new["revision"])) is not None:
            raise ValueError("activity revision already exists")
        return True

    def apply(self, connection, request):
        self._validate(self.reader, request)
        payload = request["payload"]
        row = self.reader.one("SELECT owner_json,record_json FROM staged_role_activities WHERE owner_key=? AND activity_id=?",
                              (request["owner_key"], payload["activity_id"]))
        new = _build(json.loads(row["record_json"]), payload)
        result_json = _encode(new)
        changed = connection.execute("""UPDATE staged_role_activities SET revision=?, state=?, updated_at=?, record_json=?
            WHERE owner_key=? AND activity_id=? AND revision=? AND record_json=?""",
            (new["revision"], new["state"], new["updated_at"], result_json, request["owner_key"],
             payload["activity_id"], request["expected_revision"], row["record_json"]))
        if changed.rowcount != 1:
            raise ValueError("activity revision changed during commit")
        connection.execute("""INSERT INTO staged_role_activity_revisions
            (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
            VALUES (?, ?, ?, ?, 'recorded', ?, ?)""",
            (request["owner_key"], payload["activity_id"], new["revision"], new["updated_at"],
             payload["operation_id"], result_json))
        connection.execute("""INSERT INTO staged_role_life_operations(operation_id, owner_key, result_json)
            VALUES (?, ?, ?)""", (payload["operation_id"], request["owner_key"], result_json))
        control = payload.get("reading_control")
        if control and control["mode"] == "start":
            connection.execute("""INSERT INTO staged_life_runtime_metadata(key,value) VALUES (?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                ("role.life.reading_pace:" + request["context_key"], _encode(control["pace"])))
            if control["retry_task_id"] is not None:
                connection.execute("DELETE FROM staged_life_runtime_metadata WHERE key=?",
                                   ("role.life.retry_interrupted:" + control["retry_task_id"],))
        elif control and control["next_activity"] is not None:
            following = control["next_activity"]
            raw = _encode(following)
            inserted = connection.execute("INSERT OR IGNORE INTO staged_role_activities VALUES (?,?,?,?,?,?,?)",
                (request["owner_key"], following["activity_id"], row["owner_json"], 1, "planned", following["updated_at"], raw))
            if inserted.rowcount:
                connection.execute("""INSERT INTO staged_role_activity_revisions
                    (owner_key,activity_id,revision,updated_at,provenance,operation_id,record_json)
                    VALUES (?,?,1,?,'recorded',NULL,?)""",
                    (request["owner_key"], following["activity_id"], following["updated_at"], raw))
        return {"activity_id": payload["activity_id"], "revision": new["revision"], "record_json": result_json}

    def acknowledge(self, command_id, *, verify_host_completion):
        return self.commands.acknowledge(command_id, verify_host_completion=verify_host_completion,
                                         after_ack=self._publish_completion)

    def _matches_result(self, request, result, *, require_current=True):
        payload = request["payload"]
        if (type(result) is not dict or set(result) != {"activity_id", "revision", "record_json"}
                or result["activity_id"] != payload["activity_id"] or result["revision"] != request["expected_revision"] + 1):
            return False
        row = self.reader.one("""SELECT a.revision, a.record_json, r.record_json, r.provenance,
                r.operation_id, o.result_json, p.record_json FROM staged_role_activities a
            JOIN staged_role_activity_revisions r ON r.owner_key=a.owner_key AND r.activity_id=a.activity_id AND r.revision=?
            JOIN staged_role_activity_revisions p ON p.owner_key=a.owner_key AND p.activity_id=a.activity_id AND p.revision=?
            JOIN staged_role_life_operations o ON o.owner_key=a.owner_key AND o.operation_id=?
            WHERE a.owner_key=? AND a.activity_id=?""", (result["revision"], request["expected_revision"], payload["operation_id"],
                                                          request["owner_key"], payload["activity_id"]))
        if row is None or row[0] < result["revision"]:
            return False
        if (require_current and row[0] != result["revision"]) or (row[0] == result["revision"] and row[1] != result["record_json"]):
            return False
        if hashlib.sha256(row[6].encode("utf-8")).hexdigest() != payload["previous_hash"]:
            return False
        if _encode(_build(json.loads(row[6]), payload)) != result["record_json"]:
            return False
        control = payload.get("reading_control")
        if control and require_current:
            if control["mode"] == "start":
                pace = self.reader.one("SELECT value FROM staged_life_runtime_metadata WHERE key=?",
                                       ("role.life.reading_pace:" + request["context_key"],))
                if pace is None or json.loads(pace[0]) != control["pace"]:
                    return False
            elif control["next_activity"] is not None:
                following = control["next_activity"]
                saved = self.reader.one("""SELECT record_json FROM staged_role_activity_revisions
                    WHERE owner_key=? AND activity_id=? AND revision=1 AND provenance='recorded'""",
                    (request["owner_key"], following["activity_id"]))
                if saved is None or saved[0] != _encode(following):
                    return False
        return (row[2] == result["record_json"] and row[3] == "recorded"
                and row[4] == payload["operation_id"] and row[5] == result["record_json"])

    def verify_commit(self, decision):
        if not self.commands.verify_commit(decision):
            return False
        try:
            row = self.reader.one("SELECT * FROM life_prepared_commands WHERE command_id=?", (decision["command_id"],))
            if row is None:
                return False
            request = {"command_id": row["command_id"], "owner_key": row["owner_key"], "context_key": row["context_key"],
                       "kind": row["kind"], "expected_revision": row["expected_revision"], "payload": json.loads(row["payload_json"])}
            return self._matches_result(request, json.loads(row["result_json"]), require_current=row["state"] != "acknowledged")
        except (AttributeError, KeyError, TypeError, ValueError):
            return False

    def _publish_completion(self, _connection, request, result):
        payload = request["payload"]
        if not self._matches_result(request, result):
            raise ValueError("committed activity or receipt changed before event delivery")
        if not payload["event_consumers"]:
            return
        activity = json.loads(result["record_json"])
        event_id = "life-activity-" + hashlib.sha256(payload["operation_id"].encode("utf-8")).hexdigest()
        self.storage.record_event(event_id=event_id, owner_key=request["owner_key"], context_key=request["context_key"],
            kind="activity.completed", payload={"activity_id": result["activity_id"], "revision": result["revision"],
                                                "operation_id": payload["operation_id"]},
            occurred_at=_time(activity["updated_at"]), consumers=payload["event_consumers"])
