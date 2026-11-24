"""Forward calendar changes to a life provider after a verified cutover.

The host binds this bridge only after a verified adoption and ownership change.
It is currently exercised with isolated records; an absent provider must never
fall back to a second writer.
"""

import hashlib
import json

from companion_v2.execution.models import TaskError, digest, encode
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository

from . import life_commands
from .calendar_handoff import CalendarHandoff, calendar_parameters


class CalendarCommandBridge:
    def __init__(self, calendar, records, commands, *, legacy_adopted):
        if legacy_adopted is not True:
            raise ValueError("calendar legacy ownership must be verified before cutover")
        self.calendar, self.records, self.commands = calendar, records, commands
        self.handoff = CalendarHandoff(calendar, commands)

    def command(self, context, *, identifier, action, expected_revision, operation_key,
                data=None, sources=(), operator=None, planning=None):
        try:
            return self._command(context, identifier=identifier, action=action, expected_revision=expected_revision,
                                 operation_key=operation_key, data=data, sources=sources, operator=operator, planning=planning)
        except TaskError:
            raise
        except Exception as error:
            code = getattr(error, "code", None)
            if code == "life_calendar_pending" or isinstance(error, (ValueError, KeyError, TypeError)):
                raise TaskError(code or "life_calendar_command_invalid", "calendar command could not be applied") from error
            raise

    def recover(self, context, request):
        """Finish a decided command or abort an abandoned preparation for this context.

        The caller holds the calendar bridge lock, so an undecided request cannot
        be an in-flight entry-point command. A decision is never invented on retry.
        """
        self.handoff._outside_host_transaction()
        _, request_hash = life_commands._request(context, request)
        if request["kind"] != "calendar.change" or not request["command_id"].startswith("life-calendar-"):
            raise TaskError("life_calendar_command_invalid", "only original calendar commands can be recovered")
        decision = life_commands.lookup(self.calendar.runtime, context_key=context.key,
                                        command_id=request["command_id"], request_hash=request_hash)
        if decision is None:
            status = self.commands.status(request)
            if status["state"] != "prepared":
                raise TaskError("life_calendar_unconfirmed", "calendar command has no host decision")
            self._abort_undecided(context, request, request_hash, self.calendar.tasks.now())
            decision = life_commands.lookup(self.calendar.runtime, context_key=context.key,
                                            command_id=request["command_id"], request_hash=request_hash)
            if decision is None:
                raise TaskError("life_calendar_unconfirmed", "calendar preparation could not be released")
        if decision["decision"] == "abort":
            self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.calendar.runtime, value))
            return {"state": "aborted"}
        self.handoff.apply(decision)
        self.handoff.complete(context, request, decision, observed_at=self.calendar.tasks.now())
        self.handoff.acknowledge(decision)
        return {"state": "completed"}

    def _command(self, context, *, identifier, action, expected_revision, operation_key,
                 data=None, sources=(), operator=None, planning=None):
        self.handoff._outside_host_transaction()
        if (type(identifier) is not str or not identifier.strip() or len(identifier) > 512
                or type(operation_key) is not str or not operation_key.strip() or len(operation_key) > 512
                or type(expected_revision) is not int or expected_revision < 0):
            raise TaskError("record_command_invalid", "invalid record identity or revision")
        now = self.calendar.tasks.now()
        self.calendar.access.check(context, "calendar.write", operator=operator, at=now)
        self.calendar._evidence(context, sources, at=now)
        role_key, conversation_key = self.calendar._legacy_records.role_key(context), self.calendar._legacy_records.conversation_key(context)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        actor = operator.subject if operator else context.role_binding.role
        operation_id = "calendar:" + digest([self.calendar._legacy_records.operation_key(context), operation_key])
        command_id = "life-calendar-" + digest([context.key, operation_key])
        parameters = calendar_parameters(action, data, sources, actor, operator, planning)
        parameter_hash = digest([context.key, identifier, expected_revision, parameters])
        existing = OperationRepository(self.calendar.runtime).get(operation_id)
        if existing.is_success and existing.value.parameter_hash != parameter_hash:
            raise TaskError("idempotency_key_conflict", "existing operation uses the same key with different parameters")
        request = self.commands.find_request(command_id=command_id, owner_key=digest(owner), context_key=context.key)
        if request is None and existing.is_success:
            return self._legacy_receipt(existing.value, context, operation_id, role_key, conversation_key)
        if request is None:
            request = self._request(context, role_key, conversation_key, digest(owner), command_id,
                                    operation_id, identifier, action, expected_revision, data,
                                    sources, actor, operator, planning, now)
        else:
            payload = request["payload"]
            if (request["expected_revision"] != expected_revision or payload["record_id"] != identifier
                    or payload["action"] != action or payload["data"] != data or payload["planning"] != planning
                    or payload["sources"] != json.loads(encode(sources)) or payload["actor"] != json.loads(encode(actor))
                    or payload["operator_authenticated"] is not (operator is not None)):
                raise TaskError("idempotency_key_conflict", "existing operation uses the same key with different parameters")
        prepared = self.handoff.prepare(context, request, operation_key=operation_key, sources=sources,
                                        operator=operator, observed_at=now)
        try:
            decision = self.handoff.decide(context, request, operation_key=operation_key, sources=sources,
                                           operator=operator, observed_at=now)
            self.handoff.apply(decision)
            self.handoff.complete(context, request, decision, observed_at=now)
            self.handoff.acknowledge(decision)
            receipt = self.commands.status(request)["proposal"]["receipt"]
            return {**receipt, "replayed": existing.is_success or prepared["state"] == "acknowledged"}
        except Exception:
            self._abort_undecided(context, request, prepared["request_hash"], now)
            raise

    def _request(self, context, role_key, conversation_key, owner_key, command_id, operation_id,
                 identifier, action, expected_revision, data, sources, actor, operator, planning, now, *, timezone_name=None):
        if timezone_name is None:
            timezone_name = self.calendar.access.role_state.settings(context)[0]["planning_timezone"]
        role_raw = self.records.dispatch("get_raw", owner_key=role_key, identifier=identifier)
        conversation_raw = self.records.dispatch("get_raw", owner_key=conversation_key, identifier=identifier)
        role = json.loads(role_raw) if role_raw else None
        conversation = json.loads(conversation_raw) if conversation_raw else None
        current = role or conversation
        kind = data.get("kind") if action == "create" and type(data) is dict else current.get("kind") if current else "user_report"
        storage_key = role_key if kind == "role_plan" else conversation_key
        if current and kind == "role_plan" and role is None and conversation is not None:
            if conversation.get("reminder_id") and not conversation.get("reminder_context_key"):
                storage_key = conversation_key
            else:
                raise TaskError("calendar_legacy_owner_pending", "legacy role plan must be adopted before the split")
        raw = role_raw if storage_key == role_key else conversation_raw
        return {"command_id": command_id, "owner_key": owner_key, "context_key": context.key,
                "kind": "calendar.change", "expected_revision": expected_revision,
                "payload": {"storage_key": storage_key, "lookup_owner_keys": list(dict.fromkeys((role_key, conversation_key))),
                    "record_id": identifier, "operation_id": operation_id,
                    "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest() if raw is not None else None,
                    "action": action, "data": data, "sources": json.loads(encode(sources)),
                    "actor": json.loads(encode(actor)), "operator_authenticated": operator is not None,
                    "planning": planning, "planning_timezone": timezone_name,
                    "observed_at": now.isoformat(timespec="microseconds")}}

    def _legacy_receipt(self, operation, context, operation_id, role_key, conversation_key):
        if (operation.state is not OperationState.COMPLETED or operation.operation.owner != "calendar"
                or operation.operation.scope != context.scope or operation.operation.idempotency_key != operation_id):
            raise TaskError("life_calendar_unconfirmed", "original calendar operation is not confirmed")
        for key in dict.fromkeys((role_key, conversation_key)):
            try:
                receipt = self.records.dispatch("receipt", owner_key=key, operation_id=operation_id)
            except KeyError:
                continue
            if receipt.get("operation_id") != operation_id:
                raise TaskError("life_calendar_command_invalid", "original calendar receipt is invalid")
            return {**receipt, "replayed": True}
        raise TaskError("operation_unavailable", "original calendar operation is unavailable")

    def _abort_undecided(self, context, request, request_hash, observed_at):
        if life_commands.lookup(self.calendar.runtime, context_key=context.key, command_id=request["command_id"],
                                request_hash=request_hash) is not None:
            return
        try:
            decision = life_commands.decide(self.calendar.runtime, context=context, request=request, outcome="abort",
                observed_at=observed_at, verify_prepared=self.commands.verify_prepared,
                authorize=lambda *_: None, apply=lambda *_: None)
            self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.calendar.runtime, value))
        except (TaskError, ValueError):
            # A concurrent durable decision wins; recovery must inspect it.
            return
