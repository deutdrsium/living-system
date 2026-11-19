"""Approved architecture contract v2 batch 1, alongside unchanged v1 records.

Declarations only. Providers advertise implemented capabilities separately;
importing this module neither enables a capability nor validates a record.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from .models import ExecutionScope, ReasonCode, ReceiptStatus, Revision
from .ports import (
    AuthorityRequest, AuthorizedMaterial, MaterialRequest, ModelTaskRequest,
    PersonaContext, PersonaRequest, TaskInspection, TaskUsage,
)

SUPPORTED_CONTRACT_VERSIONS = (1, 2)
API_VERSION = "companion.life-host.async.v2"
COMMAND_RECONCILE_CAPABILITY = "life.command.reconcile"
ABORT_OWN_CAPABILITY = "life.command.abort-own"
AUTHORIZED_INTENT_CAPABILITY = "life.intent.authorized"
MODEL_TASK_CAPABILITY = "life.model-task.v2"
# No automatic opt-in, including when a caller negotiates contract_version=2.
DEFAULT_CAPABILITIES: tuple[str, ...] = ()


class CommandDecision(str, Enum):
    UNDECIDED = "undecided"
    COMMIT = "commit"
    ABORT = "abort"


class CommandSettlement(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    UNKNOWN = "unknown"


class ResultAccessStatus(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


class TaskInspectionStatus(str, Enum):
    FOUND = "found"
    ABSENT = "absent"
    UNKNOWN = "unknown"


# ReceiptStatus is reused without adding/changing any v1 enum member. These
# edges constrain v2 execution facts only; access checks never advance revision.
RECEIPT_TRANSITIONS = (
    (ReceiptStatus.PENDING, (ReceiptStatus.RUNNING, ReceiptStatus.SUCCEEDED,
                            ReceiptStatus.REJECTED, ReceiptStatus.FAILED,
                            ReceiptStatus.CANCELLED, ReceiptStatus.UNKNOWN)),
    (ReceiptStatus.RUNNING, (ReceiptStatus.SUCCEEDED, ReceiptStatus.FAILED,
                            ReceiptStatus.CANCELLED, ReceiptStatus.UNKNOWN)),
    (ReceiptStatus.UNKNOWN, (ReceiptStatus.PENDING, ReceiptStatus.RUNNING,
                            ReceiptStatus.SUCCEEDED, ReceiptStatus.REJECTED,
                            ReceiptStatus.FAILED, ReceiptStatus.CANCELLED)),
    (ReceiptStatus.SUCCEEDED, ()),
    (ReceiptStatus.REJECTED, ()),
    (ReceiptStatus.FAILED, ()),
    (ReceiptStatus.CANCELLED, ()),
)


@dataclass(frozen=True)
class OperationReceiptV2:
    operation_id: str
    kind: str
    status: ReceiptStatus
    evidence_ref: str | None
    receipt_revision: Revision
    observed_at: datetime
    reason_code: ReasonCode | None = None
    message: str | None = None


@dataclass(frozen=True)
class ResultAccess:
    status: ResultAccessStatus
    checked_at: datetime
    reason_code: ReasonCode | None = None
    message: str | None = None


@dataclass(frozen=True)
class ModelTaskReceiptV2:
    task_id: str
    receipt: OperationReceiptV2
    result_ref: str | None
    usage: TaskUsage
    result_access: ResultAccess
    accepted_at: datetime | None = None
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class TaskInspectionResult:
    task_id: str
    status: TaskInspectionStatus
    receipt: ModelTaskReceiptV2 | None
    reason_code: ReasonCode | None = None
    message: str | None = None


@dataclass(frozen=True)
class LifeCommandRequest:
    scope: ExecutionScope
    command_id: str
    request_hash: str
    command_ref: str
    proof_ref: str


@dataclass(frozen=True)
class AbortPreparedCommandRequest:
    scope: ExecutionScope
    command_id: str
    request_hash: str
    command_ref: str


@dataclass(frozen=True)
class LifeCommandReceipt:
    command_id: str
    request_hash: str
    decision_id: str | None
    decision: CommandDecision
    settlement: CommandSettlement
    result_ref: str | None = None
    reason_code: ReasonCode | None = None
    message: str | None = None


@dataclass(frozen=True)
class AuthorizedIntentRequest:
    scope: ExecutionScope
    operation_id: str
    params_digest: str
    intent_ref: str


@dataclass(frozen=True)
class IntentInspection:
    scope: ExecutionScope
    operation_id: str
    params_digest: str


@dataclass(frozen=True)
class IntentStartReceipt:
    operation_id: str
    status: ReceiptStatus
    start_record_ref: str | None
    evidence_ref: str | None
    reason_code: ReasonCode | None = None
    message: str | None = None


class LifeCoordinationPortsV2(Protocol):
    """Local decisions/settlement, never real platform actions or model calls."""

    async def reconcile_life_command(self, request: LifeCommandRequest) -> LifeCommandReceipt: ...

    async def abort_own_prepared_command(self, request: AbortPreparedCommandRequest) -> LifeCommandReceipt: ...

    async def start_authorized_intent(self, request: AuthorizedIntentRequest) -> IntentStartReceipt: ...

    async def inspect_authorized_intent(self, request: IntentInspection) -> IntentStartReceipt: ...


class ModelTaskPortsV2(Protocol):
    """Negotiated separately; declarations do not imply runtime implementation."""

    async def run_model_task(self, request: ModelTaskRequest) -> ModelTaskReceiptV2: ...

    async def inspect_task(self, request: TaskInspection) -> TaskInspectionResult: ...


class CompanionHostPortsV2(LifeCoordinationPortsV2, ModelTaskPortsV2, Protocol):
    """Each method additionally requires its advertised capability group.

    Platform/asset/event v2 expansion is deferred; use separately negotiated v1
    ports for v1 work. Never downgrade a v2 in-flight operation into new v1 work.
    """

    async def resolve_authority(self, request: AuthorityRequest) -> ExecutionScope: ...

    async def get_persona_context(self, request: PersonaRequest) -> PersonaContext: ...

    async def read_material(self, request: MaterialRequest) -> AuthorizedMaterial: ...
