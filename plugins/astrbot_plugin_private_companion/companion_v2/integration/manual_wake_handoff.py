"""Transfer a dashboard wake command while preserving both operation audits."""

from dataclasses import replace
from datetime import datetime
import hashlib
import json

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.life.physiology import sleep_quality_range
from companion_v2.role_work.models import RoleActivityState as State, RoleSleepReceipt, advance_activity
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from . import activity_transition_authority, life_commands
from .manual_sleep_handoff import ManualSleepHandoff


PREFIX = "life.activity.wake.v1:"
CHECKPOINT = "已醒来，梦境由独立任务记录"


def _consumers(sleep):
    return ("journal", "dream", "wardrobe") if sleep.kind == "night" else ("journal", "dream")


class ManualWakeHandoff(ManualSleepHandoff):
    action = "wake"
    command_prefix = "activity-wake-"
    transition_state = State.COMPLETED
    checkpoint = CHECKPOINT
    role_suffix = ":wake"

    def _authorize_routine(self):
        self.manager.life.authorize()

    def _consumers(self, sleep):
        return list(_consumers(sleep))

    def _propose(self, activity, at, debt):
        return self.provider.propose_manual_wake(activity=json.loads(encode(activity)), observed_at=stamp(at),
                                                  sleep_debt_minutes=debt)

    def _identity(self, operator, data):
        if type(data) is not dict or data.get("action") != self.action:
            raise ValueError("not a manual wake command")
        key, identifier, revision = data["operation_key"], data["activity_id"], data["expected_revision"]
        if (type(key) is not str or not key.strip() or len(key) > 128 or type(identifier) is not str
                or not identifier or type(revision) is not int or revision < 1):
            raise ValueError("invalid wake command")
        access = self.manager.authorize(operator)
        if data.get("view_token") != self.manager.token(operator, access, identifier, revision):
            raise TaskError("life_view_stale", "refresh before applying this change")
        operation_id = self.manager.operation_id("life-command-", operator, key)
        return operation_id, self.command_prefix + digest([self.manager.context.key, operation_id])

    def _expected(self, activity, at):
        body = self.manager.app.role_state.body_clock
        receipt = activity.sleep or body._legacy_receipt(activity)
        elapsed = max(0, round((at - activity.updated_at).total_seconds())) if activity.state is State.ACTIVE else 0
        debt = body.profile(self.manager.context, now=at)["sleep_debt_minutes"]
        low, high = sleep_quality_range(receipt.workload_score, debt, receipt.interruptions, receipt.early_interruptions)
        receipt = replace(receipt, slept_seconds=min(172800, receipt.slept_seconds + elapsed),
                          wake_until=None, quality_min=low, quality_max=high)
        return advance_activity(activity, expected_revision=activity.revision, state=self.transition_state,
                                observed_at=at, checkpoint=self.checkpoint, sleep=receipt), debt

    def already_ended(self, operator, data, *, observed_at):
        """Keep the old no-op wake behavior for an already ended episode."""
        self._outside_host_transaction()
        operation_id, command_id = self._identity(operator, data)
        context = self.manager.context
        if self.provider.request_for_replay(command_id=command_id, owner_key=digest(self.manager.owner),
                                            context_key=context.key) is not None:
            return None
        with self.manager.app.role_life.consistent_snapshot():
            activity = self.manager.life.activity(data["activity_id"])
            if activity.revision != data["expected_revision"]:
                raise TaskError("life_revision_conflict", "sleep activity changed")
            if activity.state not in (State.COMPLETED, State.CANCELLED):
                return None
            with self.runtime.transaction():
                self._identity(operator, data)
                self._authorize_routine()
                operation = OperationRef(operation_id, "role.life.management", context.scope, VersionRef(operation_id, 1), operation_id)
                receipt = require(OperationRepository(self.runtime).reserve(operation, parameter_hash=digest(data), observed_at=observed_at))
                if receipt.replayed:
                    return {"operation_id": operation_id, "state": receipt.state.value, "replayed": True}
                require(OperationRepository(self.runtime).transition(operation_id, expected_state=OperationState.ACCEPTED,
                                                                      next_state=OperationState.COMPLETED, observed_at=observed_at))
            return {"operation_id": operation_id, "activity_id": activity.activity_id}

    def prepare(self, operator, data, *, observed_at):
        self._outside_host_transaction()
        operation_id, command_id = self._identity(operator, data)
        owner, context = self.manager.owner, self.manager.context
        original = self.provider.request_for_replay(command_id=command_id, owner_key=digest(owner), context_key=context.key)
        if original is not None:
            if original["payload"].get("management") != {"operation_id": operation_id, "parameter_hash": digest(data)}:
                raise TaskError("life_activity_transition_invalid", "management request conflicts with its original wake")
            return original
        with self.manager.app.role_life.consistent_snapshot():
            self._authorize_routine()
            activity = self.manager.life.activity(data["activity_id"])
            if (activity.revision != data["expected_revision"] or activity.work.kind != "sleep"
                    or activity.state not in (State.ACTIVE, State.PAUSED)
                    or observed_at < activity.started_at or (self.action == "wake" and observed_at == activity.started_at)):
                raise TaskError("life_sleep_invalid", "select an unchanged sleep that has actually started")
            expected, debt = self._expected(activity, observed_at)
            sleep = self._propose(activity, observed_at, debt)
            if sleep != json.loads(encode(expected.sleep)):
                raise TaskError("life_activity_transition_invalid", "life wake proposal changed")
        request = {"command_id": command_id, "owner_key": digest(owner), "context_key": context.key,
            "kind": "activity.transition", "expected_revision": activity.revision,
            "payload": {"activity_id": activity.activity_id, "operation_id": operation_id + self.role_suffix,
                "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest(),
                "state": self.transition_state.value, "observed_at": stamp(observed_at), "checkpoint": self.checkpoint,
                "reading": None, "work": None, "sleep": sleep, "event_consumers": self._consumers(expected.sleep),
                "management": {"operation_id": operation_id, "parameter_hash": digest(data)}}}
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def _authorize(self, operator, data, request, proposal, *, observed_at):
        operation_id, command_id = self._identity(operator, data)
        self._authorize_routine()
        payload = request["payload"]
        if (request["command_id"] != command_id or payload.get("management") !=
                {"operation_id": operation_id, "parameter_hash": digest(data)}
                or payload["activity_id"] != data["activity_id"]
                or request["expected_revision"] != data["expected_revision"]):
            raise TaskError("life_activity_transition_invalid", "wake management identity changed")
        previous = activity_from_json(proposal["previous_json"])
        at = datetime.fromisoformat(payload["observed_at"])
        if (previous.work.kind != "sleep" or previous.state not in (State.ACTIVE, State.PAUSED)
                or at < previous.started_at or (self.action == "wake" and at == previous.started_at)
                or payload["checkpoint"] != self.checkpoint):
            raise TaskError("life_sleep_invalid", "sleep has not actually started")
        expected, _ = self._expected(previous, at)
        if activity_from_json(proposal["result_json"]) != expected:
            raise TaskError("life_activity_transition_invalid", "sleep result changed")
        def completion(_context, _previous, result, _request, _now):
            return Outcome.success(result == expected)
        return activity_transition_authority.authorize(self.manager.app.tasks, self.manager.app.role_journal.sources,
            self.manager.context, request, proposal, expected_operation_id=operation_id + self.role_suffix,
            expected_consumers=self._consumers(expected.sleep), observed_at=observed_at, verify_completion=completion,
            require_life_authority=self.action == "wake")

    def _role_parameters(self, request):
        payload = request["payload"]
        receipt = RoleSleepReceipt(**payload["sleep"])
        return digest([self.manager.owner, payload["activity_id"], request["expected_revision"],
                       self.transition_state, self.checkpoint, receipt])

    def decide(self, operator, data, request, *, observed_at):
        self._outside_host_transaction()
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        existing = life_commands.lookup(self.runtime, context_key=context.key, command_id=request["command_id"],
                                        request_hash=request_hash)
        if existing is not None:
            self._check(data, request, existing)
            return existing
        proposal = self.provider.proposal(request)
        role_hash = self._role_parameters(request)
        management_id = request["payload"]["management"]["operation_id"]
        role_id = request["payload"]["operation_id"]
        decision_id = "life-decision-" + digest([context.key, request["command_id"]])

        def reserve(connection, _request):
            for identifier, owner, parameters in ((management_id, "role.life.management", digest(data)),
                                                   (role_id, "role.life", role_hash)):
                operation = OperationRef(identifier, owner, context.scope, VersionRef(identifier, 1), identifier)
                receipt = require(OperationRepository(self.runtime).reserve(operation, parameter_hash=parameters, observed_at=observed_at))
                if receipt.replayed:
                    raise TaskError("life_activity_transition_unconfirmed", "wake operation lacks its original decision")
            evidence = {"request_hash": request_hash, "operation_id": management_id, "parameter_hash": digest(data),
                        "scope_hash": digest(context.scope), "role_operation_id": role_id, "role_parameter_hash": role_hash}
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)", (PREFIX + decision_id, encode(evidence)))

        with self.manager.app.role_life.consistent_snapshot():
            return life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=observed_at,
                verify_prepared=self.provider.verify_prepared,
                authorize=lambda _context, at: self._authorize(operator, data, request, proposal, observed_at=at), apply=reserve)

    def _check(self, data, request, decision):
        context = self.manager.context
        _, request_hash = life_commands._request(context, request)
        if (decision.get("decision") != "commit" or decision.get("request_hash") != request_hash
                or not life_commands.verify_decision(self.runtime, decision)):
            raise TaskError("life_activity_transition_invalid", "wake decision changed")
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + decision["decision_id"],))
        try:
            record = json.loads(row["value"])
            payload = request["payload"]
            parameters = digest(data) if data is not None else payload["management"]["parameter_hash"]
            if (set(record) != {"request_hash", "operation_id", "parameter_hash", "scope_hash", "role_operation_id", "role_parameter_hash"}
                    or record["request_hash"] != request_hash or record["scope_hash"] != digest(context.scope)
                    or record["operation_id"] != payload["management"]["operation_id"]
                    or record["parameter_hash"] != parameters or payload["management"]["parameter_hash"] != parameters
                    or record["role_operation_id"] != payload["operation_id"]
                    or payload["operation_id"] != record["operation_id"] + self.role_suffix
                    or payload["state"] != self.transition_state.value or payload["checkpoint"] != self.checkpoint
                    or record["role_parameter_hash"] != self._role_parameters(request)):
                raise ValueError("wake evidence changed")
            for identifier, owner, parameters in ((record["operation_id"], "role.life.management", record["parameter_hash"]),
                                                   (record["role_operation_id"], "role.life", record["role_parameter_hash"])):
                receipt = OperationRepository(self.runtime).get(identifier)
                if (not receipt.is_success or receipt.value.operation.owner != owner or receipt.value.operation.scope != context.scope
                        or receipt.value.parameter_hash != parameters or receipt.value.commit_state is not CommitState.COMMITTED
                        or receipt.value.operation.version != VersionRef(identifier, 1)
                        or receipt.value.operation.idempotency_key != identifier
                        or receipt.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                    raise ValueError("wake receipt changed")
            return record
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_activity_transition_unconfirmed", "wake host evidence is unavailable") from error

    def complete(self, data, request, decision, *, observed_at):
        self._outside_host_transaction()
        record = self._check(data, request, decision)
        def finalize(_connection, _decision):
            self._check(data, request, decision)
            for identifier in (record["operation_id"], record["role_operation_id"]):
                require(OperationRepository(self.runtime).transition(identifier, expected_state=OperationState.ACCEPTED,
                                                                      next_state=OperationState.COMPLETED, observed_at=observed_at))
        return life_commands.complete(self.runtime, decision, observed_at=observed_at,
                                      verify_life_commit=self.provider.verify_commit, finalize=finalize)

    def acknowledge(self, data, request, decision):
        self._outside_host_transaction()
        record = self._check(data, request, decision)
        def verify(evidence):
            self._check(data, request, decision)
            receipts = [OperationRepository(self.runtime).get(record[key]) for key in ("operation_id", "role_operation_id")]
            return (life_commands.verify_completion(self.runtime, evidence)
                    and all(item.is_success and item.value.state is OperationState.COMPLETED for item in receipts))
        return self.provider.acknowledge(decision["command_id"], verify_host_completion=verify)


class ManualCancelSleepHandoff(ManualWakeHandoff):
    action = "cancel"
    command_prefix = "activity-cancel-sleep-"
    transition_state = State.CANCELLED
    checkpoint = "睡眠被手动结束并保留实际睡眠时长"
    role_suffix = ":cancel"

    def _authorize_routine(self):
        pass

    def _consumers(self, sleep):
        return []

    def _propose(self, activity, at, debt):
        return self.provider.propose_manual_cancel_sleep(activity=json.loads(encode(activity)), observed_at=stamp(at),
                                                          sleep_debt_minutes=debt)
