"""Authorize a state update on the host and commit its record in the life store."""

from datetime import datetime
import hashlib
import json
from threading import get_ident

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.role_work.state.ownership import role_owner
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from . import life_commands


PREFIX = "life.state.handoff.v1:"


class LifeStateCommandsProxy:
    def __init__(self, lease):
        self.lease = lease

    def __getattr__(self, name):
        if name not in {"prepare", "status", "find_request", "verify_prepared", "resolve", "verify_commit", "acknowledge"}:
            raise AttributeError(name)
        return lambda *args, **kwargs: self.lease.dispatch("state_command", name, *args, **kwargs)


class _HostStateEffect:
    def __init__(self, service, context, request, previous):
        self.service, self.context, self.request, self.previous = service, context, request, previous
        self.effect = self.parameter_hash = None

    def change(self, context, identifier, *, operation_key, expected_revision, parameters, transform, at):
        payload = self.request["payload"]
        if (context.key != self.context.key or identifier != "current" or expected_revision != self.request["expected_revision"]
                or stamp(at) != stamp(datetime.fromisoformat(payload["observed_at"]))):
            raise TaskError("life_state_conflict", "state operation changed during authorization")
        operation_id = "role_state:" + digest([self.service._legacy_records.operation_key(context), operation_key])
        if operation_id != payload["operation_id"]:
            raise TaskError("life_state_conflict", "state operation identity changed")
        operation = OperationRef(operation_id, "role_state", context.scope, VersionRef(operation_id, 1), operation_id)
        self.parameter_hash = digest([context.key, identifier, expected_revision, parameters])
        reserved = require(OperationRepository(self.service.runtime).reserve(operation,
            parameter_hash=self.parameter_hash, observed_at=at))
        if reserved.replayed:
            raise TaskError("life_state_unconfirmed", "state operation lacks its original life decision")
        record = {**transform(self.previous), "id": identifier, "revision": expected_revision + 1, "updated_at": stamp(at)}
        if len(encode(record).encode("utf-8")) > 131072:
            raise TaskError("record_too_large", "record exceeds its storage contract")
        receipt = {"operation_id": operation_id, "id": identifier, "revision": record["revision"],
                   "state": record.get("state", "saved"), "replayed": False}
        self.effect = {"record": record, "receipt": receipt}
        return receipt


class _LegacyStateReceipt:
    def __init__(self, handoff, operation):
        self.handoff, self.operation = handoff, operation

    def change(self, context, identifier, *, operation_key, expected_revision, parameters, transform, at):
        operation_id = "role_state:" + digest([self.handoff.service._legacy_records.operation_key(context), operation_key])
        if (self.operation.operation.operation_id != operation_id or self.operation.operation.scope != context.scope
                or self.operation.operation.owner != "role_state" or self.operation.state is not OperationState.COMPLETED
                or self.operation.commit_state is not CommitState.COMMITTED):
            raise TaskError("life_state_unconfirmed", "original state operation is not confirmed")
        if self.operation.parameter_hash != digest([context.key, identifier, expected_revision, parameters]):
            raise TaskError("idempotency_key_conflict", "existing state operation uses different parameters")
        return {**self.handoff.records.receipt(context, operation_id), "replayed": True}


