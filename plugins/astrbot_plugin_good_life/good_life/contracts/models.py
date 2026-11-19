"""Contract v1 declarations; adapters validate values, permissions and persistence.

No existing runtime imports this module. Mappings are read-only by contract;
constructors must receive owned, immutable data (frozen is shallow in Python).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Mapping, NewType, TypeAlias

CONTRACT_VERSION = 2

OwnerKey = NewType("OwnerKey", str)
ContextKey = NewType("ContextKey", str)
SchemaVersion = NewType("SchemaVersion", int)
TemplateVersion = NewType("TemplateVersion", int)
Revision = NewType("Revision", int)
RegistrationGeneration = NewType("RegistrationGeneration", int)
JSONValue: TypeAlias = "None | bool | int | float | str | tuple[JSONValue, ...] | Mapping[str, JSONValue]"
JSONObject: TypeAlias = Mapping[str, JSONValue]


class CandidateStatus(str, Enum):
    PENDING = "pending"
    CONSUMED = "consumed"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ActivityStatus(str, Enum):
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExecutionOwner(str, Enum):
    LIFE = "life"
    HOST_BRIDGE = "host_bridge"


class OutcomeStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ReceiptStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class StateQuality(str, Enum):
    VALID = "valid"
    ESTIMATED = "estimated"
    STALE = "stale"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"


class ValueType(str, Enum):
    BOOLEAN = "boolean"
    INTEGER = "integer"
    NUMBER = "number"
    STRING = "string"
    OBJECT = "object"
    ARRAY = "array"


class Provenance(str, Enum):
    SOURCE_BACKED = "source_backed"
    SIMULATED = "simulated"
    AUTHORED = "authored"
    GENERATED = "generated"


class ReasonCode(str, Enum):
    INVALID_CONFIG = "invalid_config"
    MISSING_DEPENDENCY = "missing_dependency"
    UNAUTHORIZED = "unauthorized"
    STALE_VERSION = "stale_version"
    INSUFFICIENT_RESOURCE = "insufficient_resource"
    NO_TIME_WINDOW = "no_time_window"
    SOURCE_UNAVAILABLE = "source_unavailable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RESULT_UNKNOWN = "result_unknown"
    CANCELLED = "cancelled"


# Every omitted edge is illegal. Self-replay uses the original command receipt,
# not a state transition. UNKNOWN edges require evidence about the original op.
ACTIVITY_TRANSITIONS: tuple[tuple[ActivityStatus, tuple[ActivityStatus, ...]], ...] = (
    (ActivityStatus.PREPARING, (ActivityStatus.RUNNING, ActivityStatus.FAILED,
                              ActivityStatus.CANCELLED, ActivityStatus.UNKNOWN)),
    (ActivityStatus.RUNNING, (ActivityStatus.PAUSED, ActivityStatus.COMPLETED,
                            ActivityStatus.FAILED, ActivityStatus.CANCELLED,
                            ActivityStatus.UNKNOWN)),
    (ActivityStatus.PAUSED, (ActivityStatus.RUNNING, ActivityStatus.FAILED,
                           ActivityStatus.CANCELLED, ActivityStatus.UNKNOWN)),
    (ActivityStatus.UNKNOWN, (ActivityStatus.PREPARING, ActivityStatus.RUNNING,
                            ActivityStatus.PAUSED, ActivityStatus.COMPLETED,
                            ActivityStatus.FAILED, ActivityStatus.CANCELLED)),
    (ActivityStatus.COMPLETED, ()),
    (ActivityStatus.FAILED, ()),
    (ActivityStatus.CANCELLED, ()),
)
CANDIDATE_TRANSITIONS: tuple[tuple[CandidateStatus, tuple[CandidateStatus, ...]], ...] = (
    (CandidateStatus.PENDING, (CandidateStatus.CONSUMED, CandidateStatus.REJECTED,
                              CandidateStatus.EXPIRED)),
    (CandidateStatus.CONSUMED, ()),
    (CandidateStatus.REJECTED, ()),
    (CandidateStatus.EXPIRED, ()),
)


@dataclass(frozen=True)
class ExecutionScope:
    owner_key: OwnerKey
    context_key: ContextKey
    authority_ref: str
    registration_generation: RegistrationGeneration


@dataclass(frozen=True)
class TemplateRef:
    id: str
    version: TemplateVersion


@dataclass(frozen=True)
class RecordRef:
    kind: str
    id: str
    revision: Revision | None


@dataclass(frozen=True)
class Money:
    amount_minor: int
    currency: str


@dataclass(frozen=True)
class AudienceBound:
    permission_ref: str
    context_keys: tuple[ContextKey, ...]


@dataclass(frozen=True)
class SourceRef:
    source_id: str
    version: str | None
    content_digest: str | None
    fragment_start: int | None
    fragment_end: int | None
    retrieved_at: datetime
    collected_at: datetime | None
    permission_ref: str


@dataclass(frozen=True)
class FieldProvenance:
    field_path: str
    provenance: Provenance


@dataclass(frozen=True)
class ActivityTemplate:
    id: str
    version: TemplateVersion
    executor: str
    requires: tuple[str, ...]
    triggers: tuple[JSONObject, ...]
    gating: JSONObject
    inputs: JSONObject
    params: JSONObject
    frequency: JSONObject | None = None
    model_policy: JSONObject | None = None
    presentation: JSONObject | None = None
    schema_version: SchemaVersion = SchemaVersion(1)


@dataclass(frozen=True)
class Candidate:
    candidate_id: str
    owner_key: OwnerKey
    template_ref: TemplateRef
    target_ref: RecordRef | None
    reasons: tuple[str, ...]
    created_at: datetime
    expires_at: datetime
    dedupe_key: str
    snapshot_revision: Revision
    status: CandidateStatus


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    partial: bool
    summary: str
    reason_code: ReasonCode | None = None


@dataclass(frozen=True)
class ActivityInstance:
    instance_id: str
    owner_key: OwnerKey
    context_key: ContextKey
    template_id: str
    template_version: TemplateVersion
    executor_version: str
    execution_owner: ExecutionOwner
    target_ref: RecordRef | None
    candidate_id: str | None
    causal_event_id: str | None
    status: ActivityStatus
    revision: Revision
    phase: str
    started_at: datetime | None
    updated_at: datetime
    planned_duration_s: int
    elapsed_s: int
    checkpoint: JSONObject
    reservation_refs: tuple[RecordRef, ...]
    source_refs: tuple[SourceRef, ...]
    operation_refs: tuple[str, ...]
    outcome: Outcome | None
    reason_code: ReasonCode | None


@dataclass(frozen=True)
class Progress:
    actual_duration_s: int
    completed_units: JSONObject
    partial: bool


@dataclass(frozen=True)
class OperationReceipt:
    operation_id: str
    kind: str
    status: ReceiptStatus
    evidence_ref: str | None
    reason_code: ReasonCode | None = None
    message: str | None = None


@dataclass(frozen=True)
class Effect:
    effect_id: str
    instance_id: str
    target_owner: str
    kind: str
    payload: JSONObject
    effective_at: datetime
    source_refs: tuple[SourceRef, ...]


@dataclass(frozen=True)
class EventProposal:
    type: str
    schema_version: SchemaVersion
    payload: JSONObject
    source_refs: tuple[SourceRef, ...]
    provenance: Provenance
    audience_bound: AudienceBound


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    type: str
    content_ref: str
    source_refs: tuple[SourceRef, ...]
    provenance: Provenance
    audience_bound: AudienceBound
    field_provenance: tuple[FieldProvenance, ...] = ()


@dataclass(frozen=True)
class ShareCandidate:
    artifact_refs: tuple[str, ...]
    relevance_reason: str
    audience_bound: AudienceBound


@dataclass(frozen=True)
class ActivityResult:
    instance_id: str
    expected_revision: Revision
    outcome: Outcome
    occurred_at: datetime
    progress: Progress
    receipts: tuple[OperationReceipt, ...]
    effects: tuple[Effect, ...]
    events: tuple[EventProposal, ...]
    artifacts: tuple[Artifact, ...]
    share_candidates: tuple[ShareCandidate, ...]


@dataclass(frozen=True)
class EventEnvelope:
    event_id: str
    type: str
    schema_version: SchemaVersion
    owner_key: OwnerKey
    context_key: ContextKey
    aggregate_id: str
    aggregate_revision: Revision
    occurred_at: datetime
    recorded_at: datetime
    causation_id: str | None
    payload: JSONObject
    source_refs: tuple[SourceRef, ...]
    provenance: Provenance
    audience_bound: AudienceBound


@dataclass(frozen=True)
class StateRange:
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True)
class StateBucket:
    label: str
    lower: float | None
    upper: float | None


@dataclass(frozen=True)
class StateVar:
    key: str
    owner_module: str
    value_type: ValueType
    unit: str
    range: StateRange | None
    initial: JSONValue
    baseline: JSONValue
    timescale_s: int | None
    expose_to_llm: bool
    buckets: tuple[StateBucket, ...]
    schema_version: SchemaVersion


@dataclass(frozen=True)
class EstimateAssumptions:
    model_version: str
    assumptions: tuple[str, ...]


@dataclass(frozen=True)
class StateValue:
    value: JSONValue
    updated_at: datetime
    revision: Revision
    source_refs: tuple[SourceRef, ...]
    quality: StateQuality
    assumptions: EstimateAssumptions | None = None


@dataclass(frozen=True)
class StateSnapshot:
    owner_key: OwnerKey
    observed_at: datetime
    revision: Revision
    values: Mapping[str, StateValue]


@dataclass(frozen=True)
class ContractError:
    reason_code: ReasonCode
    message: str
    instance_id: str | None = None
    operation_id: str | None = None
