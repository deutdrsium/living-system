"""Configured, explicitly simulated buy-drink component for A1.

No sources/providers/models/platform payments or inventory claims. Economy owns
all financial facts. purchase_local@2 reserves in the final start transaction.
The pinned @1 executor retains its awaiting_reservation checkpoint/timer for old
instances. Cancellation keeps paid stages and releases unused funds at commit.
"""
from ..contracts.models import (
    ActivityStatus, ActivityTemplate, Effect, EventProposal, Outcome, OutcomeStatus,
    Provenance, ReasonCode, RecordRef, SchemaVersion, TemplateVersion,
)
from ..economy.wallet import SPEC, Wallet, amount, currency
from ..kernel.codec import encode, freeze, integer, moment, require
from ..kernel.gating import PreparedActivity
from ..kernel.lifecycle import ExecutorStep
from ..kernel.resources import ResourcePreparation

AVAILABLE = "economy.available_minor"
PURCHASE_BUDGET = "economy.purchase_budget_minor"


def _validate_params(params):
    require(set(params) == {"currency", "reserve_minor", "payments"}, "unsupported purchase parameters")
    currency(params["currency"])
    limit = amount(params["reserve_minor"], 1)
    payments = params["payments"]
    require(type(payments) is tuple and 1 <= len(payments) <= 32, "purchase requires a bounded stage list")
    previous, total = 0, 0
    for payment in payments:
        require(set(payment) == {"at_s", "amount_minor"}, "invalid payment stage")
        at_s = integer(payment["at_s"], 1)
        require(previous < at_s <= 86400, "payment times must increase within one day")
        previous, total = at_s, total + amount(payment["amount_minor"], 1)
    require(total <= limit, "configured payments exceed reserved upper quote")
    return payments[-1]["at_s"]


def buy_drink_template(*, configured_currency, reserve_minor, payments, executor_version="2"):
    """All prices and stage timings come from an explicit configuration pack."""
    params = freeze({"currency": configured_currency, "reserve_minor": reserve_minor, "payments": payments})
    duration = _validate_params(params)
    require(executor_version in ("1", "2"), "unsupported purchase executor")
    return ActivityTemplate(
        id="buy_drink", version=TemplateVersion(int(executor_version)), executor="purchase_local@" + executor_version, requires=("economy",),
        triggers=(freeze({"id": "drink_requested", "type": "intent"}),),
        gating=freeze({"free_s": duration, "resources": {AVAILABLE: reserve_minor}}), inputs=freeze({}),
        params=params, presentation=freeze({"provenance": "simulated"}),
    )


def _commands(instance, *, reserve, start_step, end_step, release):
    cp = instance.checkpoint
    reservation_id = instance.instance_id + ":budget"
    commands = []

    def append(suffix, kind, payload, key):
        commands.append(freeze({"effect_id": instance.instance_id + ":" + suffix, "kind": kind,
                                "payload": payload, "business_key": encode(key)}))

    if reserve:
        payload = {"reservation_id": reservation_id, "amount_minor": cp["params"]["reserve_minor"], "review_at": cp["review_at"]}
        if instance.executor_version == "2":
            payload["expected_revision"] = integer(cp["reserve_revision"], 1)
        append("reserve", "reserve", payload, ("reserve", reservation_id))
    for index in range(start_step, end_step):
        step_id = str(index + 1)
        append("pay:" + step_id, "spend", {"reservation_id": reservation_id, "step_id": step_id,
            "amount_minor": cp["params"]["payments"][index]["amount_minor"]}, ("spend", reservation_id, step_id))
    if release:
        append("release", "release", {"reservation_id": reservation_id}, ("release", reservation_id))
    return tuple(commands)


