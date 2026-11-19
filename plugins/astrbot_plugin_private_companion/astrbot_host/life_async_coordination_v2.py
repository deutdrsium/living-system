"""Opt-in local coordination v2. No model/platform submission methods.

The v1 adapter stays intact. Response constructors come from negotiation so
this host never imports the optional life package. Commands resolve through
its existing synchronous provider, with original request hashes/decisions.
"""
from datetime import datetime
import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.integration import life_commands, manual_reading_queue
from companion_v2.integration.life_async_codec import params_digest
from companion_v2.integration.model_completion_handoff import ModelCompletionHandoff
from companion_v2.integration.reading_handoff import ReadingStartHandoff
from companion_v2.integration.natural_wake_handoff import NaturalWakeHandoff
from companion_v2.model_gateway.life_tasks import PREFIX as MODEL_PREFIX
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from .life_async_capabilities import HostPortError, LifeAsyncPorts, capability

API_VERSION = "companion.life-host.async.v2"
COMMAND_REF_PREFIX = "life.prepared-command.v1:"
RECONCILE = "life.command.reconcile"
ABORT_OWN = "life.command.abort-own"
INTENT = "life.intent.authorized"
GROUPS = {
    RECONCILE: ("reconcile_life_command",),
    ABORT_OWN: ("abort_own_prepared_command",),
    INTENT: ("start_authorized_intent", "inspect_authorized_intent"),
}


class _IntentStartHandoff(ReadingStartHandoff):
    """Pin start command identity before prepare, including a lost response."""

    def __init__(self, app, routine, provider, started_at, record_refusal, verify_attempt):
        super().__init__(app, routine, provider)
        self.started_at = started_at
        self.record_refusal = record_refusal
        self.verify_attempt = verify_attempt

    def prepare(self, activity_id, *, expected_revision, request_id, observed_at):
        # Preflight may yield while another request repins an unprepared attempt.
        # Never create a command under a timestamp the journal no longer holds.
        self.verify_attempt()
        return super().prepare(activity_id, expected_revision=expected_revision,
            request_id=request_id, observed_at=self.started_at)

    def original_decision(self, request):
        _, request_hash = life_commands._request(self.routine.context, request)
        return life_commands.lookup(self.runtime, context_key=self.routine.context.key,
            command_id=request["command_id"], request_hash=request_hash)

    def decide(self, request, *, observed_at):
        try:
            return super().decide(request, observed_at=observed_at)
        except (TaskError, ValueError) as error:
            decision = self.original_decision(request)
            if decision is not None and decision["decision"] == "commit":
                return decision
            if decision is None:
                # ReadingStartHandoff.execute will abort next. Persist the
                # actual cause before that abort (including a lost response).
                self.record_refusal(request, error)
            raise

    async def recover(self, request):
        try:
            return await NaturalWakeHandoff.recover(self, request)
        except (TaskError, ValueError) as error:
            if self.original_decision(request) is None:
                self.record_refusal(request, error)
                try:
                    self.abort(request, observed_at=self.app.now())
                except (TaskError, ValueError):
                    decision = self.original_decision(request)
                    if decision is not None and decision["decision"] == "commit":
                        return await NaturalWakeHandoff.recover(self, request)
                    raise
            raise


