"""Kernel component for simulated travel, waiting, attendance and explicit skip.

The timetable is manual evidence of a plan. Attendance is simulated participation,
never proof of reading, learning, a teacher quotation or a real school receipt.
"""
from collections.abc import Mapping
from dataclasses import replace
import re
from datetime import datetime, timedelta

from ..contracts.models import (ActivityStatus, ActivityTemplate, Effect, EventProposal, Outcome,
    OutcomeStatus, Provenance, ReasonCode, RecordRef, SchemaVersion, TemplateRef, TemplateVersion)
from ..kernel.codec import digest, freeze, integer, moment, name, require, wire
from ..kernel.engine import CandidateComponentError
from ..kernel.gating import PreparedActivity
from ..kernel.lifecycle import ExecutorStep
from ..kernel.resources import ResourcePreparation
from ..academics.attendance import CAUSE_LABELS, settled_attendance
from ..academics.campus import CLASS_STOP_EFFECT, campus_route_timing
from ..academics.models import fields
from ..academics.service import timestamp

REASONS = ("travel_delay", "health", "other_obligation", "manual_decision", "late_start", "missed")
PINNED_DEPARTURE_REASON = "class_due_pinned_departure"


def eligibility_key(session, decision="attend"):
    return "academics.session." + digest((session["session_id"], session["revision"], decision))[:40]


def _pinned_departure(route):
    if isinstance(route, Mapping) and "departure_at" in route:
        return timestamp(route["departure_at"])
    return None


def _departure_valid_until(route, departure):
    # Older quotes had no verified grace and only cover their exact instant.
    return timestamp(route["departure_valid_until"]) if "departure_valid_until" in route else departure


def departure_matches(params, now):
    route = params["route"]
    departure = _pinned_departure(route)
    return departure is None or departure <= moment(now) <= _departure_valid_until(route, departure)


def class_arrival(route, departure, travel_s, class_start, ready=None):
    if travel_s == 0 and route["from_place_id"] == route["place_id"]:
        # The role is already in the classroom. A late wake still counts.
        return min(departure, max(class_start, ready or class_start))
    return departure + timedelta(seconds=travel_s)


def class_session_template(session, *, decision="attend", reason="manual_decision", route=None, attendance=None):
    params = {"session": session, "decision": decision, "reason": reason, "route": route}
    if attendance is not None:
        params["attendance"] = attendance
    params = freeze(params)
    trigger = PINNED_DEPARTURE_REASON if _pinned_departure(route) is not None else "class_due"
    template = ActivityTemplate("class_session_" + digest(params)[:40], TemplateVersion(1), "class_session_local@1",
        ("academics", "campus") if decision == "attend" else ("academics",),
        (freeze({"id": trigger, "type": "intent"}),),
        freeze({"free_s": 0, "resources": {eligibility_key(session, decision): 1}}), freeze({}), params,
        presentation=freeze({"provenance": "simulated"}))
    ClassSessionLocal().validate(template)
    return template


def participation_payload(instance, *, final):
    cp, session = instance.checkpoint, instance.checkpoint["session"]
    intervals, total, previous = cp["intervals"], 0, None
    for interval in intervals:
        fields(interval, "start_at end_at", "participation interval")
        begins, ends = timestamp(interval["start_at"]), timestamp(interval["end_at"])
        require(timestamp(session["start_at"]) <= begins < ends <= timestamp(session["end_at"]), "attendance outside class")
        require(previous is None or previous <= begins, "attendance intervals overlap")
        require(cp["arrival_at"] is not None and timestamp(cp["arrival_at"]) <= begins, "attendance precedes arrival")
        total += int((ends - begins).total_seconds())
        previous = ends
    require(total == integer(cp["attended_s"]) and total <= instance.elapsed_s, "attendance lacks online progress")
    require(cp["params"]["decision"] != "skip" or total == 0, "skip cannot record attendance")
    duration = int((timestamp(session["end_at"]) - timestamp(session["start_at"])).total_seconds())
    attendance = settled_attendance(instance) if final else None
    return freeze({**({"attendance": attendance} if attendance is not None else {}),
        "session_id": session["session_id"], "session_revision": session["revision"],
        "instance_id": instance.instance_id, "decision": cp["params"]["decision"],
        "status": ("skipped" if cp["params"]["decision"] == "skip" else "attended" if total == duration else "partial" if total else "missed") if final else "active",
        "attended_s": total, "intervals": intervals, "arrival_at": cp["arrival_at"],
        "late_reason": cp["params"]["reason"] if cp["arrival_at"] and timestamp(cp["arrival_at"]) > timestamp(session["start_at"]) else None,
        "early_departure_reason": cp["params"]["reason"] if final and cp["ended_at"] and timestamp(cp["ended_at"]) < timestamp(session["end_at"]) and total else None,
        "decision_reason": cp["params"]["reason"], "sequence": cp["sequence"], "provenance": "simulated"})


