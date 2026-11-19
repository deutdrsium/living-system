"""Transfer a dashboard sleep command to the isolated life ledger."""

from datetime import timedelta
import json
from threading import get_ident

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.life.physiology import sleep_receipt
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from . import activity_creation_authority, life_commands


PREFIX = "life.activity.creation.v1:"


class ManualSleepHandoff:
    def __init__(self, manager, provider):
        self.manager, self.provider, self.runtime = manager, provider, manager.runtime

    def _outside_host_transaction(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_activity_transaction_active", "sleep creation needs an independent host transaction")

    def _identity(self, operator, data):
        if type(data) is not dict or data.get("action") != "sleep":
            raise ValueError("not a manual sleep command")
        key, minutes = data["operation_key"], data["minutes"]
        identifier, revision = data.get("activity_id", "life"), data.get("expected_revision", 0)
        if (type(key) is not str or not key.strip() or len(key) > 128
                or type(minutes) is not int or not 1 <= minutes <= 1440
                or type(identifier) is not str or not identifier or type(revision) is not int or revision < 0):
            raise ValueError("invalid sleep command")
        access = self.manager.authorize(operator)
        if data.get("view_token") != self.manager.token(operator, access, identifier, revision):
            raise TaskError("life_view_stale", "refresh before applying this change")
        operation_id = self.manager.operation_id("life-command-", operator, key)
        return operation_id, "activity-create-" + digest([self.manager.context.key, operation_id])

    def _expected(self, operation_id, minutes, at):
        context, routine = self.manager.context, self.manager.life
        profile = self.manager.app.role_state.body_clock.profile(context, now=at)
        base = routine.settings["sleep_minutes"] if routine.settings else minutes
        receipt = sleep_receipt(kind="manual", base_need_minutes=base, target_minutes=minutes,
                                workload_score=profile["workload_score"], sleep_debt_minutes=profile["sleep_debt_minutes"])
        work = RoleWorkSpec("sleep", context.key, operation_id, due_at=at + timedelta(minutes=minutes))
        activity = RoleActivity("sleep-" + digest([context.key, operation_id]), self.manager.owner.persona_id,
            context.scope, at, RoleActivityState.ACTIVE, description="角色开始一段睡眠", work=work, sleep=receipt)
        return activity, base, profile

    def prepare(self, operator, data, *, observed_at):
        self._outside_host_transaction()
        operation_id, command_id = self._identity(operator, data)
        owner, context = self.manager.owner, self.manager.context
        original = self.provider.request_for_replay(command_id=command_id, owner_key=digest(owner), context_key=context.key)
        if original is not None:
            management = original["payload"].get("management")
            if management != {"operation_id": operation_id, "parameter_hash": digest(data)}:
                raise TaskError("life_activity_creation_invalid", "management request conflicts with its original creation")
            return original
        self.manager.life.refresh()
        with self.manager.app.role_life.consistent_snapshot():
            self.manager.life.authorize()
            expected, base, profile = self._expected(operation_id, data["minutes"], observed_at)
            proposal = self.provider.propose_manual_sleep(owner=json.loads(encode(owner)), scope=json.loads(encode(context.scope)),
                context_key=context.key, operation_id=operation_id, observed_at=stamp(observed_at), minutes=data["minutes"],
                base_need_minutes=base, workload_score=profile["workload_score"], sleep_debt_minutes=profile["sleep_debt_minutes"])
            if proposal != json.loads(encode(expected)):
                raise TaskError("life_activity_creation_invalid", "life sleep proposal changed")
        request = {"command_id": command_id, "owner_key": digest(owner), "context_key": context.key,
            "kind": "activity.create", "expected_revision": 0,
            "payload": {"owner": json.loads(encode(owner)), "activity": proposal,
                        "management": {"operation_id": operation_id, "parameter_hash": digest(data)}}}
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def _authorize(self, operator, data, request, *, observed_at):
        operation_id, command_id = self._identity(operator, data)
        self.manager.life.authorize()
        activity_creation_authority.authorize_identity(self.manager.app.tasks, self.manager.context, request,
                                                        observed_at=observed_at)
        if (request["command_id"] != command_id or request["payload"].get("management") !=
                {"operation_id": operation_id, "parameter_hash": digest(data)}):
            raise TaskError("life_activity_creation_invalid", "sleep management identity changed")
        activity = activity_from_json(encode(request["payload"]["activity"]))
        expected, _, _ = self._expected(operation_id, data["minutes"], activity.started_at)
        if activity != expected:
            raise TaskError("life_activity_creation_invalid", "sleep physiology or configuration changed")
        return Outcome.success(True)

    def decide(self, operator, data, request, *, observed_at):
        self._outside_host_transaction()
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        existing = life_commands.lookup(self.runtime, context_key=context.key, command_id=request["command_id"],
                                        request_hash=request_hash)
        if existing is not None:
            self._check(data, request, existing)
            return existing
        self.manager.life.refresh()
        operation_id = request["payload"]["management"]["operation_id"]
        operation = OperationRef(operation_id, "role.life.management", context.scope, VersionRef(operation_id, 1), operation_id)
        decision_id = "life-decision-" + digest([context.key, request["command_id"]])

        def reserve(connection, _request):
            receipt = require(OperationRepository(self.runtime).reserve(operation, parameter_hash=digest(data), observed_at=observed_at))
            if receipt.replayed:
                raise TaskError("life_activity_creation_unconfirmed", "management operation lacks its original decision")
            evidence = {"request_hash": request_hash, "operation_id": operation_id,
                        "parameter_hash": digest(data), "scope_hash": digest(context.scope)}
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)", (PREFIX + decision_id, encode(evidence)))

        with self.manager.app.role_life.consistent_snapshot():
            return life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=observed_at,
                verify_prepared=self.provider.verify_prepared,
                authorize=lambda _context, at: self._authorize(operator, data, request, observed_at=at), apply=reserve)

    def _check(self, data, request, decision):
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        if (decision.get("decision") != "commit" or decision.get("request_hash") != request_hash
                or not life_commands.verify_decision(self.runtime, decision)):
            raise TaskError("life_activity_creation_invalid", "sleep creation decision changed")
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + decision["decision_id"],))
        try:
            record = json.loads(row["value"])
            management = request["payload"]["management"]
            parameters = digest(data) if data is not None else management["parameter_hash"]
            if (record != {"request_hash": request_hash, "operation_id": management["operation_id"],
                           "parameter_hash": parameters, "scope_hash": digest(context.scope)}
                    or management["parameter_hash"] != parameters):
                raise ValueError("creation evidence changed")
            receipt = OperationRepository(self.runtime).get(record["operation_id"])
            if (not receipt.is_success or receipt.value.operation.owner != "role.life.management"
                    or receipt.value.operation.scope != context.scope or receipt.value.parameter_hash != record["parameter_hash"]
                    or receipt.value.commit_state is not CommitState.COMMITTED
                    or receipt.value.operation.version != VersionRef(record["operation_id"], 1)
                    or receipt.value.operation.idempotency_key != record["operation_id"]
                    or receipt.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                raise ValueError("management receipt changed")
            return record
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_activity_creation_unconfirmed", "sleep creation host evidence is unavailable") from error

    def apply(self, data, request, decision):
        self._outside_host_transaction()
        self._check(data, request, decision)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def abort(self, request, *, observed_at):
        self._outside_host_transaction()
        decision = life_commands.decide(self.runtime, context=self.manager.context, request=request, outcome="abort",
            observed_at=observed_at, verify_prepared=self.provider.verify_prepared,
            authorize=lambda *_: None, apply=lambda *_: None)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def complete(self, data, request, decision, *, observed_at):
        self._outside_host_transaction()
        record = self._check(data, request, decision)
        def finalize(_connection, _decision):
            self._check(data, request, decision)
            require(OperationRepository(self.runtime).transition(record["operation_id"], expected_state=OperationState.ACCEPTED,
                                                                  next_state=OperationState.COMPLETED, observed_at=observed_at))
        return life_commands.complete(self.runtime, decision, observed_at=observed_at,
                                      verify_life_commit=self.provider.verify_commit, finalize=finalize)

    def acknowledge(self, data, request, decision):
        self._outside_host_transaction()
        record = self._check(data, request, decision)
        def verify(evidence):
            self._check(data, request, decision)
            receipt = OperationRepository(self.runtime).get(record["operation_id"])
            return (life_commands.verify_completion(self.runtime, evidence) and receipt.is_success
                    and receipt.value.state is OperationState.COMPLETED and receipt.value.parameter_hash == record["parameter_hash"])
        return self.provider.acknowledge(decision["command_id"], verify_host_completion=verify)

    async def recover(self, request):
        """Resolve a prepared manual command without repeating the user's action."""
        self._outside_host_transaction()
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        decision = life_commands.lookup(self.runtime, context_key=context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            result = self.abort(request, observed_at=self.manager.app.now())
        elif decision["decision"] == "abort":
            result = self.provider.resolve(decision,
                verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        else:
            self._check(None, request, decision)
            result = self.apply(None, request, decision)
            self.complete(None, request, decision, observed_at=self.manager.app.now())
            self.acknowledge(None, request, decision)
        self.manager.app.notify()
        return result
