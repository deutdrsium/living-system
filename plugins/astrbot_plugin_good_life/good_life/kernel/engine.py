"""Default-off, local-only activity coordinator; no legacy execution takeover."""
from contextlib import ExitStack
from dataclasses import dataclass, replace
from uuid import uuid4
import weakref

from ..contracts.models import (
    ActivityInstance, ActivityResult, ActivityStatus, AudienceBound, Candidate, CandidateStatus, ExecutionOwner, ExecutionScope, Outcome, OutcomeStatus, Progress, Provenance, ReasonCode, RecordRef, Revision
)
from ..prepared_commands import _synchronous
from ..staged_activity_commands import staged_foreground_busy
from .codec import KernelError, digest, freeze, integer, moment, name, owned, require, restore, wire
from .clock import MonotonicClock
from .gating import GateResult, GateSnapshot, PreparedActivity, check_gate
from .lifecycle import terminal_status, validate_instance, validate_outcome, validate_transition
from .repository import KernelRepository, READABLE_SCHEMA_VERSIONS, VERSION_KEY
from .resources import ResourcePreparation


MAX_STALE_ACTIVATIONS = 3


def _failure_code(error):
    # Report codes and types only. Exception messages can contain private data.
    return error.code if isinstance(error, KernelError) else "unexpected_error:" + type(error).__name__


class ActivationGateError(KernelError):
    """A post-preparation eligibility denial, distinguished from domain bugs."""


class CandidateComponentError(KernelError):
    """A deterministic lookup/configuration defect, safe to reject in tick."""


@dataclass(frozen=True)
class KernelConfig:
    enabled: bool = False
    max_snapshot_age_s: int = 30
    max_candidates_per_tick: int = 16

    def __post_init__(self):
        require(type(self.enabled) is bool, "enabled must be a boolean")
        integer(self.max_snapshot_age_s, 1)
        require(type(self.max_candidates_per_tick) is int and 1 <= self.max_candidates_per_tick <= 32,
                "candidate budget must be between 1 and 32")


@dataclass(frozen=True)
class Preparation:
    gate: GateResult
    instance: ActivityInstance | None


