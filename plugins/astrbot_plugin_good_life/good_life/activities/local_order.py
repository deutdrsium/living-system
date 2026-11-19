"""A1 sample: explicitly simulated desk tidying, with no external side effects.

Component: tidy_desk@1 -> local_timed_units@1. Input slots: none. Requires:
local_order capability. Facts: local_order uniquely owns completed local units;
no mood/energy/wallet/location/academic effects and no model calls. Missing
capability makes the template unavailable. Cancellation retains completed units.
"""
from ..contracts.models import (
    ActivityStatus, ActivityTemplate, Effect, EventProposal, Outcome, OutcomeStatus, Provenance, ReasonCode, SchemaVersion, TemplateVersion
)
from ..kernel.codec import freeze, integer, moment, name, require
from ..kernel.gating import PreparedActivity
from ..kernel.lifecycle import ExecutorStep
from ..kernel.registry import ModuleSpec


TEMPLATE = ActivityTemplate(
    id="tidy_desk", version=TemplateVersion(1), executor="local_timed_units@1",
    requires=("local_order",), triggers=(freeze({"id": "tidy_requested", "type": "intent"}),),
    gating=freeze({"free_s": 60}), inputs=freeze({}),
    params=freeze({"duration_s": 180, "unit_s": 60, "label": "整理书桌（规则模拟）"}),
    presentation=freeze({"provenance": "simulated"}),
)
SPEC = ModuleSpec("local_order", "1", ("local_order.tidied_units",),
                  emits=("kernel.local_order.progressed", "kernel.activity.completed", "kernel.activity.cancelled"),
                  consumes=("local_order.unit_completed",))
DOMAIN_DDL = """CREATE TABLE life_local_order_units (
    owner_key TEXT PRIMARY KEY, completed_units INTEGER NOT NULL CHECK(completed_units>=0))"""


class LocalOrder:
    """The only writer of this demonstration domain's facts."""
    def install(self, storage):
        with storage.transaction() as db:
            row = db.execute("SELECT value FROM life_metadata WHERE key='local_order_schema_version'").fetchone()
            present = db.execute("SELECT sql FROM sqlite_master WHERE name='life_local_order_units'").fetchone()
            if row is None:
                require(present is None, "unclaimed local-order schema")
                db.execute(DOMAIN_DDL)
                db.execute("INSERT INTO life_metadata VALUES ('local_order_schema_version','1')")
            else:
                require(row[0] == "1" and present is not None
                        and " ".join(present[0].lower().split()) == " ".join(DOMAIN_DDL.lower().split()),
                        "unsupported local-order schema")

    def apply_effect(self, db, instance, effect):
        require(effect.target_owner == SPEC.name and effect.kind == "local_order.unit_completed",
                "local-order effect kind is invalid")
        require(effect.instance_id == instance.instance_id and set(effect.payload) == {"unit"}, "local-order effect instance mismatch")
        unit = integer(effect.payload["unit"], 1)
        completed = instance.checkpoint["completed_units"]["tidied_units"]
        require(unit <= completed and effect.effect_id == instance.instance_id + ":unit:" + str(unit),
                "effect is not backed by persisted local progress")
        db.execute("""INSERT INTO life_local_order_units VALUES (?,1)
            ON CONFLICT(owner_key) DO UPDATE SET completed_units=completed_units+1""", (instance.owner_key,))
        return True