def class_stop_effect(instance, now):
    cp = instance.checkpoint
    if not cp.get("commute_stop_unknown"):
        return None
    route = cp["params"]["route"]
    require(cp["shared_position"] and cp["final"] and not cp["arrived"]
            and 0 < cp["travel_elapsed_s"] < route["travel_s"], "stop lacks interrupted commute evidence")
    return Effect(instance.instance_id + ":commute-stop", instance.instance_id, "campus", CLASS_STOP_EFFECT,
                  freeze({"from_place_id": route["from_place_id"], "progress_s": cp["travel_elapsed_s"],
                          "expected_revision": cp["location_revision"]}), now, ())


class ClassSessionLocal:
    name, version, local_only = "class_session_local", "1", True
    pausable_phases = ("travel", "wait", "attend", "skip")

    def validate(self, template):
        params = template.params
        expected_params = "session decision reason route"
        if "attendance" in params:
            expected_params += " attendance"
        fields(params, expected_params, "class parameters")
        session = params["session"]
        fields(session, "session_id slot_id term_id course_id course_name start_at end_at local_date place_id revision cancelled source", "class session")
        require(session["source"] == "manual" and session["cancelled"] is False and timestamp(session["start_at"]) < timestamp(session["end_at"]), "invalid class target")
        integer(session["revision"], 1)
        require(params["decision"] in ("attend", "skip") and params["reason"] in REASONS, "explicit class decision/reason required")
        if params["decision"] == "attend":
            route = params["route"]
            require(isinstance(route, Mapping), 'campus route must be a mapping')
            expected = "from_place_id place_id path travel_s"
            if 'graph_id' in route:
                expected += " graph_id graph_version graph_digest attribution departure_at assumptions needs_review"
                if 'departure_valid_until' in route:
                    expected += " departure_valid_until"
            fields(route, expected, "campus route")
            if 'graph_id' in route:
                require(isinstance(route['graph_id'], str) and route['graph_id'].strip()
                        and isinstance(route['graph_digest'], str) and re.fullmatch(r'[a-f0-9]{64}', route['graph_digest'])
                        and route['attribution'] == '© OpenStreetMap contributors',
                        'invalid pinned campus graph')
                integer(route['graph_version'], 1)
                departure = timestamp(route['departure_at'])
                valid_until = _departure_valid_until(route, departure)
                require(departure <= valid_until <= departure.replace(microsecond=999999),
                        'invalid pinned campus departure window')
                require(type(route['needs_review']) is bool and isinstance(route['assumptions'], (tuple, list))
                        and route['needs_review'] == bool(route['assumptions'])
                        and all(isinstance(note, str) and note for note in route['assumptions']),
                        'invalid campus access assumptions')
            require(route["place_id"] == session["place_id"] and 0 <= integer(route["travel_s"]) <= 86400, "route does not reach classroom")
            require(route["path"] and route["path"][0] == route["from_place_id"] and route["path"][-1] == route["place_id"], "invalid route path")
        else:
            require(params["route"] is None, "skip has no route")
        if "attendance" in params:
            attendance = params["attendance"]
            expected = "skip_cause ready_at planned_departure_at delay_s assignment_id catch_up_work_s"
            if "on_time_departure_at" in attendance:
                expected += " on_time_departure_at"
            fields(attendance, expected, "attendance plan")
            if "on_time_departure_at" in attendance:
                require(timestamp(attendance["on_time_departure_at"]) <= timestamp(session["start_at"]),
                        "invalid on-time class departure")
            skip_cause = attendance["skip_cause"]
            require((params["decision"] == "skip") == (skip_cause is not None)
                    and (skip_cause is None or skip_cause in CAUSE_LABELS), "invalid attendance skip cause")
            departure = timestamp(attendance["planned_departure_at"])
            if attendance["ready_at"] is not None:
                require(timestamp(attendance["ready_at"]) > departure, "invalid attendance wake delay")
            delay_s = integer(attendance["delay_s"])
            if params["decision"] == "attend":
                require(delay_s <= params["route"]["travel_s"], "attendance delay exceeds total travel time")
                require(not delay_s or _pinned_departure(params["route"]) is None, "timed route must include its own delay")
            else:
                require(delay_s == 0, "skip has no travel delay")
            require(integer(attendance["catch_up_work_s"]) <= 3600, "attendance catch-up load too large")
            if attendance["assignment_id"] is not None:
                name(attendance["assignment_id"])
        require(template.requires == (("academics", "campus") if params["decision"] == "attend" else ("academics",)), "invalid class dependencies")

    def prepare(self, template, now):
        self.validate(template)
        session, params = template.params["session"], template.params
        if not departure_matches(params, now):
            # Retire this quote through the kernel's candidate isolation path so
            # the calendar can request a fresh route instead of retrying forever.
            raise CandidateComponentError(ReasonCode.MISSING_DEPENDENCY,
                "confirmed campus quote missed its departure; request a fresh class proposal")
        remaining = max(0, int((timestamp(session["end_at"]) - moment(now)).total_seconds()))
        require(params["decision"] == "skip" or remaining > 0, "class has already ended", ReasonCode.NO_TIME_WINDOW)
        return PreparedActivity("skip" if params["decision"] == "skip" else "travel", 0 if params["decision"] == "skip" else remaining,
            freeze({"schema_version": 1, "params": params, "session": session, "last_tick": None,
                "travel_elapsed_s": 0, "location_revision": None, "arrived": False, "arrival_at": None,
                "attended_s": 0, "intervals": (), "sequence": 0, "ended_at": None, "final": False,
                "arrival_effect": False, "completed_units": {"attended_s": 0}}), template.gating["resources"])

    def remaining_duration_s(self, instance, now):
        if instance.checkpoint["params"]["decision"] == "skip":
            return 0
        return max(0, int((timestamp(instance.checkpoint["session"]["end_at"]) - moment(now)).total_seconds()))

    def start(self, instance, now):
        require(instance.started_at is None, "class already started")
        # AcademicsAccess denies an obsolete pinned departure in the start gate.
        return self.resume(instance, now)

    def resume(self, instance, now):
        # No offline attendance/travel. The kernel resets its monotonic anchor.
        return freeze({**instance.checkpoint, "last_tick": wire(moment(now)), "arrival_effect": False})

    def prepare_resources(self, instance, now, snapshot):
        cp, session = instance.checkpoint, instance.checkpoint["session"]
        require(instance.target_ref == RecordRef("academics.session", session["session_id"], session["revision"]), "class target reference mismatch")
        require(cp["params"]["decision"] == "skip" or instance.reservation_refs or moment(now) < timestamp(session["end_at"]), "class has ended", ReasonCode.NO_TIME_WINDOW)
        effects = ()
        if not instance.reservation_refs:
            effects = (Effect(instance.instance_id + ":claim", instance.instance_id, "academics", "academics.claim",
                freeze({"session_id": session["session_id"], "revision": session["revision"]}), now, ()),)
        if cp["params"]["decision"] == "attend" and moment(now) < timestamp(session["end_at"]):
            location = snapshot.state.values.get("geo.place_id")
            expected_place = cp["params"]["route"]["place_id" if cp["arrived"] else "from_place_id"]
            require(location and location.quality.value == "valid", "campus origin is unavailable", ReasonCode.STALE_VERSION)
            value = location.value
            shared = isinstance(value, Mapping)
            place = value["place_id"] if shared else value
            require(place == expected_place, "campus origin is unavailable or changed", ReasonCode.STALE_VERSION)
            revision = integer(value["revision"], 1) if shared else location.revision
            if cp["location_revision"] is None:
                cp = freeze({**cp, "location_revision": revision, **({"shared_position": True} if shared else {})})
            elif not cp["arrived"]:
                require(shared == cp.get("shared_position", False) and (not shared or revision == cp["location_revision"]),
                        "campus origin revision changed", ReasonCode.STALE_VERSION)
        return ResourcePreparation(instance.phase, cp, effects,
            (RecordRef("academics.participation", session["session_id"], None),))

    @staticmethod
    def _effects(instance, now):
        cp, effects = instance.checkpoint, []
        if instance.started_at is None:
            return ()
        if cp["arrival_effect"]:
            route = cp["params"]["route"]
            effects.append(Effect(instance.instance_id + ":arrive", instance.instance_id, "campus", "campus.arrive",
                freeze({"from_place_id": route["from_place_id"], "place_id": route["place_id"], "expected_revision": cp["location_revision"]}), now, ()))
        stop = class_stop_effect(instance, now)
        if stop is not None:
            effects.append(stop)
        effects.append(Effect(instance.instance_id + ":participation:" + str(cp["sequence"]), instance.instance_id,
            "academics", "academics.participation", participation_payload(instance, final=cp["final"]), now, ()))
        return tuple(effects)

    def _step(self, instance, now, audience, *, elapsed_delta_s, cancel):
        now = moment(now)
        cp, session = instance.checkpoint, instance.checkpoint["session"]
        require(now >= instance.updated_at, "class clock moved backwards", ReasonCode.STALE_VERSION)
        delta = integer(elapsed_delta_s)
        if instance.status != ActivityStatus.RUNNING:
            require(delta == 0, "stopped class cannot accumulate participation")
        skip = cp["params"]["decision"] == "skip"
        final = cancel or skip or now >= timestamp(session["end_at"])
        if not delta and not final:
            return ExecutorStep(instance.phase, cp, instance.elapsed_s, cp["completed_units"])
        delta = min(delta, max(0, int((now - timestamp(cp["last_tick"])).total_seconds()))) if cp["last_tick"] else 0
        start_online = now - timedelta(seconds=delta)
        end_online = min(now, timestamp(session["end_at"])) if not skip else now
        delta = max(0, int((end_online - start_online).total_seconds()))
        elapsed = min(instance.planned_duration_s, instance.elapsed_s + delta)
        travel, arrived, arrival = cp["travel_elapsed_s"], cp["arrived"], cp["arrival_at"]
        arrival_effect = False
        intervals = list(cp["intervals"])
        if not skip and instance.status == ActivityStatus.RUNNING:
            if not arrived and (not cancel or cp.get("shared_position")) and start_online < end_online:
                remaining = cp["params"]["route"]["travel_s"] - travel
                travel += min(delta, remaining)
                if travel == cp["params"]["route"]["travel_s"]:
                    arrived, arrival_effect = True, True
                    plan = cp["params"].get("attendance")
                    if plan is not None and cp["params"]["route"]["travel_s"] == 0:
                        ready = timestamp(plan["ready_at"]) if plan["ready_at"] else None
                        start = timestamp(session["start_at"])
                        arrival = wire(class_arrival(cp["params"]["route"], start_online, remaining, start, ready))
                    else:
                        arrival = wire(start_online + timedelta(seconds=remaining))
            if arrived:
                begins = max(start_online, timestamp(session["start_at"]), timestamp(arrival))
                ends = min(now, timestamp(session["end_at"]))
                if begins < ends:
                    if intervals and intervals[-1]["end_at"] == wire(begins):
                        intervals[-1] = {**intervals[-1], "end_at": wire(ends)}
                    else:
                        intervals.append({"start_at": wire(begins), "end_at": wire(ends)})
        attended = sum(int((timestamp(item["end_at"]) - timestamp(item["start_at"])).total_seconds()) for item in intervals)
        next_cp = freeze({**cp, "last_tick": wire(now), "travel_elapsed_s": travel, "arrived": arrived,
            "arrival_at": arrival, "arrival_effect": arrival_effect, "intervals": intervals, "attended_s": attended,
            "sequence": cp["sequence"] + 1, "final": final, "ended_at": wire(now) if final else None,
            "completed_units": {"attended_s": attended}})
        if final and cp.get("shared_position") and travel and not arrived:
            # Class v1 routes carry total time and an untimed path. Partial
            # progress cannot prove which intermediate node was reached.
            next_cp = freeze({**next_cp, "commute_stop_unknown": True})
        phase = "skip" if skip else "attend" if arrived and now >= timestamp(session["start_at"]) else "wait" if arrived else "travel"
        staged = replace(instance, checkpoint=next_cp, elapsed_s=elapsed)
        effects = self._effects(staged, now)
        outcome = None
        duration = int((timestamp(session["end_at"]) - timestamp(session["start_at"])).total_seconds())
        if cancel:
            outcome = Outcome(OutcomeStatus.CANCELLED, attended > 0, "课次中止，保留实际参与区间（规则模拟）", ReasonCode.CANCELLED)
        elif final:
            outcome = Outcome(OutcomeStatus.SUCCEEDED, not skip and attended < duration,
                              "明确缺席（规则模拟）" if skip else "课次结束，按实际参与区间记录（规则模拟）")
        event = EventProposal("academics.class.cancelled" if cancel else "academics.class.completed" if final else "academics.class.progressed",
            SchemaVersion(1), freeze({"session_id": session["session_id"], "instance_id": instance.instance_id,
                "attended_s": attended, "decision": cp["params"]["decision"]}), (), Provenance.SIMULATED, audience)
        return ExecutorStep(phase, next_cp, elapsed, next_cp["completed_units"], effects, (event,), outcome)

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, elapsed_delta_s=elapsed_delta_s, cancel=False)

    def cancel(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, elapsed_delta_s=elapsed_delta_s, cancel=True)

    def validate_result(self, instance, result):
        cp = instance.checkpoint
        require(cp["schema_version"] == 1 and cp["final"] and result.progress.completed_units == cp["completed_units"], "invalid class result")
        participation_payload(instance, final=True)
        require(result.effects == self._effects(instance, result.occurred_at), "class result changed effects")
        if result.outcome.status == OutcomeStatus.SUCCEEDED:
            require(cp["params"]["decision"] == "skip" or result.occurred_at >= timestamp(cp["session"]["end_at"]), "class cannot complete before its end")
        else:
            require(result.outcome.status == OutcomeStatus.CANCELLED and result.outcome.partial == (cp["attended_s"] > 0), "invalid class cancellation")
        return True

    def abandon_resources(self, instance, result, now):
        return (Effect(instance.instance_id + ":abandon", instance.instance_id, "academics", "academics.abandon",
            freeze({"session_id": instance.checkpoint["session"]["session_id"]}), now, ()),) if instance.started_at is not None else ()


