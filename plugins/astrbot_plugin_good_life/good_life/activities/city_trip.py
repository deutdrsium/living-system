"""Pure, pinned, explicitly simulated round-trip executor over the A1 kernel."""
from dataclasses import replace
from datetime import datetime, timedelta

from ..contracts.models import (ActivityStatus, ActivityTemplate, Effect, EventProposal, Outcome,
    OutcomeStatus, Provenance, ReasonCode, RecordRef, SchemaVersion, TemplateVersion)
from ..kernel.codec import KernelError, digest, encode, freeze, integer, moment, require, wire
from ..kernel.gating import PreparedActivity
from ..kernel.lifecycle import ExecutorStep
from ..kernel.resources import ResourcePreparation
from ..city.navigation import Segment, validate_segment

AVAILABLE = "economy.available_minor"
BUDGET = "city.secured_budget_minor"
LOCATION = "geo.city_location"


def _time(value):
    return moment(datetime.fromisoformat(value.replace("Z", "+00:00")))


def _uses_amap(params):
    return any(params[key]["provider"] == "amap-transit-v5@1" for key in ("outbound", "return"))


def _departure_allowed(params, now):
    quote = params["outbound"]
    departure = _time(quote["departure_at"])
    if _uses_amap(params):
        # Network work precedes preparation. These are estimated routes with
        # an explicit 60s activation window, covered by the pinned margin.
        return departure <= moment(now) <= departure + timedelta(seconds=60)
    return moment(now) == departure


def _departure_missed(params, now):
    quote = params["outbound"]
    if _uses_amap(params):
        return (moment(now) > _time(quote["departure_at"]) + timedelta(seconds=60)
                or any(q["provider"] == "amap-transit-v5@1" and moment(now) >= _time(q["valid_until"])
                       for q in (params["outbound"], params["return"])))
    return _time(quote["departure_at"]) < moment(now)


def segments(params):
    result = list(params["outbound"]["segments"])
    if params["stay_s"]:
        place = params["outbound"]["destination"]
        result.append(freeze(wire(Segment(place, place, "stay", params["stay_s"], 0, 0, "manual-stay"))))
    return tuple((*result, *params["return"]["segments"]))


def _validate(params):
    require(set(params) == {"outbound", "return", "stay_s", "reserve_minor", "currency"}, "invalid city trip parameters")
    integer(params["stay_s"])
    integer(params["reserve_minor"])
    outbound, inbound = params["outbound"], params["return"]
    require(outbound["origin"] == inbound["destination"] and outbound["destination"] == inbound["origin"],
            "trip must include the complete return route")
    for quote in (outbound, inbound):
        require(quote["currency"] == params["currency"] and quote["provider"] in ("offline-home-transit@1", "amap-transit-v5@1"),
                "unsupported city navigation provider")
        require(quote.get("fare_known", True) is True,
                "高德票价未知，无法预留预算；请重试或使用显式手工报价", ReasonCode.PROVIDER_UNAVAILABLE)
        if quote["provider"] == "amap-transit-v5@1":
            require(quote["currency"] == "CNY" and quote.get("requested_at") and quote.get("evidence")
                    and quote.get("assumptions"), "Amap quote lacks pinned evidence")
            require(_time(quote["valid_until"]) > _time(quote["requested_at"]), "invalid Amap quote validity")
        require(quote["segments"] and len(quote["segments"]) <= 256, "invalid route segment count")
        previous = quote["origin"]
        for item in quote["segments"]:
            validate_segment(Segment(**item))
            require(item["origin"] == previous, "discontinuous route")
            previous = item["destination"]
        require(previous == quote["destination"], "invalid quote endpoint")
        if quote["provider"] == "offline-home-transit@1":
            require(_time(quote["valid_until"]) > _time(quote["departure_at"]), "invalid offline quote validity")
    require(all(outbound[k] == inbound[k] for k in ("graph_id", "graph_version", "graph_digest", "attribution")),
            "round trip must pin one confirmed graph version")
    total_out = sum(s["duration_s"] for s in outbound["segments"])
    require(_time(inbound["departure_at"]) == _time(outbound["departure_at"]) + timedelta(seconds=total_out + params["stay_s"]),
            "return timetable does not follow outbound and stay")
    route = segments(params)
    duration = sum(s["duration_s"] for s in route)
    require(0 < duration <= 7 * 86400 and sum(s["cost_minor"] for s in route) <= params["reserve_minor"],
            "trip exceeds time or budget bounds")
    return duration


