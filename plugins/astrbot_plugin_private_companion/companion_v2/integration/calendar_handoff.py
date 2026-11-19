"""Coordinate one prepared calendar command across the host and life stores.

The provider exposes only calendar operations and verification callbacks. The
caller must supply the original context, sources and operation key; this module
is not yet connected to the production calendar entry points.
"""

import json
from threading import get_ident

from companion_v2.execution.models import TaskError, digest, encode, require
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository

from . import calendar_authority, calendar_reminder_hold, life_commands


PREFIX = "life.calendar.handoff.v1:"


def calendar_parameters(action, data, sources, actor, operator, planning):
    if action == "remind":
        return ["remind", data["sequence"], operator.subject if operator else None]
    return [action, data, sources, actor] + ([planning] if planning is not None else [])


class CalendarHandoff:
    def __init__(self, calendar, provider):
        self.calendar, self.provider, self.runtime = calendar, provider, calendar.runtime

    def _outside_host_transaction(self):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            raise TaskError("life_calendar_transaction_active", "calendar handoff requires an independent host transaction")

    def _check_request(self, context, request, *, operation_key, sources, operator):
        try:
            life_commands._request(context, request)
            if (request["kind"] != "calendar.change" or type(operation_key) is not str
                    or not operation_key.strip() or len(operation_key) > 512 or not isinstance(sources, (tuple, list))):
                raise ValueError("calendar command identity is invalid")
            payload = request["payload"]
            actor = operator.subject if operator else context.role_binding.role
            operation_id = "calendar:" + digest([self.calendar._legacy_records.operation_key(context), operation_key])
            keys = {self.calendar._legacy_records.role_key(context), self.calendar._legacy_records.conversation_key(context)}
            if (payload["operation_id"] != operation_id or payload["sources"] != json.loads(encode(sources))
                    or payload["actor"] != json.loads(encode(actor))
                    or payload["operator_authenticated"] is not (operator is not None)
                    or set(payload["lookup_owner_keys"]) != keys or len(payload["lookup_owner_keys"]) != len(keys)
                    or payload["storage_key"] not in keys):
                raise ValueError("calendar command crosses its original authority")
        except TaskError:
            raise
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise TaskError("life_calendar_command_invalid", "calendar identity or authority is invalid") from error

    def prepare(self, context, request, *, operation_key, sources, operator, observed_at):
        self._outside_host_transaction()
        self._check_request(context, request, operation_key=operation_key, sources=sources, operator=operator)
        self.calendar.access.check(context, "calendar.write", operator=operator, at=observed_at)
        self.calendar._evidence(context, sources, at=observed_at)
        return self.provider.prepare(request, prepared_at=observed_at)

    def decide(self, context, request, *, operation_key, sources, operator, observed_at):
        self._outside_host_transaction()
        self._check_request(context, request, operation_key=operation_key, sources=sources, operator=operator)
        self.calendar.access.check(context, "calendar.write", operator=operator, at=observed_at)
        self.calendar._evidence(context, sources, at=observed_at)
        status = self.provider.status(request)
        if status["state"] == "aborted" or status["proposal"] is None:
            raise TaskError("life_calendar_aborted", "the original calendar command was aborted")
        proposal, payload = status["proposal"], request["payload"]
        actor = operator.subject if operator else context.role_binding.role
        parameters = calendar_parameters(payload["action"], payload["data"], sources, actor, operator, payload["planning"])
        parameter_hash = digest([context.key, payload["record_id"], request["expected_revision"], parameters])
        operation_id = "calendar:" + digest([self.calendar._legacy_records.operation_key(context), operation_key])
        operation = OperationRef(operation_id, "calendar", context.scope, VersionRef(operation_id, 1), operation_id)
        decision_id = "life-decision-" + digest([context.key, request["command_id"]])

        def authorize(original_context, at):
            return calendar_authority.authorize(self.calendar, original_context, request, proposal,
                operation_key=operation_key, sources=sources, operator=operator, observed_at=at)

        def reserve(connection, _request):
            reserved = require(OperationRepository(self.runtime).reserve(operation,
                parameter_hash=parameter_hash, observed_at=observed_at))
            if reserved.replayed:
                raise TaskError("life_calendar_unconfirmed", "calendar operation lacks its original handoff decision")
            previous = proposal["cancel_reminder"]
            reminder = self.calendar.agenda.repository.reminder(previous["reminder_id"]) if previous else None
            held = None
            if reminder and reminder.state not in ("cancelled", "completed"):
                calendar_reminder_hold.hold(connection, reminder.reminder_id, decision_id)
                held = {"id": reminder.reminder_id, "revision": reminder.revision}
            record = {"request_hash": status["request_hash"], "operation_id": operation_id,
                      "parameter_hash": parameter_hash, "operation_key": operation_key,
                      "storage_key": payload["storage_key"], "scope_hash": digest(context.scope), "reminder": held}
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, ?)",
                               (PREFIX + decision_id, encode(record)))

        decision = life_commands.decide(self.runtime, context=context, request=request, outcome="commit",
            observed_at=observed_at, verify_prepared=self.provider.verify_prepared,
            authorize=authorize, apply=reserve)
        record = self._evidence(decision)
        if (record is None or record["request_hash"] != status["request_hash"] or record["operation_id"] != operation_id
                or record["parameter_hash"] != parameter_hash or record["operation_key"] != operation_key
                or record["storage_key"] != payload["storage_key"] or record["scope_hash"] != digest(context.scope)):
            raise TaskError("life_calendar_conflict", "calendar handoff differs from its original decision")
        return decision

    def apply(self, decision):
        self._outside_host_transaction()
        return self.provider.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))

    def complete(self, context, request, decision, *, observed_at):
        self._outside_host_transaction()
        _, request_hash = life_commands._request(context, request)
        record = self._evidence(decision)
        if (record is None or record["request_hash"] != request_hash or decision["request_hash"] != request_hash
                or record["operation_id"] != request["payload"]["operation_id"]
                or record["storage_key"] != request["payload"]["storage_key"]
                or record["scope_hash"] != digest(context.scope)
                or record["operation_id"] != "calendar:" + digest([self.calendar._legacy_records.operation_key(context), record["operation_key"]])):
            raise TaskError("life_calendar_conflict", "calendar handoff no longer matches its request")
        proposal = self.provider.status(request)["proposal"]
        if proposal is None or proposal["receipt"]["operation_id"] != record["operation_id"]:
            raise TaskError("life_calendar_unconfirmed", "calendar result is unavailable")
        original = OperationRepository(self.runtime).get(record["operation_id"])
        if (not original.is_success or original.value.operation.scope != context.scope
                or original.value.operation.owner != "calendar" or original.value.parameter_hash != record["parameter_hash"]
                or original.value.state not in (OperationState.ACCEPTED, OperationState.COMPLETED)):
            raise TaskError("life_calendar_conflict", "calendar operation audit changed")

        def finalize(connection, _decision):
            held = record["reminder"]
            if held is not None:
                previous = proposal["cancel_reminder"]
                reminder = self.calendar.agenda.repository.reminder(held["id"])
                if (previous is None or previous.get("reminder_id") != held["id"] or reminder is None
                        or reminder.revision != held["revision"] or not calendar_reminder_hold.held(self.runtime, held["id"])):
                    raise TaskError("calendar_reminder_changed", "the original held reminder changed")
                self.calendar._cancel_reminder_at_storage(context, previous, record["operation_key"], record["storage_key"])
                calendar_reminder_hold.release(connection, held["id"], decision["decision_id"])
            elif proposal["cancel_reminder"] is not None:
                identifier = proposal["cancel_reminder"]["reminder_id"]
                reminder = self.calendar.agenda.repository.reminder(identifier)
                if calendar_reminder_hold.held(self.runtime, identifier) or (reminder and reminder.state not in ("cancelled", "completed")):
                    raise TaskError("calendar_reminder_changed", "the original reminder no longer matches the decision")
            payload = request["payload"]
            if payload["action"] == "remind":
                from companion_v2.agenda.models import Schedule
                previous, data = proposal["previous"], payload["data"]
                due = calendar_authority.reminder_due(previous, data["sequence"],
                    calendar_authority.request_time(payload))
                require(self.calendar.agenda.create_reminder(context.actor, target=self.calendar._target(context),
                    reminder_id=data["reminder_id"], message="日历提醒：" + previous["title"],
                    schedule=Schedule(due, previous["schedule"]["timezone_name"]),
                    operation=self.calendar._operation(context, record["operation_key"]),
                    expected_role_revision=context.role_binding.revision, observed_at=observed_at))
            require(OperationRepository(self.runtime).transition(record["operation_id"],
                expected_state=OperationState.ACCEPTED, next_state=OperationState.COMPLETED, observed_at=observed_at))

        return life_commands.complete(self.runtime, decision, observed_at=observed_at,
                                      verify_life_commit=self.provider.verify_commit, finalize=finalize)

    def acknowledge(self, decision):
        self._outside_host_transaction()
        record = self._evidence(decision)
        if record is None:
            raise TaskError("life_calendar_conflict", "calendar handoff evidence is unavailable")

        def verify(evidence):
            receipt = OperationRepository(self.runtime).get(record["operation_id"])
            held = record["reminder"]
            return (life_commands.verify_completion(self.runtime, evidence) and receipt.is_success
                    and receipt.value.state is OperationState.COMPLETED and receipt.value.operation.owner == "calendar"
                    and receipt.value.parameter_hash == record["parameter_hash"]
                    and receipt.value.operation.idempotency_key == record["operation_id"]
                    and receipt.value.operation.version == VersionRef(record["operation_id"], 1)
                    and digest(receipt.value.operation.scope) == record["scope_hash"]
                    and (held is None or not calendar_reminder_hold.held(self.runtime, held["id"])))

        return self.provider.acknowledge(decision["command_id"], verify_host_completion=verify)

    def _evidence(self, decision):
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (PREFIX + decision["decision_id"],))
        if row is None:
            return None
        try:
            record = json.loads(row["value"])
            if type(record) is not dict or set(record) != {"request_hash", "operation_id", "parameter_hash", "operation_key", "storage_key", "scope_hash", "reminder"}:
                return None
            if record["request_hash"] != decision["request_hash"]:
                return None
            if record["reminder"] is not None and (type(record["reminder"]) is not dict
                    or set(record["reminder"]) != {"id", "revision"}
                    or type(record["reminder"]["id"]) is not str
                    or type(record["reminder"]["revision"]) is not int or record["reminder"]["revision"] < 1):
                return None
            return record
        except (TypeError, ValueError):
            return None