def purchase_commands(instance):
    """Validate the persisted executor checkpoint before a domain accepts effects."""
    cp = instance.checkpoint
    require(instance.executor_version in ("1", "2") and cp["schema_version"] == int(instance.executor_version),
            "unsupported purchase checkpoint")
    duration = _validate_params(cp["params"])
    require(instance.planned_duration_s == duration and 0 <= instance.elapsed_s <= duration, "invalid purchase timer")
    require(type(cp["reserved"]) is bool and type(cp["released"]) is bool, "invalid reservation flags")
    completed = sum(payment["at_s"] <= instance.elapsed_s for payment in cp["params"]["payments"])
    require(cp["completed_units"] == {"paid_steps": completed} and (cp["reserved"] or completed == 0),
            "purchase progress not supported by reserved online timer")
    start_step = integer(cp["command_start_step"])
    require(start_step <= completed, "purchase command range is invalid")
    require(type(cp["command_reserve"]) is bool and (not cp["command_reserve"] or cp["reserved"]), "invalid reserve command")
    require(not cp["released"] or cp["reserved"], "cannot release an uncreated reservation")
    expected = _commands(instance, reserve=cp["command_reserve"], start_step=start_step, end_step=completed,
                         release=cp["released"])
    require(cp["commands"] == expected, "purchase commands differ from persisted progress")
    return expected


def _effects(instance, commands, now):
    return tuple(Effect(command["effect_id"], instance.instance_id, SPEC.name, "economy.purchase",
                        command["payload"], now, ()) for command in commands)


class PurchaseLocal:
    name, version, local_only = "purchase_local", "1", True
    accepts_new_instances = False  # Pinned historical instances only.
    pausable_phases = ("awaiting_reservation", "purchasing")

    def validate(self, template):
        duration = _validate_params(template.params)
        require(template.requires == ("economy",) and template.gating == {
            "free_s": duration, "resources": {AVAILABLE: template.params["reserve_minor"]}},
            "purchase requires the wallet and its configured upper quote")

    def prepare(self, template, now):
        duration = _validate_params(template.params)
        return PreparedActivity("awaiting_reservation", duration, freeze({
            "schema_version": 1, "params": template.params, "reserved": False, "released": False,
            "review_at": None,
            "completed_units": {"paid_steps": 0}, "command_start_step": 0,
            "command_reserve": False, "commands": (),
        }), freeze({PURCHASE_BUDGET: template.params["reserve_minor"]}))

    def start(self, instance, now):
        require(instance.started_at is None, "purchase already started")
        return self.resume(instance, now)

    def resume(self, instance, now):
        purchase_commands(instance)
        return instance.checkpoint

    def _step(self, instance, now, audience, *, cancel, elapsed_delta_s):
        from dataclasses import replace
        moment(now)
        require(now >= instance.updated_at, "clock moved backwards", ReasonCode.STALE_VERSION)
        integer(elapsed_delta_s)
        cp = instance.checkpoint
        purchase_commands(instance)
        if not cp["reserved"] and cancel:
            outcome = Outcome(OutcomeStatus.CANCELLED, False, "购买取消，尚未预留或付款（规则模拟）", ReasonCode.CANCELLED)
            return ExecutorStep(instance.phase, cp, 0, cp["completed_units"], outcome=outcome)
        reserve = not cp["reserved"]
        elapsed = instance.elapsed_s
        if not reserve and instance.status == ActivityStatus.RUNNING:
            elapsed = min(instance.planned_duration_s, elapsed + elapsed_delta_s)
        elif instance.status != ActivityStatus.RUNNING:
            require(elapsed_delta_s == 0, "stopped purchase cannot accumulate time")
        steps = sum(payment["at_s"] <= elapsed for payment in cp["params"]["payments"])
        complete = elapsed == instance.planned_duration_s
        if not reserve and elapsed == instance.elapsed_s and not cancel:
            return ExecutorStep(instance.phase, cp, elapsed, cp["completed_units"])
        if reserve:
            from datetime import timedelta
            cp = freeze({**cp, "review_at": (moment(now) + timedelta(seconds=instance.planned_duration_s)).isoformat()})
        commands = _commands(replace(instance, checkpoint=cp), reserve=reserve,
                             start_step=cp["completed_units"]["paid_steps"], end_step=steps, release=cancel or complete)
        checkpoint = freeze({**cp, "reserved": True, "released": cancel or complete,
            "completed_units": {"paid_steps": steps}, "command_start_step": cp["completed_units"]["paid_steps"],
            "command_reserve": reserve, "commands": commands})
        # Run the same persisted-progress validator before handing effects to A1.
        purchase_commands(replace(instance, checkpoint=checkpoint, elapsed_s=elapsed))
        outcome = None
        if cancel:
            outcome = Outcome(OutcomeStatus.CANCELLED, elapsed > 0, "购买取消，保留实际进度及已付款阶段（规则模拟）", ReasonCode.CANCELLED)
        elif complete:
            outcome = Outcome(OutcomeStatus.SUCCEEDED, False, "饮料购买流程完成（规则模拟）")
        event_type = "kernel.purchase.cancelled" if cancel else "kernel.purchase.completed" if complete else "kernel.purchase.progressed"
        events = (EventProposal(event_type, SchemaVersion(1), freeze({"instance_id": instance.instance_id,
            "paid_steps": steps, "reserved": True, "released": cancel or complete}), (), Provenance.SIMULATED, audience),)
        return ExecutorStep("purchasing", checkpoint, elapsed, checkpoint["completed_units"],
                            _effects(instance, commands, now), events, outcome)

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, cancel=False, elapsed_delta_s=elapsed_delta_s)

    def cancel(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, cancel=True, elapsed_delta_s=elapsed_delta_s)

    def abandon_resources(self, instance, result, now):
        # The checkpoint is an abandoned proposal, not a financial receipt.
        # The wallet releases its actual remaining amount, never these commands.
        if not instance.checkpoint["reserved"]:
            return ()
        return (Effect(instance.instance_id + ":abandon-release", instance.instance_id, SPEC.name,
                       "economy.abandon_release", freeze({"reservation_id": instance.instance_id + ":budget"}), now, ()),)

    def validate_result(self, instance, result):
        cp = instance.checkpoint
        commands = purchase_commands(instance)
        require(result.progress.completed_units == cp["completed_units"], "purchase result units mismatch")
        require(result.effects == _effects(instance, commands, result.occurred_at), "terminal proposal omitted or changed wallet effects")
        if result.outcome.status == OutcomeStatus.SUCCEEDED:
            require(instance.elapsed_s == instance.planned_duration_s and cp["released"] and not result.outcome.partial,
                    "purchase cannot claim unfinished work")
        else:
            require(result.outcome.status == OutcomeStatus.CANCELLED
                    and result.outcome.partial == (instance.elapsed_s > 0)
                    and (not cp["reserved"] or cp["released"]), "invalid purchase cancellation")
        return True