def city_trip_template(outbound, inbound, *, stay_s=0, reserve_minor=None):
    require(getattr(outbound, "fare_known", True) and getattr(inbound, "fare_known", True),
            "高德票价未知，无法预留预算；请重试或使用显式手工报价", ReasonCode.PROVIDER_UNAVAILABLE)
    total = outbound.cost_minor + inbound.cost_minor
    params = freeze({"outbound": wire(outbound), "return": wire(inbound), "stay_s": stay_s,
                     "reserve_minor": total if reserve_minor is None else reserve_minor, "currency": outbound.currency})
    duration = _validate(params)
    return ActivityTemplate("city_trip_" + digest(params), TemplateVersion(1), "city_trip@1", ("city", "economy"),
        (freeze({"id": "trip_requested", "type": "intent"}),),
        freeze({"free_s": duration, "resources": {AVAILABLE: params["reserve_minor"]} if params["reserve_minor"] else {}}), freeze({}), params,
        presentation=freeze({"provenance": "simulated"}))


def _position(params, elapsed):
    offset, completed = 0, 0
    place = params["outbound"]["origin"]
    for index, segment in enumerate(segments(params)):
        end = offset + segment["duration_s"]
        event_end = offset + segment["cancel_after_s"] if segment["cancel_after_s"] is not None else None
        if event_end is not None and elapsed >= event_end:
            return freeze({"status": "at_node", "place_id": segment["safe_stop"], "segment_index": index,
                           "progress_s": segment["cancel_after_s"]}), completed
        if elapsed < end:
            progress = max(0, elapsed - offset)
            stationary = progress == 0 or segment["mode"] in ("wait", "stay")
            location = {"status": "at_node" if stationary else "in_transit", "place_id": place,
                        "segment_index": index, "progress_s": progress}
            if not stationary:
                route = params["outbound"] if index < len(params["outbound"]["segments"]) else params["return"]
                location["route_id"] = route["route_id"]
            return freeze(location), completed
        place, offset, completed = segment["destination"], end, completed + 1
    return freeze({"status": "at_node", "place_id": place, "segment_index": completed, "progress_s": 0}), completed


def _commands(instance):
    cp, commands = instance.checkpoint, []
    reservation = instance.instance_id + ":budget"
    def add(suffix, kind, payload, key):
        commands.append(freeze({"effect_id": instance.instance_id + ":" + suffix, "kind": kind,
                               "payload": payload, "business_key": encode(key)}))
    if cp["command_reserve"]:
        add("reserve", "reserve", {"reservation_id": reservation, "amount_minor": cp["params"]["reserve_minor"],
            "review_at": cp["review_at"], "expected_revision": cp["reserve_revision"]}, ("reserve", reservation))
    offset = 0
    for index, segment in enumerate(segments(cp["params"])):
        end = offset + (segment["cancel_after_s"] or segment["duration_s"])
        step = str(index)
        if segment["cost_minor"] and cp["command_start_s"] <= offset < instance.elapsed_s:
            add("pay:" + step, "spend", {"reservation_id": reservation, "step_id": step,
                "amount_minor": segment["cost_minor"]}, ("spend", reservation, step))
        if segment["refund_minor"] and cp["command_start_s"] < end <= instance.elapsed_s:
            # Wallet transaction identities are deterministic, including replay.
            transaction = "wallet-tx-" + digest({"owner": instance.owner_key,
                "business_key": encode(("spend", reservation, step))})
            refund_key = instance.instance_id + ":refund:" + step
            add("refund:" + step, "refund", {"related_transaction_id": transaction, "refund_key": refund_key,
                "amount_minor": segment["refund_minor"]}, ("refund", transaction, refund_key))
        offset += segment["duration_s"]
    if cp["final"] and cp["reserved"]:
        add("release", "release", {"reservation_id": reservation}, ("release", reservation))
    return tuple(commands)


