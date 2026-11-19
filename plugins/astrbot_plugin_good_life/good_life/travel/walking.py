"""Versioned kernel walk executor. Route and clock progress are durable facts."""
from dataclasses import replace
from datetime import datetime, timedelta

from ..city.navigation import Segment, validate_segment
from ..contracts.models import (ActivityStatus, ActivityTemplate, Effect, Outcome,
                                OutcomeStatus, ReasonCode, TemplateRef, TemplateVersion)
from ..kernel.codec import digest, freeze, integer, moment, require, wire
from ..kernel.gating import PreparedActivity
from ..kernel.lifecycle import ExecutorStep
from ..kernel.resources import ResourcePreparation
from .position import STATE_KEY

TRIGGER = "campus_walk_requested"
EFFECT = "campus.walk_position"


def walk_template(quote, position_revision):
    params = freeze({"route": wire(quote), "position_revision": integer(position_revision, 1)})
    return ActivityTemplate("campus_walk_" + digest(params), TemplateVersion(1), "campus_walk@1", ("campus",),
        (freeze({"id": TRIGGER, "type": "intent"}),), freeze({"free_s": quote.duration_s, "resources": {}}),
        freeze({}), params, presentation=freeze({"provenance": "simulated"}))


def _cursor(route, elapsed):
    place = route["origin"]
    for index, segment in enumerate(route["segments"]):
        duration = segment["duration_s"]
        if elapsed < duration:
            stationary = elapsed == 0 or segment["mode"] == "wait"
            return freeze({"status": "at_node" if stationary else "in_transit",
                           "place_id": place if stationary else None, "last_confirmed_node": place,
                           "route_id": route["route_id"], "segment_index": index, "progress_s": elapsed})
        elapsed -= duration
        place = segment["destination"]
    return freeze({"status": "at_node", "place_id": place, "last_confirmed_node": place,
                   "route_id": route["route_id"], "segment_index": len(route["segments"]), "progress_s": 0})


def _effects(instance, now):
    cp = instance.checkpoint
    if not cp["position_effect"]:
        return ()
    cursor = _cursor(cp["params"]["route"], instance.elapsed_s)
    if cp["cancelled"] and cursor["status"] == "in_transit":
        cursor = freeze({**cursor, "status": "unknown"})
    revision = cp["position_revision"]
    return (Effect(f"{instance.instance_id}:walk-position:{revision}", instance.instance_id, "campus", EFFECT,
                   freeze({**cursor, "expected_revision": revision - 1}), now, ()),)


def apply_walk_effect(position, db, instance, effect):
    require(instance.template_id.startswith("campus_walk_") and instance.executor_version == "1"
            and effect in _effects(instance, effect.effective_at), "walk position lacks checkpoint evidence")
    position.commit(db, instance.owner_key, **effect.payload, effect_id=effect.effect_id)
    return True


def propose_walk(kernel, campus, scope, destination, *, now, opportunity_key):
    """Quote from the confirmed stop and propose one explicitly authorized walk."""
    scope = kernel._scope(scope, "kernel.propose")
    quote, revision = campus.plan_walk(scope.owner_key, destination, departure_at=now)
    template = walk_template(quote, revision)
    with kernel.storage._lock:
        if (template.id, template.version) not in kernel.registry.templates:
            kernel.registry.register_template(template)
        return kernel.propose(scope, TemplateRef(template.id, template.version), opportunity_key=opportunity_key,
                              reason=TRIGGER, now=now, expires_at=quote.valid_until)


