"""Pure simulated study executor. Academic chunks persist across activity IDs."""
from dataclasses import replace
from datetime import timedelta

from ..contracts.models import (ActivityStatus, ActivityTemplate, Effect, EventProposal, Outcome,
    OutcomeStatus, Provenance, ReasonCode, RecordRef, SchemaVersion, TemplateRef, TemplateVersion)
from ..kernel.codec import digest, freeze, integer, moment, name, require, wire
from ..kernel.gating import PreparedActivity
from ..kernel.lifecycle import ExecutorStep
from ..kernel.resources import ResourcePreparation


def eligibility_key(target, params=None):
    # New keys match the full template identity, including pacing. Keep the old
    # target-only key for pinned templates created before proposal isolation.
    identity = params if params is not None and "opportunity_key" in params else target
    return "academics.study." + digest(identity)[:40]


def study_template(target, *, session_s=1500, efficiency=1000, load=0.5, evaluation="fixed_rule_v1", opportunity_key=None):
    require(1 <= integer(session_s, 1) <= 2400 and 250 <= integer(efficiency, 1) <= 1000, "invalid study pacing")
    remaining = target["chunk"]["remaining_work_s"]
    duration = min(session_s, (remaining * 1000 + efficiency - 1) // efficiency)
    params = freeze({"target": target, "duration_s": duration, "efficiency": efficiency,
        "load": load, "evaluation": evaluation, **({"opportunity_key": opportunity_key} if opportunity_key is not None else {})})
    template = ActivityTemplate("study_" + digest(params)[:40], TemplateVersion(1), "study_local@1", ("academics",),
        (freeze({"id": "study_requested", "type": "intent"}),),
        freeze({"free_s": duration, "resources": {eligibility_key(target, params): 1}}), freeze({}), params,
        presentation=freeze({"provenance": "simulated"}))
    StudyLocal().validate(template)
    return template


def study_payload(instance):
    cp, params = instance.checkpoint, instance.checkpoint["params"]
    chunk = params["target"]["chunk"]
    work = min(chunk["required_work_s"], chunk["completed_work_s"] + instance.elapsed_s * params["efficiency"] // 1000)
    require(0 <= instance.elapsed_s <= params["duration_s"], "study elapsed exceeds plan")
    require(cp["completed_units"] == {"work_s": work - chunk["completed_work_s"]}, "study work lacks online evidence")
    return freeze({**chunk, "completed_work_s": work, "remaining_work_s": chunk["required_work_s"] - work,
        "status": "completed" if work == chunk["required_work_s"] else "in_progress" if work else "pending",
        "evaluation": params["evaluation"] + (":chunk_complete" if work == chunk["required_work_s"] else ":partial_work" if work else ":no_work"),
        "instance_id": instance.instance_id, "sequence": cp["sequence"], "active": not cp["final"]})


class StudyLocal:
    name, version, local_only = "study_local", "1", True
    pausable_phases = ("study",)

    def validate(self, template):
        require(template.requires == ("academics",), "study requires academics")
        params = template.params
        require(set(params) in ({"target", "duration_s", "efficiency", "load", "evaluation"},
            {"target", "duration_s", "efficiency", "load", "evaluation", "opportunity_key"}), "invalid study parameters")
        if "opportunity_key" in params:
            name(params["opportunity_key"])
        require(1 <= integer(params["duration_s"], 1) <= 2400 and 250 <= integer(params["efficiency"], 1) <= 1000, "invalid study duration/efficiency")
        require(type(params["load"]) in (int, float) and 0 <= params["load"] <= 1, "invalid study load")
        require(params["evaluation"] in ("fixed_rule_v1", "shadow_energy_v1"), "invalid evaluation rule")
        target = params["target"]
        require(set(target) == {"chunk", "assignment", "unit"}, "invalid study target")
        chunk = target["chunk"]
        require(chunk["assignment_id"] == target["assignment"]["assignment_id"] and chunk["required_work_s"] > chunk["completed_work_s"] >= 0,
            "invalid study chunk")
        require(chunk["remaining_work_s"] == chunk["required_work_s"] - chunk["completed_work_s"], "invalid remaining work")

    def prepare(self, template, now):
        self.validate(template)
        return PreparedActivity("study", template.params["duration_s"], freeze({"schema_version": 1,
            "params": template.params, "last_tick": None, "sequence": 0, "final": False, "completed_units": {"work_s": 0}}), template.gating["resources"])

    def start(self, instance, now):
        require(instance.started_at is None, "study already started")
        return self.resume(instance, now)

    def resume(self, instance, now):
        return freeze({**instance.checkpoint, "last_tick": wire(moment(now))})

    def prepare_resources(self, instance, now, snapshot):
        chunk = instance.checkpoint["params"]["target"]["chunk"]
        require(instance.target_ref == RecordRef("academics.chunk", chunk["chunk_id"], chunk["revision"] + 1), "study target mismatch")
        effects = () if instance.reservation_refs else (Effect(instance.instance_id + ":claim", instance.instance_id, "academics",
            "academics.study_claim", chunk, now, ()),)
        return ResourcePreparation("study", instance.checkpoint, effects, (RecordRef("academics.study", chunk["chunk_id"], None),))

    @staticmethod
    def _effects(instance, now):
        if instance.started_at is None:
            return ()
        return (Effect(instance.instance_id + ":progress:" + str(instance.checkpoint["sequence"]), instance.instance_id,
            "academics", "academics.study_progress", study_payload(instance), now, ()),)

    def _step(self, instance, now, audience, *, elapsed_delta_s, cancel):
        cp, params = instance.checkpoint, instance.checkpoint["params"]
        now = moment(now)
        require(now >= instance.updated_at, "study clock moved backwards", ReasonCode.STALE_VERSION)
        delta = integer(elapsed_delta_s)
        require(instance.status == ActivityStatus.RUNNING or delta == 0, "stopped study cannot progress")
        if not delta and not cancel:
            return ExecutorStep("study", cp, instance.elapsed_s, cp["completed_units"])
        elapsed = min(params["duration_s"], instance.elapsed_s + delta)
        final = cancel or elapsed == params["duration_s"]
        work = min(params["target"]["chunk"]["remaining_work_s"], elapsed * params["efficiency"] // 1000)
        next_cp = freeze({**cp, "last_tick": wire(now), "sequence": cp["sequence"] + 1, "final": final, "completed_units": {"work_s": work}})
        staged = replace(instance, elapsed_s=elapsed, checkpoint=next_cp)
        outcome = (Outcome(OutcomeStatus.CANCELLED, work > 0, "学习中断，保留分块进度（规则模拟）", ReasonCode.CANCELLED) if cancel else
            Outcome(OutcomeStatus.SUCCEEDED, work < params["target"]["chunk"]["remaining_work_s"], "本次学习结束，继续原作业分块（规则模拟）") if final else None)
        event = EventProposal("academics.study.cancelled" if cancel else "academics.study.completed" if final else "academics.study.progressed",
            SchemaVersion(1), freeze({"instance_id": instance.instance_id, "chunk_id": params["target"]["chunk"]["chunk_id"],
                "work_s": work, "load": params["load"], "evaluation": params["evaluation"]}), (), Provenance.SIMULATED, audience)
        return ExecutorStep("study", next_cp, elapsed, next_cp["completed_units"], self._effects(staged, now), (event,), outcome)

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, elapsed_delta_s=elapsed_delta_s, cancel=False)

    def cancel(self, instance, now, audience, *, elapsed_delta_s):
        return self._step(instance, now, audience, elapsed_delta_s=elapsed_delta_s, cancel=True)

    def validate_result(self, instance, result):
        cp = instance.checkpoint
        require(cp["schema_version"] == 1 and cp["final"] and result.progress.completed_units == cp["completed_units"], "invalid study result")
        payload = study_payload(instance)
        require(result.effects == self._effects(instance, result.occurred_at), "study result effects changed")
        require(result.outcome.partial == (cp["completed_units"]["work_s"] > 0 if result.outcome.status == OutcomeStatus.CANCELLED
            else payload["remaining_work_s"] > 0), "study partial mismatch")
        require(result.outcome.status == OutcomeStatus.CANCELLED or result.outcome.status == OutcomeStatus.SUCCEEDED
            and instance.elapsed_s == cp["params"]["duration_s"], "premature study completion")
        return True

    def abandon_resources(self, instance, result, now):
        return (Effect(instance.instance_id + ":abandon", instance.instance_id, "academics", "academics.study_abandon",
            freeze({"chunk_id": instance.checkpoint["params"]["target"]["chunk"]["chunk_id"]}), now, ()),) if instance.started_at is not None else ()


async def propose_study(kernel, academics, scope, *, now, opportunity_key, ports=None, bind_material_ports=None,
                        shadow_state=None, session_s=1500):
    """Explicit opt-in entry. Material I/O precedes all kernel transactions."""
    from ..academics.study import authorize_materials, next_chunk
    scope = kernel._scope(scope, "kernel.propose")
    target = next_chunk(academics, scope.owner_key, scope.context_key)
    require(target is not None, "no pending study chunk", ReasonCode.MISSING_DEPENDENCY)
    materials = target["unit"]["materials"]
    if materials and ports is None and bind_material_ports is not None:
        ports = bind_material_ports()
    await authorize_materials(ports, scope, materials)
    efficiency, load, evaluation = 1000, 0.5, "fixed_rule_v1"
    if shadow_state is not None and shadow_state.config.enabled and shadow_state.config.energy:
        snapshot = shadow_state.snapshot(scope.owner_key)
        energy = snapshot.values.get("energy") if snapshot else None
        require(energy and energy.value is not None and energy.quality.value in ("valid", "estimated")
            and 0 <= (moment(now) - energy.updated_at).total_seconds() <= kernel.config.max_snapshot_age_s,
            "shadow energy unavailable or stale", ReasonCode.MISSING_DEPENDENCY)
        efficiency = max(250, min(1000, int(energy.value * 1000)))
        load, evaluation = 1 - energy.value / 2, "shadow_energy_v1"
    template = study_template(target, session_s=session_s, efficiency=efficiency, load=load, evaluation=evaluation,
        opportunity_key=opportunity_key)
    key = (template.id, template.version)
    # No external awaits while locked. GC must see registration and its durable
    # candidate atomically, for both new templates and reused idle templates.
    with academics.storage._lock:
        if key not in kernel.registry.templates:
            kernel.registry.register_template(template)
        academics._template_refs.setdefault((scope.owner_key, scope.context_key), set()).add(key)
        chunk = target["chunk"]
        return kernel.propose(scope, TemplateRef(template.id, template.version), opportunity_key=opportunity_key,
            reason="study_requested", now=now, expires_at=moment(now) + timedelta(minutes=40),
            target_ref=RecordRef("academics.chunk", chunk["chunk_id"], chunk["revision"] + 1))
