"""Authorize and audit a due sleep transition proposed by the life plugin."""

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
from threading import get_ident

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.codec import activity_from_json
from companion_v2.role_work.life.physiology import oversleep_extension, sleep_quality_range
from companion_v2.role_work.models import RoleActivityState as State, advance_activity
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from . import activity_transition_authority, life_commands
from .manual_wake_handoff import _consumers


PREFIX = "life.activity.natural-wake.v1:"


class NaturalWakeHandoff:
    prefix = PREFIX

    def __init__(self, app, routine, provider):
        self.app, self.routine, self.provider, self.runtime = app, routine, provider, app.runtime

    def _outside_host_transaction(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_activity_transaction_active", "natural wake needs an independent host transaction")

    def _command_id(self, operation_id, observed_at):
        return "activity-due-" + digest([self.routine.context.key, operation_id, observed_at])

    def _proposal(self, activity, observed_at, debt):
        if activity.state is State.PAUSED:
            return self.provider.propose_resume_sleep(activity=json.loads(encode(activity)), observed_at=stamp(observed_at))
        return self.provider.propose_due_sleep(activity=json.loads(encode(activity)), observed_at=stamp(observed_at),
                                                sleep_debt_minutes=debt)

    def _expected(self, activity, at):
        if activity.work is None or activity.work.kind != "sleep":
            raise TaskError("life_sleep_invalid", "sleep is not due")
        identity = digest([activity.activity_id, activity.revision])
        if activity.state is State.PAUSED:
            receipt = activity.sleep
            if receipt is None or receipt.wake_until is None or receipt.wake_until > at or at < activity.updated_at:
                raise TaskError("life_sleep_invalid", "sleep wake grace has not ended")
            remaining = receipt.target_minutes * 60 - receipt.slept_seconds
            sleep = replace(receipt, wake_until=None)
            if remaining <= 0:
                return {"state": "completed", "checkpoint": "中断后已达到本段睡眠目标", "work": None,
                        "sleep": json.loads(encode(sleep)), "operation_id": "sleep-finish-short-" + identity,
                        "event_consumers": list(_consumers(sleep))}, None
            work = replace(activity.work, due_at=at + timedelta(seconds=remaining))
            return {"state": "active", "checkpoint": "清醒间隔结束，继续补足本段睡眠",
                    "work": json.loads(encode(work)), "sleep": json.loads(encode(sleep)),
                    "operation_id": "sleep-resume-" + identity, "event_consumers": []}, None
        if activity.state is not State.ACTIVE or activity.work.due_at > at or at <= activity.started_at:
            raise TaskError("life_sleep_invalid", "sleep is not due")
        body = self.app.role_state.body_clock
        receipt = activity.sleep or body._legacy_receipt(activity)
        elapsed = max(0, round((at - activity.updated_at).total_seconds()))
        total = min(172800, receipt.slept_seconds + elapsed)
        debt = body.profile(self.routine.context, now=at)["sleep_debt_minutes"]
        extension = oversleep_extension(activity, sleep_debt_minutes=debt)
        if extension:
            sleep = replace(receipt, slept_seconds=total, oversleep_minutes=extension)
            work = replace(activity.work, due_at=activity.work.due_at + timedelta(minutes=extension))
            return {"state": "active", "checkpoint": "睡眠延长片刻，稍后自然醒来", "work": json.loads(encode(work)),
                    "sleep": json.loads(encode(sleep)), "operation_id": "sleep-oversleep-" + identity,
                    "event_consumers": []}, debt
        low, high = sleep_quality_range(receipt.workload_score, debt, receipt.interruptions, receipt.early_interruptions)
        sleep = replace(receipt, slept_seconds=total, wake_until=None, quality_min=low, quality_max=high)
        return {"state": "completed", "checkpoint": "睡眠结束；记录实际恢复与睡眠质量范围", "work": None,
                "sleep": json.loads(encode(sleep)), "operation_id": "body-wake-" + identity,
                "event_consumers": list(_consumers(sleep))}, debt

    def prepare(self, activity_id, *, expected_revision, observed_at):
        self._outside_host_transaction()
        if self.app.role_life.activity_owner() != "good_life":
            raise TaskError("life_activity_owner_invalid", "natural wake belongs to the life plugin")
        self.routine.refresh()
        if not self.routine.settings or not self.routine.settings["enabled"]:
            raise TaskError("life_disabled", "the original life routine is disabled")
        with self.app.role_life.consistent_snapshot():
            self.routine.authorize()
            activity = self.routine.activity(activity_id)
            if activity.revision != expected_revision:
                raise TaskError("life_revision_conflict", "sleep activity changed")
            expected, debt = self._expected(activity, observed_at)
            proposal = self._proposal(activity, observed_at, debt)
            if proposal != expected:
                raise TaskError("life_activity_transition_invalid", "life due-sleep proposal changed")
        context, owner = self.routine.context, self.routine.owner
        request = {"command_id": self._command_id(expected["operation_id"], observed_at),
            "owner_key": digest(owner), "context_key": context.key, "kind": "activity.transition",
            "expected_revision": expected_revision,
            "payload": {"activity_id": activity_id, "operation_id": expected["operation_id"],
                "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest(),
                "state": expected["state"], "observed_at": stamp(observed_at), "checkpoint": expected["checkpoint"],
                "reading": None, "work": expected["work"], "sleep": expected["sleep"],
                "event_consumers": expected["event_consumers"]}}
        self.provider.prepare(request, prepared_at=observed_at)
        return request

    def _parameters(self, request):
        payload = request["payload"]
        parameters = [self.routine.owner, payload["activity_id"], request["expected_revision"],
                      State(payload["state"]), payload["checkpoint"]]
        if payload["work"] is not None:
            parameters.append(payload["work"])
        if payload["sleep"] is not None:
            parameters.append(payload["sleep"])
        return digest(parameters)

    def _authorize(self, request, proposal, *, observed_at):
        self.routine.authorize()
        if not self.routine.settings or not self.routine.settings["enabled"]:
            raise TaskError("life_disabled", "the original life routine is disabled")
        previous = activity_from_json(proposal["previous_json"])
        payload = request["payload"]
        at = datetime.fromisoformat(payload["observed_at"])
        expected, _ = self._expected(previous, at)
        if any(payload[key] != expected[key] for key in ("state", "checkpoint", "work", "sleep", "operation_id", "event_consumers")):
            raise TaskError("life_activity_transition_invalid", "due sleep outcome changed")
        if request["command_id"] != self._command_id(expected["operation_id"], at):
            raise TaskError("life_activity_transition_invalid", "due sleep operation changed")
        result = activity_from_json(proposal["result_json"])
        expected_result = advance_activity(previous, expected_revision=previous.revision, state=State(expected["state"]),
            observed_at=at, checkpoint=expected["checkpoint"], sleep=result.sleep,
            work=result.work if expected["work"] is not None else None)
        return activity_transition_authority.authorize(self.app.tasks, self.app.role_journal.sources, self.routine.context,
            request, proposal, expected_operation_id=expected["operation_id"], expected_consumers=expected["event_consumers"],
            observed_at=observed_at, verify_completion=lambda *_: Outcome.success(result == expected_result))

    def decide(self, request, *, observed_at):
        self._outside_host_transaction()
        context = self.routine.context
        _, request_hash = life_commands._request(context, request)
        existing = life_commands.lookup(self.runtime, context_key=context.key, command_id=request["command_id"],
                                        request_hash=request_hash)
        if existing is not None:
            self._check(request, existing)
            return existing
        self.routine.refresh()
        proposal = self.provider.proposal(request)
        role_id, role_hash = request["payload"]["operation_id"], self._parameters(request)
        decision_id = "life-decision-" + digest([context.key, request["command_id"]])
        def reserve(connection, _request):
            operation = OperationRef(role_id, "role.life", context.scope, VersionRef(role_id, 1), role_id)
            receipt = require(OperationRepository(self.runtime).reserve(operation, parameter_hash=role_hash, observed_at=observed_at))
            if receipt.replayed:
                raise TaskError("life_activity_transition_unconfirmed", "natural wake lacks its original decision")
            evidence = {"request_hash": request_hash, "scope_hash": digest(context.scope),
                        "role_operation_id": role_id, "role_parameter_hash": role_hash}
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)", (self.prefix + decision_id, encode(evidence)))
        with self.app.role_life.consistent_snapshot():
            return life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=observed_at,
                verify_prepared=self.provider.verify_prepared,
                authorize=lambda _context, at: self._authorize(request, proposal, observed_at=at), apply=reserve)

    def _check(self, request, decision):
        context = self.routine.context
        _, request_hash = life_commands._request(context, request)
        if (decision.get("decision") != "commit" or decision.get("request_hash") != request_hash
                or not life_commands.verify_decision(self.runtime, decision)):
            raise TaskError("life_activity_transition_invalid", "natural wake decision changed")
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (self.prefix + decision["decision_id"],))
        try:
            record = json.loads(row["value"])
            role_id = request["payload"]["operation_id"]
            if record != {"request_hash": request_hash, "scope_hash": digest(context.scope),
                           "role_operation_id": role_id, "role_parameter_hash": self._parameters(request)}:
                raise ValueError("natural wake evidence changed")
            receipt = OperationRepository(self.runtime).get(role_id)
            if (not receipt.is_success or receipt.value.operation.owner != "role.life" or receipt.value.operation.scope != context.scope
                    or receipt.value.parameter_hash != record["role_parameter_hash"]
                    or receipt.value.commit_state is not CommitState.COMMITTED
                    or receipt.value.operation.version != VersionRef(role_id, 1) or receipt.value.operation.idempotency_key != role_id
                    or receipt.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                raise ValueError("natural wake receipt changed")
            return record
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_activity_transition_unconfirmed", "natural wake host evidence is unavailable") from error

    def apply(self, request, decision):
        self._outside_host_transaction()
        self._check(request, decision)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def abort(self, request, *, observed_at):
        self._outside_host_transaction()
        decision = life_commands.decide(self.runtime, context=self.routine.context, request=request, outcome="abort",
            observed_at=observed_at, verify_prepared=self.provider.verify_prepared,
            authorize=lambda *_: None, apply=lambda *_: None)
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def complete(self, request, decision, *, observed_at):
        self._outside_host_transaction()
        record = self._check(request, decision)
        def finalize(_connection, _decision):
            self._check(request, decision)
            require(OperationRepository(self.runtime).transition(record["role_operation_id"], expected_state=OperationState.ACCEPTED,
                                                                  next_state=OperationState.COMPLETED, observed_at=observed_at))
        return life_commands.complete(self.runtime, decision, observed_at=observed_at,
                                      verify_life_commit=self.provider.verify_commit, finalize=finalize)

    def acknowledge(self, request, decision):
        self._outside_host_transaction()
        record = self._check(request, decision)
        def verify(evidence):
            self._check(request, decision)
            receipt = OperationRepository(self.runtime).get(record["role_operation_id"])
            return (life_commands.verify_completion(self.runtime, evidence) and receipt.is_success
                    and receipt.value.state is OperationState.COMPLETED)
        return self.provider.acknowledge(decision["command_id"], verify_host_completion=verify)

    async def execute(self, activity_id, *, expected_revision):
        """Run a due timer only after the original routine's async preflight."""
        await self.routine.preflight()
        at = self.app.now()
        request = self.prepare(activity_id, expected_revision=expected_revision, observed_at=at)
        try:
            decision = self.decide(request, observed_at=at)
        except (TaskError, ValueError):
            try:
                self.abort(request, observed_at=at)
            except (TaskError, ValueError):
                pass
            raise
        result = self.apply(request, decision)
        self.complete(request, decision, observed_at=at)
        self.acknowledge(request, decision)
        self.app.notify()
        return result

    async def recover(self, request):
        """Resume the original durable command without fabricating a new wake."""
        self._outside_host_transaction()
        _, request_hash = life_commands._request(self.routine.context, request)
        decision = life_commands.lookup(self.runtime, context_key=self.routine.context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            await self.routine.preflight()
            decision = self.decide(request, observed_at=self.app.now())
        else:
            self._check(request, decision)
        result = self.apply(request, decision)
        self.complete(request, decision, observed_at=self.app.now())
        self.acknowledge(request, decision)
        self.app.notify()
        return result
