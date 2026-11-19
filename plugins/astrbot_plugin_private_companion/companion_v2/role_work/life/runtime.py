"""Role routines driven by the host wake loop, with durable activity intent."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from companion_v2.conversation.models import InboundConversationEvent
from companion_v2.execution.models import TaskError, digest, require
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef, SourceRef, VersionRef
from companion_v2.sources.models import SourceRecord
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State
from companion_v2.role_work.life.physiology import (nap_ready, night_sleep_ready,
    recommended_sleep_minutes, sleep_receipt)
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.role_work.journal.models import JournalKind

from .configuration import GROUP, target_for, values
from .scheduling import next_bedtime
from .smart_reading import SmartReading
from .smart_social import SmartSocial
from .reading_queue import queue_key
from companion_v2.role_work.interests import scope_key, visible_interests
from companion_v2.role_work.interest_tasks import KIND as INTEREST_TASK_KIND


class RoleLifeRuntime:
    def __init__(self, app, *, context=None):
        self.app, self.runtime = app, app.runtime
        self.context, self.instruction, self.guard = context, None, None
        self.settings = None
        self._revision = None
        self._workers, self.results = {}, {}
        self._dreams = set()
        self._next_sleep = None
        self._next_wake = None
        self.failure_code = None
        self._closed = False
        self._retired = False
        self._smart_reading = SmartReading(self)
        self._smart_social = SmartSocial(self)
        self._next_interest_check = None

    @staticmethod
    def _route(context):
        scope = context.scope
        return (scope.installation_id, scope.platform, scope.bot_id, scope.account_id,
                scope.native_scope_id, context.role_binding.role.subject_id)

    def bind(self, context, role_instruction, *, guard=None):
        # Unrelated grant changes advance policy_revision without changing the
        # original principal, binding, role or audience represented by this key.
        if self.context is not None:
            if self.instruction not in (None, role_instruction):
                raise TaskError("life_binding_changed", "a live routine cannot change its authority")
            if self.context.key != context.key and self._route(self.context) != self._route(context):
                raise TaskError("life_binding_changed", "a live routine cannot change its authority")
            if self.context.key == context.key:
                # Refresh the same context's policy/audience snapshot.
                self.context = context
        else:
            self.context = context
        # The Dashboard target and native intake can name the same private route
        # with different conversation IDs.  Keep the first context (and the
        # activities/grants created under it) so a later native bind does not
        # turn that live routine into a different, unbound window.
        self.instruction, self.guard = role_instruction, guard
        self._retired = False

    @property
    def schedule_id(self):
        return "role.life.daily:" + self.context.key

    def retire(self):
        self._retired = True
        self.failure_code = "life_binding_changed"
        self._next_sleep = self._next_wake = None
        if self.schedule_id in self.app.journal_schedule._schedules:
            self.app.journal_schedule.remove(self.schedule_id)
        self.cancel_running()

    @property
    def owner(self):
        c = self.context
        return RoleOwnerRef(c.scope.installation_id, c.scope.bot_id, c.role_binding.role.subject_id)

    def _require_legacy_execution(self):
        if self.app.role_life.activity_owner() != "legacy":
            raise TaskError("life_executor_transferred", "life activity execution belongs to the life plugin")

    def authorize(self):
        self.app.require_running()
        if self.context is None or self.instruction is None:
            raise TaskError("life_not_bound", "the host must bind the original persona first")
        if self._retired:
            raise TaskError("life_binding_changed", "the host has selected another persona")
        require(self.app.tasks.identity.authorize(self.context.actor, scope=self.context.scope,
            capability="role.life.run", observed_at=self.app.now()))
        require(self.app.tasks.authorize_context(self.context, action="role.life.run", observed_at=self.app.now()))

    async def preflight(self):
        self.authorize()
        if self.guard:
            await self.guard(self.context.scope, self.context.role_binding, self.instruction)
        self.authorize()

    def event(self, label, text, *, at=None):
        at = at or self.app.now()
        identifier = "life-source-" + digest([self.context.key, label, at])
        source = SourceRef(identifier, GROUP, self.context.scope, VersionRef(identifier, 1), at)
        require(self.app.role_journal.sources.publish(SourceRecord(source, self.context.audience, digest(text), text),
                                                     observed_at=self.app.now()))
        return InboundConversationEvent(identifier, self.context.actor.principal, self.context.scope,
            source, "role.journal.write", digest(text), at)

    def activities(self, *, state=None):
        if self.context is None:
            return ()
        result, cursor = [], None
        while True:
            page = self.app.role_life.activities_for_role(self.owner, state=state, limit=100, after_id=cursor)
            result.extend(a for a in page if a.scope == self.context.scope and a.work is not None
                          and a.work.context_key == self.context.key)
            if len(page) < 100:
                return tuple(result)
            cursor = page[-1].activity_id

    def activity(self, identifier):
        try:
            a = self.app.role_life.activity(identifier, owner=self.owner)
        except KeyError:
            raise TaskError("life_activity_unavailable", "activity is unavailable in this role") from None
        if a.scope != self.context.scope or a.work is None or a.work.context_key != self.context.key:
            raise TaskError("life_context_changed", "activity belongs to another authority")
        return a

    def reading_pace_until(self, settings=None):
        settings = settings or self.settings
        if not settings:
            return None
        if self.app.role_life.activity_owner() == "good_life":
            pace = self.app.role_life.transition_provider().reading_metadata(context_key=self.context.key)["pace"]
        else:
            row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?",
                                        ("role.life.reading_pace:" + self.context.key,))
            pace = json.loads(row["value"]) if row else None
        if pace is None:
            return None
        start = datetime.fromisoformat(pace["started_at"])
        return start + timedelta(seconds=60 * pace["characters"] / settings["reading_speed_chars_per_minute"])

    def refresh(self):
        if self.instruction is None or self._closed or self._retired:
            return
        snapshot = require(self.app.configuration.read(self.context.actor, target=target_for(self.context),
            group_id=GROUP, observed_at=self.app.now()))
        if snapshot.resolution_token == self._revision:
            return
        settings = values(snapshot)
        generation = self.app.life_runtime.consumer_generation()
        if self._revision is not None:
            self.app.journal_schedule.remove(self.schedule_id)
        self.settings, self._revision = settings, snapshot.resolution_token
        self._next_sleep = self._next_wake = None
        self.failure_code = None
        if not settings["enabled"]:
            self.cancel_running()
            require(self.app.configuration.report_application(self.context.actor, snapshot=snapshot,
                generation=generation, succeeded=True, observed_at=self.app.now()))
            return
        self.authorize()
        if settings["diary_enabled"]:
            event = self.event("routine-registration", "管理员启用了本窗口的私人日记日程；这不是用户发来的聊天。")
            require(self.app.journal_schedule.register_daily(self.schedule_id, self.context, event,
                role_instruction=self.instruction, timezone=settings["timezone"], local_time=settings["diary_time"]))
        if settings["sleep_enabled"]:
            self._next_sleep = next_bedtime(self.app.now(), self.owner, settings["sleep_timezone"],
                time.fromisoformat(settings["sleep_time"]), settings["sleep_variation_minutes"])
            self._next_wake = self._next_sleep + timedelta(minutes=settings["sleep_minutes"])
        require(self.app.configuration.report_application(self.context.actor, snapshot=snapshot,
            generation=generation, succeeded=True, observed_at=self.app.now()))

    def start_sleep(self, request_id, minutes, *, label="角色开始一段睡眠", sleep_kind="manual"):
        self._require_legacy_execution()
        with self.runtime.transaction():
            return self._start_sleep(request_id, minutes, label=label, sleep_kind=sleep_kind)

    def _start_sleep(self, request_id, minutes, *, label, sleep_kind):
        self.authorize()
        if type(minutes) is not int or not 1 <= minutes <= 1440:
            raise ValueError("sleep duration must be 1..1440 minutes")
        if sleep_kind not in ("night", "nap", "manual"):
            raise ValueError("unknown sleep kind")
        identifier = "sleep-" + digest([self.context.key, request_id])
        try:
            return self.activity(identifier)
        except TaskError as error:
            if error.code != "life_activity_unavailable":
                raise
        if self.app.role_state.body_clock.pending_sleep(self.context):
            raise TaskError("life_already_sleeping", "there is already an active sleep")
        if self.app.role_state.body_clock.active(self.context, "reading"):
            raise TaskError("life_reading_active", "wait for the current reading or cancel it before sleeping")
        now = self.app.now()
        base_need = self.settings["sleep_minutes"] if self.settings else minutes
        profile = self.app.role_state.body_clock.profile(self.context, now=now)
        target = (25 if sleep_kind == "nap" else recommended_sleep_minutes(base_need,
            profile["sleep_debt_minutes"], profile["workload_score"])) if sleep_kind != "manual" else minutes
        receipt = sleep_receipt(kind=sleep_kind, base_need_minutes=base_need, target_minutes=target,
            workload_score=profile["workload_score"], sleep_debt_minutes=profile["sleep_debt_minutes"])
        work = RoleWorkSpec("sleep", self.context.key, request_id, due_at=now + timedelta(minutes=target))
        saved = self.app.role_life.add_activity(RoleActivity(identifier, self.owner.persona_id,
            self.context.scope, now, State.ACTIVE, description=label, work=work, sleep=receipt))
        self.app.notify()
        return saved

    def wake(self, identifier, *, expected_revision, operation_id):
        self._require_legacy_execution()
        self.authorize()
        a = self.activity(identifier)
        if a.state in (State.COMPLETED, State.CANCELLED):
            # A retry after a successful/failed transition must not turn into a
            # generic "command invalid"; the sleep is already over.
            return a
        if a.work.kind != "sleep" or self.app.now() <= a.started_at:
            raise TaskError("life_sleep_invalid", "sleep must actually have started before waking")
        saved = self.app.role_state.body_clock.finish(self.context, a, operation_id=operation_id,
            observed_at=self.app.now(), checkpoint="已醒来，梦境由独立任务记录")
        self.app.notify()
        return saved

    def approved_interrupted_reading_retry(self, activity, task_id):
        """One owner-approved retry keeps the unknown provider receipt intact."""
        try:
            if self.app.role_life.activity_owner() == "good_life":
                grant = self.app.role_life.transition_provider().reading_retry(context_key=self.context.key, task_id=task_id)
            else:
                marker = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", ("role.life.retry_interrupted:" + task_id,))
                grant = json.loads(marker["value"]) if marker else None
            if not grant:
                return False
            if grant != {"activity_id": activity.activity_id, "revision": activity.revision}:
                return False
            task = self.runtime.read_one("SELECT state, record_json, attempt_id FROM execution_tasks WHERE task_id = ?", (task_id,))
            if not task or task["state"] != "cancelled" or json.loads(task["record_json"]).get("effect") != "none":
                return False
            attempt = self.runtime.read_one("SELECT slot_held, record_json FROM execution_attempts WHERE attempt_id = ?", (task["attempt_id"],))
            if not attempt or attempt["slot_held"] or json.loads(attempt["record_json"]).get("stopped_proof") is None:
                return False
            return not self.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id = ? AND state IN ('reserved','dispatched')", (task_id,))
        except (ValueError, TypeError, KeyError):
            return False

    def start_reading(self, identifier, *, expected_revision, request_id):
        self._require_legacy_execution()
        self.authorize()
        self.refresh()
        a = self.activity(identifier)
        if a.work.kind != "reading":
            raise TaskError("life_reading_invalid", "select a reading activity")
        approved_retry = False
        if a.state is State.PAUSED:
            task_id = self.app.role_reader.task_id(self.context, a)
            row = self.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id = ?", (task_id,))
            unknown = self.runtime.read_one("SELECT 1 FROM model_calls WHERE task_id = ? AND state IN ('reserved','dispatched','unknown')", (task_id,))
            approved_retry = bool(unknown and self.approved_interrupted_reading_retry(a, task_id))
            if (unknown and not approved_retry) or (row is not None and row["state"] not in ("failed", "cancelled", "expired", "rejected")):
                raise TaskError("life_reading_unresolved", "the original reading must settle before an explicit retry")
        if self.app.role_state.body_clock.active(self.context, "sleep"):
            raise TaskError("life_sleeping", "wake the role before reading")
        if self.app.role_state.body_clock.active(self.context, "reading"):
            raise TaskError("life_reading_active", "wait for the current reading to finish")
        pace_until = self.reading_pace_until()
        if not approved_retry and pace_until is not None and self.app.now() < pace_until:
            raise TaskError("life_reading_paced", "wait for the current reading time to finish")
        record = require(self.app.role_journal.sources.read(a.work.source, resolved=self.context.actor,
            capability="role.reading.record", observed_at=self.app.now(), authorizer=self.app.tasks.identity))
        if record.content_hash != a.work.content_hash or digest(record.payload) != a.work.content_hash:
            raise TaskError("reading_source_changed", "reading must retain its original text")
        work = replace(a.work, request_id=request_id)
        if (a.state is State.PLANNED and self.settings and self.runtime.read_one(
                "SELECT 1 FROM runtime_metadata WHERE key = ?", (queue_key(self.context.key, a.work.source),))):
            size = self.settings["reading_chunk_chars"]
            start = work.start_offset
            while start < len(record.payload) and not record.payload[start:start + size].strip():
                start += size
            if start >= len(record.payload):
                raise TaskError("reading_source_empty", "the remaining original has no readable text")
            work = replace(work, start_offset=start, end_offset=min(len(record.payload), start + size))
        started_at = self.app.now()
        with self.runtime.transaction() as connection:
            saved = self.app.role_life.transition_activity(identifier, owner=self.owner, expected_revision=expected_revision,
                state=State.ACTIVE, operation_id="read-start-" + digest([self.context.key, request_id]),
                observed_at=started_at, work=work, checkpoint="正在阅读实际提供的片段")
            if a.state is State.PAUSED:
                connection.execute("DELETE FROM runtime_metadata WHERE key = ?", ("role.life.retry_interrupted:" + task_id,))
            connection.execute("""INSERT INTO runtime_metadata(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                ("role.life.reading_pace:" + self.context.key,
                 json.dumps({"started_at": started_at.isoformat(), "characters": work.end_offset - work.start_offset})))
        self.launch(identifier, lambda: self._read(saved))
        return saved

    async def _read(self, activity):
        self._require_legacy_execution()
        result = None
        try:
            await self.preflight()
            self._require_legacy_execution()
            result = await self.app.role_reader.consume(self.context, activity, role_instruction=self.instruction)
            return result
        finally:
            if self.app.role_life.activity_owner() == "legacy":
                current = self.activity(activity.activity_id)
                if current.state is State.ACTIVE:
                    code = (result.failure.code if result and result.failure else
                            result.unwrap().task.state if result else "interrupted")
                    self.app.role_life.transition_activity(current.activity_id, owner=self.owner,
                        expected_revision=current.revision, state=State.PAUSED,
                        operation_id="read-pause-" + digest([current.activity_id, current.work.request_id]),
                        observed_at=self.app.now(), checkpoint=code)

    async def derive(self, target, event, *, automatic=True):
        if automatic and (not self.settings or not self.settings["derive_enabled"]):
            return None
        await self.preflight()
        entry = self.app.role_journal.get(target.key)
        existing = self.app.role_journal.derivatives(target.key)
        if existing:
            return None
        event = self.event("derive-" + digest(entry.reference), "独立提取已保存的私人书写索引。", at=entry.updated_at)
        result = await self.app.journal_writer.derive(self.context, event, entry.reference,
            role_instruction=self.instruction, request_id="life-derive-" + digest(entry.reference))
        self.results["derive"] = result
        return result

    async def diary(self, *, local_date=None, retry_of=None, request_id=None):
        await self.preflight()
        zone = self.settings["timezone"]
        day = local_date or self.app.now().astimezone(ZoneInfo(zone)).date().isoformat()
        event = self.event("diary-" + day, "生活执行端请求保存本日私人日记；这不是用户聊天内容。")
        result = await self.app.journal_triggers.daily(self.context, event, local_date=day, timezone=zone,
            role_instruction=self.instruction, retry_of=retry_of, retry_id=request_id if retry_of else None)
        if result.is_success and result.unwrap().state == "written":
            target = self.app.journal_triggers.target(self.context, JournalKind.DIARY, day, zone)
            await self.derive(target, event)
        return result

    async def dream(self, activity, *, retry_of=None, request_id=None):
        await self.preflight()
        zone = self.settings["timezone"]
        event = self.event("sleep-completed-" + activity.activity_id,
            "生活执行端确认本段睡眠已结束，没有执行其他醒后动作。", at=activity.updated_at)
        result = await self.app.journal_triggers.sleep_completed(self.context, event,
            started_at=activity.started_at, completed_at=activity.updated_at, timezone=zone,
            role_instruction=self.instruction, retry_of=retry_of, retry_id=request_id if retry_of else None)
        if result.is_success and result.unwrap().state == "written":
            target = self.app.journal_triggers.target(self.context, JournalKind.DREAM,
                activity.updated_at.astimezone(ZoneInfo(zone)).date().isoformat(), zone)
            await self.derive(target, event)
        return result

    def launch(self, key, factory, *, timeout_seconds=None):
        if self._closed or key in self._workers:
            return False
        async def execute():
            try:
                work = factory()
                return (await asyncio.wait_for(work, timeout=timeout_seconds)
                        if timeout_seconds is not None else await work)
            except asyncio.TimeoutError:
                return Outcome.failed("life_wake_timeout", "scheduled wake did not finish before its deadline")
            except TaskError as error:
                return Outcome.failed(error.code, str(error))
            except Exception:
                return Outcome.failed("life_execution_failed", "routine stopped; inspect its recorded outcome")
        self._workers[key] = asyncio.create_task(execute(), name="companion:life:" + key)
        def collect(done):
            if timeout_seconds is not None and self._workers.get(key) is done:
                if not done.cancelled():
                    self.results[key] = done.result()
                del self._workers[key]
            self.app.notify()
        self._workers[key].add_done_callback(collect)
        return True

    def advance(self):
        if self.context is None or self._closed:
            return
        try:
            self._require_legacy_execution()
        except TaskError:
            self.cancel_running()
            return
        for key, worker in tuple(self._workers.items()):
            if worker.done():
                if not worker.cancelled():
                    self.results[key] = worker.result()
                del self._workers[key]
        if self.instruction is None or self._retired:
            return
        try:
            self.refresh()
            if not self.settings or not self.settings["enabled"]:
                return
            self.authorize()
            now = self.app.now()
            self.app.role_state.body_clock.resume_due(self.context, observed_at=now)
            for sleeping in self.app.role_state.body_clock.active(self.context, "sleep"):
                wake_key = "wake:" + sleeping.activity_id
                if (sleeping.work.due_at <= now and wake_key not in self._workers
                        and wake_key not in self.results):
                    self.launch(wake_key, self._wake_shared_due, timeout_seconds=30)
            records = self.activities()
            for a in records:
                if a.work.kind == "reading" and a.state is State.ACTIVE and a.activity_id not in self._workers:
                    # A restart has no in-memory reading call. Never turn elapsed time into a completion.
                    self.app.role_life.transition_activity(a.activity_id, owner=self.owner, expected_revision=a.revision,
                        state=State.PAUSED, operation_id="recover-reading-" + digest([a.activity_id, a.revision]),
                        observed_at=now, checkpoint="interrupted; inspect the original model result before retrying")
                elif a.work.kind == "social" and a.state is State.ACTIVE and a.activity_id not in self._workers:
                    self.app.role_life.transition_activity(a.activity_id, owner=self.owner, expected_revision=a.revision,
                        state=State.PAUSED, operation_id="recover-social-" + digest([a.activity_id, a.revision]),
                        observed_at=now, checkpoint="浏览中断；不会重复结果不明的互动")
                elif a.work.kind == "sleep" and a.state is State.COMPLETED and a.activity_id not in self._dreams:
                    self._dreams.add(a.activity_id)
                    self.launch(a.activity_id, lambda a=a: self.dream(a))
                if a.work.kind == "sleep" and a.state is State.COMPLETED:
                    self.app.role_wardrobe.advance(self, a, now)
            pending_sleep = self.app.role_state.body_clock.pending_sleep(self.context)
            busy = self._workers or any(a.state is State.ACTIVE for a in records) or pending_sleep
            if (self._next_sleep is not None and self._next_sleep <= now and not busy
                    and self.app.life_runtime.owns_sleep_schedule(self)):
                zone = self.settings["sleep_timezone"]
                due_day = self._next_sleep.astimezone(ZoneInfo(zone)).date()
                if self._next_wake is not None and now >= self._next_wake:
                    self._next_sleep = next_bedtime(now + timedelta(microseconds=1), self.owner, zone,
                        time.fromisoformat(self.settings["sleep_time"]), self.settings["sleep_variation_minutes"])
                    self._next_wake = self._next_sleep + timedelta(minutes=self.settings["sleep_minutes"])
                elif not self.app.role_state.body_clock.active(self.context, "reading"):
                    profile = self.app.role_state.body_clock.profile(self.context, now=now)
                    window_closed = now >= self._next_sleep + timedelta(minutes=self.settings["sleep_window_minutes"])
                    if night_sleep_ready(profile) or window_closed:
                        self.start_sleep("daily-sleep-" + due_day.isoformat(),
                            self.settings["sleep_minutes"], sleep_kind="night")
                        self._next_sleep = next_bedtime(now + timedelta(microseconds=1), self.owner, zone,
                            time.fromisoformat(self.settings["sleep_time"]), self.settings["sleep_variation_minutes"])
                        self._next_wake = self._next_sleep + timedelta(minutes=self.settings["sleep_minutes"])
                        return
            local_now = now.astimezone(ZoneInfo(self.settings["sleep_timezone"]))
            profile = self.app.role_state.body_clock.profile(self.context, now=now)
            if (self.settings["sleep_enabled"] and not busy
                    and nap_ready(profile, local_minutes=local_now.hour * 60 + local_now.minute)):
                today = local_now.date().isoformat()
                if not any(item.sleep and item.sleep.kind == "nap" and item.started_at.astimezone(
                        ZoneInfo(self.settings["sleep_timezone"])).date().isoformat() == today
                        for item in self.app.role_state.body_clock.episodes(self.context)):
                    self.start_sleep("daily-nap-" + today, 25, label="角色短暂午休", sleep_kind="nap")
                    return
            if (not busy and INTEREST_TASK_KIND in self.app.gateway.router.profiles
                    and (self._next_interest_check is None or now >= self._next_interest_check)):
                self._next_interest_check = now + timedelta(hours=2)
                candidates = [a for a in records if a.state is State.COMPLETED and a.work.kind in ("reading", "social")
                              and not self.app.role_interests.store.processed(self.context, a.activity_id)]
                candidates = [a for a in candidates if not (a.work.kind == "social" and
                    self.app.role_social.exposure.session_platform(self.context, a.activity_id) is not None)]
                candidates = [a for a in candidates if a.work.kind == "social" or (a.reading and a.reading.reflection and
                    self.app.role_journal.sources.validate_reference_for_audience(a.reading.source,
                        recipient=self.context.actor.subject, observed_at=now).is_success)]
                day = now.astimezone(ZoneInfo(self.settings["timezone"])).date().isoformat()
                daily_key = "role.interests.daily:" + scope_key(self.context) + ":" + day
                row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (daily_key,))
                count = int(row["value"]) if row else 0
                if candidates and count < 2:
                    candidates.sort(key=lambda a: (a.updated_at, a.activity_id))
                    cursor_key = "role.interests.cursor:" + scope_key(self.context)
                    cursor = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", (cursor_key,))
                    index = next((i + 1 for i, item in enumerate(candidates)
                                  if cursor and item.activity_id == cursor["value"]), 0)
                    selected = candidates[index % len(candidates)]
                    if self.launch("interest:" + selected.activity_id,
                                   lambda: self.app.role_interests.consume(self, selected)):
                        with self.runtime.transaction() as connection:
                            connection.execute("""INSERT INTO runtime_metadata(key, value) VALUES (?, ?)
                                ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (daily_key, str(count + 1)))
                            connection.execute("""INSERT INTO runtime_metadata(key, value) VALUES (?, ?)
                                ON CONFLICT(key) DO UPDATE SET value = excluded.value""", (cursor_key, selected.activity_id))
            pace_until = self.reading_pace_until()
            if not busy and not self._workers and self.settings["reading_enabled"]:
                self._smart_reading.resume_approved(records, now)
            if not busy and not self._workers and (pace_until is None or now >= pace_until):
                interests = visible_interests(self.app, self.context, now)
                day = now.astimezone(ZoneInfo(self.settings["timezone"])).date().isoformat()
                social_first = bool(interests) and int(digest([self.owner, day])[:8], 16) % 5 < 2
                completed = [a for a in records if a.state is State.COMPLETED
                             and a.work.kind in ("reading", "social")]
                if completed:
                    last = max(completed, key=lambda a: (a.updated_at, a.activity_id))
                    social_first = last.work.kind == "reading"
                if social_first:
                    self._smart_social.advance(records, now)
                if self.settings["reading_enabled"] and not self._workers:
                    self._smart_reading.advance(records, now)
                if not social_first and not self._workers:
                    self._smart_social.advance(records, now)
        except (TaskError, ValueError, KeyError) as error:
            self.failure_code = error.code if isinstance(error, TaskError) else "life_configuration_invalid"
            self.cancel_running()

    def advance_host_tasks(self):
        """Keep host-owned diary registration without advancing life activities."""
        if self.context is None or self._closed or self._retired or self.instruction is None:
            return False
        try:
            self.refresh()
            return True
        except (TaskError, ValueError, KeyError) as error:
            self.failure_code = error.code if isinstance(error, TaskError) else "life_configuration_invalid"
            return False

    async def _wake_shared_due(self):
        self._require_legacy_execution()
        await self.preflight()
        self._require_legacy_execution()
        self.app.role_state.body_clock.finish_due(self.context)
        self.app.notify()
        return Outcome.success({"state": "sleep_timer_checked"})

    @property
    def active_handles(self):
        return tuple("life-" + key for key in self._workers)

    def cancel_running(self):
        for worker in tuple(self._workers.values()):
            worker.cancel()

    async def close(self):
        self._closed = True
        self.cancel_running()
        if self._workers:
            await asyncio.wait(tuple(self._workers.values()), timeout=0.25)
        for key, worker in tuple(self._workers.items()):
            if worker.done():
                if not worker.cancelled():
                    worker.exception()
                del self._workers[key]
