"""Explicit contract-v1 async capabilities, separate from companion.life.v1.

Response constructors are supplied by the negotiated contract adapter. This
keeps the host installable without importing the optional life plugin package.
No live application, routine or database object crosses the port.
"""
from dataclasses import replace
from datetime import datetime
import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.model_gateway.life_tasks import PREFIX
from companion_v2.role_work.codec import source_from_document
from companion_v2.integration.model_completion_handoff import ModelCompletionHandoff
from companion_v2.integration import life_event_inbox
from companion_v2.integration.life_async_codec import params_digest, plain

API_VERSION = "companion.life-host.async.v1"
CAPABILITIES = ("resolve_authority", "get_persona_context", "read_material", "run_model_task",
                "inspect_task", "execute_platform_action", "inspect_action", "accept_life_event")
ALLOWED_ACTIONS = frozenset({"role.reading.record"})
READING_TASK_KINDS = frozenset({"role.reading.consume", "role.reading.choose"})


class HostPortError(ValueError):
    def __init__(self, error, *, task_absent=False):
        super().__init__(error.message)
        self.error, self.code, self.task_absent = error, error.reason_code.value, task_absent


def capability(method):
    async def invoke(self, request):
        try:
            return await method(self, request)
        except HostPortError:
            raise
        except (ConnectionError, TimeoutError, TaskError, KeyError, TypeError, ValueError) as error:
            code = getattr(error, "code", "result_unknown" if isinstance(error, (ConnectionError, TimeoutError)) else "invalid_config")
            reason = ("result_unknown" if code in ("result_unknown", "task_revision_conflict") else
                      "provider_unavailable" if code in ("life_provider_unavailable", "host_not_running") else
                      "stale_version" if "changed" in code or "revision" in code else
                      "source_unavailable" if "source" in code or "reading_source" in code else
                      "unauthorized" if "denied" in code or "authority" in code or "context" in code else "invalid_config")
            contract = self.make("ContractError", reason_code=self.types["ReasonCode"](reason),
                message="Host capability refused: " + code, operation_id=getattr(request, "task_id",
                    getattr(request, "operation_id", getattr(request, "command_id", None))))
            task_id = getattr(request, "task_id", None)
            app = getattr(getattr(self.controller, "session", None), "application", None)
            if (task_id and method.__name__ == "inspect_task" and app is not None
                    and reason not in ("provider_unavailable", "unauthorized")
                    and self.authorities.get(request.scope.authority_ref) == request.scope
                    and self.registry.owns_registration(self.registration_id, self.plugin)):
                row = app.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (task_id,))
                if row is not None:
                    try:
                        # Preserve the original task audit, but a permanently
                        # invalid result is a failed receipt, not uncertainty.
                        return self.inspect(request.scope, task_id, request.params_digest,
                            include_result=False, recover=False,
                            result_error=contract if reason != "result_unknown" else None)
                    except (TaskError, KeyError, TypeError, ValueError):
                        pass
            raise HostPortError(contract) from error
    return invoke


