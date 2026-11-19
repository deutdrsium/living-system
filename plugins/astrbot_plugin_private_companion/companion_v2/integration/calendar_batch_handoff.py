"""Coordinate an atomic life-owned calendar batch and host reminder effects."""

import json

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.identity_policy.private_domain import source_ref
from companion_v2.storage.models import CommitState, OperationState
from companion_v2.storage.repositories import OperationRepository

from . import calendar_authority, calendar_reminder_hold, life_commands
from .calendar_handoff import CalendarHandoff, calendar_parameters


PREFIX = "life.calendar.batch.v1:"


class CalendarBatchHandoff:
    def __init__(self, calendar, provider):
        self.calendar, self.provider, self.runtime = calendar, provider, calendar.runtime
        self.single = CalendarHandoff(calendar, provider)

    def _call(self, name, *args, **kwargs):
        return self.provider.batch(name, *args, **kwargs)

    def _items(self, context, request, operation_keys):
        life_commands._request(context, request)
        if request["kind"] != "calendar.batch" or type(operation_keys) not in (list, tuple):
            raise TaskError("life_calendar_command_invalid", "calendar batch identity is invalid")
        items = request["payload"].get("commands")
        if type(items) is not list or len(items) != len(operation_keys):
            raise TaskError("life_calendar_command_invalid", "calendar batch operations changed")
        result = []
        for item, key in zip(items, operation_keys):
            payload = item["payload"]
            if payload["action"] not in ("create", "cancel", "refine", "exception") or payload["operator_authenticated"]:
                raise TaskError("life_calendar_command_invalid", "unsupported planning batch operation")
            sources = tuple(source_ref(value) for value in payload["sources"])
            self.single._check_request(context, item, operation_key=key, sources=sources, operator=None)
            result.append((item, key, sources))
        return result

    def prepare(self, context, request, *, operation_keys, observed_at):
        self.single._outside_host_transaction()
        for _item, _key, sources in self._items(context, request, operation_keys):
            self.calendar.access.check(context, "calendar.write", at=observed_at)
            self.calendar._evidence(context, sources, at=observed_at)
        return self._call("prepare", request, prepared_at=observed_at)

    def decide(self, context, request, *, operation_keys, observed_at, authorize_execution=None, planning_task_id=None):
        self.single._outside_host_transaction()
        items = self._items(context, request, operation_keys)
        status = self._call("status", request)
        proposal = status["proposal"]
        if proposal is None or len(proposal["proposals"]) != len(items):
            raise TaskError("life_calendar_unconfirmed", "calendar batch proposal is unavailable")
        decision_id = "life-decision-" + digest([context.key, request["command_id"]])
        operations = OperationRepository(self.runtime)
        scope_hash = digest(context.scope)
        if planning_task_id is not None:
            task_operation = operations.get(planning_task_id)
            if (not task_operation.is_success or task_operation.value.operation.owner != "calendar.planning"
                    or task_operation.value.operation.scope != context.scope
                    or task_operation.value.state is not OperationState.ACCEPTED):
                raise TaskError("life_calendar_unconfirmed", "planning task audit is unavailable")

        def authorize(_context, at):
            for (item, key, sources), candidate in zip(items, proposal["proposals"]):
                require(calendar_authority.authorize(self.calendar, context, item, candidate,
                    operation_key=key, sources=sources, operator=None, observed_at=at))
            if authorize_execution is not None:
                authorize_execution()
            return Outcome.success(True)

        def reserve(connection, _request):
            evidence = []
            for (item, key, sources), candidate in zip(items, proposal["proposals"]):
                payload = item["payload"]
                actor = context.role_binding.role
                params = calendar_parameters(payload["action"], payload["data"], sources, actor, None, payload["planning"])
                parameter_hash = digest([context.key, payload["record_id"], item["expected_revision"], params])
                identifier = payload["operation_id"]
                operation = OperationRef(identifier, "calendar", context.scope, VersionRef(identifier, 1), identifier)
                if require(operations.reserve(operation, parameter_hash=parameter_hash, observed_at=observed_at)).replayed:
                    raise TaskError("life_calendar_unconfirmed", "calendar batch operation already exists")
                previous = candidate["cancel_reminder"]
                reminder = self.calendar.agenda.repository.reminder(previous["reminder_id"]) if previous else None
                held = None
                if reminder and reminder.state not in ("cancelled", "completed"):
                    calendar_reminder_hold.hold(connection, reminder.reminder_id, decision_id)
                    held = {"id": reminder.reminder_id, "revision": reminder.revision}
                evidence.append({"operation_key": key, "operation_id": identifier, "parameter_hash": parameter_hash,
                                 "storage_key": payload["storage_key"], "reminder": held})
            connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (PREFIX + decision_id,
                encode({"request_hash": status["request_hash"], "scope_hash": scope_hash, "commands": evidence,
                        "planning_task_id": planning_task_id})))

        return life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=observed_at,
            verify_prepared=lambda value: self._call("verify_prepared", value), authorize=authorize, apply=reserve)

    def _evidence(self, context, request, decision):
        _, request_hash = life_commands._request(context, request)
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + decision["decision_id"],))
        try:
            record = json.loads(row["value"]) if row else None
            if (type(record) is not dict or set(record) != {"request_hash", "scope_hash", "commands", "planning_task_id"}
                    or record["request_hash"] != request_hash or decision["request_hash"] != request_hash
                    or record["scope_hash"] != digest(context.scope) or type(record["commands"]) is not list):
                raise ValueError("batch evidence changed")
            keys = [item["operation_key"] for item in record["commands"]]
            commands = self._items(context, request, keys)
            if len(commands) != len(record["commands"]):
                raise ValueError("batch operation count changed")
            for (item, key, sources), evidence in zip(commands, record["commands"]):
                if type(evidence) is not dict or set(evidence) != {"operation_key", "operation_id", "parameter_hash", "storage_key", "reminder"}:
                    raise ValueError("batch operation evidence changed")
                payload = item["payload"]
                params = calendar_parameters(payload["action"], payload["data"], sources,
                    context.role_binding.role, None, payload["planning"])
                expected_hash = digest([context.key, payload["record_id"], item["expected_revision"], params])
                original = OperationRepository(self.runtime).get(payload["operation_id"])
                if (evidence["operation_key"] != key or evidence["operation_id"] != payload["operation_id"]
                        or evidence["parameter_hash"] != expected_hash or evidence["storage_key"] != payload["storage_key"]
                        or not original.is_success or original.value.operation.scope != context.scope
                        or original.value.operation.owner != "calendar" or original.value.parameter_hash != expected_hash
                        or original.value.operation.idempotency_key != payload["operation_id"]
                        or original.value.operation.version != VersionRef(payload["operation_id"], 1)
                        or original.value.commit_state is not CommitState.COMMITTED
                        or original.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                    raise ValueError("batch operation audit changed")
            task_id = record["planning_task_id"]
            if task_id is not None:
                task_operation = OperationRepository(self.runtime).get(task_id)
                if (type(task_id) is not str or not task_operation.is_success
                        or task_operation.value.operation.owner != "calendar.planning"
                        or task_operation.value.operation.scope != context.scope
                        or task_operation.value.operation.idempotency_key != task_id
                        or task_operation.value.operation.version != VersionRef(task_id, 1)
                        or task_operation.value.commit_state is not CommitState.COMMITTED
                        or task_operation.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
                    raise ValueError("planning task audit changed")
            return record
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_calendar_unconfirmed", "calendar batch evidence is unavailable") from error

    def apply(self, decision):
        self.single._outside_host_transaction()
        return self._call("resolve", decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def complete(self, context, request, decision, *, observed_at):
        self.single._outside_host_transaction()
        evidence = self._evidence(context, request, decision)
        status = self._call("status", request)
        proposals = status["proposal"]["proposals"]
        if len(proposals) != len(evidence["commands"]):
            raise TaskError("life_calendar_unconfirmed", "calendar batch results are unavailable")

        def finalize(connection, _decision):
            for item, record, proposal in zip(request["payload"]["commands"], evidence["commands"], proposals):
                held, previous = record["reminder"], proposal["cancel_reminder"]
                if held is not None:
                    reminder = self.calendar.agenda.repository.reminder(held["id"])
                    if (previous is None or previous.get("reminder_id") != held["id"] or reminder is None
                            or reminder.revision != held["revision"] or not calendar_reminder_hold.held(self.runtime, held["id"])):
                        raise TaskError("calendar_reminder_changed", "the held reminder changed")
                    self.calendar._cancel_reminder_at_storage(context, previous, record["operation_key"], record["storage_key"])
                    calendar_reminder_hold.release(connection, held["id"], decision["decision_id"])
                elif previous is not None:
                    reminder = self.calendar.agenda.repository.reminder(previous["reminder_id"])
                    if calendar_reminder_hold.held(self.runtime, previous["reminder_id"]) or (reminder and reminder.state not in ("cancelled", "completed")):
                        raise TaskError("calendar_reminder_changed", "the original reminder changed")
                require(OperationRepository(self.runtime).transition(item["payload"]["operation_id"],
                    expected_state=OperationState.ACCEPTED, next_state=OperationState.COMPLETED, observed_at=observed_at))
            if evidence["planning_task_id"] is not None:
                require(OperationRepository(self.runtime).transition(evidence["planning_task_id"],
                    expected_state=OperationState.ACCEPTED, next_state=OperationState.COMPLETED, observed_at=observed_at))
        return life_commands.complete(self.runtime, decision, observed_at=observed_at,
            verify_life_commit=lambda value: self._call("verify_commit", value), finalize=finalize)

    def acknowledge(self, context, request, decision):
        self.single._outside_host_transaction()
        evidence = self._evidence(context, request, decision)
        def verify(value):
            if not life_commands.verify_completion(self.runtime, value):
                return False
            for record in evidence["commands"]:
                receipt = OperationRepository(self.runtime).get(record["operation_id"])
                if (not receipt.is_success or receipt.value.state is not OperationState.COMPLETED
                        or record["reminder"] is not None and calendar_reminder_hold.held(self.runtime, record["reminder"]["id"])):
                    return False
            if evidence["planning_task_id"] is not None:
                receipt = OperationRepository(self.runtime).get(evidence["planning_task_id"])
                if not receipt.is_success or receipt.value.state is not OperationState.COMPLETED:
                    return False
            return True
        return self._call("acknowledge", decision["command_id"], verify_host_completion=verify)

    def recover(self, context, request):
        self.single._outside_host_transaction()
        _, request_hash = life_commands._request(context, request)
        if request["kind"] != "calendar.batch" or not request["command_id"].startswith("life-calendar-batch-"):
            raise TaskError("life_calendar_command_invalid", "calendar batch recovery identity is invalid")
        decision = life_commands.lookup(self.runtime, context_key=context.key, command_id=request["command_id"],
                                        request_hash=request_hash)
        if decision is None:
            if self._call("status", request)["state"] != "prepared":
                raise TaskError("life_calendar_unconfirmed", "calendar batch has no host decision")
            decision = life_commands.decide(self.runtime, context=context, request=request, outcome="abort",
                observed_at=self.calendar.tasks.now(), verify_prepared=lambda value: self._call("verify_prepared", value),
                authorize=lambda *_: None, apply=lambda *_: None)
        if decision["decision"] == "abort":
            self.apply(decision)
            return {"state": "aborted"}
        self.apply(decision)
        self.complete(context, request, decision, observed_at=self.calendar.tasks.now())
        self.acknowledge(context, request, decision)
        return {"state": "completed"}
