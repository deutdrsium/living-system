"""Generic, durable model results for an explicitly negotiated life port.

The registered task profiles, supervisor, gateway and model audit remain the
host's authority. Activity prompts and parsing belong to the caller. Results
are private assets: every read revalidates all original source dependencies.
"""
from dataclasses import dataclass
from datetime import timedelta
import json

from companion_v2.conversation.foreground import PromptMaterial, PromptRequest, SealedForegroundContext
from companion_v2.conversation.models import InboundConversationEvent
from companion_v2.execution.models import TaskError, TaskResult, TaskSpec, digest, encode, require
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.integration.life_async_codec import plain

PREFIX = "life.model.result.v1:"
MODEL_TASK_TIMEOUT_SECONDS = 480


@dataclass(frozen=True)
class PortJob:
    context: object
    request: object
    validate: object
    instruction: str


class LifeModelTasks:
    def __init__(self, reader):
        self.reader = reader
        self.pending = {}

    def validate(self, spec, at):
        job = self.pending[spec.task_id]
        if spec.context != job.context or spec.request_ref != job.request.params_digest:
            raise TaskError("life_model_input_changed", "model task identity changed")
        job.validate()
        return True

    async def generate(self, run):
        job = self.pending[run.task.task_id]
        job.validate()
        request, context = job.request, job.context
        inputs = request.inputs
        refs = {ref.source_id: json.loads(ref.permission_ref) for ref in request.source_refs}
        from companion_v2.role_work.codec import source_from_document
        original = source_from_document(next(iter(refs.values())))
        definition = self.reader.tasks.registrations[request.task_type].definition
        event = InboundConversationEvent(inputs["event_id"], context.actor.principal,
            context.scope, original, definition.action, request.source_refs[0].content_digest, original.issued_at)
        materials = tuple(PromptMaterial(item["owner"], item["kind"], item["content"],
            source_from_document(refs[item["source_id"]]) if item.get("source_id") else None)
            for item in inputs["materials"])
        prompt = PromptRequest(event, job.instruction,
            SealedForegroundContext(materials, (), ()), inputs["prompt"], context.role_binding,
            output_format=inputs["output_format"], response_format="json_object")
        # Admit only budgets that cover the existing bounded provider chain,
        # including its one ceiling retry per provider. Never silently widen a
        # caller's budget or alter production routing to make it fit.
        plan = self.reader.gateway.router.resolve(run, prompt)
        calls = 2 * len(plan.providers)
        if (request.budget.max_calls < calls
                or request.budget.max_input_tokens < sum(2 * provider.maximum_input_estimate for provider in plan.providers)
                or request.budget.max_output_tokens < calls * 65536):
            raise TaskError("life_model_budget_insufficient", "budget cannot cover the host provider chain")
        result = await self.reader.gateway.generate(run, prompt)
        try:
            # Schema interpretation is the activity's job, but an unbounded or
            # malformed response is never exposed as an accepted JSON asset.
            json.loads(result.draft.text)
            if len(result.draft.text) > 65536:
                raise ValueError("model result exceeds the asset limit")
            with self.reader.runtime.transaction() as db:
                run.validate()
                run.begin_effect()
                document = {"task_id": request.task_id, "context_key": context.key,
                    "params_digest": request.params_digest, "text": result.draft.text,
                    "created_at": self.reader.tasks.now(),
                    "sources": [json.loads(encode(ref)) for ref in request.source_refs],
                    "inputs_digest": digest(plain(inputs)), "call_id": result.call_id}
                operation_id = "life-model-result-" + digest(request.task_id)
                operations = OperationRepository(self.reader.runtime)
                require(operations.reserve(OperationRef(operation_id, "life.model", context.scope,
                    VersionRef(operation_id, 1), operation_id), parameter_hash=request.params_digest,
                    observed_at=self.reader.tasks.now()))
                db.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)",
                           (PREFIX + request.task_id, encode(document)))
                require(operations.transition(operation_id, expected_state=OperationState.ACCEPTED,
                    next_state=OperationState.COMPLETED, observed_at=self.reader.tasks.now()))
                run.record_operation(operation_id)
            return TaskResult("succeeded", effect="committed", result_ref=PREFIX + request.task_id,
                              receipt_ref=result.call_id, operation_id=operation_id)
        except BaseException:
            self.reader.gateway.ledger.discard(result.call_id)
            raise

    def recover_result(self, context, task):
        """Settle only a stopped attempt with its committed original result."""
        if task.state != "uncertain":
            return task
        runtime = self.reader.runtime
        row = runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + task.task_id,))
        if row is None:
            return task
        asset = json.loads(row["value"])
        operation_id = "life-model-result-" + digest(task.task_id)
        if (task.operation_id != operation_id or asset["context_key"] != context.key
                or asset["params_digest"] != task.spec.request_ref):
            return task
        operation = require(OperationRepository(runtime).get(operation_id))
        call = runtime.read_one("SELECT task_id,state FROM model_calls WHERE call_id=?", (asset["call_id"],))
        unknown = runtime.read_one("SELECT 1 FROM model_calls WHERE task_id=? AND state IN ('reserved','dispatched','unknown')", (task.task_id,))
        if (operation.state != OperationState.COMPLETED or operation.parameter_hash != task.spec.request_ref
                or operation.operation.owner != "life.model" or operation.operation.scope != context.scope
                or call is None or call["task_id"] != task.task_id or call["state"] != "generated" or unknown):
            return task
        stopped = require(self.reader.tasks.observe(context, task.task_id))
        if stopped["slot_held"] or not stopped["worker_stopped"]:
            return task
        return require(self.reader.tasks.reconcile(context, task.task_id, expected_revision=task.revision,
            result=TaskResult("succeeded", effect="committed", result_ref=PREFIX + task.task_id,
                              receipt_ref=asset["call_id"], operation_id=operation_id)))

    async def submit(self, context, request, *, validate, instruction):
        reader = self.reader
        # Presence, including an old bridge task or a queued/uncertain task,
        # means inspect only. Never invoke a second runner for the same ID.
        if reader.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id=?", (request.task_id,)):
            return
        validate()
        now = reader.tasks.now()
        job = PortJob(context, request, validate, instruction)
        self.pending[request.task_id] = job
        import asyncio
        invocation = asyncio.current_task()
        reader._invocations.add(invocation)
        try:
            require(reader.tasks.submit(TaskSpec(request.task_id, context, request.task_type,
                request.params_digest, request.task_id, now, now + timedelta(seconds=MODEL_TASK_TIMEOUT_SECONDS))))
            await reader.runner.run(context, request.task_id)
        finally:
            self.pending.pop(request.task_id, None)
            reader._invocations.discard(invocation)