class LocalTimedUnits:
    name, version, local_only = "local_timed_units", "1", True
    pausable_phases = ("tidying",)

    def validate(self, template):
        require(set(template.params) == {"duration_s", "unit_s", "label"}, "unsupported local-rule parameters")
        duration = integer(template.params["duration_s"], 1)
        unit = integer(template.params["unit_s"], 1)
        require(duration % unit == 0 and duration <= 86400, "local-rule duration must contain whole units and fit one day")
        name(template.params["label"])
        require(template.requires == ("local_order",), "local rule requires its unique effect owner")

    def prepare(self, template, now):
        return PreparedActivity("tidying", template.params["duration_s"], freeze({
            "schema_version": 1, "duration_s": template.params["duration_s"], "unit_s": template.params["unit_s"],
            "last_tick": None, "completed_units": {"tidied_units": 0}, "settled_units": 0,
        }), template.gating.get("resources", freeze({})))

    def start(self, instance, now):
        require(instance.started_at is None, "executor already started")
        return self.resume(instance, now)

    def resume(self, instance, now):
        # Restart preserves exactly the known progress, never inventing offline
        # desk-tidying work. Only online deltas after this UTC anchor accrue.
        return freeze({**instance.checkpoint, "last_tick": moment(now).isoformat().replace("+00:00", "Z")})

    def _step(self, instance, now, audience, *, cancel, elapsed_delta_s):
        now = moment(now)
        require(now >= instance.updated_at, "clock moved backwards", ReasonCode.STALE_VERSION)
        checkpoint = instance.checkpoint
        integer(elapsed_delta_s)
        if not cancel and elapsed_delta_s == 0:
            return ExecutorStep(instance.phase, checkpoint, instance.elapsed_s, checkpoint["completed_units"])
        elapsed = instance.elapsed_s
        if instance.status == ActivityStatus.RUNNING:
            elapsed = min(checkpoint["duration_s"], elapsed + elapsed_delta_s)
        else:
            require(elapsed_delta_s == 0, "stopped activity cannot accumulate time")
        anchor = now
        units = elapsed // checkpoint["unit_s"]
        effects = tuple(Effect(instance.instance_id + ":unit:" + str(unit), instance.instance_id,
            SPEC.name, "local_order.unit_completed", freeze({"unit": unit}), now, ())
            for unit in range(checkpoint["settled_units"] + 1, units + 1))
        complete = elapsed == checkpoint["duration_s"]
        outcome = None
        if cancel:
            outcome = Outcome(OutcomeStatus.CANCELLED, elapsed > 0, "整理中止，保留已完成部分（规则模拟）", ReasonCode.CANCELLED)
        elif complete:
            outcome = Outcome(OutcomeStatus.SUCCEEDED, False, "整理完成（规则模拟）")
        events = ()
        if effects or outcome is not None:
            event_type = ("kernel.activity.cancelled" if cancel else "kernel.activity.completed" if complete
                          else "kernel.local_order.progressed")
            events = (EventProposal(event_type, SchemaVersion(1), freeze({"instance_id": instance.instance_id,
                "completed_units": units, "elapsed_s": elapsed}), (), Provenance.SIMULATED, audience),)
        new_checkpoint = freeze({**checkpoint, "last_tick": anchor.isoformat().replace("+00:00", "Z"),
                                 "completed_units": {"tidied_units": units}, "settled_units": units})
        return ExecutorStep("tidying", new_checkpoint, elapsed, new_checkpoint["completed_units"], effects, events, outcome)

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, cancel=False, elapsed_delta_s=elapsed_delta_s)

    def cancel(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, cancel=True, elapsed_delta_s=elapsed_delta_s)

    def validate_result(self, instance, result):
        checkpoint = instance.checkpoint
        require(checkpoint["schema_version"] == 1, "unsupported local-rule checkpoint")
        require(result.progress.completed_units == checkpoint["completed_units"], "result units mismatch")
        require(instance.elapsed_s <= checkpoint["duration_s"] and checkpoint["completed_units"]["tidied_units"]
                == instance.elapsed_s // checkpoint["unit_s"], "progress not supported by local timer")
        if result.outcome.status == OutcomeStatus.SUCCEEDED:
            require(instance.elapsed_s == checkpoint["duration_s"] and not result.outcome.partial,
                    "proposal cannot claim unexecuted work")
        elif result.outcome.status == OutcomeStatus.CANCELLED:
            require(result.outcome.partial == (instance.elapsed_s > 0), "cancellation partial flag mismatch")
        return True


def register_local_order(registry):
    registry.register_module(SPEC, LocalOrder())
    registry.register_executor(LocalTimedUnits())
    return registry.register_template(TEMPLATE)