def trip_commands(instance):
    cp = instance.checkpoint
    require(cp["schema_version"] == 1 and instance.executor_version == "1", "unsupported city checkpoint")
    duration = _validate(cp["params"])
    require(instance.planned_duration_s == duration and 0 <= cp["command_start_s"] <= instance.elapsed_s <= duration,
            "invalid trip progress")
    require(type(cp["reserved"]) is bool and type(cp["command_reserve"]) is bool and type(cp["final"]) is bool,
            "invalid trip flags")
    require(not cp["command_reserve"] or cp["reserved"], "reservation command without budget")
    require(cp["reserved"] or cp["params"]["reserve_minor"] == 0 or instance.elapsed_s == 0, "unfunded travel cannot advance")
    position, completed = _position(cp["params"], instance.elapsed_s)
    if cp["location"]["status"] == "unknown":
        require(position["status"] == "in_transit" and cp["final"] and cp["cancel_requested"]
                and cp["location_effect"] and cp["command_start_s"] == instance.elapsed_s,
                "unknown stop requires an interrupted leg with no new progress")
        position = freeze({**position, "status": "unknown"})
    require(cp["location"] == position and cp["completed_units"] == {"segments": completed}, "unproven location/progress")
    require(cp["commands"] == _commands(instance), "financial commands differ from persisted trip progress")
    return cp["commands"]


def trip_effects(instance, now):
    cp = instance.checkpoint
    effects = [Effect(c["effect_id"], instance.instance_id, "economy", "economy.city_trip", c["payload"], now, ())
               for c in trip_commands(instance)]
    if cp["location_effect"]:
        effects.append(Effect(instance.instance_id + ":location:" + str(cp["location_revision"]), instance.instance_id,
            "city", "city.location", freeze({"location": cp["location"], "expected_revision": cp["location_revision"] - 1}), now, ()))
    return tuple(effects)