class LifeCoordinationPortsV2:
    def __init__(self, controller, registry, plugin, registration_id, types, enabled):
        self.core = LifeAsyncPorts(controller, registry, plugin, registration_id, types)
        # capability's shared error wrapper reads these; no v1 submit methods
        # are exposed by the v2 facade.
        self.controller, self.registry = controller, registry
        self.plugin, self.registration_id, self.types = plugin, registration_id, types
        self.enabled = frozenset(enabled)

    def make(self, name, **fields):
        return self.core.make(name, **fields)

    def missing(self, request, message):
        return HostPortError(self.make("ContractError",
            reason_code=self.types["ReasonCode"]("missing_dependency"),
            message=message, operation_id=request.command_id))

    def require_capability(self, name):
        if name not in self.enabled:
            raise TaskError("life_authority_denied", "coordination capability was not negotiated")

    async def resolve_authority(self, request):
        return await self.core.resolve_authority(request)

    def command(self, request):
        app, routine = self.core.scoped(request.scope)
        if (not request.command_id.startswith("activity-model-complete-")
                or request.command_ref != COMMAND_REF_PREFIX + request.command_id):
            raise TaskError("life_command_invalid", "command kind/reference is not negotiated")
        provider = app.role_life.transition_provider()
        command = provider.request_for_replay(command_id=request.command_id,
            owner_key=request.scope.owner_key, context_key=request.scope.context_key)
        if command is None:
            raise self.missing(request, "Original prepared command is unavailable; retain recovery evidence")
        command, request_hash = life_commands._request(routine.context, command)
        if (request_hash != request.request_hash or command["command_id"] != request.command_id
                or command["kind"] != "activity.transition"):
            raise TaskError("life_command_invalid", "original command hash or kind differs")
        return app, routine, provider, command

    @staticmethod
    def decision(app, routine, request):
        return life_commands.lookup(app.runtime, context_key=routine.context.key,
            command_id=request.command_id, request_hash=request.request_hash)

    def receipt(self, request, decision, *, confirmed=False):
        return self.make("LifeCommandReceipt", command_id=request.command_id,
            request_hash=request.request_hash, decision_id=decision["decision_id"] if decision else None,
            decision=self.types["CommandDecision"](decision["decision"] if decision else "undecided"),
            settlement=self.types["CommandSettlement"]("confirmed" if confirmed else "unknown"),
            result_ref=request.command_ref if confirmed and decision["decision"] == "commit" else None,
            reason_code=None if confirmed else self.types["ReasonCode"]("result_unknown"),
            message=None if confirmed else "Original command awaits settlement; query the same command")

    async def settle(self, app, routine, provider, command, decision, proof):
        if decision and decision["decision"] == "abort":
            result = provider.resolve(decision,
                verify_host_decision=lambda value: life_commands.verify_decision(app.runtime, value))
            if result["state"] != "aborted":
                raise TaskError("life_command_unconfirmed", "original abort is not confirmed")
        else:
            # Existing commit recovery does not reauthorize withdrawn model
            # content; it verifies original host decision/effects and confirms
            # the life result. A committed command is never sent to abort().
            await ModelCompletionHandoff(app, routine, provider, proof).recover(command)

    @capability
    async def reconcile_life_command(self, request):
        self.require_capability(RECONCILE)
        app, routine, provider, command = self.command(request)
        decision = self.decision(app, routine, request)
        proof = {}
        if decision is None:
            if not request.proof_ref.startswith(MODEL_PREFIX) or not request.proof_ref.endswith(":manifest"):
                raise TaskError("life_command_invalid", "proof reference is not negotiated")
            row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (request.proof_ref,))
            if row is None:
                raise self.missing(request, "Original proof is unavailable; retain the original command")
            proof = json.loads(row["value"])
            if request.proof_ref != MODEL_PREFIX + proof["task_id"] + ":manifest":
                raise TaskError("life_command_invalid", "original proof reference differs")
        try:
            await self.settle(app, routine, provider, command, decision, proof)
        except (ConnectionError, TimeoutError, TaskError, KeyError, TypeError, ValueError) as error:
            decision = self.decision(app, routine, request)
            reasons = {"life_activity_evidence_required": "invalid_config",
                       "life_command_invalid": "invalid_config",
                       "life_revision_conflict": "stale_version",
                       "life_command_denied": "unauthorized", "life_context_changed": "unauthorized",
                       "life_authority_denied": "unauthorized", "life_activity_transition_invalid": "invalid_config",
                       "life_model_input_changed": "invalid_config", "life_command_conflict": "invalid_config",
                       "reading_source_changed": "source_unavailable", "life_reading_source_changed": "source_unavailable"}
            reason = reasons.get(getattr(error, "code", ""))
            if decision is None and reason is not None:
                raise HostPortError(self.make("ContractError", reason_code=self.types["ReasonCode"](reason),
                    message="Original command refused: " + error.code, operation_id=request.command_id)) from error
            return self.receipt(request, decision)
        return self.receipt(request, self.decision(app, routine, request), confirmed=True)

    @capability
    async def abort_own_prepared_command(self, request):
        self.require_capability(ABORT_OWN)
        app, routine, provider, command = self.command(request)
        # Look up the host's original immutable decision before considering an
        # abort. decide() atomically checks again against a concurrent commit.
        decision = self.decision(app, routine, request)
        try:
            if decision is None:
                try:
                    decision = life_commands.decide(app.runtime, context=routine.context,
                        request=command, outcome="abort", observed_at=app.now(),
                        verify_prepared=provider.verify_prepared,
                        authorize=lambda *_: None, apply=lambda *_: None)
                except (TaskError, ValueError):
                    decision = self.decision(app, routine, request)
                    if decision is None:
                        raise
            await self.settle(app, routine, provider, command, decision, {})
        except (ConnectionError, TimeoutError, TaskError, KeyError, TypeError, ValueError):
            return self.receipt(request, self.decision(app, routine, request))
        return self.receipt(request, decision, confirmed=True)

    @staticmethod
    def intent_key(scope, operation_id):
        return "life.async.intent.v2:" + digest([scope.owner_key, scope.context_key, operation_id])

    def intent_identity(self, request):
        return {"owner_key": request.scope.owner_key, "context_key": request.scope.context_key,
                "operation_id": request.operation_id, "params_digest": request.params_digest,
                "intent_ref": manual_reading_queue.PREFIX + request.operation_id}

    def inspect_intent(self, request):
        app, routine = self.core.scoped(request.scope)
        row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
            (self.intent_key(request.scope, request.operation_id),))
        if row is None or json.loads(row["value"])["identity"] != self.intent_identity(request):
            raise TaskError("life_intent_invalid", "original intent identity is unavailable")
        operation = require(OperationRepository(app.runtime).get(request.operation_id))
        if (operation.operation.owner != "role.life.management"
                or operation.operation.scope != routine.context.scope
                or operation.commit_state is not CommitState.COMMITTED):
            raise TaskError("life_authority_denied", "original intent authorization differs")
        saved = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
            (manual_reading_queue.RESULT_PREFIX + request.operation_id,))
        start_id = "read-start-" + digest([routine.context.key, request.operation_id])
        state, reason, start_ref = "pending", None, None
        if saved and json.loads(saved["value"]).get("state") == "rejected":
            state = "rejected"
            reason = ("stale_version" if json.loads(saved["value"]).get("code") == "life_revision_conflict" else
                      json.loads(row["value"]).get("abort_cause", {}).get("reason_code") or "invalid_config")
        else:
            try:
                activity = app.role_life.activity_receipt(start_id, owner=routine.owner)
            except TaskError as error:
                if error.code != "life_activity_pending":
                    raise
                return self.make("IntentStartReceipt", operation_id=request.operation_id,
                    status=self.types["ReceiptStatus"]("unknown"), start_record_ref=None,
                    evidence_ref=request.operation_id, reason_code=self.types["ReasonCode"]("result_unknown"),
                    message="Original start awaits host confirmation")
            if activity is not None:
                start = require(OperationRepository(app.runtime).get(start_id))
                if (start.operation.owner == "role.life" and start.operation.scope == routine.context.scope
                        and start.commit_state is CommitState.COMMITTED and start.state is OperationState.COMPLETED
                        and operation.state is OperationState.COMPLETED):
                    state, start_ref = "succeeded", start_id
                else:
                    state, reason = "unknown", "result_unknown"
            elif operation.state is not OperationState.ACCEPTED:
                state, reason = "unknown", "result_unknown"
        return self.make("IntentStartReceipt", operation_id=request.operation_id,
            status=self.types["ReceiptStatus"](state), start_record_ref=start_ref,
            evidence_ref=request.operation_id,
            reason_code=self.types["ReasonCode"](reason) if reason else None,
            message="Original authorized intent is " + state if reason else None)

    def record_intent_refusal(self, app, routine, request, command, error):
        _, request_hash = life_commands._request(routine.context, command)
        code = getattr(error, "code", "invalid_config")
        # Only explicit permanent authority/revision refusals terminate an
        # intent. Source/provider availability and unknown causes remain retryable.
        reason = {"life_command_denied": "unauthorized", "life_authority_denied": "unauthorized",
                  "life_context_changed": "unauthorized", "life_revision_conflict": "stale_version"}.get(code)
        key = self.intent_key(request.scope, request.operation_id)
        with app.runtime.transaction() as db:
            record = json.loads(db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()["value"])
            if (record["identity"] != self.intent_identity(request)
                    or datetime.fromisoformat(record["started_at"]) != datetime.fromisoformat(command["payload"]["observed_at"])):
                raise TaskError("task_revision_conflict", "original intent attempt changed")
            record["abort_cause"] = {"command_id": command["command_id"], "request_hash": request_hash,
                                     "code": code, "reason_code": reason}
            db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (encode(record), key))

    def verify_intent_attempt(self, app, request, record):
        current = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
            (self.intent_key(request.scope, request.operation_id),))
        if current is None or json.loads(current["value"]) != record:
            raise TaskError("task_revision_conflict", "original intent attempt changed")

    def repin_intent(self, app, request, record, *, after_abort=False):
        now = app.now()
        if after_abort and now <= datetime.fromisoformat(record["started_at"]):
            # Do not reuse an aborted command identity if the clock has not
            # advanced. Leave the intent pending for the next scheduler turn.
            return None
        key = self.intent_key(request.scope, request.operation_id)
        with app.runtime.transaction() as db:
            current = json.loads(db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()["value"])
            if current != record:
                raise TaskError("task_revision_conflict", "original intent attempt changed")
            current["started_at"] = now.isoformat()
            current.pop("abort_cause", None)
            db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (encode(current), key))
        return current

    def settle_aborted_intent(self, app, provider, request, decision):
        self.inspect_intent(request)  # Recheck original management authority.
        result = provider.resolve(decision,
            verify_host_decision=lambda value: life_commands.verify_decision(app.runtime, value))
        if result["state"] != "aborted":
            raise TaskError("life_reading_unresolved", "original start abort is not confirmed")
        record = json.loads(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
            (self.intent_key(request.scope, request.operation_id),))["value"])
        cause = record.get("abort_cause", {})
        if (cause.get("command_id") != decision["command_id"] or cause.get("request_hash") != decision["request_hash"]
                or cause.get("reason_code") not in ("unauthorized", "stale_version")):
            return False  # v1 sweep abort or transient refusal: retain the queue.
        operations = OperationRepository(app.runtime)
        with app.runtime.transaction() as db:
            operation = require(operations.get(request.operation_id))
            if operation.state is OperationState.ACCEPTED:
                require(operations.transition(request.operation_id, expected_state=OperationState.ACCEPTED,
                    next_state=OperationState.COMPLETED, observed_at=app.now()))
            elif operation.state is not OperationState.COMPLETED:
                raise TaskError("life_reading_unresolved", "original intent cannot settle")
            key = manual_reading_queue.RESULT_PREFIX + request.operation_id
            rejected = {"state": "rejected", "code": "life_intent_start_aborted"}
            prior = db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()
            if prior is not None and json.loads(prior["value"]) != rejected:
                raise TaskError("life_intent_invalid", "original intent result conflicts")
            db.execute("INSERT OR IGNORE INTO runtime_metadata VALUES (?,?)", (key, encode(rejected)))
            db.execute("DELETE FROM runtime_metadata WHERE key=?", (manual_reading_queue.PREFIX + request.operation_id,))
        return True

    @capability
    async def inspect_authorized_intent(self, request):
        self.require_capability(INTENT)
        return self.inspect_intent(request)

    @capability
    async def start_authorized_intent(self, request):
        self.require_capability(INTENT)
        app, routine = self.core.scoped(request.scope)
        if (request.intent_ref != manual_reading_queue.PREFIX + request.operation_id
                or request.params_digest != params_digest("authorized_intent", request)):
            raise TaskError("life_intent_invalid", "original intent reference or digest differs")
        key, identity = self.intent_key(request.scope, request.operation_id), self.intent_identity(request)
        prior = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))
        if prior is None:
            row = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (request.intent_ref,))
            if row is None:
                raise TaskError("life_intent_invalid", "original intent is unavailable")
            intent = json.loads(row["value"])
            operation = require(OperationRepository(app.runtime).get(request.operation_id))
            if (intent["owner_key"] != request.scope.owner_key or intent["context_key"] != request.scope.context_key
                    or intent["operation_id"] != request.operation_id
                    or operation.parameter_hash != intent["parameter_hash"]
                    or operation.operation.owner != "role.life.management"
                    or operation.operation.scope != routine.context.scope
                    or operation.commit_state is not CommitState.COMMITTED):
                raise TaskError("life_authority_denied", "original intent authorization differs")
            with app.runtime.transaction() as db:
                db.execute("INSERT OR IGNORE INTO runtime_metadata VALUES (?,?)",
                    (key, encode({"identity": identity, "started_at": app.now().isoformat()})))
        # Validate the persisted identity before dispatch, including a race with
        # another request. After queue deletion, inspect uses durable receipts.
        record = json.loads(app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"])
        if record["identity"] != identity:
            raise TaskError("life_intent_invalid", "original intent identity differs")
        provider = app.role_life.transition_provider()
        def starter_for(attempt):
            return _IntentStartHandoff(app, routine, provider, datetime.fromisoformat(attempt["started_at"]),
                lambda command, error: self.record_intent_refusal(app, routine, request, command, error),
                lambda: self.verify_intent_attempt(app, request, attempt))
        starter = starter_for(record)
        start_id = "read-start-" + digest([routine.context.key, request.operation_id])
        original = provider.request_for_replay(command_id=starter._command_id(start_id, starter.started_at),
            owner_key=request.scope.owner_key, context_key=request.scope.context_key)
        if original is not None:
            # Recover the original handoff before fenced receipt reads. An
            # unmarked abort can come from the v1 recovery sweep and is retryable.
            decision = starter.original_decision(original)
            if decision is not None and decision["decision"] == "abort":
                if self.settle_aborted_intent(app, provider, request, decision):
                    return self.inspect_intent(request)
                record = self.repin_intent(app, request, record, after_abort=True)
                if record is None:
                    return self.inspect_intent(request)
                starter = starter_for(record)
            else:
                await starter.recover(original)
        else:
            receipt = self.inspect_intent(request)
            if receipt.status.value in ("succeeded", "rejected"):
                return receipt  # A v1 start may already have finished this item.
            # No prepared command holds this timestamp: admission/pacing can be
            # retried with current time instead of keeping a failed preflight pin.
            record = self.repin_intent(app, request, record)
            starter = starter_for(record)
        receipt = self.inspect_intent(request)
        if receipt.status.value not in ("succeeded", "rejected"):
            await routine.preflight()
            await manual_reading_queue.start(app, routine, request.operation_id, handoff=starter)
        return self.inspect_intent(request)


def capabilities_for(controller, registry, *, plugin, registration_id, response_types, requested_capabilities):
    requested = frozenset(requested_capabilities)
    if requested - GROUPS.keys():
        raise TaskError("life_async_incompatible", "requested v2 capability is not implemented")
    types = dict(response_types)
    required = {"ContractError", "ReasonCode", "ExecutionScope"}
    if requested & {RECONCILE, ABORT_OWN}:
        required |= {"LifeCommandReceipt", "CommandDecision", "CommandSettlement"}
    if INTENT in requested:
        required |= {"IntentStartReceipt", "ReceiptStatus"}
    if not required.issubset(types) or any(not callable(types[name]) for name in required):
        raise TaskError("life_async_incompatible", "v2 response constructors are incomplete")
    capabilities = ("resolve_authority",) + tuple(method for group, methods in GROUPS.items()
        if group in requested for method in methods)
    return {"api_version": API_VERSION, "contract_version": 2, "capabilities": capabilities,
            "capability_groups": tuple(group for group in GROUPS if group in requested),
            "local_actions": (), "ports": LifeCoordinationPortsV2(controller, registry, plugin,
                registration_id, types, requested)}