class ActivityKernel:
    def __init__(self, storage, registry, *, config=None, access=None, clock=None, installed_modules=()):
        self.storage, self.registry = storage, registry
        self.config = config if config is not None else KernelConfig()
        require(isinstance(self.config, KernelConfig), "kernel configuration required")
        self.access, self.closed = access, False
        self.clock = clock or MonotonicClock()
        self.repo = KernelRepository(storage)
        # Per-process monotonic anchors are never deserialized after restart.
        self._session_instances = {}
        self._activation_failures = {}
        if self.config.enabled:
            require(access is not None and all(callable(getattr(access, method, None))
                    for method in ("authorize", "snapshot", "scopes", "audience")),
                    "explicit host-verified kernel access adapter required", ReasonCode.MISSING_DEPENDENCY)
            with storage._lock:
                previous = getattr(storage, "_kernel_lifetime", lambda: None)()
                require(previous is None or previous.closed, "another kernel coordinator is still active")
                self.repo.install()
                for key, (_spec, module) in self.registry.modules.items():
                    if key in installed_modules:
                        continue
                    install = getattr(module, "install", None)
                    if callable(install):
                        _synchronous(install(storage))
                storage._kernel_lifetime = weakref.ref(self)

    def close(self):
        # This coordinator owns no timers, network tasks or cross-store commands.
        # Pure calculations and already persisted proposals survive shutdown.
        with self.storage._lock:
            self.closed = True
            self._session_instances.clear()
            self._activation_failures.clear()

    def _enabled(self):
        require(self.config.enabled and not self.closed, "kernel is disabled or stopped", ReasonCode.MISSING_DEPENDENCY)

    def _scope(self, scope, action, *, settlement_only=False):
        if settlement_only and not self.config.enabled:
            require(not self.closed and self.access is not None, "explicit recovery adapter required", ReasonCode.MISSING_DEPENDENCY)
            version = self.repo.reader.one("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,))
            require(version is not None and version[0] in READABLE_SCHEMA_VERSIONS,
                    "no supported persisted kernel to recover", ReasonCode.MISSING_DEPENDENCY)
        else:
            self._enabled()
        require(isinstance(scope, ExecutionScope), "host-resolved execution scope required", ReasonCode.UNAUTHORIZED)
        scope = owned(scope)
        for value in (scope.owner_key, scope.context_key, scope.authority_ref):
            name(value)
        integer(scope.registration_generation, 1)
        require(callable(getattr(self.access, "authorize", None)), "authority adapter unavailable", ReasonCode.MISSING_DEPENDENCY)
        require(_synchronous(self.access.authorize(scope, action)) is True,
                "authority, context or registration is no longer valid", ReasonCode.UNAUTHORIZED)
        return scope

    def _snapshot(self, scope, now, db=None):
        snapshot = owned(_synchronous(self.access.snapshot(scope, now)))
        self._enabled()  # Fence a snapshot callback that stopped this coordinator.
        require(isinstance(snapshot, GateSnapshot), "trusted gate snapshot required")
        require(snapshot.state.owner_key == scope.owner_key, "snapshot owner mismatch", ReasonCode.UNAUTHORIZED)
        integer(snapshot.state.revision, 1)
        require(snapshot.busy is None or type(snapshot.busy) is bool, "invalid foreground availability")
        require(type(snapshot.capabilities) is tuple, "capability list must be a tuple")
        if db is not None:
            return self.repo.coordinate_snapshot(db, snapshot)
        with self.storage.transaction() as snapshot_db:
            self._enabled()
            return self.repo.coordinate_snapshot(snapshot_db, snapshot)

    def _audience(self, scope):
        audience = owned(_synchronous(self.access.audience(scope)))
        require(isinstance(audience, AudienceBound), "verified audience required", ReasonCode.UNAUTHORIZED)
        name(audience.permission_ref)
        require(audience.context_keys == (scope.context_key,), "A1 local events must retain the original private context", ReasonCode.UNAUTHORIZED)
        return audience

    def _gate(self, scope, snapshot, template, now, duration_s, resources, instance=None, cooldown=True):
        if self.registry._is_template_settlement_only(template):
            return GateResult(False, ReasonCode.MISSING_DEPENDENCY, snapshot.state.revision, resources, now,
                              "capability_disabled")
        gate_snapshot = getattr(self.access, "gate_snapshot", None)
        if callable(gate_snapshot):
            snapshot = gate_snapshot(snapshot, template, instance=instance)
        return check_gate(scope=scope, snapshot=snapshot, template=template, registry=self.registry,
                          now=now, duration_s=duration_s, resources=resources, authorized=True,
                          busy=(self.repo.foreground_busy(scope.owner_key, instance.instance_id if instance else None)
                                or staged_foreground_busy(self.repo.reader, scope.owner_key)),
                          cooldown_until=self.repo.cooldown_until(scope.owner_key, template) if cooldown else None,
                          max_snapshot_age_s=self.config.max_snapshot_age_s)

    def _remaining_duration(self, executor, instance, now):
        remaining = max(0, instance.planned_duration_s - instance.elapsed_s)
        hook = getattr(executor, "remaining_duration_s", None)
        if callable(hook):
            quoted = integer(_synchronous(hook(instance, now)))
            require(quoted <= remaining, "remaining duration exceeds the pinned plan")
            return quoted
        return remaining

    def _instance(self, scope, instance_id, expected_revision, *, pending_ok=False):
        integer(expected_revision, 1)
        instance = self.repo.instance(scope.owner_key, name(instance_id))
        require(instance.context_key == scope.context_key, "instance belongs to another context", ReasonCode.UNAUTHORIZED)
        require(instance.revision == expected_revision, "instance changed", ReasonCode.STALE_VERSION)
        if not pending_ok:
            require(self.repo.pending_result(scope.owner_key, instance_id) is None,
                    "persisted terminal proposal must be settled first", ReasonCode.RESULT_UNKNOWN)
        return instance

    def _fence_write(self, fence, scope, action):
        # Called with the storage lock held. Hosted access locks its lease
        # through commit, after executor callbacks have finished.
        self._enabled()
        guard = getattr(self.access, "write_guard", None)
        if callable(guard):
            fence.enter_context(guard(scope, action))
        else:
            self._scope(scope, action)

    def _command(self, scope, command_id, request, apply, *, settlement_only=False, write_action=None):
        with ExitStack() as fence, self.storage.transaction() as db:
            if settlement_only:
                require(not self.closed, "kernel was stopped", ReasonCode.MISSING_DEPENDENCY)
            else:
                self._enabled()  # Fence a callback authorized before close.
            request_hash, prior = self.repo.command(db, scope, command_id, request)
            response = apply(db) if prior is None else None
            if write_action is not None:
                self._fence_write(fence, scope, write_action)
            if prior is not None:
                return prior
            serialized = wire(response)
            # A step with no fact change has no durable receipt to append. The
            # next tick can reuse its revision-based ID when work is due.
            if serialized.get("changed", True):
                self.repo.save_command(db, scope, command_id, request_hash, serialized)
            return serialized

    def _anchor(self, instance):
        anchor = self._session_instances.get((instance.owner_key, instance.instance_id))
        return anchor if anchor is not None and anchor[0] == instance.revision else None

    def propose(self, scope, template_ref, *, opportunity_key, reason, now, expires_at, target_ref=None):
        scope = self._scope(scope, "kernel.propose")
        now, expires_at = moment(now), moment(expires_at)
        require(expires_at > now, "candidate expiry must be after creation")
        template_ref = owned(template_ref)
        template = self.registry.creation_template(template_ref)
        require(reason in {trigger["id"] for trigger in template.triggers}, "undeclared trigger reason")
        name(opportunity_key)
        if target_ref is not None:
            target_ref = owned(target_ref)
            name(target_ref.kind)
            name(target_ref.id)
            if target_ref.revision is not None:
                integer(target_ref.revision, 1)
        # Opportunity identity includes target version, not just template ID.
        key = digest({"owner": scope.owner_key, "template": template_ref,
                      "target": target_ref, "opportunity": opportunity_key})
        snapshot = self._snapshot(scope, now)
        require(snapshot.state.owner_key == scope.owner_key, "snapshot owner mismatch", ReasonCode.UNAUTHORIZED)
        integer(snapshot.state.revision, 1)
        candidate = Candidate("kernel-candidate-" + uuid4().hex, scope.owner_key, template_ref, target_ref,
                                (reason,), now, expires_at, key, snapshot.state.revision, CandidateStatus.PENDING)
        with self.storage.transaction() as db:
            self._enabled()
            return self.repo.propose(db, scope, candidate)

    def prepare_candidate(self, scope, candidate_id, *, command_id, now):
        scope = self._scope(scope, "kernel.prepare")
        now = moment(now)
        audience = self._audience(scope)

        def apply(db):
            snapshot = self._snapshot(scope, now, db)
            context, candidate = self.repo.candidate(scope.owner_key, candidate_id)
            require(context == scope.context_key, "candidate context mismatch", ReasonCode.UNAUTHORIZED)
            require(candidate.status == CandidateStatus.PENDING, "candidate is not pending", ReasonCode.STALE_VERSION)
            try:
                template = self.registry.template(candidate.template_ref)
                executor = self.registry.executor(template)
            except KernelError as error:
                if error.code != ReasonCode.MISSING_DEPENDENCY.value:
                    raise
                raise CandidateComponentError(error.error.reason_code, str(error)) from error
            gate = self._gate(scope, snapshot, template, now, template.gating.get("free_s", 0),
                              template.gating.get("resources", {}))
            if getattr(executor, "accepts_new_instances", True) is not True:
                gate = replace(gate, allowed=False, reason_code=ReasonCode.MISSING_DEPENDENCY,
                               message="executor version is recovery-only")
            if now >= candidate.expires_at:
                gate = replace(gate, allowed=False, reason_code=ReasonCode.NO_TIME_WINDOW, message="candidate expired")
                status = CandidateStatus.EXPIRED
            else:
                status = CandidateStatus.REJECTED
            self.repo.gate(db, scope, command_id, "planning", gate)
            if not gate.allowed:
                self.repo.save_candidate(db, replace(candidate, status=status))
                return Preparation(gate, None)
            try:
                prepared = owned(_synchronous(executor.prepare(template, now)))
                require(isinstance(prepared, PreparedActivity), "executor returned an invalid prepare result")
                name(prepared.phase)
                integer(prepared.planned_duration_s)
                for resource, amount in prepared.required_resources.items():
                    name(resource)
                    integer(amount)
            except KernelError as error:
                # A declared configuration error or invalid result is permanent.
                # Provider, host and storage failures remain retryable; an
                # arbitrary exception from prepare is not proof of a defect.
                if error.code != ReasonCode.INVALID_CONFIG.value:
                    raise
                raise CandidateComponentError(error.error.reason_code, str(error)) from error
            instance = ActivityInstance("kernel-instance-" + uuid4().hex, scope.owner_key, scope.context_key,
                template.id, template.version, executor.version, ExecutionOwner.LIFE, candidate.target_ref,
                candidate.candidate_id, None, ActivityStatus.PREPARING, Revision(1), prepared.phase, None, now,
                prepared.planned_duration_s, 0, prepared.checkpoint, (), (), (), None, None)
            # Both are persisted under the same BEGIN IMMEDIATE, including the
            # foreground uniqueness fence. No old planned record is imported.
            self.repo.create(db, instance, template, prepared, audience)
            self.repo.save_candidate(db, replace(candidate, status=CandidateStatus.CONSUMED))
            return Preparation(gate, instance)

        response = self._command(scope, command_id, {"action": "prepare", "candidate_id": name(candidate_id), "now": now},
                                 apply, write_action="kernel.prepare")
        return restore(Preparation, response)

    def start(self, scope, instance_id, *, expected_revision, command_id, now):
        scope = self._scope(scope, "kernel.start")
        now = moment(now)
        started = False

        def apply(db):
            nonlocal started
            old = self._instance(scope, instance_id, expected_revision)
            require(old.status in (ActivityStatus.PREPARING, ActivityStatus.PAUSED), "instance cannot start")
            template, prepared, _audience = self.repo.pinned(scope.owner_key, instance_id)
            require(not self.registry._is_template_settlement_only(template), "capability_disabled", ReasonCode.MISSING_DEPENDENCY)
            snapshot = self._snapshot(scope, now, db)
            executor = self.registry.executor(template)
            gate = self._gate(scope, snapshot, template, now, self._remaining_duration(executor, old, now),
                              prepared.required_resources, old, cooldown=old.started_at is None)
            if old.started_at is None and old.candidate_id is not None:
                _context, candidate = self.repo.candidate(scope.owner_key, old.candidate_id)
                if now >= candidate.expires_at:
                    gate = replace(gate, allowed=False, reason_code=ReasonCode.NO_TIME_WINDOW,
                                   message="prepared opportunity expired before start")
            self.repo.gate(db, scope, command_id, "before_start", gate)
            if not gate.allowed:
                if old.status == ActivityStatus.PAUSED:
                    return old  # No accumulation, resampling or terminal claim.
                outcome = Outcome(OutcomeStatus.FAILED, False, gate.message, gate.reason_code)
                new = replace(old, status=ActivityStatus.FAILED, outcome=outcome,
                              reason_code=gate.reason_code, revision=Revision(old.revision + 1), updated_at=now)
            else:
                new = self._try_activate(db, scope, old, template, prepared, executor, snapshot, command_id, now)
                started = new.status == ActivityStatus.RUNNING
                return new
            validate_transition(old, new.status)
            validate_instance(old, new)
            self.repo.save(db, old, new)
            return new

        with self.storage._lock:
            response = self._command(scope, command_id, {"action": "start", "instance_id": name(instance_id),
                                     "expected_revision": expected_revision, "now": now}, apply)
            instance = restore(ActivityInstance, response)
            if started:
                self._session_instances[(scope.owner_key, instance_id)] = (instance.revision, self.clock.stamp(now))
            return instance

    def _try_activate(self, db, scope, old, template, prepared, executor, snapshot, command_id, now, *, action="kernel.start"):
        """Undo tentative resources before persisting a safe blocked state.

        The retry counter is local to this coordinator and instance revision.
        A successful activation or another kind of error clears the streak.
        """
        key = (old.owner_key, old.instance_id, old.revision)
        db.execute("SAVEPOINT kernel_activation")
        try:
            new = self._activate(db, scope, old, template, prepared, executor, snapshot, command_id, now, action=action)
        except BaseException as error:
            db.execute("ROLLBACK TO kernel_activation")
            db.execute("RELEASE kernel_activation")
            recovering_gate_denial = isinstance(error, ActivationGateError) and old.status != ActivityStatus.PREPARING
            stale = (isinstance(error, KernelError) and error.code == ReasonCode.STALE_VERSION.value
                     and not recovering_gate_denial)
            if stale:
                attempts = self._activation_failures.get(key, 0) + 1
                self._activation_failures[key] = attempts
                if attempts < MAX_STALE_ACTIVATIONS:
                    raise
            else:
                self._activation_failures.pop(key, None)
                if not isinstance(error, ActivationGateError) or old.status == ActivityStatus.PREPARING:
                    raise
            self._activation_failures.pop(key, None)
            if old.status == ActivityStatus.PAUSED:
                return old  # Already releases the foreground; keep its resources.
            target = ActivityStatus.FAILED if old.status == ActivityStatus.PREPARING else ActivityStatus.PAUSED
            if target == ActivityStatus.PAUSED:
                require(old.phase in executor.pausable_phases,
                        "recovery is blocked until a permitted stopping phase can be revalidated", error.error.reason_code)
            validate_transition(old, target)
            reason = error.error.reason_code
            outcome = (Outcome(OutcomeStatus.FAILED, False, "Activity start blocked by repeatedly changing snapshots", reason)
                       if target == ActivityStatus.FAILED else None)
            new = replace(old, status=target, revision=Revision(old.revision + 1), updated_at=now,
                          outcome=outcome, reason_code=reason)
            validate_instance(old, new)
            self.repo.save(db, old, new)
            return new
        else:
            db.execute("RELEASE kernel_activation")
            self._activation_failures.pop(key, None)
            return new

    def _activate(self, db, scope, old, template, prepared, executor, snapshot, command_id, now, *, action="kernel.start"):
        """Commit pure preparation, domain facts, references and running together."""
        checkpoint = _synchronous(executor.start(old, now) if old.started_at is None else executor.resume(old, now))
        new = replace(old, status=ActivityStatus.RUNNING, revision=Revision(old.revision + 1), updated_at=now,
                      started_at=old.started_at or now, checkpoint=freeze(checkpoint), reason_code=None)
        hook = getattr(executor, "prepare_resources", None)
        effects = ()
        if callable(hook):
            plan = owned(_synchronous(hook(new, now, snapshot)))
            require(isinstance(plan, ResourcePreparation), "invalid resource preparation")
            require(type(plan.effects) is tuple and type(plan.reservation_refs) is tuple,
                    "resource effects and references must be tuples")
            refs = list(old.reservation_refs)
            for ref in plan.reservation_refs:
                require(isinstance(ref, RecordRef), "invalid reservation reference")
                name(ref.kind)
                name(ref.id)
                if ref.revision is not None:
                    integer(ref.revision, 1)
                matches = [prior for prior in refs if (prior.kind, prior.id) == (ref.kind, ref.id)]
                require(not matches or matches == [ref], "conflicting reservation reference")
                if not matches:
                    refs.append(ref)
            new = replace(new, phase=plan.phase, checkpoint=freeze(plan.checkpoint), reservation_refs=tuple(refs))
            effects = plan.effects
        # A pure callback must not silently change a gate dependency. This also
        # fences changes in the host view while a local preparation is planned.
        current = self._snapshot(scope, now, db)
        require(current.state.revision == snapshot.state.revision,
                "gate snapshot changed during preparation", ReasonCode.STALE_VERSION)
        if old.status != ActivityStatus.RUNNING:
            validate_transition(old, ActivityStatus.RUNNING)
        validate_instance(old, new)
        self.repo.save(db, old, new)
        _template, _prepared, audience = self.repo.pinned(scope.owner_key, old.instance_id)
        self._effects_events(db, new, effects, (), now, audience)
        # Resource preparation can alter the budget or the full free window.
        # The post-preparation projection sees the staged instance/references.
        final_snapshot = self._snapshot(scope, now, db)
        gate = self._gate(scope, final_snapshot, template, now,
                          self._remaining_duration(executor, new, now), prepared.required_resources,
                          new, cooldown=False)
        self.repo.gate(db, scope, command_id, "after_resources", gate)
        if not gate.allowed:
            raise ActivationGateError(gate.reason_code, gate.message)
        require(self._snapshot(scope, now, db).state.revision == final_snapshot.state.revision,
                "gate snapshot changed before running commit", ReasonCode.STALE_VERSION)
        require(_synchronous(self.access.authorize(scope, action)) is True,
                "authority changed during preparation", ReasonCode.UNAUTHORIZED)
        return new

    def _effects_events(self, db, instance, effects, events, now, audience, *, recorded_at=None):
        require(len({effect.effect_id for effect in effects}) == len(effects), "duplicate proposed effect")
        for effect in effects:
            require(effect.target_owner in self.registry.modules, "effect owner unavailable", ReasonCode.MISSING_DEPENDENCY)
            spec, module = self.registry.modules[effect.target_owner]
            require(effect.kind in spec.consumes, "domain does not accept this effect kind")
            require(not effect.source_refs and instance.started_at is not None
                    and instance.started_at <= moment(effect.effective_at) <= now,
                    "invalid local effect time or sources")
            self.repo.commit_effect(db, instance, effect, module)
        for event in events:
            name(event.type)
            require(event.schema_version == 1 and event.provenance == Provenance.SIMULATED
                    and not event.source_refs and event.audience_bound == audience,
                    "A1 local event source or audience mismatch", ReasonCode.UNAUTHORIZED)
            require(any(event.type in spec.emits for spec, _module in self.registry.modules.values()),
                    "event type is not declared by a registered module")
            self.repo.emit(db, instance, event, now, recorded_at or now)

    def advance(self, scope, instance_id, *, expected_revision, command_id, now):
        return self._step(scope, instance_id, expected_revision=expected_revision, command_id=command_id, now=now, cancel=False)

    def cancel(self, scope, instance_id, *, expected_revision, command_id, now):
        return self._step(scope, instance_id, expected_revision=expected_revision, command_id=command_id, now=now, cancel=True)

    def _step(self, scope, instance_id, *, expected_revision, command_id, now, cancel):
        action = "cancel" if cancel else "advance"
        scope, now = self._scope(scope, "kernel." + action), moment(now)
        next_anchor = None

        def apply(db):
            nonlocal next_anchor
            old = self._instance(scope, instance_id, expected_revision)
            if old.status == ActivityStatus.UNKNOWN:
                raise KernelError(ReasonCode.RESULT_UNKNOWN, "inspect the original operation; unknown cannot be cancelled or replayed")
            if cancel:
                require(old.status in (ActivityStatus.PREPARING, ActivityStatus.RUNNING, ActivityStatus.PAUSED), "instance cannot cancel")
            else:
                require(old.status == ActivityStatus.RUNNING, "only running activities advance")
            template, _prepared, audience = self.repo.pinned(scope.owner_key, instance_id)
            settlement_only = self.registry._is_template_settlement_only(template)
            require(cancel or not settlement_only, "capability_disabled", ReasonCode.MISSING_DEPENDENCY)
            anchor = self._anchor(old) if old.status == ActivityStatus.RUNNING and not settlement_only else None
            if old.status == ActivityStatus.RUNNING and not cancel:
                require(anchor is not None, "recover the persisted checkpoint before advancing", ReasonCode.STALE_VERSION)
            executor = self.registry.executor(template)
            delta_s = 0
            if anchor is not None:
                _revision, stamp_at = anchor
                stamp = self.clock.stamp(now)
                require(stamp >= stamp_at, "monotonic clock moved backwards", ReasonCode.STALE_VERSION)
                delta_s = int(stamp - stamp_at)
                next_anchor = (old.revision + 1, stamp_at + delta_s)
            # Cancellation after restart only settles persisted progress; it
            # requires neither a successful recovery gate nor offline work.
            step = owned(_synchronous(executor.cancel(old, now, audience, elapsed_delta_s=delta_s) if cancel
                                      else executor.advance(old, now, audience, elapsed_delta_s=delta_s)))
            require(step.elapsed_s >= old.elapsed_s, "executor moved progress backwards")
            if (step.phase == old.phase and step.checkpoint == old.checkpoint and step.elapsed_s == old.elapsed_s
                    and not step.effects and not step.events and step.outcome is None):
                next_anchor = None  # Keep fractional monotonic time for later.
                require(now >= old.updated_at, "clock moved backwards", ReasonCode.STALE_VERSION)
                return {"instance": old, "result_id": None, "changed": False}
            checkpoint = replace(old, phase=step.phase, checkpoint=step.checkpoint, elapsed_s=step.elapsed_s,
                                 revision=Revision(old.revision + 1), updated_at=now)
            validate_instance(old, checkpoint)
            self.repo.save(db, old, checkpoint)
            result_id = None
            if step.outcome is not None:
                validate_outcome(step.outcome)
                result = ActivityResult(instance_id, checkpoint.revision, step.outcome, now,
                    Progress(step.elapsed_s, step.completed_units, step.outcome.partial), (),
                    step.effects, step.events, (), ())
                self._validate_result(checkpoint, result, executor)
                result_id = self.repo.stage_result(db, scope, result)
            else:
                self._effects_events(db, checkpoint, step.effects, step.events, now, audience)
            return {"instance": checkpoint, "result_id": result_id}

        # Publish the in-process clock anchor before releasing the storage
        # lock, so another command cannot count the same online interval twice.
        with self.storage._lock:
            response = self._command(scope, command_id, {"action": action, "instance_id": name(instance_id),
                                     "expected_revision": expected_revision, "now": now}, apply)
            if next_anchor is not None:
                key = (scope.owner_key, instance_id)
                if next_anchor[0] > self._session_instances.get(key, (0, 0))[0]:
                    self._session_instances[key] = next_anchor
        if response["result_id"] is not None:
            return self.commit_result(scope, response["result_id"], now=now)
        return restore(ActivityInstance, response["instance"])

    def _validate_result(self, instance, result, executor):
        require(result.instance_id == instance.instance_id and result.expected_revision == instance.revision,
                "result does not match current instance revision", ReasonCode.STALE_VERSION)
        require(result.progress.actual_duration_s == instance.elapsed_s and result.progress.partial == result.outcome.partial,
                "result progress does not match persisted checkpoint")
        require(not result.receipts and not result.artifacts and not result.share_candidates,
                "external receipts and assets require a separately negotiated host adapter", ReasonCode.MISSING_DEPENDENCY)
        validate_transition(instance, terminal_status(result.outcome))
        require(_synchronous(executor.validate_result(instance, result)) is True, "result is a proposal, not verified activity completion")

    def commit_result(self, scope, result_id, *, now):
        # Recovery replays the durable proposal, not executor/model/platform work.
        scope, now = self._scope(scope, "kernel.commit_result", settlement_only=True), moment(now)
        with self.storage.transaction() as db:
            require(not self.closed, "kernel was stopped", ReasonCode.MISSING_DEPENDENCY)
            result, final = self.repo.result(scope.owner_key, name(result_id))
            if final is not None:
                require(final.context_key == scope.context_key, "result context mismatch", ReasonCode.UNAUTHORIZED)
                return final
            old = self._instance(scope, result.instance_id, result.expected_revision, pending_ok=True)
            template, _prepared, audience = self.repo.pinned(scope.owner_key, old.instance_id)
            self._validate_result(old, result, self.registry.executor(template))
            # Business occurrence/progress remain from the proposal. updated_at
            # is this record update; the envelope retains both UTC timestamps.
            new = replace(old, status=terminal_status(result.outcome), outcome=result.outcome,
                          reason_code=result.outcome.reason_code, updated_at=now,
                          revision=Revision(old.revision + 1))
            validate_instance(old, new)
            self.repo.save(db, old, new)
            require(now >= result.occurred_at, "cannot record a future terminal result", ReasonCode.STALE_VERSION)
            self._effects_events(db, new, result.effects, result.events, result.occurred_at, audience, recorded_at=now)
            self.repo.finalize_result(db, scope.owner_key, result_id, new)
            return new

    def abandon_result(self, scope, result_id, *, expected_revision, command_id, reason, now):
        """Authorized local-only administrative exit for an unresolvable proposal.

        Retain its original proposal and known progress; commit no proposed
        effects. Never use abandonment to clear UNKNOWN or external operations.
        This uses the existing running/preparing/paused -> failed contract edge.
        """
        scope = self._scope(scope, "kernel.abandon_result", settlement_only=True)
        now = moment(now)
        require(type(reason) is str and bool(reason.strip()) and len(reason) <= 1000,
                "abandonment needs an explicit administrative reason")

        def apply(db):
            result, final = self.repo.result(scope.owner_key, name(result_id))
            require(final is None, "result already settled", ReasonCode.STALE_VERSION)
            old = self._instance(scope, result.instance_id, expected_revision, pending_ok=True)
            require(not old.operation_refs and not result.receipts,
                    "external operations cannot be abandoned as known local failures", ReasonCode.RESULT_UNKNOWN)
            validate_transition(old, ActivityStatus.FAILED)
            outcome = Outcome(OutcomeStatus.FAILED, old.elapsed_s > 0,
                              "Local result abandoned: " + reason.strip(), ReasonCode.INVALID_CONFIG)
            new = replace(old, status=ActivityStatus.FAILED, outcome=outcome, reason_code=outcome.reason_code,
                          updated_at=now, revision=Revision(old.revision + 1))
            validate_instance(old, new)
            self.repo.save(db, old, new)
            template, _prepared, audience = self.repo.pinned(scope.owner_key, old.instance_id)
            executor = self.registry.executors.get(template.executor)
            cleanup = getattr(executor, "abandon_resources", None)
            require(callable(cleanup) or not old.reservation_refs,
                    "resource cleanup executor unavailable", ReasonCode.MISSING_DEPENDENCY)
            if callable(cleanup):
                effects = _synchronous(cleanup(new, result, now))
                require(type(effects) is tuple, "invalid abandonment cleanup")
                effects = tuple(owned(effect) for effect in effects)
                require(not {effect.effect_id for effect in effects}.intersection(effect.effect_id for effect in result.effects),
                        "cleanup cannot commit abandoned proposal effects")
                self._effects_events(db, new, effects, (), now, audience)
            self.repo.finalize_result(db, scope.owner_key, result_id, new)
            return new

        return restore(ActivityInstance, self._command(scope, command_id, {"action": "abandon_result",
            "result_id": name(result_id), "expected_revision": expected_revision, "reason": reason, "now": now},
            apply, settlement_only=True))

    def pause(self, scope, instance_id, *, expected_revision, command_id, now):
        # Settle the actual running interval first, under its own stable command.
        progressed = self.advance(scope, instance_id, expected_revision=expected_revision,
                                  command_id=command_id + ":progress", now=now)
        if progressed.status != ActivityStatus.RUNNING:
            return progressed
        scope, now = self._scope(scope, "kernel.pause"), moment(now)

        def apply(db):
            old = self._instance(scope, instance_id, progressed.revision)
            template, _prepared, _audience = self.repo.pinned(scope.owner_key, instance_id)
            require(old.phase in self.registry.executor(template).pausable_phases, "executor cannot pause this phase")
            validate_transition(old, ActivityStatus.PAUSED)
            new = replace(old, status=ActivityStatus.PAUSED, revision=Revision(old.revision + 1), updated_at=now)
            validate_instance(old, new)
            self.repo.save(db, old, new)
            return new

        return restore(ActivityInstance, self._command(scope, command_id, {"action": "pause", "instance_id": instance_id,
                       "expected_revision": expected_revision, "now": now}, apply))

    def mark_unknown(self, scope, instance_id, *, expected_revision, command_id, operation_id, now):
        scope, now = self._scope(scope, "kernel.mark_unknown"), moment(now)
        name(operation_id)

        def apply(db):
            old = self._instance(scope, instance_id, expected_revision)
            validate_transition(old, ActivityStatus.UNKNOWN)
            new = replace(old, status=ActivityStatus.UNKNOWN, reason_code=ReasonCode.RESULT_UNKNOWN,
                          operation_refs=tuple(sorted(set(old.operation_refs + (operation_id,)))),
                          revision=Revision(old.revision + 1), updated_at=now)
            validate_instance(old, new)
            self.repo.save(db, old, new)
            return new

        return restore(ActivityInstance, self._command(scope, command_id, {"action": "unknown", "instance_id": instance_id,
                       "expected_revision": expected_revision, "operation_id": operation_id, "now": now}, apply))

    def recover_pending(self, scope, *, now):
        """Explicit safety settlement also works after disabling new activity.

        It installs no schema, creates no instance, starts no executor, and does
        not resolve UNKNOWN. A trusted adapter is still mandatory on every call.
        """
        scope, now = self._scope(scope, "kernel.recover_pending", settlement_only=True), moment(now)
        recovered, errors = self._settle_pending(scope, now)
        if errors:
            raise errors[0]
        return tuple(recovered)

    def _settle_pending(self, scope, now):
        recovered, errors = [], []
        for result_id in self.repo.pending_results(scope.owner_key, scope.context_key):
            try:
                recovered.append(self.commit_result(scope, result_id, now=now))
            except Exception as error:
                # Each proposal has its own transaction. Leave failed results
                # pending, and still attempt the rest of this context's queue.
                errors.append(error)
        return recovered, errors

    def recover(self, scope, *, now):
        scope, now = self._scope(scope, "kernel.recover"), moment(now)
        recovered, errors = self._settle_pending(scope, now)
        if errors:
            raise errors[0]
        recovered.extend(self._recover_active(scope, now))
        return tuple(sorted(recovered, key=lambda item: item.instance_id))

    def _recover_active(self, scope, now):
        recovered = []
        for old in self.repo.active(scope.owner_key):
            if old.context_key != scope.context_key or self.repo.pending_result(scope.owner_key, old.instance_id) is not None:
                continue
            template, prepared, _audience = self.repo.pinned(scope.owner_key, old.instance_id)
            if self.registry._is_template_settlement_only(template):
                continue  # This group may settle the persisted checkpoint explicitly.
            if old.status in (ActivityStatus.UNKNOWN, ActivityStatus.PAUSED):
                recovered.append(old)  # Never reissue an unknown operation.
                continue
            if old.status == ActivityStatus.PREPARING:
                recovered.append(self.start(scope, old.instance_id, expected_revision=old.revision,
                    command_id="recover-start-" + old.instance_id + "-" + str(old.revision), now=now))
                continue
            if self._anchor(old) is not None:
                recovered.append(old)
                continue
            command_id = "recover-" + old.instance_id + "-" + str(old.revision)
            resumed = False

            def apply(db):
                nonlocal resumed
                current = self._instance(scope, old.instance_id, old.revision)
                snapshot = self._snapshot(scope, now, db)
                executor = self.registry.executor(template)
                gate = self._gate(scope, snapshot, template, now, self._remaining_duration(executor, current, now),
                                  prepared.required_resources, current, cooldown=False)
                self.repo.gate(db, scope, command_id, "recovery", gate)
                if gate.allowed:
                    new = self._try_activate(db, scope, current, template, prepared, executor, snapshot, command_id, now,
                                             action="kernel.recover")
                    resumed = new.status == ActivityStatus.RUNNING
                    return new
                require(current.phase in executor.pausable_phases, "recovery is blocked until resources are revalidated", gate.reason_code)
                validate_transition(current, ActivityStatus.PAUSED)
                new = replace(current, status=ActivityStatus.PAUSED,
                    revision=Revision(current.revision + 1), updated_at=now, reason_code=gate.reason_code)
                validate_instance(current, new)
                self.repo.save(db, current, new)
                return new

            with self.storage._lock:
                new = restore(ActivityInstance, self._command(scope, command_id,
                              {"action": "recover", "instance_id": old.instance_id, "expected_revision": old.revision, "now": now}, apply))
                if resumed:
                    self._session_instances[(scope.owner_key, old.instance_id)] = (new.revision, self.clock.stamp(now))
            recovered.append(new)
        return tuple(recovered)

    def tick(self, *, now):
        if not self.config.enabled or self.closed:
            return {"advanced": 0, "started": 0, "failures": ()}
        now = moment(now)
        advanced = started = 0
        failures = []
        scopes = _synchronous(self.access.scopes())
        require(type(scopes) is tuple and len(scopes) <= 10000, "invalid kernel scope inventory")
        for scope in sorted(scopes, key=lambda item: (item.owner_key, item.context_key)):
            try:
                scope = self._scope(scope, "kernel.recover")
                _recovered, errors = self._settle_pending(scope, now)
                failures.extend(_failure_code(error) for error in errors)
                self._recover_active(scope, now)
                for instance in self.repo.active(scope.owner_key):
                    if instance.context_key != scope.context_key or instance.status != ActivityStatus.RUNNING:
                        continue
                    if self.repo.pending_result(scope.owner_key, instance.instance_id) is not None:
                        continue
                    template, _prepared, _audience = self.repo.pinned(scope.owner_key, instance.instance_id)
                    if self.registry._is_template_settlement_only(template):
                        continue
                    updated = self.advance(scope, instance.instance_id, expected_revision=instance.revision,
                        command_id="tick-" + instance.instance_id + "-" + str(instance.revision), now=now)
                    advanced += int(updated.revision != instance.revision)
                if self.repo.foreground_busy(scope.owner_key):
                    continue
                for candidate in self.repo.pending_candidates(scope.owner_key)[:self.config.max_candidates_per_tick]:
                    context, _candidate = self.repo.candidate(scope.owner_key, candidate.candidate_id)
                    if context != scope.context_key:
                        continue
                    try:
                        preparation = self.prepare_candidate(scope, candidate.candidate_id,
                            command_id="prepare-" + candidate.candidate_id, now=now)
                        if preparation.instance is not None:
                            instance = preparation.instance
                            final = self.start(scope, instance.instance_id, expected_revision=instance.revision,
                                command_id="start-" + instance.instance_id, now=now)
                            if final.status == ActivityStatus.RUNNING:
                                started += 1
                                break
                    except CandidateComponentError as error:
                        failures.append(error.code)
                        # Only proven component defects retire an opportunity.
                        # All other errors reach the scope handler, preserving
                        # pending/consumed state and skipping this scope's queue.
                        with ExitStack() as fence, self.storage.transaction() as db:
                            self._fence_write(fence, scope, "kernel.prepare")
                            _context, current = self.repo.candidate(scope.owner_key, candidate.candidate_id)
                            if current.status == CandidateStatus.PENDING:
                                self.repo.gate(db, scope, "candidate-error-" + candidate.candidate_id, "candidate_error",
                                    GateResult(False, error.error.reason_code, current.snapshot_revision, freeze({}), now, "candidate component unavailable or failed"))
                                self.repo.save_candidate(db, replace(current, status=CandidateStatus.EXPIRED
                                    if now >= current.expires_at else CandidateStatus.REJECTED))
                        if self.repo.foreground_busy(scope.owner_key):
                            break
            except Exception as error:
                # Cancellation and shutdown still propagate to the caller.
                failures.append(_failure_code(error))
        return {"advanced": advanced, "started": started, "failures": tuple(failures)}
