"""Calendar state changes, migrated from private_companion/companion_v2/agenda/calendar.py.

The caller supplies canonical source and actor documents after host authorization.
Reminder cancellation is returned as an effect for the host; this module does
not access host storage, send notifications or execute the effect.
"""

from dataclasses import asdict
from datetime import datetime, timezone
import json

from .calendar_schedule import Schedule


class CalendarChangeError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _text(value, limit, *, blank=True):
    if type(value) is not str or len(value) > limit or (not blank and not value.strip()):
        raise CalendarChangeError("calendar_text_invalid", "invalid calendar text")
    return value


def _stamp(value):
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("task timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _schedule_document(value):
    schedule = asdict(value)
    schedule["anchor"] = _stamp(value.anchor)
    schedule["frequency"] = value.frequency.value
    schedule["gap_policy"] = value.gap_policy.value
    return schedule


def event_data(data):
    if type(data) is not dict or set(data) != {"kind", "title", "detail", "schedule", "duration_minutes", "presence"}:
        raise CalendarChangeError("calendar_event_invalid", "provide the declared event fields")
    if data["kind"] not in ("role_plan", "user_report", "commitment"):
        raise CalendarChangeError("calendar_kind_invalid", "choose plan, reported plan or commitment")
    schedule = Schedule.restore(data["schedule"])
    if schedule is None or type(data["duration_minutes"]) is not int or not 1 <= data["duration_minutes"] <= 1440:
        raise CalendarChangeError("calendar_duration_invalid", "provide a schedule and duration of at most one day")
    return {"kind": data["kind"], "title": _text(data["title"], 256, blank=False),
            "detail": _text(data["detail"], 4096), "schedule": _schedule_document(schedule),
            "duration_minutes": data["duration_minutes"], "presence": _text(data["presence"], 8)}


def _canonical_source(source):
    scope = source["scope"]
    required = ("installation_id", "platform", "kind", "native_scope_id")
    optional = ("bot_id", "account_id", "conversation_id", "session_id")
    if set(scope) - set(required) - set(optional):
        raise ValueError("source scope has unexpected fields")
    return {"source_id": source["source_id"], "owner": source["owner"],
            "scope": {**{name: scope[name] for name in required},
                      **{name: scope.get(name) for name in optional}},
            "version": source["version"], "issued_at": _stamp(datetime.fromisoformat(source["issued_at"])),
            "expires_at": _stamp(datetime.fromisoformat(source["expires_at"])) if source["expires_at"] else None}


def _merge_sources(previous, new):
    result, seen = [], set()
    for value in (*previous, *new):
        item = _canonical_source(value)
        encoded = json.dumps(item, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
        if encoded not in seen:
            seen.add(encoded)
            result.append(item)
    return result


def change(current, *, action, data, sources, actor, operator_authenticated=False,
           planning=None, identifier_exists=False, observed_at=None):
    """Compute a record and the host-side reminder cancellation it requires.

    `sources` and `actor` are host-normalized documents. The host must recheck
    returned `evidence` and the original owner before making its commit decision.
    """
    if action == "create":
        if current or identifier_exists:
            raise CalendarChangeError("calendar_exists", "event already exists")
        fields = event_data(data)
        record = {**fields, **({"planning": planning} if planning is not None else {}),
                  "state": "accepted" if fields["kind"] == "role_plan" else "proposed",
                  "sources": list(sources), "exceptions": {}, "accepted_by": [],
                  "changed_by": actor, "reminder_id": None, "reminder_context_key": None}
        return {"record": record, "cancel_reminder": None, "evidence": list(sources)}

    if current is None or current["state"] == "cancelled":
        raise CalendarChangeError("calendar_closed", "event is missing or cancelled")
    result = {**current, "changed_by": actor}
    evidence = _merge_sources(current["sources"], sources)
    if action == "revise":
        fields = event_data(data)
        if fields["kind"] != current["kind"]:
            raise CalendarChangeError("calendar_kind_changed", "create a separately attributed event to change its kind")
        result.update(fields, state="accepted" if fields["kind"] == "role_plan" else "proposed",
                      accepted_by=[], exceptions={}, details={}, sources=list(sources))
        evidence = _merge_sources(evidence, sources)
    elif action == "refine":
        if current["kind"] != "role_plan" or current["state"] != "accepted":
            raise CalendarChangeError("calendar_refine_target_invalid", "only accepted role plans can be refined")
        if type(data) is not dict or set(data) != {"sequence", "detail"}:
            raise CalendarChangeError("calendar_refine_invalid", "select a period and its intended next steps")
        if (Schedule.restore(current["schedule"]).at(data["sequence"]) is None
                or (str(data["sequence"]) in current["exceptions"]
                    and current["exceptions"][str(data["sequence"])] is None)):
            raise CalendarChangeError("calendar_occurrence_missing", "original occurrence is unavailable")
        dependencies = _merge_sources(current["sources"], sources)
        details = {**current.get("details", {}), str(data["sequence"]): _text(data["detail"], 4096, blank=False)}
        if len(details) > 128:
            raise CalendarChangeError("calendar_exception_limit", "split this series before retaining more refinements")
        result.update(details=details, sources=dependencies)
        evidence = dependencies
    elif action == "presence":
        if current["kind"] != "role_plan":
            raise CalendarChangeError("calendar_presence_target_invalid", "only role plans supply a public status")
        if type(data) is not dict or set(data) != {"presence"}:
            raise CalendarChangeError("calendar_presence_invalid", "provide only the public wording")
        result["presence"] = _text(data["presence"], 8).strip()
    elif action == "remind":
        if current["state"] != "accepted":
            raise CalendarChangeError("calendar_not_accepted", "only accepted events can schedule a reminder")
        if (type(data) is not dict or set(data) != {"sequence", "reminder_id", "context_key"}
                or type(data["sequence"]) is not int or data["sequence"] < 0
                or any(type(data[name]) is not str or not data[name] for name in ("reminder_id", "context_key"))
                or observed_at is None):
            raise CalendarChangeError("calendar_reminder_invalid", "invalid reminder command")
        sequence = data["sequence"]
        due = Schedule.restore(current["schedule"]).at(sequence)
        if str(sequence) in current["exceptions"]:
            value = current["exceptions"][str(sequence)]
            due = datetime.fromisoformat(value) if value else None
        if due is None or due <= observed_at:
            raise CalendarChangeError("calendar_reminder_past", "choose an existing future occurrence")
        result = {**current, "reminder_id": data["reminder_id"], "reminder_context_key": data["context_key"]}
    elif action == "accept":
        if not operator_authenticated:
            raise CalendarChangeError("calendar_confirmation_required", "acceptance needs an explicit authenticated human command")
        result.update(state="accepted", accepted_by=[actor])
    elif action == "cancel":
        result["state"] = "cancelled"
    elif action == "exception":
        if type(data) is not dict or set(data) != {"sequence", "start"}:
            raise CalendarChangeError("calendar_exception_invalid", "select an original occurrence and optional new start")
        if Schedule.restore(current["schedule"]).at(data["sequence"]) is None:
            raise CalendarChangeError("calendar_occurrence_missing", "original occurrence does not exist")
        moved = _stamp(datetime.fromisoformat(data["start"])) if data["start"] else None
        exceptions = {**current["exceptions"], str(data["sequence"]): moved}
        if len(exceptions) > 128:
            raise CalendarChangeError("calendar_exception_limit", "split this long series before adding more exceptions")
        result.update(exceptions=exceptions)
        if current["kind"] == "commitment" and moved:
            result.update(state="proposed", accepted_by=[])
    else:
        raise CalendarChangeError("calendar_action_invalid", "unknown calendar command")
    if planning is not None:
        result["planning"] = planning
    cancel_reminder = None
    if current.get("reminder_id") and action not in ("presence", "refine"):
        cancel_reminder = current
        if action != "remind":
            result.update(reminder_id=None, reminder_context_key=None)
    return {"record": result, "cancel_reminder": cancel_reminder, "evidence": evidence}
