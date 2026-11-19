"""Contract-v1 lifecycle validation, separate from executor proposals."""
from dataclasses import dataclass
from typing import Mapping

from ..contracts.models import ACTIVITY_TRANSITIONS, ActivityStatus as S, Effect, EventProposal, Outcome, OutcomeStatus, ReasonCode
from .codec import integer, moment, name, require


@dataclass(frozen=True)
class ExecutorStep:
    phase: str
    checkpoint: Mapping
    elapsed_s: int
    completed_units: Mapping
    effects: tuple[Effect, ...] = ()
    events: tuple[EventProposal, ...] = ()
    outcome: Outcome | None = None


def validate_transition(old, target):
    """Use the frozen graph, with an additional fail-closed UNKNOWN fence.

    Until an original-operation audit adapter is implemented, UNKNOWN exits
    stay unavailable. A caller-supplied receipt string is never such evidence.
    """
    require(target in dict(ACTIVITY_TRANSITIONS)[old.status], "illegal activity transition")
    require(old.status != S.UNKNOWN, "unknown requires verified original operation evidence", ReasonCode.RESULT_UNKNOWN)
    if target == S.PREPARING:
        require(old.started_at is None, "cannot prepare an already started instance")


def validate_outcome(outcome):
    require(isinstance(outcome, Outcome), "outcome required")
    require(type(outcome.partial) is bool and type(outcome.summary) is str, "invalid outcome")
    if outcome.status == OutcomeStatus.SUCCEEDED:
        require(outcome.reason_code is None, "successful outcome must not have an error")
    elif outcome.status == OutcomeStatus.CANCELLED:
        require(outcome.reason_code == ReasonCode.CANCELLED, "cancelled outcome requires cancelled reason")
    else:
        require(outcome.status == OutcomeStatus.FAILED and isinstance(outcome.reason_code, ReasonCode), "failed outcome requires a reason")


def terminal_status(outcome):
    validate_outcome(outcome)
    return {OutcomeStatus.SUCCEEDED: S.COMPLETED, OutcomeStatus.FAILED: S.FAILED,
            OutcomeStatus.CANCELLED: S.CANCELLED}[outcome.status]


def validate_instance(old, new):
    name(new.phase)
    integer(new.revision, 1)
    integer(new.planned_duration_s)
    integer(new.elapsed_s)
    require(new.elapsed_s >= old.elapsed_s, "progress cannot move backwards")
    require(moment(new.updated_at) >= moment(old.updated_at), "clock moved backwards", ReasonCode.STALE_VERSION)
    require(new.started_at == old.started_at or old.started_at is None and new.status == S.RUNNING
            and new.started_at == new.updated_at, "first start must be retained")
    if new.status in (S.RUNNING, S.PAUSED, S.COMPLETED):
        require(new.started_at is not None, "activity has no actual start evidence")
    if new.outcome is not None:
        require(new.status == terminal_status(new.outcome) and new.reason_code == new.outcome.reason_code,
                "terminal outcome/status mismatch")
    else:
        require(new.status not in (S.COMPLETED, S.CANCELLED, S.FAILED), "terminal outcome required")
