"""Recheck host-owned calendar authority before a life-store decision.

This capability does not expose host storage or commit a calendar command.
Call it inside the host decision transaction with the immutable proposal.
"""

import hashlib
import json
from datetime import datetime

from companion_v2.agenda.models import AgendaError, Schedule
from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef, normalize_utc
from companion_v2.identity_policy.private_domain import source_ref


def request_time(payload):
    return normalize_utc(datetime.fromisoformat(payload["observed_at"]), field="calendar observation")


def reminder_due(previous, sequence, observed_at):
    if previous is None or previous["state"] != "accepted":
        raise TaskError("calendar_not_accepted", "only accepted events can schedule a reminder")
    due = Schedule.restore(previous["schedule"]).at(sequence)
    if str(sequence) in previous["exceptions"]:
        value = previous["exceptions"][str(sequence)]
        due = datetime.fromisoformat(value) if value else None
    if due is None or due <= observed_at:
        raise TaskError("calendar_reminder_past", "choose an existing future occurrence")
    return due


def authorize(calendar, context, request, proposal, *, operation_key, sources, operator, observed_at):
    try:
        if (type(request) is not dict or request.get("kind") != "calendar.change"
                or type(request.get("payload")) is not dict or type(proposal) is not dict
                or set(proposal) != {"previous", "previous_raw", "record", "receipt", "cancel_reminder", "evidence"}
                or type(operation_key) is not str or not operation_key.strip() or len(operation_key) > 512
                or not isinstance(sources, (tuple, list))):
            raise ValueError("invalid calendar command")
        payload, previous = request["payload"], proposal["previous"]
        if type(request["expected_revision"]) is not int or request["expected_revision"] < 0:
            raise ValueError("calendar expected revision is invalid")
        requested_at = request_time(payload)
        if requested_at > normalize_utc(observed_at, field="host observation"):
            raise ValueError("calendar observation cannot be in the future")
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        role_key, conversation_key = calendar._legacy_records.role_key(context), calendar._legacy_records.conversation_key(context)
        actor = operator.subject if operator else context.role_binding.role
        operation_id = "calendar:" + digest([calendar._legacy_records.operation_key(context), operation_key])
        if (request["owner_key"] != digest(owner) or request["context_key"] != context.key
                or payload["operation_id"] != operation_id or payload["actor"] != json.loads(encode(actor))
                or payload["sources"] != json.loads(encode(sources))
                or payload["operator_authenticated"] is not (operator is not None)
                or set(payload["lookup_owner_keys"]) != {role_key, conversation_key}
                or len(payload["lookup_owner_keys"]) != len({role_key, conversation_key})):
            raise ValueError("calendar command crosses its original authority")
        if previous is None:
            if request["expected_revision"] != 0 or payload["previous_hash"] is not None or proposal["previous_raw"] is not None:
                raise ValueError("calendar original record is missing")
            kind = payload["data"]["kind"] if payload["action"] == "create" else "user_report"
        else:
            if (type(previous) is not dict or previous["id"] != payload["record_id"]
                    or previous["revision"] != request["expected_revision"]
                    or type(proposal["previous_raw"]) is not str or json.loads(proposal["previous_raw"]) != previous
                    or hashlib.sha256(proposal["previous_raw"].encode("utf-8")).hexdigest() != payload["previous_hash"]):
                raise ValueError("calendar original record changed")
            kind = previous["kind"]
        expected_storage = role_key if kind == "role_plan" else conversation_key
        if previous and kind == "role_plan" and payload["storage_key"] == conversation_key:
            if previous.get("reminder_id") and not previous.get("reminder_context_key"):
                expected_storage = conversation_key
            else:
                raise TaskError("calendar_legacy_owner_pending", "legacy role plan must be adopted before the split")
        if payload["storage_key"] != expected_storage:
            raise ValueError("calendar storage owner changed")
        result = proposal["record"]
        if (type(result) is not dict or result.get("id") != payload["record_id"]
                or result.get("revision") != request["expected_revision"] + 1
                or result.get("updated_at") != stamp(requested_at)):
            raise ValueError("calendar proposed record identity changed")
        receipt = {"operation_id": operation_id, "id": payload["record_id"], "revision": result["revision"],
                   "state": result.get("state", "saved"), "replayed": False}
        if proposal["receipt"] != receipt:
            raise ValueError("calendar operation receipt changed")
        needs_cancel = bool(previous and previous.get("reminder_id") and payload["action"] not in ("presence", "refine"))
        if proposal["cancel_reminder"] != (previous if needs_cancel else None):
            raise ValueError("calendar reminder effect differs from the original")
        is_remind = payload["action"] == "remind"
        if is_remind:
            data = payload["data"]
            if (type(data) is not dict or set(data) != {"sequence", "reminder_id", "context_key"}
                    or type(data["sequence"]) is not int or data["sequence"] < 0
                    or data["reminder_id"] != "calendar-reminder-" + digest([context.key, operation_key])
                    or data["context_key"] != context.key or sources or payload["planning"] is not None):
                raise ValueError("calendar reminder identity changed")
            reminder_due(previous, data["sequence"], observed_at)
            expected = {**previous, "reminder_id": data["reminder_id"], "reminder_context_key": context.key,
                        "revision": request["expected_revision"] + 1, "updated_at": stamp(requested_at)}
            if result != expected:
                raise ValueError("calendar reminder changed unrelated fields")
        if needs_cancel and not is_remind and (result.get("reminder_id") is not None or result.get("reminder_context_key") is not None):
            raise ValueError("calendar change did not clear the cancelled reminder")
        if previous is None and (result.get("reminder_id") is not None or result.get("reminder_context_key") is not None):
            raise ValueError("new calendar record cannot invent a reminder")
        if (not needs_cancel and not is_remind and previous is not None
                and (result.get("reminder_id"), result.get("reminder_context_key"))
                != (previous.get("reminder_id"), previous.get("reminder_context_key"))):
            raise ValueError("calendar change altered an unrelated reminder")
    except TaskError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError) as error:
        raise TaskError("life_calendar_command_invalid", "calendar identity or proposal is invalid") from error

    calendar.access.check(context, "calendar.write", operator=operator, at=observed_at)
    calendar._evidence(context, sources, at=observed_at)
    if previous is not None:
        calendar._evidence(context, previous["sources"], at=observed_at)
    if payload["action"] == "refine":
        dependencies = tuple(dict.fromkeys((*(source_ref(item) for item in previous["sources"]),
            *(source_ref(item) if isinstance(item, dict) else item for item in sources))))
        calendar._evidence(context, dependencies, at=observed_at)
        if result.get("sources") != json.loads(encode(dependencies)):
            raise TaskError("life_calendar_command_invalid", "calendar source dependencies changed")
    elif payload["action"] in ("create", "revise"):
        if result.get("sources") != json.loads(encode(sources)):
            raise TaskError("life_calendar_command_invalid", "calendar source dependencies changed")
    elif previous is not None and result.get("sources") != previous["sources"]:
        raise TaskError("life_calendar_command_invalid", "calendar source dependencies changed")
    if is_remind:
        try:
            target = calendar._target(context)
            for action in ("reminder.create", "reminder.deliver"):
                role_revision = calendar.agenda._authorize(context.actor, target, action, observed_at)
                if role_revision != context.role_binding.revision:
                    raise AgendaError("role_binding_changed", "role binding changed after this command was prepared")
            if calendar.agenda.repository.reminder(payload["data"]["reminder_id"]) is not None:
                raise AgendaError("agenda_id_conflict", "reminder identifier is already in use")
        except AgendaError as error:
            raise TaskError(error.code, str(error)) from error
    if needs_cancel:
        origin = previous.get("reminder_context_key")
        if ((origin is not None and origin != context.key)
                or (origin is None and payload["storage_key"] == role_key)):
            raise TaskError("calendar_reminder_context_mismatch", "change this reminder from the conversation that created it")
        reminder = calendar.agenda.repository.reminder(previous["reminder_id"])
        if reminder and reminder.state not in ("cancelled", "completed"):
            try:
                target = calendar._target(context)
                role_revision = calendar.agenda._authorize(context.actor, target, "reminder.cancel", observed_at,
                    calendar.agenda.object_key("reminder", reminder.reminder_id))
                calendar.agenda._reminder(reminder.reminder_id, target)
                if role_revision != context.role_binding.revision:
                    raise AgendaError("role_binding_changed", "role binding changed after this command was prepared")
            except AgendaError as error:
                raise TaskError(error.code, str(error)) from error
    return Outcome.success(True)
