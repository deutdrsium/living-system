"""Versioned plans, reported plans and explicitly accepted commitments."""

from datetime import datetime, timedelta
import json
from threading import RLock, get_ident

from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.refs import OperationRef, VersionRef
from companion_v2.identity_policy.private_domain import source_ref
from companion_v2.integration import calendar_reminder_hold
from .calendar_access import CalendarAccess
from .models import AgendaTarget, Schedule
from .records import CalendarRecords


INSTRUCTION = (
    "日历里的 role_plan 是角色计划（由角色身份持有），user_report 是用户自述，commitment/proposed 是未接受的约定；仅使用当前角色与当前私聊用户可验证的来源。accepted 只表示接受安排，不代表到点已发生或已完成，缺席和过时不能推断完成。条件讨论先保留候选，不代替对方确认；改期后的约定重新确认。presence 是公开显示的角色短状态，不包含用户姓名、地点、约定正文或健康隐私。根据当前话题选择工具；日历冲突是供协商的材料，不强行拒绝人物互动。"
)
LIFE_CALENDAR_OWNER_KEY = "life.calendar.execution_owner.v1"
LIFE_RECORD_METHODS = ("get", "list", "version", "receipt", "by_reminder", "shared_role_plans",
                       "role_key", "conversation_key", "operation_key")


def text(value, limit, *, blank=True):
    if type(value) is not str or len(value) > limit or (not blank and not value.strip()):
        raise TaskError("calendar_text_invalid", "invalid calendar text")
    return value


def event_data(data):
    if type(data) is not dict or set(data) != {"kind", "title", "detail", "schedule", "duration_minutes", "presence"}:
        raise TaskError("calendar_event_invalid", "provide the declared event fields")
    if data["kind"] not in ("role_plan", "user_report", "commitment"):
        raise TaskError("calendar_kind_invalid", "choose plan, reported plan or commitment")
    schedule = Schedule.restore(data["schedule"])
    if schedule is None or type(data["duration_minutes"]) is not int or not 1 <= data["duration_minutes"] <= 1440:
        raise TaskError("calendar_duration_invalid", "provide a schedule and duration of at most one day")
    return {"kind": data["kind"], "title": text(data["title"], 256, blank=False),
            "detail": text(data["detail"], 4096), "schedule": json.loads(encode(schedule)),
            "duration_minutes": data["duration_minutes"], "presence": text(data["presence"], 8)}


class _LifeCalendarReader:
    def __init__(self, calendar, records):
        self._calendar, self._records = calendar, records

    def __getattr__(self, name):
        if name not in LIFE_RECORD_METHODS:
            raise AttributeError(name)

        def read(*args, **kwargs):
            with self._calendar._life_bridge_lock:
                if self._calendar._life_calendar_owner() != "good_life":
                    raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
                self._calendar._require_life_boundary(allow_pinned_read=True)
                self._calendar._resolve_life_bridge()
                if self._calendar._life_read_lease is not self:
                    raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
            # Provider calls may wait on a life snapshot that also reads the calendar.
            return getattr(self._records, name)(*args, **kwargs)
        return read