class StateWriteHandoff:
    def __init__(self, service, records, commands):
        self.service, self.records, self.commands, self.runtime = service, records, commands, service.runtime

    def _outside_host_transaction(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_state_transaction_active", "state handoff requires an independent host transaction")

    def _effect(self, context, request, decision):
        _, request_hash = life_commands._request(context, request)
        if (decision.get("request_hash") != request_hash or decision.get("decision") != "commit"
                or not life_commands.verify_decision(self.runtime, decision)):
            raise TaskError("life_state_conflict", "state decision is unavailable")
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + decision["decision_id"],))
        try:
            evidence = json.loads(row["value"])
            if (type(evidence) is not dict or set(evidence) != {"request_hash", "scope_hash", "operation_id", "parameter_hash", "effect"}
                    or evidence["request_hash"] != request_hash or evidence["scope_hash"] != digest(context.scope)
                    or evidence["operation_id"] != request["payload"]["operation_id"]):
                raise ValueError("state evidence differs")
            operation = OperationRepository(self.runtime).get(evidence["operation_id"])
            if (not operation.is_success or operation.value.operation.owner != "role_state"
                    or operation.value.operation.scope != context.scope or operation.value.parameter_hash != evidence["parameter_hash"]
                    or operation.value.operation.version != VersionRef(evidence["operation_id"], 1)
                    or operation.value.operation.idempotency_key != evidence["operation_id"]
                    or operation.value.commit_state is not CommitState.COMMITTED
                    or operation.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                raise ValueError("state audit differs")
            return evidence
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_state_unconfirmed", "state host evidence is unavailable") from error

    def _finish(self, context, request, decision):
        evidence = self._effect(context, request, decision)
        result = self.commands.resolve(decision,
            verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
            read_host_effect=lambda value: self._effect(context, request, value)["effect"])

        def finalize(_connection, _decision):
            self._effect(context, request, decision)
            require(OperationRepository(self.runtime).transition(evidence["operation_id"],
                expected_state=OperationState.ACCEPTED, next_state=OperationState.COMPLETED, observed_at=self.service.tasks.now()))

        life_commands.complete(self.runtime, decision, observed_at=self.service.tasks.now(),
                               verify_life_commit=self.commands.verify_commit, finalize=finalize)

        def verify(value):
            operation = OperationRepository(self.runtime).get(evidence["operation_id"])
            return (life_commands.verify_completion(self.runtime, value) and operation.is_success
                    and operation.value.state is OperationState.COMPLETED
                    and operation.value.parameter_hash == evidence["parameter_hash"])
        self.commands.acknowledge(decision["command_id"], verify_host_completion=verify)
        return result["result"]["receipt"]

    def recover(self, context, request):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(context, request)
        if request["kind"] != "state.change" or not request["command_id"].startswith("life-state-"):
            raise TaskError("life_state_command_invalid", "only original state commands can be recovered")
        decision = life_commands.lookup(self.runtime, context_key=context.key, command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            decision = life_commands.decide(self.runtime, context=context, request=request, outcome="abort",
                observed_at=self.service.tasks.now(), verify_prepared=self.commands.verify_prepared,
                authorize=lambda *_: None, apply=lambda *_: None)
        if decision["decision"] == "abort":
            self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                                  read_host_effect=lambda _value: None)
            return {"state": "aborted"}
        return {"state": "completed", "receipt": self._finish(context, request, decision)}

    def write(self, context, *, data, expected_revision, operation_key, sources=(), operator=None, reason="maintenance",
              cycle_alignment=None, projected_inputs=False, expected_settings_token=None, mood_interest=None,
              preserve_mood_dynamics=False, dialogue_through=None, precommit=None):
        self._outside_host_transaction()
        if (type(operation_key) is not str or not operation_key.strip() or len(operation_key) > 512
                or type(expected_revision) is not int or expected_revision < 0
                or (precommit is not None and (not callable(precommit) or operator is not None or not projected_inputs))):
            raise TaskError("record_command_invalid", "invalid state operation identity or revision")
        self.service.access.check(context, "role.state.maintain", operator=operator)
        owner = role_owner(context)
        owner_key, context_key = digest(owner), context.key
        role_key, legacy_key = self.records.owner_key(context), self.records.legacy.owner_key(context)
        operation_id = "role_state:" + digest([self.service._legacy_records.operation_key(context), operation_key])
        command_id = "life-state-" + digest([context_key, operation_key])
        command_hash = digest([data, expected_revision, sources, operator.subject if operator else None,
            operator.binding_id if operator else None, operator.binding_revision if operator else None, reason, cycle_alignment,
            projected_inputs, expected_settings_token, mood_interest, preserve_mood_dynamics, dialogue_through])
        request = self.commands.find_request(command_id=command_id, owner_key=owner_key, context_key=context_key)
        if request is not None:
            if request["payload"]["command_hash"] != command_hash or request["payload"]["operation_id"] != operation_id:
                raise TaskError("idempotency_key_conflict", "existing state operation uses different parameters")
            recovered = self.recover(context, request)
            if recovered["state"] == "completed":
                return {**recovered["receipt"], "replayed": True}
            raise TaskError("life_state_aborted", "the original state request was interrupted before authorization")
        existing = OperationRepository(self.runtime).get(operation_id)
        if existing.is_success:
            with self.service.life.consistent_snapshot():
                return self.service._write(context, data=data, expected_revision=expected_revision, operation_key=operation_key,
                    sources=sources, operator=operator, reason=reason, cycle_alignment=cycle_alignment,
                    projected_inputs=projected_inputs, expected_settings_token=expected_settings_token, mood_interest=mood_interest,
                    preserve_mood_dynamics=preserve_mood_dynamics, records=_LegacyStateReceipt(self, existing.value))
        raw = self.records._call("current_raw", owner_key=role_key, legacy_key=legacy_key)
        previous = json.loads(raw) if raw is not None else None
        now = self.service.tasks.now()
        if dialogue_through is not None and (operator is not None or not projected_inputs or dialogue_through > now):
            raise TaskError("life_state_cursor_invalid", "dialogue cursor requires an authorized maintenance round")
        request = {"command_id": command_id, "owner_key": owner_key, "context_key": context_key,
                   "kind": "state.change", "expected_revision": expected_revision,
                   "payload": {"storage_key": role_key, "legacy_key": legacy_key, "record_id": "current",
                       "operation_id": operation_id, "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw else None,
                       "observed_at": stamp(now), "command_hash": command_hash,
                       "dialogue_through": stamp(dialogue_through) if dialogue_through is not None else None}}
        self.commands.prepare(request, prepared_at=now)
        _, request_hash = life_commands._request(context, request)
        decision_id = "life-decision-" + digest([context_key, command_id])

        def authorize(value, at):
            self.service.access.check(value, "role.state.maintain", operator=operator, at=at)
            if self.service.state_owner() != "good_life":
                raise TaskError("life_state_owner_changed", "state ownership changed")
            if precommit is not None:
                precommit()
            return Outcome.success(True)

        def apply(connection, _request):
            records = _HostStateEffect(self.service, context, request, previous)
            self.service._write(context, data=data, expected_revision=expected_revision, operation_key=operation_key,
                sources=sources, operator=operator, reason=reason, cycle_alignment=cycle_alignment,
                projected_inputs=projected_inputs, expected_settings_token=expected_settings_token, mood_interest=mood_interest,
                preserve_mood_dynamics=preserve_mood_dynamics, records=records, observed_at=now)
            evidence = {"request_hash": request_hash, "scope_hash": digest(context.scope), "operation_id": operation_id,
                        "parameter_hash": records.parameter_hash, "effect": records.effect}
            connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (PREFIX + decision_id, encode(evidence)))

        try:
            with self.service.life.consistent_snapshot():
                decision = life_commands.decide(self.runtime, context=context, request=request, outcome="commit",
                    observed_at=now, verify_prepared=self.commands.verify_prepared, authorize=authorize, apply=apply)
            return self._finish(context, request, decision)
        except Exception:
            if life_commands.lookup(self.runtime, context_key=context_key, command_id=command_id, request_hash=request_hash) is None:
                try:
                    self.recover(context, request)
                except (TaskError, ValueError):
                    pass
            raise