class LifeAsyncPorts:
    def __init__(self, controller, registry, plugin, registration_id, types):
        self.controller, self.registry = controller, registry
        self.plugin, self.registration_id, self.types = plugin, registration_id, types
        self.generation = 1
        self.authorities = {}
        self.authority_actions = {}

    def make(self, name, **fields):
        return self.types[name](**fields)

    def current(self, context_key):
        if (getattr(self.controller, "_terminated", False)
                or not self.registry.owns_registration(self.registration_id, self.plugin)):
            raise TaskError("life_provider_unavailable", "original provider lease is unavailable")
        app = self.controller.session.application
        app.require_running()
        if app.role_life.activity_owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "life execution has not transferred")
        routine = app.life_runtime.routines.get(context_key)
        if routine is None or routine.context is None or routine._closed or routine._retired:
            raise TaskError("life_context_changed", "original context is unavailable")
        routine.refresh()
        routine.authorize()
        return app, routine

    def scoped(self, scope):
        app, routine = self.current(scope.context_key)
        if (self.authorities.get(scope.authority_ref) != scope or scope.owner_key != digest(routine.owner)
                or scope.registration_generation != self.generation):
            raise TaskError("life_context_changed", "scope is not held by this provider lease")
        action = self.authority_actions.get(scope.authority_ref)
        if action not in ALLOWED_ACTIONS:
            raise TaskError("life_authority_denied", "scope action is not negotiated")
        require(app.tasks.authorize_context(routine.context, action=action, observed_at=app.now()))
        return app, routine

    def audience(self, scope):
        return self.make("AudienceBound", permission_ref="context:" + digest([scope.owner_key, scope.context_key]), context_keys=(scope.context_key,))

    def check_audience(self, scope, audience):
        if audience != self.audience(scope):
            raise TaskError("source_audience_mismatch", "requested audience exceeds the original context")

    @capability
    async def resolve_authority(self, request):
        if request.action not in ALLOWED_ACTIONS:
            raise TaskError("life_authority_denied", "action is not negotiated for reading")
        app, routine = self.current(request.context_key)
        require(app.tasks.authorize_context(routine.context, action=request.action, observed_at=app.now()))
        scope = self.make("ExecutionScope", owner_key=digest(routine.owner), context_key=request.context_key,
            authority_ref=digest([self.registration_id, routine.context, request.action]), registration_generation=self.generation)
        self.authorities[scope.authority_ref] = scope
        self.authority_actions[scope.authority_ref] = request.action
        return scope

    def original(self, scope, ref):
        app, routine = self.scoped(scope)
        try:
            original = source_from_document(json.loads(ref.permission_ref))
        except (KeyError, TypeError, ValueError) as error:
            raise TaskError("source_reference_invalid", "original source reference is unavailable") from error
        source = require(app.role_journal.sources.read(original, resolved=routine.context.actor,
            capability="role.reading.record", observed_at=app.now(), authorizer=app.tasks.identity))
        if (ref.source_id != original.source_id or ref.version != encode(original.version)
                or ref.content_digest != source.content_hash or digest(source.payload) != source.content_hash
                or not set(routine.context.audience.recipients).issubset(source.audience.recipients)):
            raise TaskError("reading_source_changed", "source version or audience changed")
        return source

    @capability
    async def get_persona_context(self, request):
        app, routine = self.scoped(request.scope)
        for ref in request.allowed_source_refs:
            self.original(request.scope, ref)
        materials, refs = [], []
        if request.purpose == "book_choice":
            for item in app.role_reader.interest_materials(routine.context, app.now()):
                source_id = None
                if item.source is not None:
                    original = require(app.role_journal.sources.read(item.source, resolved=routine.context.actor,
                        capability="role.reading.record", observed_at=app.now(), authorizer=app.tasks.identity))
                    ref = self.make("SourceRef", source_id=item.source.source_id, version=encode(item.source.version),
                        content_digest=original.content_hash, fragment_start=0, fragment_end=len(original.payload),
                        retrieved_at=item.source.issued_at, collected_at=None, permission_ref=encode(item.source))
                    self.original(request.scope, ref)
                    refs.append(ref)
                    source_id = ref.source_id
                materials.append({"owner": item.owner, "kind": item.purpose, "content": item.payload, "source_id": source_id})
        content = {"instruction": routine.instruction, "materials": materials}
        return self.make("PersonaContext", context_ref="persona:" + digest(content),
            version=digest(content), content=content,
            source_refs=tuple(refs), audience_bound=self.audience(request.scope))

    def asset(self, scope, result_ref):
        app, routine = self.scoped(scope)
        row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (result_ref,))
        if row is None or not result_ref.startswith(PREFIX):
            raise TaskError("life_model_result_unavailable", "model result is unavailable")
        asset = json.loads(row["value"])
        if asset["context_key"] != routine.context.key:
            raise TaskError("source_audience_mismatch", "model result belongs to another context")
        for raw in asset["sources"]:
            raw["retrieved_at"] = datetime.fromisoformat(raw["retrieved_at"])
            if raw["collected_at"]:
                raw["collected_at"] = datetime.fromisoformat(raw["collected_at"])
            self.original(scope, self.make("SourceRef", **raw))
        return asset

    @capability
    async def read_material(self, request):
        ref = request.source_ref
        if ref.source_id.startswith(PREFIX):
            asset = self.asset(request.scope, ref.source_id)
            content = asset["text"]
            ref = replace(ref, version="1", content_digest=digest(content), retrieved_at=datetime.fromisoformat(asset["created_at"]))
        else:
            source = self.original(request.scope, ref)
            start, end = ref.fragment_start, ref.fragment_end
            if start is None and end is None:
                start, end = 0, len(source.payload)
                ref = replace(ref, fragment_start=start, fragment_end=end)
            if (start is None or type(start) is not int or type(end) is not int
                    or not 0 <= start <= end <= len(source.payload)):
                raise TaskError("source_reference_invalid", "material fragment is invalid")
            content = source.payload[start:end]
        return self.make("AuthorizedMaterial", source_ref=ref, content=content,
                         audience_bound=self.audience(request.scope))

    @capability
    async def run_model_task(self, request):
        app, routine = self.scoped(request.scope)
        self.check_audience(request.scope, request.audience_bound)
        registered = {entry.definition.kind for entry in app.role_reader.registrations}
        if (request.task_type not in READING_TASK_KINDS or request.task_type not in registered
                or request.schema_version != 1 or request.budget.max_calls < 1):
            raise TaskError("life_model_task_invalid", "model task profile is not negotiated")
        expected = params_digest("model_task", request)
        if request.params_digest != expected or not request.source_refs:
            raise TaskError("life_model_input_changed", "model input digest changed")
        def validate():
            active, bound = self.scoped(request.scope)
            require(active.tasks.authorize_context(bound.context, action="role.reading.record", observed_at=active.now()))
            if digest(bound.instruction) != request.inputs["instruction_version"]:
                raise TaskError("life_context_changed", "original persona changed")
            for ref in request.source_refs:
                self.original(request.scope, ref)
            record = request.inputs.get("record_ref")
            if record:
                current = bound.activity(record["activity_id"])
                if current.revision != record["revision"] or current.state.value != "active":
                    raise TaskError("life_revision_conflict", "original activity changed")
        validate()
        if app.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id=? AND state IN ('reserved','dispatched','unknown') LIMIT 1", (request.task_id,)):
            return self.inspect(request.scope, request.task_id, request.params_digest, include_result=False, recover=False)
        proof = plain({"task_id": request.task_id, "params_digest": request.params_digest,
                 "record_ref": request.inputs.get("record_ref"), "bindings": request.inputs.get("bindings", {})})
        key = PREFIX + request.task_id + ":manifest"
        with app.runtime.transaction() as db:
            prior = db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
            accepted = db.execute("SELECT 1 FROM execution_tasks WHERE task_id=?", (request.task_id,)).fetchone()
            if prior is not None and json.loads(prior["value"]) != proof and accepted is not None:
                raise TaskError("life_model_input_changed", "original task manifest changed")
            if accepted is None:
                db.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (key, encode(proof)))
            elif prior is None:
                db.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (key, encode(proof)))
        app.calendar.prepare_life_snapshot_read()
        with app.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            await app.role_reader.port_tasks.submit(routine.context, request, validate=validate, instruction=routine.instruction)
        # Admission errors belong to this submission; result revalidation uses
        # the inspection boundary even when the model completed synchronously.
        return await self.inspect_task(request)

    def inspect(self, scope, task_id, params_digest, *, include_result=True, recover=True, result_error=None):
        app, routine = self.scoped(scope)
        row = app.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (task_id,))
        result_ref = None
        state = "pending"
        pending_call = app.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id=? AND state IN ('reserved','dispatched') LIMIT 1", (task_id,))
        unknown_call = app.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id=? AND state='unknown' LIMIT 1", (task_id,))
        if row is not None:
            task = require(app.tasks.read(routine.context, task_id))
            if recover and not pending_call and not unknown_call:
                task = app.role_reader.port_tasks.recover_result(routine.context, task)
            state = {"created": "pending", "admitted": "pending", "retry_scheduled": "pending", "running": "running", "cancelling": "running",
                     "uncertain": "unknown", "succeeded": "succeeded", "failed": "failed",
                     "cancelled": "cancelled", "rejected": "rejected", "expired": "failed"}.get(task.state, "unknown")
        # Only a completed, settled result can be permanently rejected.
        # A failed recheck must not turn live or uncertain work into failure.
        if state != "succeeded" or pending_call or unknown_call:
            result_error = None
        if result_error is not None:
            state = "failed"
        elif pending_call and row is not None and state in ("pending", "running"):
            state = "running"
        elif pending_call or unknown_call:
            # Stopped/absent tasks with unsettled call audit are uncertain,
            # not perpetually in flight; they use the bounded result checks.
            state = "unknown"
        elif row is not None:
            result = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + task_id,))
            if result is not None and include_result and state == "succeeded":
                asset = self.asset(scope, PREFIX + task_id)
                if asset["params_digest"] != params_digest:
                    raise TaskError("life_model_input_changed", "original task parameters changed")
                result_ref = PREFIX + task_id
            elif state == "succeeded":
                # Suppressing or losing a result never proves usable success.
                state = "unknown"
        if row is None and not pending_call and not unknown_call:
            raise HostPortError(self.make("ContractError", reason_code=self.types["ReasonCode"]("missing_dependency"),
                message="Original model task is absent", operation_id=task_id), task_absent=True)
        reason = (result_error.reason_code.value if result_error else
                  "result_unknown" if state == "unknown" else "cancelled" if state == "cancelled" else
                  "invalid_config" if state in ("failed", "rejected") else None)
        usage = app.runtime.read_one("""SELECT COUNT(*) AS calls,
            CASE WHEN COUNT(input_tokens)=COUNT(*) THEN SUM(input_tokens) END AS input_tokens,
            CASE WHEN COUNT(output_tokens)=COUNT(*) THEN SUM(output_tokens) END AS output_tokens
            FROM model_calls WHERE task_id=?""", (task_id,))
        return self.make("ModelTaskReceipt", task_id=task_id,
            receipt=self.make("OperationReceipt", operation_id=task_id, kind="model", status=self.types["ReceiptStatus"](state),
                evidence_ref=task_id, reason_code=self.types["ReasonCode"](reason) if reason else None,
                message=result_error.message if result_error else "Original task is " + state if reason else None), result_ref=result_ref,
            usage=self.make("TaskUsage", calls=usage["calls"], input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"]))

    @capability
    async def inspect_task(self, request):
        return self.inspect(request.scope, request.task_id, request.params_digest)

    @capability
    async def execute_platform_action(self, request):
        app, routine = self.scoped(request.scope)
        self.check_audience(request.scope, request.audience_bound)
        if request.action not in ("life.command.commit", "life.intent.start") or request.params_digest != params_digest("platform_action", request):
            raise TaskError("life_action_invalid", "only negotiated local life actions are supported")
        payload = plain(request.payload)
        key = "life.async.action.v1:" + request.operation_id
        identity = {"params_digest": request.params_digest, "context_key": routine.context.key, "action": request.action}
        prior = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))
        if prior is not None and json.loads(prior["value"]) != identity:
            return self.make("OperationReceipt", operation_id=request.operation_id, kind=request.action,
                status=self.types["ReceiptStatus"]("rejected"), evidence_ref=request.operation_id,
                reason_code=self.types["ReasonCode"]("invalid_config"), message="Original action parameters conflict")
        def remember():
            with app.runtime.transaction() as db:
                original = db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
                if original is not None and json.loads(original["value"]) != identity:
                    raise TaskError("life_action_invalid", "original action parameters conflict")
                db.execute("INSERT OR IGNORE INTO runtime_metadata VALUES (?,?)", (key, encode(identity)))
        # Temporary local adapter; formal action-port classification is
        # explicitly deferred to contract v2. Validate before persisting identity.
        if request.action == "life.intent.start":
            # Compatibility adapter for an already authorized management intent.
            # The intent service only authorizes/starts; it never runs a model.
            from companion_v2.integration import manual_reading_queue
            if payload != {"intent_ref": manual_reading_queue.PREFIX + request.operation_id}:
                raise TaskError("life_action_invalid", "intent reference is not negotiated")
            await routine.preflight()
            result = await manual_reading_queue.start(app, routine, request.operation_id)
            rejected = result.get("state") == "rejected"
            if not rejected:
                remember()
            return self.make("OperationReceipt", operation_id=request.operation_id, kind=request.action,
                status=self.types["ReceiptStatus"]("rejected" if rejected else "succeeded"), evidence_ref=request.operation_id,
                reason_code=self.types["ReasonCode"]("stale_version") if rejected else None,
                message="Manual intent revision changed" if rejected else None)
        command, proof = payload["command"], payload["proof"]
        if command["payload"]["operation_id"] != request.operation_id:
            raise TaskError("life_action_invalid", "command operation differs")
        handoff = ModelCompletionHandoff(app, routine, app.role_life.transition_provider(), proof)
        try:
            await handoff.recover(command)
        except (TaskError, ValueError):
            from companion_v2.integration import life_commands
            _, request_hash = life_commands._request(routine.context, command)
            decision = life_commands.lookup(app.runtime, context_key=routine.context.key,
                command_id=command["command_id"], request_hash=request_hash)
            if decision is None:
                handoff.abort(command, observed_at=app.now())
            raise
        remember()
        return self.make("OperationReceipt", operation_id=request.operation_id, kind=request.action,
                         status=self.types["ReceiptStatus"]("succeeded"), evidence_ref=command["command_id"])

    @capability
    async def inspect_action(self, request):
        app, routine = self.scoped(request.scope)
        from companion_v2.storage.repositories import OperationRepository
        row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", ("life.async.action.v1:" + request.operation_id,))
        identity = json.loads(row["value"]) if row else None
        if (identity is None or identity["params_digest"] != request.params_digest
                or identity["context_key"] != routine.context.key):
            raise TaskError("life_action_invalid", "original action parameters are unavailable")
        operation = OperationRepository(app.runtime).get(request.operation_id)
        state = "unknown"
        if (operation.is_success and operation.value.operation.scope == routine.context.scope
                and operation.value.commit_state.value == "committed"):
            state = "succeeded" if operation.value.state.value == "completed" else "unknown"
        return self.make("OperationReceipt", operation_id=request.operation_id, kind=identity["action"],
            status=self.types["ReceiptStatus"](state), evidence_ref=request.operation_id,
            reason_code=self.types["ReasonCode"]("result_unknown") if state == "unknown" else None,
            message="Original action awaits confirmation" if state == "unknown" else None)

    @capability
    async def accept_life_event(self, request):
        app, routine = self.scoped(request.scope)
        event = request.event
        self.check_audience(request.scope, event.audience_bound)
        legacy = plain(event.payload["legacy_event"])
        consumer = event.payload["consumer"]
        if (request.params_digest != params_digest("accept_life_event", request) or event.event_id != legacy["event_id"]
                or event.owner_key != request.scope.owner_key or event.context_key != request.scope.context_key
                or event.aggregate_id != legacy["payload"]["activity_id"]
                or event.aggregate_revision != legacy["payload"]["revision"]
                or event.type != legacy["kind"] or event.occurred_at != datetime.fromisoformat(legacy["occurred_at"])
                or event.recorded_at < event.occurred_at or event.causation_id is not None):
            raise TaskError("life_event_invalid", "event envelope differs from committed reference")
        for ref in event.source_refs:
            self.original(request.scope, ref)
        key = "life.async.event.v1:" + digest([routine.context.key, consumer, event.event_id])
        existing = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))
        if existing is not None and existing["value"] != request.params_digest:
            raise TaskError("life_event_invalid", "original event parameters changed")
        life_event_inbox.accept(app, routine.context, legacy, consumer=consumer, observed_at=app.now())
        with app.runtime.transaction() as db:
            prior = db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
            if prior is not None and prior["value"] != request.params_digest:
                raise TaskError("life_event_invalid", "original event parameters changed")
            db.execute("INSERT OR IGNORE INTO runtime_metadata VALUES (?,?)", (key, request.params_digest))
        return self.make("LifeEventReceipt", event_id=event.event_id, consumer_id=consumer,
            owner_key=request.scope.owner_key, status=self.types["ReceiptStatus"]("succeeded"),
            inbox_ref=life_event_inbox.inbox_key(routine.context, consumer, event.event_id), recorded_at=app.now())