class CalendarService:
    def __init__(self, tasks, agenda, role_state):
        self.tasks, self.runtime, self.agenda = tasks, tasks.runtime, agenda
        self.access = CalendarAccess(tasks, role_state)
        self._life_bridge_lock = RLock()
        self._legacy_records = CalendarRecords(self.runtime, tasks.identity)
        self._life_command_bridge = None
        self._life_read_records = None
        self._life_read_lease = None
        self._life_bridge_resolver = None
        self._life_resolved_by = None
        self._life_ledger = getattr(role_state, "life", None)

    def _life_calendar_owner(self):
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (LIFE_CALENDAR_OWNER_KEY,))
        if row is None or row["value"] == "legacy":
            return "legacy"
        if row["value"] == "good_life":
            return "good_life"
        raise TaskError("life_calendar_owner_invalid", "calendar execution ownership is invalid")

    def _require_life_boundary(self, *, allow_pinned_read=False):
        diagnostic = getattr(getattr(self.runtime, "_read_snapshot_diagnostics", None), "current", None)
        if getattr(self.runtime, "_write_owner_thread_id", None) == get_ident() or diagnostic is not None:
            provider = getattr(self._life_read_records, "provider", None)
            lease = getattr(provider, "_lease", None)
            pinned = getattr(self._life_ledger, "is_pinned_provider", None)
            if allow_pinned_read and lease is not None and callable(pinned) and pinned(lease):
                return
            raise TaskError("life_calendar_transaction_active", "calendar access requires an independent host snapshot")

    @property
    def records(self):
        if self._life_calendar_owner() == "legacy":
            return self._legacy_records
        self._require_life_boundary(allow_pinned_read=True)
        with self._life_bridge_lock:
            self._resolve_life_bridge()
            if self._life_read_lease is None:
                raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
            return self._life_read_lease

    @records.setter
    def records(self, records):
        self._legacy_records = records

    def set_life_bridge_resolver(self, resolver):
        if not callable(resolver):
            raise ValueError("calendar bridge resolver must be callable")
        # Lifecycle registration must not wait for an in-flight calendar call:
        # a provider callback may unregister itself while that call holds the lock.
        self._life_bridge_resolver = resolver

    def prepare_life_snapshot_read(self):
        """Verify and bind the calendar provider before entering a host transaction."""
        if self._life_calendar_owner() == "good_life":
            self._require_life_boundary()
            with self._life_bridge_lock:
                self._resolve_life_bridge()

    def _resolve_life_bridge(self):
        # Called while holding _life_bridge_lock, never from plugin lifecycle code.
        while (resolver := getattr(self, "_life_bridge_resolver", None)) is not None:
            try:
                bridge, records = resolver()
            except Exception:
                if resolver is not self._life_bridge_resolver:
                    continue
                self._life_command_bridge = self._life_read_records = self._life_read_lease = None
                self._life_resolved_by = None
                raise
            if resolver is not self._life_bridge_resolver:
                continue
            if (getattr(self, "_life_resolved_by", None) is resolver
                    and self._life_command_bridge is bridge and self._life_read_records is records):
                return
            self._life_command_bridge = self._life_read_records = self._life_read_lease = None
            self.bind_life_command_bridge(bridge, records=records)
            self._life_resolved_by = resolver
            return

    def bind_life_command_bridge(self, bridge, *, records):
        if getattr(bridge, "calendar", None) is not self or not callable(getattr(bridge, "command", None)):
            raise ValueError("calendar bridge must belong to this host")
        if not all(callable(getattr(records, name, None)) for name in LIFE_RECORD_METHODS):
            raise ValueError("calendar life records must support the complete read contract")
        with self._life_bridge_lock:
            if self._life_command_bridge not in (None, bridge):
                raise ValueError("another calendar bridge is still bound")
            if self._life_read_records not in (None, records):
                raise ValueError("another calendar reader is still bound")
            if self._life_read_lease is None:
                self._life_read_lease = _LifeCalendarReader(self, records)
            self._life_command_bridge, self._life_read_records = bridge, records

    def unbind_life_command_bridge(self, bridge):
        with self._life_bridge_lock:
            if self._life_command_bridge is not bridge:
                return False
            self._life_command_bridge, self._life_read_records, self._life_read_lease = None, None, None
            return True

    def _evidence(self, context, sources, *, at=None):
        self.access.evidence(context, sources, at=at)

    def role_plans_for_busy_gate(self, context, *, limit):
        """Return the bounded role-wide input for the host's busy-time policy."""
        items, _ = self.records.shared_role_plans(context, limit=limit)
        return items

    def read(self, context, identifier, *, operator=None):
        self.access.check(context, "calendar.read", operator=operator)
        record = self.records.get(context, identifier)
        if record is None:
            raise TaskError("calendar_unavailable", "event is unavailable")
        try:
            self._evidence(context, record["sources"])
        except TaskError as error:
            if error.code.startswith(("calendar_source_", "domain_source_", "source_")):
                raise TaskError("calendar_unavailable", "event is unavailable") from error
            raise
        return record

    def command(self, context, *, identifier, action, expected_revision, operation_key,
                data=None, sources=(), operator=None, planning=None):
        owner = self._life_calendar_owner()
        if owner == "good_life":
            self._require_life_boundary()
            with self._life_bridge_lock:
                self._resolve_life_bridge()
                bridge = self._life_command_bridge
                if bridge is None or self._life_read_records is None:
                    raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
            return bridge.command(context, identifier=identifier, action=action, expected_revision=expected_revision,
                                  operation_key=operation_key, data=data, sources=sources, operator=operator, planning=planning)
        with self.runtime.transaction():
            self.access.check(context, "calendar.write", operator=operator)
            self._evidence(context, sources)
            actor = operator.subject if operator else context.role_binding.role
            current_record = self.records.get(context, identifier)
            write_kind = (event_data(data)["kind"] if action == "create" else
                          current_record["kind"] if current_record else "user_report")
            storage_key = self.records.storage_key_for_change(context, identifier, write_kind)

            def transform(current):
                if action == "create":
                    if current or self.records.get(context, identifier):
                        raise TaskError("calendar_exists", "event already exists")
                    fields = event_data(data)
                    return {**fields, **({"planning": planning} if planning is not None else {}),
                        "state": "accepted" if fields["kind"] == "role_plan" else "proposed",
                        "sources": json.loads(encode(sources)), "exceptions": {}, "accepted_by": [],
                        "changed_by": json.loads(encode(actor)), "reminder_id": None,
                        "reminder_context_key": None}
                if current is None or current["state"] == "cancelled":
                    raise TaskError("calendar_closed", "event is missing or cancelled")
                self._evidence(context, current["sources"])
                result = {**current, "changed_by": json.loads(encode(actor))}
                if action == "revise":
                    fields = event_data(data)
                    if fields["kind"] != current["kind"]:
                        raise TaskError("calendar_kind_changed", "create a separately attributed event to change its kind")
                    result.update(fields, state="accepted" if fields["kind"] == "role_plan" else "proposed",
                                  accepted_by=[], exceptions={}, details={}, sources=json.loads(encode(sources)))
                elif action == "refine":
                    if current["kind"] != "role_plan" or current["state"] != "accepted":
                        raise TaskError("calendar_refine_target_invalid", "only accepted role plans can be refined")
                    if type(data) is not dict or set(data) != {"sequence", "detail"}:
                        raise TaskError("calendar_refine_invalid", "select a period and its intended next steps")
                    if Schedule.restore(current["schedule"]).at(data["sequence"]) is None or (str(data["sequence"]) in current["exceptions"] and current["exceptions"][str(data["sequence"])] is None):
                        raise TaskError("calendar_occurrence_missing", "original occurrence is unavailable")
                    dependencies = tuple(dict.fromkeys((*map(source_ref, current["sources"]),
                        *(source_ref(s) if isinstance(s, dict) else s for s in sources))))
                    self.access.evidence(context, dependencies)
                    details = {**current.get("details", {}), str(data["sequence"]): text(data["detail"], 4096, blank=False)}
                    if len(details) > 128:
                        raise TaskError("calendar_exception_limit", "split this series before retaining more refinements")
                    result.update(details=details, sources=json.loads(encode(dependencies)))
                elif action == "presence":
                    if current["kind"] != "role_plan":
                        raise TaskError("calendar_presence_target_invalid", "only role plans supply a public status")
                    if type(data) is not dict or set(data) != {"presence"}:
                        raise TaskError("calendar_presence_invalid", "provide only the public wording")
                    result["presence"] = text(data["presence"], 8).strip()
                elif action == "accept":
                    if operator is None:
                        raise TaskError("calendar_confirmation_required", "acceptance needs an explicit authenticated human command")
                    result.update(state="accepted", accepted_by=[json.loads(encode(operator.subject))])
                elif action == "cancel":
                    result["state"] = "cancelled"
                elif action == "exception":
                    if type(data) is not dict or set(data) != {"sequence", "start"}:
                        raise TaskError("calendar_exception_invalid", "select an original occurrence and optional new start")
                    schedule = Schedule.restore(current["schedule"])
                    if schedule.at(data["sequence"]) is None:
                        raise TaskError("calendar_occurrence_missing", "original occurrence does not exist")
                    moved = stamp(datetime.fromisoformat(data["start"])) if data["start"] else None
                    exceptions = {**current["exceptions"], str(data["sequence"]): moved}
                    if len(exceptions) > 128:
                        raise TaskError("calendar_exception_limit", "split this long series before adding more exceptions")
                    result.update(exceptions=exceptions)
                    if current["kind"] == "commitment" and moved:
                        result.update(state="proposed", accepted_by=[])
                else:
                    raise TaskError("calendar_action_invalid", "unknown calendar command")
                if planning is not None:
                    result["planning"] = planning
                if current.get("reminder_id") and action not in ("presence", "refine"):
                    self._cancel_reminder(context, current, operation_key)
                    result.update(reminder_id=None, reminder_context_key=None)
                return result

            return self.records.change(context, identifier, operation_key=operation_key,
                expected_revision=expected_revision, parameters=[action, data, sources, actor] + ([planning] if planning is not None else []),
                transform=transform, at=self.tasks.now(), storage_key=storage_key)

    def inventory(self, context, *, after_id="", limit=30, operator=None, kinds=None):
        self.access.check(context, "calendar.read", operator=operator)
        rows = self.records.list(context, after_id=after_id, limit=limit, kinds=kinds)
        items, skipped, skipped_reasons = [], 0, {"role_mismatch": 0, "source_unavailable": 0}
        for row in rows:
            try:
                self._evidence(context, row["sources"])
                items.append(row)
            except TaskError as error:
                skipped += 1
                reason = "role_mismatch" if error.code == "calendar_source_role_mismatch" else "source_unavailable"
                skipped_reasons[reason] += 1
        return {"items": items, "skipped": skipped,
                "skipped_reasons": {key: count for key, count in skipped_reasons.items() if count},
                "next_cursor": rows[-1]["id"] if len(rows) == limit else None}

    def occurrences(self, context, *, start, end, operator=None, kinds=None, diagnostics=False, include_cancelled=False):
        if not start < end or end - start > timedelta(days=31):
            raise TaskError("calendar_window_invalid", "query a positive window no longer than 31 days")
        self.access.check(context, "calendar.read", operator=operator)
        items, cursor, skipped, scanned, has_more = [], "", 0, 0, False
        skipped_reasons = {"role_mismatch": 0, "source_unavailable": 0}
        while scanned < 200:
            page = self.inventory(context, after_id=cursor, limit=50, operator=operator, kinds=kinds)
            skipped += page["skipped"]
            for reason, count in page.get("skipped_reasons", {}).items():
                skipped_reasons[reason] = skipped_reasons.get(reason, 0) + count
            for row in page["items"]:
                if row["state"] == "cancelled" and not include_cancelled:
                    continue
                schedule = Schedule.restore(row["schedule"])
                duration = timedelta(minutes=row["duration_minutes"])
                seq = schedule.first_at_or_after(start - duration)
                sequences = set()
                while seq is not None and schedule.at(seq) is not None and schedule.at(seq) < end:
                    sequences.add(seq)
                    seq += 1
                    if len(sequences) > 64:
                        break
                sequences.update(int(n) for n, moved in row["exceptions"].items()
                                 if moved and start - duration <= datetime.fromisoformat(moved) < end)
                for sequence in sorted(sequences):
                    due = schedule.at(sequence)
                    state = row["state"]
                    if str(sequence) in row["exceptions"]:
                        value = row["exceptions"][str(sequence)]
                        if value:
                            due = datetime.fromisoformat(value)
                        elif include_cancelled:
                            state = "skipped"
                        else:
                            due = None
                    if due and due < end and due + duration > start:
                        item = {"id": row["id"], "revision": row["revision"], "sequence": sequence,
                            "kind": row["kind"], "state": state, "title": row["title"],
                            "detail": row.get("details", {}).get(str(sequence), row["detail"]),
                            "start": stamp(due), "end": stamp(due + duration), "presence": row["presence"],
                            "sources": row["sources"]}
                        # A cancelled series is immutable. A skipped exception can
                        # outlive subsequent edits, so its timestamp is unavailable.
                        if state == "cancelled" and row.get("updated_at"):
                            item["outcome_at"] = row["updated_at"]
                        items.append(item)
            scanned += 50
            if not page["next_cursor"]:
                break
            cursor = page["next_cursor"]
            if scanned >= 200:
                probe = self.inventory(context, after_id=cursor, limit=1, operator=operator, kinds=kinds)
                skipped += probe["skipped"]
                for reason, count in probe.get("skipped_reasons", {}).items():
                    skipped_reasons[reason] = skipped_reasons.get(reason, 0) + count
                has_more = bool(probe["items"] or probe["skipped"])
        items.sort(key=lambda item: (item["start"], item["id"], item["sequence"]))
        occurrence_overflow = max(0, len(items) - 64)
        result = {"items": items[:64], "partial": has_more or occurrence_overflow > 0 or bool(skipped),
                  "skipped": skipped}
        if diagnostics:
            result.update(skipped_reasons={key: count for key, count in skipped_reasons.items() if count},
                          scan_truncated=has_more, occurrences_truncated=occurrence_overflow)
        return result

    def _target(self, context):
        return AgendaTarget(context.scope, context.role_binding.role, context.actor.subject)

    @staticmethod
    def _operation(context, key):
        identifier = "calendar-reminder-" + digest([context.key, key])
        return OperationRef(identifier, "agenda", context.scope, VersionRef(identifier, 1), identifier)

    def _cancel_reminder(self, context, record, key):
        storage_key = self.records.storage_key_for(context, record["id"])
        return self._cancel_reminder_at_storage(context, record, key, storage_key)

    def _cancel_reminder_at_storage(self, context, record, key, storage_key):
        owner_context = record.get("reminder_context_key")
        if ((owner_context is not None and owner_context != context.key)
                or (owner_context is None and storage_key == self._legacy_records.role_key(context))):
            raise TaskError("calendar_reminder_context_mismatch",
                            "change this reminder from the conversation that created it")
        identifier = record["reminder_id"]
        reminder = self.agenda.repository.reminder(identifier)
        if reminder and reminder.state not in ("cancelled", "completed"):
            require(self.agenda.change_reminder(context.actor, target=self._target(context), reminder_id=identifier, action="cancel",
                expected_revision=reminder.revision, operation=self._operation(context, key + ":cancel"),
                expected_role_revision=context.role_binding.revision, observed_at=self.tasks.now()))

    def remind(self, context, *, identifier, expected_revision, operation_key, sequence=0, operator=None):
        if self._life_calendar_owner() == "good_life":
            self._require_life_boundary()
            with self._life_bridge_lock:
                self._resolve_life_bridge()
                bridge = self._life_command_bridge
                if bridge is None or self._life_read_records is None:
                    raise TaskError("life_provider_unavailable", "the calendar life provider is unavailable")
            reminder_id = "calendar-reminder-" + digest([context.key, operation_key])
            return bridge.command(context, identifier=identifier, action="remind", expected_revision=expected_revision,
                operation_key=operation_key, data={"sequence": sequence, "reminder_id": reminder_id,
                                                   "context_key": context.key}, operator=operator)
        with self.runtime.transaction():
            self.access.check(context, "calendar.write", operator=operator)
            current_record = self.records.get(context, identifier)
            write_kind = current_record["kind"] if current_record else "user_report"
            storage_key = self.records.storage_key_for_change(context, identifier, write_kind)
            def transform(current):
                if current is None or current["state"] != "accepted":
                    raise TaskError("calendar_not_accepted", "only accepted events can schedule a reminder")
                self._evidence(context, current["sources"])
                due = Schedule.restore(current["schedule"]).at(sequence)
                if str(sequence) in current["exceptions"]:
                    value = current["exceptions"][str(sequence)]
                    due = datetime.fromisoformat(value) if value else None
                if due is None or due <= self.tasks.now():
                    raise TaskError("calendar_reminder_past", "choose an existing future occurrence")
                if current.get("reminder_id"):
                    self._cancel_reminder(context, current, operation_key)
                rid = "calendar-reminder-" + digest([context.key, operation_key])
                require(self.agenda.create_reminder(context.actor, target=self._target(context), reminder_id=rid,
                    message="日历提醒：" + current["title"], schedule=Schedule(due, current["schedule"]["timezone_name"]),
                    operation=self._operation(context, operation_key), expected_role_revision=context.role_binding.revision,
                    observed_at=self.tasks.now()))
                return {**current, "reminder_id": rid, "reminder_context_key": context.key}
            return self.records.change(context, identifier, operation_key=operation_key,
                expected_revision=expected_revision, parameters=["remind", sequence, operator.subject if operator else None],
                transform=transform, at=self.tasks.now(), storage_key=storage_key)

    def validate_reminder(self, context, reminder_id, at):
        if calendar_reminder_hold.held(self.runtime, reminder_id):
            raise TaskError(calendar_reminder_hold.ERROR, "calendar reminder awaits a life command decision")
        if not reminder_id.startswith("calendar-reminder-"):
            return
        rows = self.records.by_reminder(context, reminder_id)
        if len(rows) != 1:
            raise TaskError("calendar_reminder_changed", "original event no longer owns this reminder")
        record = rows[0]
        if record.get("reminder_context_key", context.key) != context.key:
            raise TaskError("calendar_reminder_changed", "original reminder belongs to another conversation")
        self.access.check(context, "calendar.read", at=at)
        self._evidence(context, record["sources"], at=at)
        if record["state"] != "accepted":
            raise TaskError("calendar_reminder_changed", "original event is no longer accepted")
