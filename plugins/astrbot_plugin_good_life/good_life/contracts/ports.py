"""Target async host capabilities, separate from synchronous companion.life.v1.

These protocols describe signatures only. Implementations/adapters, discovery,
validation, I/O and translation of legacy receipts belong to later packages.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from .models import (
    Artifact, AudienceBound, ContextKey, EventEnvelope, ExecutionScope,
    JSONObject, JSONValue, OperationReceipt, OwnerKey, ReasonCode, ReceiptStatus, SchemaVersion, SourceRef,
    TemplateRef,
)


@dataclass(frozen=True)
class AuthorityRequest:
    context_key: ContextKey
    action: str
    resource_refs: tuple[str, ...]


@dataclass(frozen=True)
class PersonaRequest:
    scope: ExecutionScope
    purpose: str
    allowed_source_refs: tuple[SourceRef, ...]


@dataclass(frozen=True)
class PersonaContext:
    context_ref: str
    version: str
    content: JSONObject
    source_refs: tuple[SourceRef, ...]
    audience_bound: AudienceBound


@dataclass(frozen=True)
class MaterialRequest:
    scope: ExecutionScope
    source_ref: SourceRef
    purpose: str


@dataclass(frozen=True)
class AuthorizedMaterial:
    source_ref: SourceRef
    content: str
    audience_bound: AudienceBound


@dataclass(frozen=True)
class ModelBudget:
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int


@dataclass(frozen=True)
class ModelTaskRequest:
    scope: ExecutionScope
    task_id: str
    params_digest: str
    task_type: str
    template_ref: TemplateRef
    inputs: JSONObject
    source_refs: tuple[SourceRef, ...]
    audience_bound: AudienceBound
    budget: ModelBudget
    schema_version: SchemaVersion = SchemaVersion(1)


@dataclass(frozen=True)
class TaskUsage:
    calls: int | None
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True)
class ModelTaskReceipt:
    task_id: str
    receipt: OperationReceipt
    result_ref: str | None
    usage: TaskUsage


@dataclass(frozen=True)
class TaskInspection:
    scope: ExecutionScope
    task_id: str
    params_digest: str


@dataclass(frozen=True)
class PlatformActionRequest:
    scope: ExecutionScope
    operation_id: str
    params_digest: str
    action: str
    payload: JSONObject
    source_refs: tuple[SourceRef, ...]
    audience_bound: AudienceBound


@dataclass(frozen=True)
class ActionInspection:
    scope: ExecutionScope
    operation_id: str
    params_digest: str


@dataclass(frozen=True)
class PublishAssetRequest:
    scope: ExecutionScope
    operation_id: str
    params_digest: str
    artifact: Artifact


@dataclass(frozen=True)
class AssetReceipt:
    receipt: OperationReceipt
    asset_ref: str | None


@dataclass(frozen=True)
class AcceptLifeEventRequest:
    scope: ExecutionScope
    event: EventEnvelope
    params_digest: str


@dataclass(frozen=True)
class LifeEventReceipt:
    event_id: str
    consumer_id: str
    owner_key: OwnerKey
    status: ReceiptStatus
    inbox_ref: str | None
    recorded_at: datetime | None
    reason_code: ReasonCode | None = None
    message: str | None = None


class CompanionHostPorts(Protocol):
    """All calls recheck authority/lease; success never implies downstream memory.

    run_model_task/execute_platform_action submit or return original durable work.
    UNKNOWN is inspected under the same ID. Before durable acceptance, all
    contract/authority rejections raise an adapter error carrying ContractError.
    After durable acceptance, failures use the original typed receipt and ID.
    No network task runs in a sync life provider callback.
    """

    async def resolve_authority(self, request: AuthorityRequest) -> ExecutionScope: ...

    async def get_persona_context(self, request: PersonaRequest) -> PersonaContext: ...

    async def read_material(self, request: MaterialRequest) -> AuthorizedMaterial: ...

    async def run_model_task(self, request: ModelTaskRequest) -> ModelTaskReceipt: ...

    async def inspect_task(self, request: TaskInspection) -> ModelTaskReceipt: ...

    async def execute_platform_action(self, request: PlatformActionRequest) -> OperationReceipt: ...

    async def inspect_action(self, request: ActionInspection) -> OperationReceipt: ...

    async def publish_asset(self, request: PublishAssetRequest) -> AssetReceipt: ...

    async def accept_life_event(self, request: AcceptLifeEventRequest) -> LifeEventReceipt: ...
