"""Recovery ordering through the real manual queue, handoffs and host ports."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_reading_extraction import ReadingExtractionFixture
from tests.test_role_tool_loop import answered
from companion_v2.execution.models import digest, encode
from companion_v2.integration import manual_reading_queue
from companion_v2.integration.reading_handoff import ReadingStartHandoff
from good_life.contracts import models as m, ports as p
from good_life.reading_execution import ReadingExecutor, MODEL_TASK_TIMEOUT_SECONDS
from good_life.reading_scheduler import ReadingScheduler
from good_life.reading_selection import choose, choice_key


class ReadingSchedulerRecheckTests(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    async def fixture(self, **kwargs):
        f = await super().fixture(**kwargs)
        f.executor.clock = self.clock.now
        return f

    async def manual_intent(self, f):
        card = next(item for item in f.manager.status(self.core)["activities"]
                    if item["activity_id"] == f.activity.activity_id)
        result = await f.manager.command(self.core, {
            "action": "read", "activity_id": card["activity_id"], "expected_revision": card["revision"],
            "operation_key": "manual-recovery", "view_token": card["view_token"]})
        self.assertTrue(result.is_success, result)
        return manual_reading_queue.pending(f.app)[0]

    def scheduler(self, f):
        scheduler = ReadingScheduler(f.life, f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token),
                                     executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        return scheduler

    def advance(self, f, scheduler):
        scheduler.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), SimpleNamespace(_windows={}))

    async def other_reading(self, f):
        other = replace(f.activity, activity_id="other-reading", work=replace(f.activity.work, start_offset=200))
        # Seed a second imported activity; production writes are already owned
        # by life, so the old host add_activity method is intentionally closed.
        with f.life.transaction() as db:
            db.execute("""INSERT INTO staged_role_activities
                SELECT owner_key,?,owner_json,revision,state,updated_at,record_json
                FROM staged_role_activities WHERE activity_id=?""", (other.activity_id, f.activity.activity_id))
            db.execute("UPDATE staged_role_activities SET record_json=? WHERE activity_id=?", (encode(other), other.activity_id))
            db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key,activity_id,revision,updated_at,provenance,operation_id,record_json)
                SELECT owner_key,?,revision,updated_at,provenance,NULL,?
                FROM staged_role_activity_revisions WHERE activity_id=?""",
                (other.activity_id, encode(other), f.activity.activity_id))
        started = await ReadingStartHandoff(f.app, f.routine, f.app.role_life.transition_provider()).execute(
            other.activity_id, expected_revision=other.revision, request_id="other-run")
        f.executor.claim(f.context.key, other.activity_id)
        return started

    async def assert_manual_backoff(self, f, scheduler, code):
        self.advance(f, scheduler)
        with self.assertRaisesRegex(ValueError, code):
            await scheduler.workers[f.context.key]
        scheduler.collect(self.clock.now())
        self.assertEqual(len(manual_reading_queue.pending(f.app)), 1)
        self.assertEqual(scheduler.next_check[f.context.key], self.clock.now() + timedelta(seconds=30))
        self.clock.advance(timedelta(seconds=1))
        self.advance(f, scheduler)
        self.assertNotIn(f.context.key, scheduler.workers)

    async def test_active_manual_intent_waits_then_due_unknown_recovers_before_manual(self):
        f = await self.fixture(start=False)
        await self.manual_intent(f)
        other = await self.other_reading(f)
        with patch.object(f.ports, "asset", side_effect=TimeoutError("temporary result check")):
            self.assertEqual(await f.executor.execute(f.context.key, other.activity_id), {"state": "unknown"})
        scheduler = self.scheduler(f)
        # The not-yet-due unknown yields, but real handoff refusal retains the intent.
        await self.assert_manual_backoff(f, scheduler, "life_reading_active")
        self.clock.advance(timedelta(seconds=29))
        self.advance(f, scheduler)
        self.assertEqual(scheduler.activity_ids[f.context.key], other.activity_id)
        self.assertEqual(await scheduler.workers[f.context.key], {"state": "completed"})
        scheduler.collect(self.clock.now())
        self.assertEqual(len(manual_reading_queue.pending(f.app)), 1)
        self.clock.advance(timedelta(hours=1))
        self.advance(f, scheduler)
        self.assertEqual(await scheduler.workers[f.context.key], {"state": "completed"})
        self.assertEqual(manual_reading_queue.pending(f.app), [])
        self.assertEqual(len(self.called), 2)

    async def assert_recovery_precedes_manual(self, phase):
        f = await self.fixture(start=False)
        await self.manual_intent(f)
        other = await self.other_reading(f)
        if phase == "committing":
            with patch.object(f.ports, "execute_platform_action", side_effect=ValueError("before commit")):
                with self.assertRaisesRegex(ValueError, "before commit"):
                    await f.executor.execute(f.context.key, other.activity_id)
            self.assertIsNotNone(f.executor.store.get(f.context.key, other.activity_id).get("command"))
        elif phase == "pausing":
            self.model_behavior = lambda _env: asyncio.sleep(0, result=answered("bad JSON"))
            self.assertEqual(await f.executor.execute(f.context.key, other.activity_id), {"state": "paused"})
        scheduler = self.scheduler(f)
        self.advance(f, scheduler)
        self.assertEqual(scheduler.activity_ids[f.context.key], other.activity_id)
        expected = "paused" if phase == "pausing" else "completed"
        self.assertEqual(await scheduler.workers[f.context.key], {"state": expected})
        scheduler.collect(self.clock.now())
        self.assertEqual(len(manual_reading_queue.pending(f.app)), 1)
        self.assertEqual(f.app.role_life.activity(other.activity_id, owner=f.routine.owner).state.value, expected)

    async def test_committing_recovers_before_queued_manual(self):
        await self.assert_recovery_precedes_manual("committing")

    async def test_starting_recovers_before_queued_manual(self):
        await self.assert_recovery_precedes_manual("starting")

    async def test_pausing_recovers_before_queued_manual(self):
        await self.assert_recovery_precedes_manual("pausing")

    async def test_paced_manual_intent_keeps_queue_and_respects_backoff(self):
        f = await self.fixture(start=False)
        await self.manual_intent(f)
        other = await self.other_reading(f)
        self.assertEqual(await f.executor.execute(f.context.key, other.activity_id), {"state": "completed"})
        await self.assert_manual_backoff(f, self.scheduler(f), "life_reading_paced")

    async def test_unresolved_manual_authorization_keeps_queue_and_respects_backoff(self):
        f = await self.fixture(start=False)
        intent = await self.manual_intent(f)
        key = manual_reading_queue.PREFIX + intent["operation_id"]
        with f.app.runtime.transaction() as db:
            value = json.loads(db.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()[0])
            value["parameter_hash"] = "changed"
            db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (encode(value), key))
        await self.assert_manual_backoff(f, self.scheduler(f), "life_reading_unresolved")

    async def test_source_text_change_pauses_reading_and_releases_foreground(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        # Simulate a backing source changing while the original span is pinned.
        text = "changed original text"
        with f.app.runtime.transaction() as db:
            db.execute("UPDATE sources SET payload_json=?,content_hash=? WHERE source_id=?",
                       (json.dumps(text), digest(text), f.source.source_id))
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "paused")
        self.assertEqual(f.executor.store.get(f.context.key, f.activity.activity_id)["phase"], "paused")
        self.assertEqual(f.executor.store.pending(f.context.key), [])
        self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
        self.assertEqual(len(self.called), 0)

    async def assert_pending_deadline(self, kind, status):
        f = await self.fixture(start=kind == "activity")
        scheduler = self.scheduler(f)
        async def in_flight(request):
            return p.ModelTaskReceipt(request.task_id,
                m.OperationReceipt(request.task_id, "model", status, request.task_id), None, p.TaskUsage(0, None, None))
        slot = "pending-deadline"
        activity = json.loads(encode(f.activity))
        key = choice_key(slot) if kind == "choice" else f.activity.activity_id
        async def execute():
            return (await choose(scheduler.executor, f.context.key, [activity], slot, "window")
                    if kind == "choice" else await scheduler._run_life(f.context.key, key))
        with patch.object(f.ports, "run_model_task", in_flight), patch.object(f.ports, "inspect_task", in_flight):
            scheduler.life_workers.add(f.context.key)
            scheduler.workers[f.context.key] = asyncio.create_task(execute())
            self.assertEqual(await scheduler.workers[f.context.key], {"state": "pending"})
            scheduler.collect(self.clock.now())
            self.assertEqual(scheduler.next_check[f.context.key], self.clock.now() + timedelta(seconds=30))
            state = f.executor.store.get(f.context.key, key)
            deadline = datetime.fromisoformat(state["task_deadline"])
            self.assertEqual(deadline, self.clock.now() + timedelta(seconds=MODEL_TASK_TIMEOUT_SECONDS))
            self.clock.advance(timedelta(seconds=MODEL_TASK_TIMEOUT_SECONDS - 1))
            scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
            self.assertEqual(await execute(), {"state": "pending"})
            self.assertEqual(scheduler.executor.store.get(f.context.key, key)["task_deadline"], state["task_deadline"])
            self.clock.advance(timedelta(seconds=1))
            expected = "failed" if kind == "choice" else "paused"
            self.assertEqual(await execute(), {"state": expected})
            terminal = scheduler.executor.store.get(f.context.key, key)
            self.assertEqual(terminal["reason_code"], "task_deadline_exceeded")
            self.assertEqual(terminal.get("result_check_attempts", 0), 0)
            if kind == "activity":
                self.assertEqual(f.app.role_life.activity(key, owner=f.routine.owner).state.value, "paused")
                self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
            self.assertEqual(len(self.called), 0)

    async def test_old_unknown_checkpoint_gets_one_pending_deadline_without_spending_budget(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        slot = "old-pending-checkpoint"
        async def in_flight(request):
            return p.ModelTaskReceipt(request.task_id,
                m.OperationReceipt(request.task_id, "model", m.ReceiptStatus.RUNNING, request.task_id),
                None, p.TaskUsage(0, None, None))
        with patch.object(f.ports, "run_model_task", in_flight), patch.object(f.ports, "inspect_task", in_flight):
            self.assertEqual(await choose(f.executor, f.context.key, [activity], slot, "window"), {"state": "pending"})
            state = f.executor.store.get(f.context.key, choice_key(slot))
            state.pop("task_deadline")
            state.update(phase="unknown", result_check_attempts=2,
                         result_check_after=(self.clock.now() + timedelta(minutes=15)).isoformat())
            f.executor.store.save(f.context.key, choice_key(slot), state)
            self.assertEqual(await choose(f.executor, f.context.key, [activity], slot, "window"), {"state": "pending"})
            state = f.executor.store.get(f.context.key, choice_key(slot))
            self.assertEqual(state["phase"], "submitted")
            self.assertNotIn("result_check_after", state)
            deadline = state["task_deadline"]
            self.clock.advance(timedelta(seconds=30))
            recovered = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
            self.assertEqual(await choose(recovered, f.context.key, [activity], slot, "window"), {"state": "pending"})
            self.assertEqual(recovered.store.get(f.context.key, choice_key(slot))["task_deadline"], deadline)
            self.clock.advance(timedelta(seconds=MODEL_TASK_TIMEOUT_SECONDS - 30))
            self.assertEqual(await choose(recovered, f.context.key, [activity], slot, "window"), {"state": "failed"})
            state = recovered.store.get(f.context.key, choice_key(slot))
            self.assertEqual(state["result_check_attempts"], 2)
            self.assertEqual(state["reason_code"], "task_deadline_exceeded")

    async def test_pending_choice_has_poll_backoff_and_durable_deadline(self):
        await self.assert_pending_deadline("choice", m.ReceiptStatus.PENDING)

    async def test_running_choice_has_poll_backoff_and_durable_deadline(self):
        await self.assert_pending_deadline("choice", m.ReceiptStatus.RUNNING)

    async def test_pending_reading_has_poll_backoff_and_durable_deadline(self):
        await self.assert_pending_deadline("activity", m.ReceiptStatus.PENDING)

    async def test_running_reading_has_poll_backoff_and_durable_deadline(self):
        await self.assert_pending_deadline("activity", m.ReceiptStatus.RUNNING)
