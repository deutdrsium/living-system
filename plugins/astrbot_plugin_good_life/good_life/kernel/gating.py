"""Two snapshot-only gates; never query a provider or infer unknown resources."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Mapping, Protocol

from ..contracts.models import ExecutionScope, ReasonCode, Revision, StateQuality, StateSnapshot
from .codec import freeze, integer, moment, owned, require


@dataclass(frozen=True)
class GateSnapshot:
    state: StateSnapshot
    free_until: datetime | None
    busy: bool | None
    capabilities: tuple[str, ...]


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason_code: ReasonCode | None
    snapshot_revision: Revision
    required_resources: Mapping
    estimated_finish_at: datetime
    message: str


@dataclass(frozen=True)
class PreparedActivity:
    phase: str
    planned_duration_s: int
    checkpoint: Mapping
    required_resources: Mapping


class KernelAccess(Protocol):
    """Trusted in-process adapter; never exported as companion.life.v1 handlers.

    scopes are host-resolved ExecutionScopes, not identities from templates.
    authorize MUST recheck current permission, lease/generation and context.
    snapshot MUST include all existing busy lines and hard agenda commitments.
    For the same now and unchanged underlying facts, snapshots MUST be
    deterministic: observed_at derives from now, not a fresh wall-clock read,
    and reads do not increment revisions/counters. Committed domain changes
    must still change the view, even at the same now. Repeated unstable views
    are bounded by the kernel's activation retry policy.
    Final snapshot/authorize reads run inside the life lock and SQLite writer
    transaction; adapters must support this order without opening a nested
    life transaction. Real host adapters need explicit critical-section review.
    Callbacks are synchronous local reads only; no models or platform actions.
    Callers MUST release all host locks before invoking any kernel command;
    host-lock -> life-lock calls would invert the existing snapshot lock order.
    The coordinator lifetime guard is in-process, on the shared LifeStorage;
    it does not provide cross-process leadership or exclude another process.
    Production embedding therefore requires one process and one shared store.
    """
    def scopes(self) -> tuple[ExecutionScope, ...]: ...
    def authorize(self, scope: ExecutionScope, action: str) -> bool: ...
    def snapshot(self, scope: ExecutionScope, now: datetime) -> GateSnapshot: ...
    def audience(self, scope: ExecutionScope): ...


def check_gate(*, scope, snapshot, template, registry, now, duration_s, resources,
               authorized, busy, cooldown_until=None, max_snapshot_age_s=30):
    now = moment(now)
    integer(duration_s)
    state = owned(snapshot.state)
    integer(state.revision, 1)
    require(state.owner_key == scope.owner_key, "snapshot owner mismatch", ReasonCode.UNAUTHORIZED)
    finish = now + timedelta(seconds=duration_s)
    resources = freeze(resources)

    def result(allowed, code=None, message="allowed"):
        return GateResult(allowed, code, state.revision, resources, finish, message)

    if authorized is not True:
        return result(False, ReasonCode.UNAUTHORIZED, "authority or registration is no longer valid")
    age = (now - moment(state.observed_at)).total_seconds()
    if not 0 <= age <= max_snapshot_age_s:
        return result(False, ReasonCode.STALE_VERSION, "snapshot is stale or from the future")
    if registry.unavailable(template, snapshot.capabilities):
        return result(False, ReasonCode.MISSING_DEPENDENCY, "required capability is unavailable")
    if snapshot.busy is None:
        return result(False, ReasonCode.INSUFFICIENT_RESOURCE, "foreground availability is unknown")
    if busy or snapshot.busy:
        return result(False, ReasonCode.INSUFFICIENT_RESOURCE, "foreground is occupied")
    if cooldown_until is not None and now < cooldown_until:
        return result(False, ReasonCode.INSUFFICIENT_RESOURCE, "activity is cooling down")
    if snapshot.free_until is None or finish > moment(snapshot.free_until):
        return result(False, ReasonCode.NO_TIME_WINDOW, "complete activity does not fit the known free window")
    for key, amount in resources.items():
        integer(amount)
        value = state.values.get(key)
        if (value is None or value.quality != StateQuality.VALID or value.value is None
                or type(value.value) not in (int, float) or moment(value.updated_at) > now
                or (now - moment(value.updated_at)).total_seconds() > max_snapshot_age_s
                or value.value < amount):
            return result(False, ReasonCode.INSUFFICIENT_RESOURCE, "required resource is unknown, stale or insufficient")
    return result(True)