class CampusWalk:
    name, version, local_only = "campus_walk", "1", True
    pausable_phases = ("at_stop",)

    def __init__(self, *, settle_only=False):
        self.accepts_new_instances = not settle_only

    def _active(self):
        require(self.accepts_new_instances, "capability_disabled", ReasonCode.MISSING_DEPENDENCY)

    def validate(self, template):
        params = template.params
        require(template.requires == ("campus",) and set(params) == {"route", "position_revision"}, "invalid walk plan")
        integer(params["position_revision"], 1)
        route = params["route"]
        require(route["segments"], "invalid walk segment count")
        place, duration = route["origin"], 0
        for segment in route["segments"]:
            validate_segment(Segment(**segment))
            require(segment["origin"] == place and segment["mode"] in ("walk", "wait")
                    and not any(segment[key] for key in ("cost_minor", "cancel_after_s", "delay_s", "refund_minor"))
                    and (segment["destination"] == place) == (segment["mode"] == "wait"), "invalid campus segment")
            place = segment["destination"]
            duration += segment["duration_s"]
        require(place == route["destination"] and 0 < duration <= 86400
                and template.gating == {"free_s": duration, "resources": {}}, "invalid walk duration or endpoint")

    @staticmethod
    def _check_departure(params, now):
        # Same verified subsecond margin as the confirmed campus adapter.
        departure = datetime.fromisoformat(params["route"]["departure_at"])
        valid_until = datetime.fromisoformat(params["route"]["valid_until"])
        require(departure <= moment(now) < valid_until, "campus walk needs a fresh quote", ReasonCode.NO_TIME_WINDOW)

    def prepare(self, template, now):
        self._active()
        self._check_departure(template.params, now)
        return PreparedActivity("at_stop", template.gating["free_s"], freeze({"schema_version": 1,
            "params": template.params, "position_revision": template.params["position_revision"],
            "position_effect": False, "cancelled": False, "completed_units": {"segments": 0}}), freeze({}))

    def start(self, instance, now):
        self._active()
        self._check_departure(instance.checkpoint["params"], now)
        return instance.checkpoint

    def resume(self, instance, now):
        self._active()
        require(instance.checkpoint["schema_version"] == 1, "unsupported walk checkpoint")
        return instance.checkpoint

    def prepare_resources(self, instance, now, snapshot):
        cp = instance.checkpoint
        observed = snapshot.state.values.get(STATE_KEY)
        require(observed is not None and observed.quality.value == "valid" and observed.value is not None,
                "shared position is unavailable", ReasonCode.MISSING_DEPENDENCY)
        current = observed.value
        require(current["status"] != "unknown", "current stop needs confirmation", ReasonCode.MISSING_DEPENDENCY)
        require(current["revision"] == cp["position_revision"], "walk position changed", ReasonCode.STALE_VERSION)
        route = cp["params"]["route"]
        expected = _cursor(route, instance.elapsed_s)
        keys = ("status", "place_id", "last_confirmed_node", "route_id", "segment_index", "progress_s")
        if instance.elapsed_s == 0:
            keys = ("status", "place_id")
        require(all(current[key] == expected[key] for key in keys), "walk cursor changed", ReasonCode.STALE_VERSION)
        return ResourcePreparation(instance.phase, cp)

    def _step(self, instance, now, elapsed_delta_s, *, cancel):
        cp = instance.checkpoint
        end = min(instance.planned_duration_s, instance.elapsed_s + integer(elapsed_delta_s))
        if end == instance.elapsed_s and not cancel:
            return ExecutorStep(instance.phase, cp, end, cp["completed_units"])
        cursor = _cursor(cp["params"]["route"], end)
        uncertain = cancel and cursor["status"] == "in_transit"
        moved = end != instance.elapsed_s or uncertain
        following = freeze({**cp, "position_revision": cp["position_revision"] + int(moved),
                            "position_effect": moved, "cancelled": cancel,
                            "completed_units": {"segments": cursor["segment_index"]}})
        outcome = None
        if cancel:
            outcome = Outcome(OutcomeStatus.CANCELLED, end > 0,
                              "停止位置未确认" if uncertain else "已在确认节点停止", ReasonCode.CANCELLED)
        elif end == instance.planned_duration_s:
            outcome = Outcome(OutcomeStatus.SUCCEEDED, False, "步行完成（规则模拟）")
        effects = _effects(replace(instance, checkpoint=following, elapsed_s=end), now)
        phase = "unknown_stop" if uncertain else "in_transit" if cursor["status"] == "in_transit" else "at_stop"
        return ExecutorStep(phase, following, end, following["completed_units"], effects, (), outcome)

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        self._active()
        require(instance.status == ActivityStatus.RUNNING, "walk is not running")
        return self._step(instance, now, elapsed_delta_s, cancel=False)

    def cancel(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, elapsed_delta_s if self.accepts_new_instances else 0, cancel=True)

    def validate_result(self, instance, result):
        cp = instance.checkpoint
        require(result.effects == _effects(instance, result.occurred_at)
                and result.progress.completed_units == cp["completed_units"], "invalid walk effects or progress")
        if cp["cancelled"]:
            require(result.outcome.status == OutcomeStatus.CANCELLED and result.outcome.partial == (instance.elapsed_s > 0),
                    "walk cancellation does not match checkpoint")
        else:
            require(result.outcome.status == OutcomeStatus.SUCCEEDED and instance.elapsed_s == instance.planned_duration_s,
                    "walk is incomplete")
        return True
