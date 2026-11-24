"""Opt-in wake check and class attendance settlement for the A4 embedding."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import math

from ..contracts.models import ReasonCode
from ..kernel.codec import digest, encode, freeze, integer, moment, name, require, wire
from .models import day, zone
from .service import timestamp

CAUSE_LABELS = freeze({"overslept": "睡过头", "delayed_en_route": "路上耽搁", "unwell": "身体不适", "missed": "未到课"})
FOLLOW_UP = "academics.class.catch_up"
STUDY_CONSUMER = "academics.study.attendance.v1"
VERSION_KEY = "class_attendance_schema_version"
DDL = """CREATE TABLE life_class_attendance_wakes (
    owner_key TEXT NOT NULL, session_id TEXT NOT NULL, record_json TEXT NOT NULL,
    PRIMARY KEY(owner_key,session_id))"""


@dataclass(frozen=True)
class AttendanceConfig:
    enabled: bool = False
    use_sleep_records: bool = True
    wake_check_lead_s: int = 1800
    prep_s: int = 1200
    catch_up_work_s: int = 600

    def __post_init__(self):
        require(type(self.enabled) is bool and type(self.use_sleep_records) is bool,
                "attendance switches must be boolean")
        require(integer(self.wake_check_lead_s) <= 3600, "attendance wake window too large")
        integer(self.prep_s)
        require(integer(self.catch_up_work_s) <= 3600, "attendance catch-up load too large")


def _night_sleep(records, local_start, now):
    tz = local_start.tzinfo
    night = datetime.combine(local_start.date() - timedelta(days=1), time(18), tz)
    cutoff = min(local_start, datetime.combine(local_start.date(), time(12), tz), now.astimezone(tz))
    latest = None
    for value in records:
        item = json.loads(value) if isinstance(value, str) else value
        sleep = item.get("sleep") or {}
        if (item.get("state") not in ("active", "paused", "completed", "cancelled")
                or not (sleep or (item.get("work") or {}).get("kind") == "sleep") or not item.get("started_at")
                or sleep.get("kind") == "nap" or (sleep.get("kind") == "manual" and sleep.get("target_minutes", 0) <= 60)):
            continue
        start = timestamp(item["started_at"]).astimezone(tz)
        if night <= start <= cutoff and (latest is None or start > latest[0]):
            latest = (start, item)
    return latest[1] if latest else None


class ClassAttendance:
    """Explicit execution entry points. Use the same LifeStorage as the A4 kernel."""

    def __init__(self, academics, *, config=None):
        self.academics = academics
        self.config = config if config is not None else AttendanceConfig()
        require(isinstance(self.config, AttendanceConfig), "AttendanceConfig required")
        if self.config.enabled:
            academics._enabled()
            with academics.storage.transaction() as db:
                version = db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()
                table = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='life_class_attendance_wakes'").fetchone()
                if version is None:
                    require(table is None, "unclaimed attendance schema")
                    db.execute(DDL)
                    db.execute("INSERT INTO life_metadata VALUES (?, '1')", (VERSION_KEY,))
                else:
                    require(version[0] == "1" and table and table[0].split() == DDL.split(), "invalid attendance schema")

    def _scope(self, kernel, scope):
        require(kernel.storage is self.academics.storage, "attendance and kernel must share LifeStorage")
        return kernel._scope(scope, "kernel.propose")

    def _wake(self, db, scope, session_id):
        self.academics._owner(db, scope.owner_key, scope.context_key)
        row = db.execute("SELECT record_json FROM life_class_attendance_wakes WHERE owner_key=? AND session_id=?",
                         (scope.owner_key, name(session_id))).fetchone()
        return freeze(json.loads(row[0])) if row else None

    def check_wake(self, kernel, scope, session_id, *, now, planned_departure_at, planned_travel_s=None,
                   origin_revision=None, sleep_records=(), a6_enabled=False, unwell=False):
        """Persist readiness once the night ends or class is no longer reachable."""
        if not self.config.enabled:
            return None
        scope, now = self._scope(kernel, scope), moment(now)
        with self.academics.storage.transaction() as db:
            saved = self._wake(db, scope, session_id)
            if saved is not None:
                return saved
            session = self.academics.session_context(scope.owner_key, scope.context_key, session_id)
            require(not session["cancelled"] and self.academics.participation(scope.owner_key, scope.context_key, session_id) is None,
                    "class is cancelled or already claimed", ReasonCode.STALE_VERSION)
            departure, start = moment(planned_departure_at), timestamp(session["start_at"])
            require(start - timedelta(days=1) <= departure <= start and
                    departure - timedelta(seconds=self.config.wake_check_lead_s) <= now,
                    "class wake check is outside the execution window", ReasonCode.NO_TIME_WINDOW)
            if planned_travel_s is not None:
                require(integer(planned_travel_s) <= 86400, "planned class travel exceeds one day")
            require(type(a6_enabled) is bool and type(unwell) is bool, "attendance state switches must be booleans")
            sick = a6_enabled and unwell
            ready = departure
            if self.config.use_sleep_records and not sick:
                term = self.academics._records(db, scope.owner_key, "term").get(session["term_id"])
                require(term is not None, "term is unavailable", ReasonCode.MISSING_DEPENDENCY)
                night = _night_sleep(sleep_records, start.astimezone(zone(term["timezone"])), now)
                if night is None:
                    if now < departure:
                        return None
                else:
                    prep = timedelta(seconds=self.config.prep_s)
                    # Staged activity rows store the terminal receipt's ended_at as updated_at.
                    ended = None
                    if night["state"] in ("completed", "cancelled"):
                        ended = timestamp(night.get("ended_at") or night["updated_at"])
                    if ended is not None and ended <= now:
                        ready = ended + prep
                    else:
                        # Even waking now leaves too little time to get to class.
                        ready = now + prep
                        last_departure = timestamp(session["end_at"]) - timedelta(seconds=planned_travel_s or 0)
                        if ready < last_departure:
                            return None
            slip = max(0, math.ceil((ready - departure).total_seconds()))
            saved = freeze({"session_revision": session["revision"], "decided_at": wire(now),
                "planned_departure_at": wire(departure), "planned_travel_s": planned_travel_s, "ready_at": wire(ready),
                "wake_delay_s": slip, "unwell": sick, "local_date": session["local_date"],
                **({"origin_revision": origin_revision} if origin_revision is not None else {})})
            db.execute("INSERT INTO life_class_attendance_wakes VALUES (?, ?, ?)", (scope.owner_key, session_id, encode(saved)))
            return saved

    def propose(self, kernel, scope, session_id, *, now, route=None, delay_s=0, no_viable_route=False,
                retry_key=None, defer_absence=False, departure_plan=None):
        """Propose from a durable wake check and the current A4 route, if needed."""
        if not self.config.enabled:
            return None
        from ..activities.class_session import _propose_quoted_class, class_arrival, departure_matches

        scope, now = self._scope(kernel, scope), moment(now)
        require(integer(delay_s) <= 86400, "attendance travel delay too large")
        require(type(no_viable_route) is bool, "no_viable_route must be boolean")
        # The kernel owns separate proposal transactions. Hold the shared lock
        # so a concurrent timetable edit cannot interleave these local reads.
        with self.academics.storage._lock:
            wake = self._wake(self.academics.storage._connection, scope, session_id)
            require(wake is not None, "attendance wake check is missing", ReasonCode.MISSING_DEPENDENCY)
            if self.academics.participation(scope.owner_key, scope.context_key, session_id) is not None:
                return None
            session = self.academics.session_context(scope.owner_key, scope.context_key, session_id)
            if session["cancelled"]:
                return None
            start, end = timestamp(session["start_at"]), timestamp(session["end_at"])
            departure = timestamp(wake["planned_departure_at"])
            ready = timestamp(wake["ready_at"])
            has_slip = wake["wake_delay_s"] > 0
            revised = wake["session_revision"] != session["revision"]
            earliest = min(departure, end)
            planned_travel_s = wake.get("planned_travel_s")
            if departure_plan is not None:
                earliest, planned_travel_s = departure_plan
            elif revised:
                earliest = start - timedelta(seconds=self.config.wake_check_lead_s)
            if planned_travel_s is not None:
                on_time_departure = start - timedelta(seconds=planned_travel_s)
            else:
                on_time_departure = start if revised else departure
            if now < max(earliest, timestamp(wake["decided_at"])):
                return None

            last_departure = on_time_departure + (end - start)
            skip_cause = None
            if wake["unwell"] and wake["local_date"] == session["local_date"]:
                skip_cause = "unwell"
            elif has_slip and ready >= last_departure:
                skip_cause = "overslept"
            elif has_slip and now < ready:
                return None

            reason = "manual_decision"
            if skip_cause is None:
                if now >= end or no_viable_route:
                    misses_end = True
                elif route is None or not departure_matches({"route": route}, now):
                    return None  # Allow a fresh quote while there is still time.
                else:
                    travel_s = integer(route["travel_s"]) + delay_s
                    require(travel_s <= 86400, "delayed class travel exceeds one day")
                    require(not delay_s or "departure_at" not in route, "timed route must include its own delay")
                    arrival = class_arrival(route, now, travel_s, start, ready if has_slip else None)
                    misses_end = arrival >= end
                    if not misses_end and arrival > start:
                        reason = "travel_delay"
                    if delay_s:
                        route = {**route, "travel_s": travel_s}
                if misses_end:
                    skip_cause = "delayed_en_route"
            if skip_cause and defer_absence and now < end:
                return None
            attendance = {"skip_cause": skip_cause, "ready_at": wake["ready_at"] if has_slip else None,
                          "planned_departure_at": wake["planned_departure_at"],
                          "on_time_departure_at": wire(on_time_departure), "delay_s": delay_s if not skip_cause else 0,
                          "assignment_id": self._assignment(scope.owner_key, session, end),
                          "catch_up_work_s": self.config.catch_up_work_s}
            return _propose_quoted_class(kernel, self.academics, scope, session, now=now,
                decision="skip" if skip_cause else "attend", reason=reason,
                route=None if skip_cause else route, retry_key=retry_key, attendance=attendance)

    def settle_missed(self, scope, session, *, now, write_guard):
        """Settle an ended, unclaimed occurrence without creating a foreground activity."""
        owner, context = scope.owner_key, scope.context_key
        session_id = session["session_id"]
        start, end = timestamp(session["start_at"]), timestamp(session["end_at"])
        storage = self.academics.storage
        with storage._lock, write_guard(scope, "kernel.recover"), storage.transaction() as db:
            current = self.academics.session_context(owner, context, session_id)
            if current != session or current["cancelled"] or now < end:
                return
            if self.academics.participation(owner, context, session_id) is not None:
                return
            wake = self._wake(db, scope, session_id)
            cause = "missed"
            if wake is not None:
                if wake["unwell"] and wake["local_date"] == session["local_date"]:
                    cause = "unwell"
                elif (wake["wake_delay_s"] and timestamp(wake["ready_at"]) >=
                      end - timedelta(seconds=wake["planned_travel_s"] or 0)):
                    cause = "overslept"
            grade = {"outcome": "absent", "minutes_missed": math.ceil((end - start).total_seconds() / 60),
                     "cause_code": cause, "cause_label": CAUSE_LABELS[cause]}
            instance_id = "class-missed-" + digest((owner, session_id))
            payload = {"session_id": session_id, "session_revision": session["revision"], "instance_id": instance_id,
                       "decision": "skip", "status": "missed", "attended_s": 0, "intervals": (), "arrival_at": None,
                       "late_reason": None, "early_departure_reason": None, "decision_reason": "missed",
                       "sequence": 1, "provenance": "simulated", "attendance": grade}
            db.execute("INSERT INTO life_academic_participation VALUES (?, ?, ?, ?)",
                       (owner, session_id, instance_id, encode(payload)))
            _follow_up(storage, owner, context, session, grade, now,
                       self._assignment(owner, session, end), self.config.catch_up_work_s)

    def _assignment(self, owner_key, session, end):
        db = self.academics.storage._connection
        term_id, course_id = session["term_id"], session["course_id"]
        term = self.academics._records(db, owner_key, "term").get(term_id)
        if term is None:
            return None
        local = session["local_date"]
        teaching_date = day(next((item["teaching_date"] for item in term["makeup_days"] if item["date"] == local), local))
        week = next((index for index, value in enumerate(term["week_starts"], 1)
                     if 0 <= (teaching_date - day(value)).days < 7), None)
        units = {item["unit_id"] for item in self.academics._records(db, owner_key, "syllabus").values()
                 if (item["term_id"], item["course_id"], item["week"]) == (term_id, course_id, week)}
        candidates = [(timestamp(item["due_at"]), item["assignment_id"]) for item in self.academics._records(db, owner_key, "assignment").values()
                      if (item["term_id"], item["course_id"]) == (term_id, course_id) and item["unit_id"] in units
                      and timestamp(item["due_at"]) >= end]
        return min(candidates)[1] if candidates else None


def verify_wake_claim(db, instance, now):
    session, plan = instance.checkpoint["session"], instance.checkpoint["params"]["attendance"]
    row = db.execute("SELECT record_json FROM life_class_attendance_wakes WHERE owner_key=? AND session_id=?",
                     (instance.owner_key, session["session_id"])).fetchone()
    require(row is not None, "attendance wake decision is missing", ReasonCode.MISSING_DEPENDENCY)
    wake = json.loads(row[0])
    ready = wake["ready_at"] if wake["wake_delay_s"] else None
    require(plan["ready_at"] == ready and plan["planned_departure_at"] == wake["planned_departure_at"],
            "attendance plan does not match wake decision", ReasonCode.STALE_VERSION)
    require((plan["skip_cause"] == "unwell") == (wake["unwell"] and wake["local_date"] == session["local_date"]),
            "attendance plan does not match unwell decision", ReasonCode.STALE_VERSION)
    if plan["skip_cause"] == "overslept":
        require(ready is not None, "oversleep absence requires a wake delay")
    if ready and plan["skip_cause"] is None:
        require(moment(now) >= timestamp(ready), "class travel cannot begin before wake", ReasonCode.NO_TIME_WINDOW)


def settled_attendance(instance):
    cp = instance.checkpoint
    plan = cp["params"].get("attendance")
    if plan is None or not cp["final"] or instance.started_at is None:
        return None
    session = cp["session"]
    start, end = timestamp(session["start_at"]), timestamp(session["end_at"])
    arrival = timestamp(cp["arrival_at"]) if cp["arrival_at"] else None
    ended = timestamp(cp["ended_at"])
    if not plan["skip_cause"] and (ended <= start or (arrival is None and ended < end)):
        return None  # Administrative cancellation before an attendance opportunity is ungraded.
    if arrival is not None and arrival <= start:
        return {"outcome": "on_time", "minutes_missed": 0, "cause_code": None, "cause_label": None}
    missed = min(end, arrival or end) - start
    cause = plan["skip_cause"] or "delayed_en_route"
    if not plan["skip_cause"] and plan["ready_at"] is not None:
        ready = timestamp(plan["ready_at"])
        if "on_time_departure_at" in plan:
            on_time_departure = timestamp(plan["on_time_departure_at"])
        else:
            # Preserve the cause in already staged results from earlier templates.
            base_travel_s = cp["params"]["route"]["travel_s"] - plan["delay_s"]
            on_time_departure = start - timedelta(seconds=base_travel_s)
        if on_time_departure < ready <= instance.started_at:
            cause = "overslept"
    return {"outcome": "late" if arrival is not None and arrival < end else "absent",
            "minutes_missed": math.ceil(missed.total_seconds() / 60), "cause_code": cause, "cause_label": CAUSE_LABELS[cause]}


def emit_follow_up(storage, instance, attendance, now):
    session, plan = instance.checkpoint["session"], instance.checkpoint["params"]["attendance"]
    _follow_up(storage, instance.owner_key, instance.context_key, session, attendance, now,
               plan["assignment_id"], plan["catch_up_work_s"])


def _follow_up(storage, owner, context, session, attendance, now, assignment_id, work_s):
    item_id = "class-catch-up-" + digest((owner, session["session_id"]))
    storage.record_event(event_id=item_id, owner_key=owner, context_key=context,
        kind=FOLLOW_UP, occurred_at=now, consumers=(STUDY_CONSUMER,), payload={
            "schema_version": 1, "item_id": item_id, "session_id": session["session_id"],
            "term_id": session["term_id"], "course_id": session["course_id"], **attendance,
            "action": "borrow_notes" if attendance["outcome"] == "absent" else "catch_up_notes",
            "assignment_id": assignment_id, "extra_work_s": work_s if assignment_id else 0,
            "provenance": "simulated"})
