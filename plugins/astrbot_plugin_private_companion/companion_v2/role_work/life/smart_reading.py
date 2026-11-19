"""Choose and pace reading within the role's actual calendar and sleep schedule."""

from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

from companion_v2.execution.models import TaskError, digest
from companion_v2.role_work.models import RoleActivityState as State


READING_WORDS = ("阅读", "读书", "看书", "翻书", "读几页", "读几章", "读几段", "慢慢读", "读一会", "读一读", "读小说", "读诗", "看小说", "翻阅")


def choice_slot(records, day):
    completed = sorted((a.activity_id, a.revision) for a in records
                       if a.state is State.COMPLETED and a.work.kind in ("reading", "social", "sleep"))
    return day + ":" + digest(completed)


def reading_window(calendar, now, local_now, next_sleep):
    """Require a reading plan or an unoccupied calendar window."""
    if calendar.get("partial"):
        return None
    if next_sleep is not None and next_sleep <= now:
        return None
    active = []
    for item in calendar.get("items", ()):
        if item.get("state") != "accepted" or item.get("kind") not in ("role_plan", "commitment"):
            continue
        try:
            start, end = datetime.fromisoformat(item["start"]), datetime.fromisoformat(item["end"])
        except (ValueError, KeyError, TypeError):
            return None
        if start <= now < end:
            active.append(item)
    def is_reading(item):
        description = str(item.get("title", "")) + " " + str(item.get("detail", ""))
        return item.get("kind") == "role_plan" and any(word in description for word in READING_WORDS)
    if active:
        if not all(is_reading(item) for item in active):
            return None
        return "正在进行的阅读安排：" + "；".join(str(item.get("title", "")) for item in active)[:250]
    return "日历中没有其他角色安排的空档"


class SmartReading:
    def __init__(self, routine):
        self.routine, self.app, self.runtime = routine, routine.app, routine.runtime
        self.next_calendar_check = None
        self.choice_base, self.choice_retry, self.no_choice = None, 0, 0
        self.next_choice_check = None

    def current_choice_slot(self, records, day):
        base = choice_slot(records, day)
        return base + ":" + str(self.choice_retry if self.choice_base == base else 0)

    def retry_choice(self, now):
        if self.next_choice_check is None:
            self.no_choice = min(self.no_choice + 1, 6)
            self.next_choice_check = now + timedelta(seconds=min(30 * 2 ** (self.no_choice - 1), 900))
        if now < self.next_choice_check:
            return False
        self.choice_retry += 1
        self.next_choice_check = None
        return True

    def candidates(self, records, day, selected_source=None):
        # A paused earlier span blocks its book; never silently skip unread text.
        by_source = {}
        for activity in records:
            if activity.work.kind != "reading" or activity.state not in (State.PLANNED, State.PAUSED):
                continue
            key = activity.work.source.source_id
            if key not in by_source or activity.work.start_offset < by_source[key].work.start_offset:
                by_source[key] = activity
        choices = sorted((a for a in by_source.values() if a.state is State.PLANNED and
                          (selected_source is None or a.work.source.source_id == selected_source)),
                         key=lambda a: a.work.source.source_id)
        # Rotate the finite prompt window so large libraries do not starve later titles.
        if len(choices) > 8:
            offset = int(digest(day)[:8], 16) % len(choices)
            choices = choices[offset:] + choices[:offset]
        available = []
        for item in choices:
            if self.app.role_journal.sources.validate_reference_for_audience(item.work.source,
                    recipient=self.routine.context.actor.subject, observed_at=self.app.now()).is_success:
                available.append(item)
                if len(available) == 8:
                    break
        return available

    async def _choose(self, choices, day, schedule):
        await self.routine.preflight()
        return await self.app.role_reader.select_book(self.routine.context, choices,
            role_instruction=self.routine.instruction, day=day, schedule=schedule)

    def resume_approved(self, records, now):
        routine = self.routine
        for activity in records:
            if activity.work.kind != "reading" or activity.state is not State.PAUSED or activity.work.context_key != routine.context.key:
                continue
            task_id = self.app.role_reader.task_id(routine.context, activity)
            if not routine.approved_interrupted_reading_retry(activity, task_id):
                continue
            calendar = self.app.calendar.occurrences(routine.context, start=now, end=now + timedelta(minutes=30))
            local_now = now.astimezone(ZoneInfo(routine.settings["timezone"]))
            if not reading_window(calendar, now, local_now, routine._next_sleep if routine.settings["sleep_enabled"] else None):
                return False
            routine.start_reading(activity.activity_id, expected_revision=activity.revision,
                                  request_id="resume-interrupted-" + digest([task_id, activity.revision]))
            return True
        return False

    def advance(self, records, now):
        routine = self.routine
        if self.next_calendar_check is not None and now < self.next_calendar_check:
            return
        local_now = now.astimezone(ZoneInfo(routine.settings["timezone"]))
        day = local_now.date().isoformat()
        base = choice_slot(records, day)
        if self.choice_base != base:
            self.choice_base, self.choice_retry, self.no_choice = base, 0, 0
            self.next_choice_check = None
        slot = self.current_choice_slot(records, day)
        if not any(a.work.kind == "reading" and a.state is State.PLANNED for a in records):
            return
        reader = self.app.role_reader
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?",
                                    (reader.choice_key(routine.context, slot),))
        if row:
            decision = json.loads(row["value"])
            if not decision.get("source_id"):
                if not self.retry_choice(now):
                    return
                slot = self.current_choice_slot(records, day)
                row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?",
                                            (reader.choice_key(routine.context, slot),))
                if row:
                    return
        if not row and reader.operations.get(reader.choice_id(routine.context, slot)).is_success:
            task = self.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id = ?",
                                         (reader.choice_id(routine.context, slot),))
            if not task or task["state"] not in ("failed", "cancelled", "expired", "rejected"):
                return
            if not self.retry_choice(now):
                return
            slot = self.current_choice_slot(records, day)
            if reader.operations.get(reader.choice_id(routine.context, slot)).is_success:
                return
        try:
            calendar = self.app.calendar.occurrences(routine.context, start=now, end=now + timedelta(minutes=30))
        except TaskError:
            self.next_calendar_check = now + timedelta(seconds=30)
            return
        schedule = reading_window(calendar, now, local_now,
                                  routine._next_sleep if routine.settings["sleep_enabled"] else None)
        if not schedule:
            self.next_calendar_check = now + timedelta(seconds=30)
            return
        choices = self.candidates(records, day, decision["source_id"] if row else None)
        if not choices and row:
            self.choice_retry += 1
            slot = self.current_choice_slot(records, day)
            row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?",
                                        (reader.choice_key(routine.context, slot),))
            if row:
                decision = json.loads(row["value"])
                if not decision.get("source_id"):
                    return
            elif reader.operations.get(reader.choice_id(routine.context, slot)).is_success:
                return
            choices = self.candidates(records, day, decision["source_id"] if row else None)
        if not choices:
            self.next_calendar_check = now + timedelta(seconds=30)
            return
        self.next_calendar_check = None
        if not row:
            routine.launch("book-choice:" + slot, lambda: self._choose(choices, slot, schedule))
            return
        chosen = next((a for a in choices if a.work.source.source_id == decision["source_id"]), None)
        if not chosen:
            return
        routine.start_reading(chosen.activity_id, expected_revision=chosen.revision,
                              request_id=chosen.work.request_id)
