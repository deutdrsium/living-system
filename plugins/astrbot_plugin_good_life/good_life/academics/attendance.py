"""Opt-in wake check and class attendance settlement for the A4 embedding."""
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import json
import math

from ..contracts.models import ReasonCode, StateQuality
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
    base_rate: float = 0.10
    morning_factor: float = 0.15
    afternoon_factor: float = 0.001
    bedtime: time = time(23, 30)
    late_night_bonus_per_hour: float = 1.0
    consecutive_day_bonus: float = 0.15
    consecutive_day_cap: int = 4
    a6_sleepiness_bonus: float = 0.5
    wake_check_lead_s: int = 1800
    min_slip_s: int = 300
    max_slip_s: int = 1800
    severe_slip_rate: float = 0.20
    severe_slip_s: int = 7200
    catch_up_work_s: int = 600

    def __post_init__(self):
        require(type(self.enabled) is bool, "attendance.enabled must be boolean")
        for value in (self.base_rate, self.morning_factor, self.afternoon_factor, self.severe_slip_rate):
            require(type(value) in (int, float) and 0 <= value <= 1, "attendance rate outside [0, 1]")
        for value in (self.late_night_bonus_per_hour, self.consecutive_day_bonus, self.a6_sleepiness_bonus):
            require(type(value) in (int, float) and 0 <= value <= 10, "attendance bonus outside [0, 10]")
        require(type(self.bedtime) is time and self.bedtime.tzinfo is None, "local attendance bedtime required")
        require(integer(self.consecutive_day_cap) <= 30 and integer(self.wake_check_lead_s) <= 3600,
                "attendance wake window or streak cap too large")
        require(0 < integer(self.min_slip_s) <= integer(self.max_slip_s) <= integer(self.severe_slip_s) <= 86400,
                "invalid attendance wake slip range")
        require(integer(self.catch_up_work_s) <= 3600, "attendance catch-up load too large")


def _draw(owner_key, session_id, purpose):
    # 52 hash bits fit exactly in a float. Neither Python hash seeds nor the
    # order of calls or timetable revisions enter the draw.
    return int(digest(("a4.attendance.v1", owner_key, session_id, purpose))[:13], 16) / (1 << 52)


def _night_bedtime(records, local_start, tz, now):
    night = datetime.combine(local_start.date() - timedelta(days=1), time(18), tz)
    cutoff = min(local_start, datetime.combine(local_start.date(), time(9), tz), now.astimezone(tz))
    starts = []
    for value in records:
        item = json.loads(value) if isinstance(value, str) else value
        sleep = item.get("sleep") or {}
        if (item.get("state") not in ("active", "paused", "completed", "cancelled")
                or not (sleep or (item.get("work") or {}).get("kind") == "sleep") or not item.get("started_at")
                or sleep.get("kind") == "nap" or (sleep.get("kind") == "manual" and sleep.get("target_minutes", 0) <= 60)):
            continue
        start = timestamp(item["started_at"]).astimezone(tz)
        if night <= start <= cutoff:
            starts.append(start)
    # The first night episode is bedtime, even when sleep is interrupted.
    return min(starts, default=None)


def _wake_slip(config, owner_key, session_id, local_start, hours_late, streak, sleepiness):
    start = local_start.time()
    time_factor = (1.0 if start <= time(8, 30) else config.morning_factor if start < time(12)
                   else config.afternoon_factor)
    rate = min(1.0, config.base_rate * time_factor * (1 + hours_late * config.late_night_bonus_per_hour
        + min(streak, config.consecutive_day_cap) * config.consecutive_day_bonus
        + sleepiness * config.a6_sleepiness_bonus))
    if rate == 0 or _draw(owner_key, session_id, "slip") >= rate:
        return 0
    severity = _draw(owner_key, session_id, "severity")
    if severity < config.severe_slip_rate:
        return config.severe_slip_s
    fraction = (severity - config.severe_slip_rate) / (1 - config.severe_slip_rate)
    return config.min_slip_s + int(fraction * (config.max_slip_s - config.min_slip_s + 1))


def _sleepiness(state, owner_key, now):
    if state is None:
        return 0
    require(state.owner_key == owner_key, "attendance state owner mismatch", ReasonCode.UNAUTHORIZED)
    value = state.values.get("sleep.sleepiness")
    if (value and value.quality in (StateQuality.VALID, StateQuality.ESTIMATED)
            and moment(value.updated_at) == now and type(value.value) in (int, float) and 0 <= value.value <= 1):
        return value.value
    return 0


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
                   origin_revision=None, sleep_records=(), known_ready_at=None, a6_enabled=False, a6_sleep_enabled=False,
                   a6_state=None, unwell=False):
        """Persist the first wake decision before an executor moves or misses class."""
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
            # A late bind can record known readiness while class is still running.
            cutoff = timestamp(session["end_at"]) if known_ready_at is not None else start
            require(start - timedelta(days=1) <= departure <= start and
                    departure - timedelta(seconds=self.config.wake_check_lead_s) <= now <= cutoff,
                    "class wake check is outside the execution window", ReasonCode.NO_TIME_WINDOW)
            if planned_travel_s is not None:
                require(integer(planned_travel_s) <= 86400, "planned class travel exceeds one day")
            require(all(type(flag) is bool for flag in (a6_enabled, a6_sleep_enabled, unwell)), "attendance state switches must be booleans")
            term = self.academics._records(db, scope.owner_key, "term").get(session["term_id"])
            require(term is not None, "term is unavailable", ReasonCode.MISSING_DEPENDENCY)
            tz = zone(term["timezone"])
            local = start.astimezone(tz)
            previous_day = local.date() - timedelta(days=1)
            bedtime = _night_bedtime(sleep_records, local, tz, now)
            reference = datetime.combine(previous_day, self.config.bedtime, tz)
            hours_late = max(0, (bedtime - reference).total_seconds() / 3600) if bedtime else 0
            prior_early_days = {item["local_date"] for item in self.academics._session_rows(db, scope.owner_key).values()
                if not item["cancelled"] and item["term_id"] == session["term_id"]
                and timestamp(item["start_at"]).astimezone(tz).time() <= time(8, 30)}
            streak = 0
            while streak < self.config.consecutive_day_cap and previous_day.isoformat() in prior_early_days:
                streak += 1
                previous_day -= timedelta(days=1)
            sleepiness = _sleepiness(a6_state, scope.owner_key, now) if a6_enabled and a6_sleep_enabled else 0
            sick = a6_enabled and unwell
            if sick:
                ready = departure
            elif known_ready_at is not None:
                ready = max(departure, moment(known_ready_at))
                require(ready - departure <= timedelta(days=1), "known wake delay exceeds one day")
            else:
                slip = _wake_slip(self.config, scope.owner_key, session_id, local, hours_late, streak, sleepiness)
                ready = departure + timedelta(seconds=slip)
            slip = math.ceil((ready - departure).total_seconds())
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

            skip_cause = None
            if wake["unwell"] and wake["local_date"] == session["local_date"]:
                skip_cause = "unwell"
            elif has_slip and ready >= end:
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
                    last_departure = on_time_departure + (end - start)
                    skip_cause = "overslept" if has_slip and ready >= last_departure else "delayed_en_route"
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