class CityTrip:
    name, version, local_only = "city_trip", "1", True
    pausable_phases = ("at_stop",)

    def __init__(self, *, settle_only=False):
        self.settle_only = settle_only

    def validate(self, template):
        duration = _validate(template.params)
        require(template.requires == ("city", "economy") and template.gating == {
            "free_s": duration, "resources": {AVAILABLE: template.params["reserve_minor"]} if template.params["reserve_minor"] else {}}, "invalid trip gate")

    @staticmethod
    def _fresh(params, now):
        # Only new activation checks TTL. Recovery uses committed route facts.
        for key in ("outbound", "return"):
            quote = params[key]
            if quote["provider"] == "amap-transit-v5@1":
                require(_time(quote["requested_at"]) <= moment(now) < _time(quote["valid_until"]),
                        "高德报价已过期或请求时间无效，请重新报价", ReasonCode.NO_TIME_WINDOW)

    @classmethod
    def check_departure(cls, params, now):
        cls._fresh(params, now)
        require(_departure_allowed(params, now),
                "出发时间已变，请重新获取高德报价" if _uses_amap(params)
                else "出发时间已变，请重新获取离线报价", ReasonCode.NO_TIME_WINDOW)

    def prepare(self, template, now):
        duration = _validate(template.params)
        self.check_departure(template.params, now)
        location, completed = _position(template.params, 0)
        return PreparedActivity("at_stop", duration, freeze({"schema_version": 1, "params": template.params,
            "reserved": False, "command_reserve": False, "reserve_revision": None, "review_at": None,
            "commands": (), "command_start_s": 0, "location": location, "location_revision": 0,
            "location_effect": False, "completed_units": {"segments": completed}, "final": False,
            "cancel_requested": False, "explanations": ()}),
            freeze({BUDGET: template.params["reserve_minor"]} if template.params["reserve_minor"] else {}))

    def start(self, instance, now):
        params = instance.checkpoint["params"]
        self._fresh(params, now)
        require(_departure_allowed(params, now),
                "不在高德报价的启动窗口内，请重新报价" if _uses_amap(params)
                else "出发时间已变，请重新获取离线报价", ReasonCode.NO_TIME_WINDOW)
        return self.resume(instance, now)

    def resume(self, instance, now):
        trip_commands(instance)
        # A1 resumes only online time: no transit or spending fabricated offline.
        return freeze({**instance.checkpoint, "location_effect": False})

    def remaining_duration_s(self, instance, now):
        remaining = max(0, instance.planned_duration_s - instance.elapsed_s)
        if not instance.checkpoint["cancel_requested"]:
            return remaining
        elapsed, offset = instance.elapsed_s, 0
        for segment in segments(instance.checkpoint["params"]):
            boundary = offset + segment["duration_s"]
            stop = offset + segment["cancel_after_s"] if segment["cancel_after_s"] is not None else boundary
            if elapsed < stop:
                return max(0, stop - elapsed)
            offset = boundary
        return 0

    def prepare_resources(self, instance, now, snapshot):
        cp = instance.checkpoint
        location = snapshot.state.values.get(LOCATION)
        require(location and location.quality.value == "valid" and location.value == cp["location"],
                "离线行程起点已改变或未知", ReasonCode.STALE_VERSION)
        if cp["reserved"] or not cp["params"]["reserve_minor"]:
            return ResourcePreparation(instance.phase, freeze({**cp, "location_revision": location.revision}))
        available = snapshot.state.values.get(AVAILABLE)
        require(available is not None, "wallet unavailable", ReasonCode.MISSING_DEPENDENCY)
        cp = freeze({**cp, "reserved": True, "command_reserve": True, "reserve_revision": available.revision,
            "location_revision": location.revision,
            "review_at": wire(moment(now) + timedelta(seconds=instance.planned_duration_s + 1))})
        staged = replace(instance, checkpoint=cp)
        cp = freeze({**cp, "commands": _commands(staged)})
        return ResourcePreparation(instance.phase, cp, trip_effects(replace(staged, checkpoint=cp), now),
            (RecordRef("economy.reservation", instance.instance_id + ":budget", None),))

    def _step(self, instance, now, audience, *, elapsed_delta_s, cancel):
        cp = instance.checkpoint
        trip_commands(instance)
        require(moment(now) >= instance.updated_at, "trip clock moved backwards", ReasonCode.STALE_VERSION)
        delta = integer(elapsed_delta_s)
        if instance.status != ActivityStatus.RUNNING:
            require(delta == 0, "stopped trip cannot advance")
        if cancel and instance.status == ActivityStatus.PREPARING:
            explanations = list(cp["explanations"])
            missed = ("高德报价的启动窗口或有效期已过" if _uses_amap(cp["params"])
                      else "固定出发时刻已错过")
            explanations.append(freeze({"reason_code": "no_time_window", "message":
                missed + "，原行程已取消；请重新报价（规则模拟）"}))
            next_cp = freeze({**cp, "command_reserve": False, "command_start_s": 0,
                "final": True, "cancel_requested": True, "explanations": explanations,
                "location_effect": False})
            staged = replace(instance, checkpoint=next_cp, elapsed_s=0)
            next_cp = freeze({**next_cp, "commands": _commands(staged)})
            staged = replace(staged, checkpoint=next_cp)
            outcome = Outcome(OutcomeStatus.CANCELLED, False,
                missed + "，行程未开始且未扣费（规则模拟）", ReasonCode.CANCELLED)
            event = EventProposal("city.trip.cancelled", SchemaVersion(1), freeze({
                "instance_id": instance.instance_id, "location": cp["location"],
                "explanations": explanations, "completed_segments": cp["completed_units"]["segments"]}),
                (), Provenance.SIMULATED, audience)
            return ExecutorStep("at_stop", next_cp, 0, next_cp["completed_units"],
                                trip_effects(staged, now), (event,), outcome)
        end = min(instance.planned_duration_s, instance.elapsed_s + delta)
        requested = cancel or cp["cancel_requested"]
        explanations = list(cp["explanations"])
        offset, terminal_cancel = 0, False
        for segment in segments(cp["params"]):
            boundary = offset + segment["duration_s"]
            # Cancellation requests stop at the next confirmed endpoint. They
            # remain running until enough online time reaches that safe stop.
            if cp["cancel_requested"] and instance.elapsed_s <= offset:
                end, terminal_cancel = min(end, offset), True
                break
            if segment["cancel_after_s"] is not None and end >= offset + segment["cancel_after_s"]:
                end, terminal_cancel = offset + segment["cancel_after_s"], True
                explanations.append(freeze({"reason_code": "cancelled", "message": "交通服务中止，已停靠 " + segment["safe_stop"] + "，未发生的后续路程不扣费（规则模拟）"}))
                break
            if cp["cancel_requested"] and instance.elapsed_s < boundary <= end:
                end, terminal_cancel = boundary, True
                break
            if segment["delay_s"] and instance.elapsed_s <= offset < end:
                explanations.append(freeze({"reason_code": "provider_unavailable", "message":
                    f"手工交通事件延误 {segment['delay_s']} 秒；往返门控已计入延误，来源 {segment['source']}（规则模拟）"}))
            if end < boundary:
                break
            offset = boundary
        position, completed = _position(cp["params"], end)
        unknown_stop = cancel and self.settle_only and position["status"] == "in_transit"
        if unknown_stop:
            # Match campus commute cancellation: keep the last node as evidence only.
            position = freeze({**position, "status": "unknown"})
        if cancel and position["status"] != "in_transit":
            terminal_cancel = True
        if requested and not cp["cancel_requested"] and not unknown_stop:
            explanations.append(freeze({"reason_code": "cancelled", "message":
                "已在当前可停靠点取消，保留已发生车费（规则模拟）" if terminal_cancel else
                "已请求取消，将在当前路段的下一可停靠点结束，保留已发生车费（规则模拟）"}))
        final = terminal_cancel or end == instance.planned_duration_s
        if end == instance.elapsed_s and not final and requested == cp["cancel_requested"]:
            return ExecutorStep(instance.phase, cp, end, cp["completed_units"])
        location_changed = instance.started_at is not None and (end != instance.elapsed_s or unknown_stop)
        next_cp = freeze({**cp, "command_reserve": False, "command_start_s": instance.elapsed_s,
            "final": final, "cancel_requested": requested, "explanations": explanations, "location": position,
            "location_revision": cp["location_revision"] + int(location_changed),
            "location_effect": location_changed, "completed_units": {"segments": completed}})
        staged = replace(instance, checkpoint=next_cp, elapsed_s=end)
        next_cp = freeze({**next_cp, "commands": _commands(staged)})
        staged = replace(staged, checkpoint=next_cp)
        outcome = None
        if terminal_cancel:
            stop_message = "行程已取消，停止位置未确认" if unknown_stop else "行程已在可停靠点取消"
            outcome = Outcome(OutcomeStatus.CANCELLED, end > 0,
                stop_message + "；保留已发生车费，退款另记流水，释放未用预留（规则模拟）", ReasonCode.CANCELLED)
        elif final:
            outcome = Outcome(OutcomeStatus.SUCCEEDED, False, "完整往返已完成，实际分段结算并释放剩余预留（规则模拟）")
        event = EventProposal("city.trip.cancelled" if terminal_cancel else "city.trip.completed" if final else "city.trip.progressed",
            SchemaVersion(1), freeze({"instance_id": instance.instance_id, "location": position,
                "explanations": explanations, "completed_segments": completed}), (), Provenance.SIMULATED, audience)
        phase = "unknown_stop" if unknown_stop else "at_stop" if position["status"] == "at_node" else "in_transit"
        return ExecutorStep(phase, next_cp, end, next_cp["completed_units"], trip_effects(staged, now), (event,), outcome)

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, elapsed_delta_s=elapsed_delta_s, cancel=False)

    def cancel(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, elapsed_delta_s=0 if self.settle_only else elapsed_delta_s, cancel=True)

    def validate_result(self, instance, result):
        cp = instance.checkpoint
        require(cp["final"] and result.effects == trip_effects(instance, result.occurred_at)
                and result.progress.completed_units == cp["completed_units"], "invalid city result effects/progress")
        if result.outcome.status == OutcomeStatus.SUCCEEDED:
            require(instance.elapsed_s == instance.planned_duration_s and cp["location"]["place_id"] == cp["params"]["outbound"]["origin"]
                    and not result.outcome.partial, "unfinished round trip cannot succeed")
        else:
            require(result.outcome.status == OutcomeStatus.CANCELLED and cp["location"]["status"] in ("at_node", "unknown")
                    and result.outcome.partial == (instance.elapsed_s > 0), "invalid trip cancellation")
        return True

    def abandon_resources(self, instance, result, now):
        return (Effect(instance.instance_id + ":abandon-release", instance.instance_id, "economy",
            "economy.abandon_release", freeze({"reservation_id": instance.instance_id + ":budget"}), now, ()),) if instance.checkpoint["reserved"] else ()