def capabilities_for(controller, registry, *, plugin, registration_id, contract_version, response_types,
                     requested_capabilities=()):
    if contract_version not in (1, 2) or not registry.owns_registration(registration_id, plugin):
        raise TaskError("life_async_incompatible", "async capability contract or lease is incompatible")
    app = controller.session.application
    app.require_running()
    if contract_version == 2:
        from .life_async_coordination_v2 import capabilities_for as v2_capabilities
        return v2_capabilities(controller, registry, plugin=plugin, registration_id=registration_id,
            response_types=response_types, requested_capabilities=requested_capabilities)
    if requested_capabilities:
        raise TaskError("life_async_incompatible", "v2 capability groups require contract v2")
    with app.runtime.transaction() as db:
        version = db.execute("SELECT value FROM runtime_metadata WHERE key='life.async.ports.schema.version'").fetchone()
        if version is not None and version[0] != "1":
            raise TaskError("life_async_incompatible", "async capability storage version is incompatible")
        db.execute("INSERT OR IGNORE INTO runtime_metadata VALUES ('life.async.ports.schema.version','1')")
    return {"api_version": API_VERSION, "contract_version": 1, "capabilities": CAPABILITIES,
            "local_actions": ("life.command.commit", "life.intent.start"),
            "ports": LifeAsyncPorts(controller, registry, plugin, registration_id, dict(response_types))}