def propose_class(kernel, academics, scope, session_id, *, now, campus=None, decision="attend", reason="manual_decision", retry_key=None):
    """Explicit calendar opportunity; repeat calls share the kernel dedupe key."""
    session = academics.session_context(scope.owner_key, scope.context_key, session_id)
    route = None
    if decision == "attend":
        require(campus is not None, "class requires campus routing", ReasonCode.MISSING_DEPENDENCY)
        origin, _revision = campus.location(scope.owner_key)
        route, _origin_wait_s = campus_route_timing(campus, origin, session["place_id"], now,
            class_start=timestamp(session["start_at"]), class_end=timestamp(session["end_at"]))
    return _propose_quoted_class(kernel, academics, scope, session, now=now,
        decision=decision, reason=reason, route=route, retry_key=retry_key)


def _propose_quoted_class(kernel, academics, scope, session, *, now, decision, reason, route, retry_key, attendance=None):
    """Register the exact route selected by the caller and propose the class."""
    session_id = session["session_id"]
    template = class_session_template(session, decision=decision, reason=reason, route=route, attendance=attendance)
    key = (template.id, template.version)
    # Registration and its durable candidate must be visible to GC together.
    with academics.storage._lock:
        if key not in kernel.registry.templates:
            kernel.registry.register_template(template)
        academics._template_refs.setdefault((scope.owner_key, scope.context_key), set()).add(key)
        if retry_key is not None:
            scope = kernel._scope(scope, "kernel.propose")
            kernel.registry.creation_template(TemplateRef(template.id, template.version))
            snapshot = kernel._snapshot(scope, now)
            if kernel.registry.unavailable(template, snapshot.capabilities):
                return None  # A continuing outage must not append another rejection.
        departure = _pinned_departure(route)
        class_end = timestamp(session["end_at"])
        if departure is None:
            expires_at = max(moment(now) + timedelta(seconds=1), class_end)
        else:
            require(moment(now) < class_end, "class has already ended", ReasonCode.NO_TIME_WINDOW)
            expires_at = min(_departure_valid_until(route, departure) + datetime.resolution, class_end)
        return kernel.propose(scope, TemplateRef(template.id, template.version),
            opportunity_key=session_id if retry_key is None else "class-retry-" + digest((session_id, retry_key)),
            reason=template.triggers[0]["id"], now=now, expires_at=expires_at,
            target_ref=RecordRef("academics.session", session_id, session["revision"]))