class CityActivities:
    """World-side recovery wrapper for the opt-in city component.

    A missed fixed departure is a known local planning fact, not an unknown
    external operation. Retire that consumed preparing instance before asking
    the generic kernel to recover the remaining active instances.
    """
    def __init__(self, kernel):
        self.kernel = kernel

    def __getattr__(self, name):
        return getattr(self.kernel, name)

    def _retire_missed_preparing(self, scope, now):
        retired = []
        for instance in self.kernel.repo.active(scope.owner_key):
            if (instance.context_key == scope.context_key and instance.template_id.startswith("city_trip_")
                    and instance.status == ActivityStatus.PREPARING
                    and _departure_missed(instance.checkpoint["params"], now)
                    and self.kernel.repo.pending_result(scope.owner_key, instance.instance_id) is None):
                # Leave a staged terminal proposal to kernel.recover/tick. A
                # second cancel must not replace or block its original result.
                retired.append(self.kernel.cancel(scope, instance.instance_id,
                    expected_revision=instance.revision,
                    command_id="city-missed-departure-" + instance.instance_id + "-" + str(instance.revision),
                    now=now))
        return tuple(retired)

    def settle_hosted(self, *, now, settle_only):
        for scope in self.kernel.access.scopes():
            if not self.kernel.access.authorize(scope, "kernel.cancel"):
                continue
            if not settle_only:
                self._retire_missed_preparing(scope, now)
                continue
            for instance in self.kernel.repo.active(scope.owner_key):
                if (instance.context_key == scope.context_key and instance.template_id.startswith("city_trip_")
                        and instance.status != ActivityStatus.UNKNOWN
                        and self.kernel.repo.pending_result(scope.owner_key, instance.instance_id) is None):
                    self.kernel.cancel(scope, instance.instance_id, expected_revision=instance.revision,
                        command_id=f"disabled-trip-{instance.instance_id}-{instance.revision}", now=now)

    def recover(self, scope, *, now):
        retired = self._retire_missed_preparing(scope, now)
        return (*retired, *self.kernel.recover(scope, now=now))

    def tick(self, *, now):
        if not self.kernel.config.enabled or self.kernel.closed:
            return self.kernel.tick(now=now)
        now = moment(now)
        retired, failures = 0, []
        scopes = self.kernel.access.scopes()
        require(type(scopes) is tuple and len(scopes) <= 10000, "invalid kernel scope inventory")
        for scope in scopes:
            try:
                retired += len(self._retire_missed_preparing(scope, now))
            except KernelError as error:
                failures.append(error.code)
            except Exception as error:
                # Match the kernel's scope isolation and private-error policy.
                # Shutdown/cancellation BaseExceptions still propagate.
                failures.append("unexpected_error:" + type(error).__name__)
        result = self.kernel.tick(now=now)
        return {**result, "city_missed_departures": retired, "failures": (*failures, *result["failures"])}
