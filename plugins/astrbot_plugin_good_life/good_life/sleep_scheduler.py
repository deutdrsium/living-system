"""Drive due sleep receipts through narrow host capabilities.

The scheduler uses limited host callbacks for message interruptions and does
not call models or send messages.
"""

import asyncio
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

from .activity_records import ActivityTransitionPending, StagedActivityRecords
from .calendar_scheduler import CalendarScheduler
from .prepared_commands import PreparedCommands
from .reading_scheduler import ReadingScheduler
from .sleep_planner import SleepPlanner
from .social_scheduler import SocialScheduler
from .social_schedule import social_first
from .state_scheduler import StateScheduler
from .storage import StorageReader


class DueSleepScheduler:
    def __init__(self, storage, capabilities, *, interval_seconds=1.0, reading_executor=None, clock=None,
                 monotonic_clock=None, spending_tick=None):
        if (type(capabilities) is not dict or capabilities.get("api_version") != "companion.life-host.v1"
                or not all(callable(capabilities.get(name)) for name in ("contexts", "wake", "recover"))
                or not isinstance(interval_seconds, (int, float)) or interval_seconds <= 0):
            raise ValueError("due-sleep scheduler needs compatible host capabilities")
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.storage = storage
        from .reading_execution import ReadingExecutionStore
        self.reading_store = ReadingExecutionStore(storage)
        self.records = StagedActivityRecords(storage)
        self.commands = PreparedCommands(storage)
        self.capabilities = dict(capabilities)
        self.spending_tick = spending_tick
        self.planner = SleepPlanner()
        self.state_scheduler = (StateScheduler(storage, capabilities)
            if all(callable(capabilities.get(name)) for name in ("state_bindings", "state_preflight", "state_run")) else None)
        self.reading_scheduler = (ReadingScheduler(storage, capabilities, executor=reading_executor, monotonic_clock=monotonic_clock)
            if all(callable(capabilities.get(name)) for name in ("reading_snapshot", "reading_choice_result", "choose_reading",
                                                                  "start_reading", "execute_reading", "recover_reading")) else None)
        self.social_scheduler = (SocialScheduler(capabilities)
            if all(callable(capabilities.get(name)) for name in ("social_snapshot", "start_social", "execute_social", "recover_social")) else None)
        self.calendar_scheduler = (CalendarScheduler(capabilities)
            if all(callable(capabilities.get(name)) for name in ("planning_bindings", "planning_check", "planning_run")) else None)
        self.interval_seconds = float(interval_seconds)
        self._lock = asyncio.Lock()
        self._worker = None
        self._stopped = False
        self._checked_interrupted_work = set()
        self._next_sleep_check = {}
        self.last_failure = None
        self.hold_night_sleep = None

    @staticmethod
    def _contexts(values):
        if not isinstance(values, (tuple, list)) or len(values) > 10000:
            raise ValueError("invalid life context inventory")
        result = {}
        for item in values:
            if (type(item) is not dict or set(item) != {"context_key", "owner_key"}
                    or any(type(item[key]) is not str or not item[key] or len(item[key]) > 256
                           for key in ("context_key", "owner_key"))):
                raise ValueError("invalid life context identity")
            previous = result.setdefault(item["context_key"], item["owner_key"])
            if previous != item["owner_key"]:
                raise ValueError("life context was bound to multiple owners")
        return result

    def _pending(self):
        cursor = None
        while True:
            rows = self.commands.pending(limit=100, after=cursor)
            for item in rows:
                yield item
            if len(rows) < 100:
                break
            cursor = (rows[-1]["prepared_at"], rows[-1]["command_id"])

    async def tick(self, *, now=None):
        if self._stopped:
            return {"recovered": 0, "woken": 0}
        now = now or self.clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("scheduler time must be timezone-aware")
        async with self._lock:
            recovered = woken = 0
            self.last_failure = None
            pending = tuple(self._pending())
            # Configuration can prevent the host from starting. Recover it as
            # soon as the session is assembled, before asking for live contexts.
            for item in pending:
                if (item["kind"] == "configuration.patch" and item["command_id"].startswith("life-config-")
                        and callable(self.capabilities.get("recover_configuration"))):
                    request = self.commands.recovery_request(command_id=item["command_id"], owner_key=item["owner_key"],
                        context_key=item["context_key"], request_hash=item["request_hash"])
                    if request is not None:
                        try:
                            await asyncio.wait_for(self.capabilities["recover_configuration"](request), timeout=30)
                            recovered += 1
                        except Exception as error:
                            self.last_failure = getattr(error, "code", "life_configuration_recovery_unavailable")
            contexts = self._contexts(self.capabilities["contexts"]())
            calendar_contexts = (self._contexts(self.capabilities["calendar_contexts"]())
                                 if callable(self.capabilities.get("calendar_contexts")) else contexts)
            blocked = set()
            if all(callable(self.capabilities.get(name)) for name in ("sleep_interruptions", "interrupt_sleep")):
                unresolved = {}
                for item in self.capabilities["sleep_interruptions"]():
                    if (type(item) is not dict or set(item) != {"id", "context_key", "owner_key"}
                            or any(type(item[key]) is not str or not item[key] for key in item)):
                        continue
                    unresolved[item["id"]] = item["owner_key"]
                    if contexts.get(item["context_key"]) != item["owner_key"]:
                        continue
                    try:
                        await asyncio.wait_for(self.capabilities["interrupt_sleep"](item["context_key"], item["id"]), timeout=30)
                        del unresolved[item["id"]]
                    except Exception as error:
                        self.last_failure = getattr(error, "code", "life_interruption_unavailable")
                blocked = set(unresolved.values())
            for item in pending:
                if item["kind"] == "configuration.patch":
                    continue
                transition = item["kind"] == "activity.transition" and item["command_id"].startswith("activity-due-")
                creation = (item["kind"] == "activity.create" and item["command_id"].startswith("activity-scheduled-")
                            and callable(self.capabilities.get("recover_sleep_creation")))
                manual_sleep = ((item["kind"] == "activity.create" and item["command_id"].startswith("activity-create-")
                                 or item["kind"] == "activity.transition" and item["command_id"].startswith(
                                     ("activity-wake-", "activity-cancel-sleep-")))
                                and callable(self.capabilities.get("recover_manual_sleep")))
                calendar = (item["kind"] in ("calendar.change", "calendar.batch") and item["command_id"].startswith("life-calendar-")
                            and callable(self.capabilities.get("recover_calendar")))
                state = (item["kind"] == "state.change" and item["command_id"].startswith("life-state-")
                         and callable(self.capabilities.get("recover_state")))
                interrupted = (item["kind"] == "activity.transition" and item["command_id"].startswith("activity-interrupted-")
                               and callable(self.capabilities.get("recover_interrupted_work")))
                reading = (item["kind"] == "activity.transition" and item["command_id"].startswith(
                    ("activity-reading-start-", "activity-reading-complete-")) and callable(self.capabilities.get("recover_reading")))
                reading_import = (item["kind"] == "activity.create" and item["command_id"].startswith("activity-reading-import-")
                                  and callable(self.capabilities.get("recover_reading_import")))
                reading_delete = (item["kind"] == "activity.delete" and item["command_id"].startswith("activity-delete-")
                                  and callable(self.capabilities.get("recover_reading_deletion")))
                reading_cancel = (item["kind"] == "activity.transition" and item["command_id"].startswith("activity-cancel-reading-")
                                  and callable(self.capabilities.get("recover_reading_cancel")))
                social = ((item["kind"] == "activity.transition" and item["command_id"].startswith("activity-social-complete-")
                           or item["kind"] == "activity.create" and item["command_id"].startswith("activity-social-start-"))
                          and callable(self.capabilities.get("recover_social")))
                allowed_contexts = calendar_contexts if calendar or state or reading or reading_import or reading_delete or reading_cancel or social or manual_sleep else contexts
                if (not (transition or creation or manual_sleep or calendar or state or interrupted or reading or reading_import or reading_delete or reading_cancel or social) or (not (calendar or state or reading or reading_import or reading_delete or reading_cancel or social) and item["owner_key"] in blocked)
                        or allowed_contexts.get(item["context_key"]) != item["owner_key"]):
                    continue
                request = self.commands.recovery_request(command_id=item["command_id"], owner_key=item["owner_key"],
                    context_key=item["context_key"], request_hash=item["request_hash"])
                if request is None:
                    self.last_failure = "life_command_invalid"
                    continue
                try:
                    action = (self.capabilities["recover_calendar"] if calendar else
                              self.capabilities["recover_state"] if state else
                              self.capabilities["recover_reading"] if reading else
                              self.capabilities["recover_reading_import"] if reading_import else
                              self.capabilities["recover_reading_deletion"] if reading_delete else
                              self.capabilities["recover_reading_cancel"] if reading_cancel else
                              self.capabilities["recover_social"] if social else
                              self.capabilities["recover_manual_sleep"] if manual_sleep else
                              self.capabilities["recover_interrupted_work"] if interrupted else
                              self.capabilities["recover_sleep_creation"] if creation else self.capabilities["recover"])
                    await asyncio.wait_for(action(item["context_key"], request), timeout=30)
                    recovered += 1
                except Exception as error:
                    self.last_failure = getattr(error, "code", "life_recovery_unavailable")
            pause_interrupted = self.capabilities.get("pause_interrupted_work")
            if callable(pause_interrupted):
                for context_key, owner_key in contexts.items():
                    if owner_key in blocked or context_key in self._checked_interrupted_work:
                        continue
                    checked = True
                    try:
                        for kind in ("reading", "social"):
                            rows = self.records.dispatch("work_activities", owner_key=owner_key, states=["active"],
                                kind=kind, context_key=context_key, limit=200, oldest_first=True)
                            if len(rows) == 200:
                                checked = False
                            for raw in rows:
                                activity = json.loads(raw)
                                if kind == "reading" and self.reading_store.is_pending(context_key, activity["activity_id"]):
                                    continue
                                await asyncio.wait_for(pause_interrupted(context_key, activity["activity_id"],
                                                                          activity["revision"]), timeout=30)
                    except Exception as error:
                        checked = False
                        self.last_failure = getattr(error, "code", "life_interruption_recovery_unavailable")
                    if checked:
                        self._checked_interrupted_work.add(context_key)
            for context_key, owner_key in contexts.items():
                if owner_key in blocked:
                    continue
                try:
                    rows = self.records.dispatch("work_activities", owner_key=owner_key, states=["active", "paused"], kind="sleep",
                                                 limit=200, oldest_first=True)
                except ActivityTransitionPending:
                    continue
                for raw in rows:
                    activity = json.loads(raw)
                    work = activity.get("work")
                    if type(work) is not dict or work.get("context_key") != context_key:
                        continue
                    if activity.get("state") == "paused":
                        sleep = activity.get("sleep")
                        if type(sleep) is not dict or sleep.get("wake_until") is None:
                            continue
                        due = datetime.fromisoformat(sleep["wake_until"])
                    else:
                        due = datetime.fromisoformat(work["due_at"])
                    if due.utcoffset() is None:
                        raise ValueError("sleep due time must be timezone-aware")
                    if due > now:
                        continue
                    try:
                        await asyncio.wait_for(self.capabilities["wake"](context_key, activity["activity_id"],
                                                                        activity["revision"]), timeout=30)
                        woken += 1
                    except Exception as error:
                        self.last_failure = getattr(error, "code", "life_wake_unavailable")
            if all(callable(self.capabilities.get(name)) for name in ("sleep_snapshot", "start_sleep", "recover_sleep_creation")):
                for context_key, owner_key in contexts.items():
                    identity = (context_key, owner_key)
                    if owner_key in blocked or now < self._next_sleep_check.get(identity, now):
                        continue
                    self._next_sleep_check[identity] = now + timedelta(seconds=30)
                    try:
                        snapshot = self.capabilities["sleep_snapshot"](context_key)
                        observed_at = datetime.fromisoformat(snapshot.pop("observed_at"))
                        hold_key = f"held_night_sleep:{owner_key}:{context_key}"
                        held = StorageReader(self.storage).one("SELECT value FROM life_metadata WHERE key=?", (hold_key,))
                        held_bedtime = None
                        settings = snapshot["settings"]
                        if held:
                            record = json.loads(held[0])
                            due = datetime.fromisoformat(record["scheduled_at"])
                            if (record["config_revision"] == snapshot["config_revision"]
                                    and settings["enabled"] and settings["sleep_enabled"]
                                    and observed_at < due + timedelta(minutes=settings["sleep_minutes"])):
                                held_bedtime = due
                            else:
                                with self.storage.transaction() as db:
                                    db.execute("DELETE FROM life_metadata WHERE key=?", (hold_key,))
                        plan = self.planner.plan(now=observed_at, held_bedtime=held_bedtime, **snapshot)
                        if plan is not None:
                            if (plan["kind"] == "night" and self.hold_night_sleep is not None
                                    and self.hold_night_sleep(context_key, owner_key, observed_at)):
                                if held_bedtime is None:
                                    record = {"scheduled_at": plan["scheduled_at"], "config_revision": snapshot["config_revision"]}
                                    with self.storage.transaction() as db:
                                        db.execute("INSERT INTO life_metadata VALUES (?, ?)", (hold_key, json.dumps(record)))
                                continue
                            result = await asyncio.wait_for(self.capabilities["start_sleep"](context_key, plan), timeout=30)
                            if result.get("state") in ("committed", "already_exists"):
                                self.planner.confirm(plan, owner=snapshot["owner"], settings=settings, now=observed_at)
                                if plan["kind"] == "night" and held_bedtime is not None:
                                    with self.storage.transaction() as db:
                                        db.execute("DELETE FROM life_metadata WHERE key=?", (hold_key,))
                    except Exception as error:
                        self.last_failure = getattr(error, "code", "life_sleep_start_unavailable")
            if self.state_scheduler is not None:
                try:
                    self.state_scheduler.advance(now)
                except Exception as error:
                    self.last_failure = getattr(error, "code", "life_state_scheduler_unavailable")
            if self.spending_tick is not None:
                try:
                    self.spending_tick(now)
                except Exception as error:
                    self.last_failure = getattr(error, "code", "life_planned_spending_unavailable")
            if self.calendar_scheduler is not None:
                try:
                    self.calendar_scheduler.advance(now)
                except Exception as error:
                    self.last_failure = getattr(error, "code", "life_calendar_scheduler_unavailable")
            available = {key: owner for key, owner in contexts.items() if owner not in blocked}
            if self.reading_scheduler is not None:
                self.reading_scheduler.collect(now)
            if self.social_scheduler is not None:
                self.social_scheduler.collect(now)
            manual = set()
            if callable(self.capabilities.get("reading_manual_pending")):
                manual = {item.get("context_key") for item in self.capabilities["reading_manual_pending"]()}
            for context_key, owner in available.items():
                if ((self.reading_scheduler and context_key in self.reading_scheduler.workers)
                        or (self.social_scheduler and context_key in self.social_scheduler.workers)):
                    continue
                first = False
                snapshot = None
                if self.social_scheduler is not None and context_key not in manual:
                    snapshot = self.social_scheduler.snapshot_if_due(context_key, now)
                    if snapshot is not None:
                        try:
                            local = datetime.fromisoformat(snapshot["observed_at"]).astimezone(ZoneInfo(snapshot["timezone"]))
                            first = social_first(snapshot["owner"], local.date().isoformat(), snapshot["interests"], snapshot["records"])
                        except Exception:
                            pass
                order = ((self.social_scheduler, self.reading_scheduler) if first else
                         (self.reading_scheduler, self.social_scheduler))
                for scheduler in order:
                    if scheduler is None:
                        continue
                    if scheduler is self.social_scheduler:
                        if context_key in manual:
                            snapshot = scheduler.snapshot_if_due(context_key, now)
                        if snapshot is not None:
                            scheduler.advance(context_key, snapshot, self.planner)
                    else:
                        scheduler.advance({context_key: owner}, now, self.planner)
                    if context_key in scheduler.workers:
                        break
            return {"recovered": recovered, "woken": woken}

    def start(self):
        if self._stopped:
            raise ValueError("scheduler was stopped")
        if self._worker is None:
            self._worker = asyncio.create_task(self._run(), name="good-life:due-sleep")
        return self._worker

    async def _run(self):
        while not self._stopped:
            try:
                await self.tick()
            except Exception as error:
                self.last_failure = getattr(error, "code", "life_scheduler_unavailable")
            await asyncio.sleep(self.interval_seconds)

    async def close(self):
        self._stopped = True
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self.state_scheduler is not None:
            await self.state_scheduler.close()
        if self.reading_scheduler is not None:
            await self.reading_scheduler.close()
        if self.social_scheduler is not None:
            await self.social_scheduler.close()
        if self.calendar_scheduler is not None:
            await self.calendar_scheduler.close()