class AtomicPurchaseLocal(PurchaseLocal):
    version = "2"
    accepts_new_instances = True
    pausable_phases = ("purchasing",)

    def prepare(self, template, now):
        from dataclasses import replace
        prepared = super().prepare(template, now)
        return replace(prepared, phase="purchasing", checkpoint=freeze({
            **prepared.checkpoint, "schema_version": 2, "reserve_revision": None}))

    def prepare_resources(self, instance, now, snapshot):
        from dataclasses import replace
        from datetime import timedelta
        purchase_commands(instance)
        refs = (RecordRef("economy.reservation", instance.instance_id + ":budget", None),)
        if instance.checkpoint["reserved"]:
            return ResourcePreparation("purchasing", instance.checkpoint, reservation_refs=refs)
        value = snapshot.state.values.get(AVAILABLE)
        require(value is not None, "wallet snapshot unavailable", ReasonCode.MISSING_DEPENDENCY)
        cp = freeze({**instance.checkpoint, "reserved": True, "command_reserve": True,
                     "reserve_revision": integer(value.revision, 1),
                     "review_at": (moment(now) + timedelta(seconds=instance.planned_duration_s)).isoformat()})
        staged = replace(instance, checkpoint=cp)
        commands = _commands(staged, reserve=True, start_step=0, end_step=0, release=False)
        cp = freeze({**cp, "commands": commands})
        purchase_commands(replace(staged, checkpoint=cp))
        return ResourcePreparation("purchasing", cp, _effects(instance, commands, now), refs)


def register_buy_drink(registry, *, configured_currency, reserve_minor, payments, executor_version="2"):
    wallet = Wallet(configured_currency)
    registry.register_module(SPEC, wallet)
    registry.register_executor(PurchaseLocal())
    registry.register_executor(AtomicPurchaseLocal())
    templates = {}
    for version in ("1", "2"):
        template = buy_drink_template(configured_currency=configured_currency, reserve_minor=reserve_minor,
                                      payments=payments, executor_version=version)
        registry.register_template(template)
        templates[version] = template
    require(executor_version in templates, "unsupported purchase executor")
    return wallet, templates[executor_version]
