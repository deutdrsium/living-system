"""Independent-review probes through production binding and real host audit."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import json
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from test_reading_extraction import ReadingExtractionFixture
from test_reading_life_execution import bound_default_scheduler
from tests.test_role_tool_loop import answered
from companion_v2.execution.models import TaskError, digest, encode
from companion_v2.integration import life_event_inbox, manual_reading_queue
from companion_v2.integration.model_completion_handoff import ModelCompletionHandoff
from companion_v2.model_gateway.models import ProviderResponse
from good_life.contracts import models as m, ports as p
from good_life.reading_execution import ReadingExecutor, ReadingExecutionStore, signed
from good_life.reading_scheduler import ReadingScheduler
from good_life.reading_selection import choose, choice_key, MAX_RESULT_CHECKS
from good_life.reading_choices import choice_slot, reading_window
from good_life.sleep_scheduler import DueSleepScheduler


class ReadingReviewRegressions(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    async def fixture(self, **kwargs):
        f = await super().fixture(**kwargs)
        if f.executor is not None:
            f.executor.clock = self.clock.now
        return f

    def assert_no_a3(self, f):
        self.assertEqual(f.life._connection.execute("SELECT key FROM staged_life_runtime_metadata WHERE key LIKE 'a3.%'").fetchall(), [])
        self.assertEqual(f.life._connection.execute("SELECT name FROM sqlite_master WHERE name LIKE 'a3_%'").fetchall(), [])
        self.assertEqual(tuple(f.app.runtime.read_all("SELECT key FROM runtime_metadata WHERE key LIKE 'life.async.%'", ())), ())

    async def manual_intent(self, f):
        status = f.manager.status(self.core)
        card = next(item for item in status["activities"] if item["activity_id"] == f.activity.activity_id)
        request = {"action": "read", "activity_id": card["activity_id"], "expected_revision": card["revision"],
                   "operation_key": "review-manual", "view_token": card["view_token"]}
        self.assertTrue((await f.manager.command(self.core, request)).is_success)
        return manual_reading_queue.pending(f.app)[0]

    async def test_default_runtime_deleted_manual_intent_is_rejected_and_does_not_block(self):
        f = await self.fixture(life_executor=False, start=False)
        scheduler = await bound_default_scheduler(f.provider, f.api, f.token)
        self.addAsyncCleanup(scheduler.close)
        self.assertIsNone(scheduler.reading_scheduler.executor)
        intent = await self.manual_intent(f)
        # Apply the same storage deletion as StagedActivityDeletion.apply;
        # keep the already authorized host intent queued across that boundary.
        with f.life.transaction() as db:
            for table in ("role_artifacts", "role_activity_revisions", "role_activities"):
                db.execute(f"DELETE FROM staged_{table} WHERE owner_key=? AND activity_id=?",
                           (digest(f.routine.owner), f.activity.activity_id))
        reading = scheduler.reading_scheduler
        result = await reading._manual(f.context.key, intent)
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(manual_reading_queue.pending(f.app), [])
        with patch.dict(reading.capabilities, {"reading_snapshot": lambda _key: (_ for _ in ()).throw(LookupError("automatic path reached"))}):
            reading.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), scheduler)
        self.assertNotIn(f.context.key, reading.workers)
        self.assert_no_a3(f)

    async def test_default_runtime_reads_on_legacy_path_without_a3_rows_or_objects(self):
        f = await self.fixture(life_executor=False, start=False)
        scheduler = await bound_default_scheduler(f.provider, f.api, f.token)
        self.addAsyncCleanup(scheduler.close)
        with patch.object(ReadingExecutionStore, "save", side_effect=AssertionError("default A3 write")):
            await scheduler.reading_scheduler._read(f.context.key, json.loads(encode(f.activity)), None)
        completed = f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner)
        self.assertEqual(completed.state.value, "completed")
        self.assertEqual(completed.reading.end_offset, 200)
        self.assertEqual(len(self.called), 1)
        self.assert_no_a3(f)

    async def test_default_unknown_choice_has_no_new_backoff(self):
        f = await self.fixture(life_executor=False, start=False)
        scheduler = await bound_default_scheduler(f.provider, f.api, f.token)
        self.addAsyncCleanup(scheduler.close)
        reading = scheduler.reading_scheduler
        async def unknown(*_args):
            return {"state": "unknown"}
        reading.capabilities["choose_reading"] = unknown
        worker = asyncio.create_task(reading._choose(f.context.key, [], "legacy-slot", "window", None))
        reading.workers[f.context.key] = worker
        await worker
        reading.collect(self.clock.now())
        self.assertNotIn(f.context.key, reading.next_check)
        self.assert_no_a3(f)

    async def test_bridge_manual_does_not_read_life_ledger_even_with_other_life_runs(self):
        f = await self.fixture()
        executor = ReadingExecutor(f.life, None, enabled=False)
        calls = []
        async def legacy(context, operation):
            calls.append((context, operation))
            return {"state": "rejected"}
        scheduler = ReadingScheduler(f.life, {"execute_manual_reading": legacy}, executor=executor)
        with patch.object(executor.records, "dispatch", side_effect=AssertionError("extra ledger read")):
            result = await scheduler._manual(f.context.key, {"activity_id": "deleted", "operation_id": "intent"})
        self.assertEqual(result, {"state": "rejected"})
        self.assertEqual(calls, [(f.context.key, "intent")])
        self.assertIsNone(executor.store.get(f.context.key, "deleted"))

    async def test_failed_start_does_not_pin_owner_or_write_a3_state(self):
        f = await self.fixture(start=False)
        caps = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        async def failed(*_args):
            raise ValueError("start refused")
        caps["start_reading"] = failed
        scheduler = ReadingScheduler(f.life, caps, executor=f.executor)
        with self.assertRaisesRegex(ValueError, "start refused"):
            await scheduler._read(f.context.key, json.loads(encode(f.activity)), None)
        self.assertIsNone(f.executor.store.get(f.context.key, f.activity.activity_id))
        self.assertEqual(f.life._connection.execute("SELECT key FROM staged_life_runtime_metadata WHERE key LIKE 'a3.%'").fetchall(), [])

    async def test_crash_after_model_failure_before_host_pause_recovers(self):
        f = await self.fixture()
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('not JSON'))
        self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "paused"})
        self.assertEqual(f.executor.store.get(f.context.key, f.activity.activity_id)["phase"], "pausing")
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "active")
        recovered = ReadingExecutor(f.life, f.ports, enabled=False)
        self.assertTrue(recovered.is_pending(f.context.key, f.activity.activity_id))
        scheduler = ReadingScheduler(f.life, f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token), executor=recovered)
        await scheduler._run_life(f.context.key, f.activity.activity_id)
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "paused")
        self.assertEqual(recovered.store.get(f.context.key, f.activity.activity_id)["phase"], "paused")
        self.assertEqual(len(self.called), 1)

    async def test_old_paused_checkpoint_does_not_skip_startup_pause_recovery(self):
        f = await self.fixture()
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        state["phase"] = "paused"
        f.executor.store.save(f.context.key, f.activity.activity_id, state)
        scheduler = DueSleepScheduler(f.life, f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token), reading_executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        await scheduler.tick(now=self.clock.now())
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "paused")
        self.assertEqual(len(self.called), 0)

    async def test_absent_task_refreshes_persona_and_manifest_under_same_id(self):
        f = await self.fixture()
        with patch.object(f.app.role_reader.port_tasks, "submit", side_effect=ConnectionError("before task acceptance")):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
        old = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertIsNone(f.app.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id=?", (old["task_id"],)))
        persona = f.ports.get_persona_context
        async def changed(request):
            return replace(await persona(request), version="new-persona-version")
        with patch.object(f.ports, "get_persona_context", changed):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        new = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(old["task_id"], new["task_id"])
        self.assertNotEqual(old["params_digest"], new["params_digest"])
        self.assertEqual(len(self.called), 1)

    async def test_absent_book_choice_accepts_changed_candidates_and_parameters(self):
        f = await self.fixture(start=False)
        original = json.loads(encode(f.activity))
        with patch.object(f.app.role_reader.port_tasks, "submit", side_effect=ConnectionError("before task acceptance")):
            self.assertEqual(await choose(f.executor, f.context.key, [original], "changing-candidates", "window"), {"state": "unknown"})
        old = f.executor.store.get(f.context.key, choice_key("changing-candidates"))
        added = {**original, "activity_id": "imported-second-book"}
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"2","reason":"new book"}'))
        result = await choose(f.executor, f.context.key, [original, added], "changing-candidates", "window")
        self.assertEqual(result, {"state": "committed", "value": {
            "source_id": f.source.source_id, "reason": "new book", "starts": 0, "next_at": None}})
        new = f.executor.store.get(f.context.key, choice_key("changing-candidates"))
        self.assertEqual(old["task_id"], new["task_id"])
        self.assertNotEqual(old["params_digest"], new["params_digest"])
        self.assertEqual(len(self.called), 1)

    async def test_transient_choice_asset_recheck_does_not_force_a_new_choice(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        slot = "transient-asset-recheck"
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"continue"}'))
        with patch.object(f.ports, "asset", side_effect=TimeoutError("temporary recheck failure")):
            first = await choose(f.executor, f.context.key, [activity], slot, "window")
            state = f.executor.store.get(f.context.key, choice_key(slot))
            scope = await f.executor._scope(f.context.key)
            receipt = await f.ports.inspect_task(p.TaskInspection(scope, state["task_id"], state["params_digest"]))
            self.assertEqual((receipt.receipt.status, receipt.receipt.reason_code, receipt.result_ref),
                             (m.ReceiptStatus.UNKNOWN, m.ReasonCode.RESULT_UNKNOWN, None))
        recovered = await choose(f.executor, f.context.key, [activity], slot, "window")
        self.assertEqual(recovered, {"state": "committed", "value": {
            "source_id": f.source.source_id, "reason": "continue", "starts": 0, "next_at": None}})
        self.assertNotEqual(first["state"], "failed")
        self.assertEqual(len(self.called), 1)

    def choice_scheduler(self, f, executor):
        executor.clock = self.clock.now
        capabilities = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        snapshot = capabilities["reading_snapshot"](f.context.key)
        snapshot["sleep_enabled"] = False
        capabilities["reading_snapshot"] = lambda _key: {**snapshot, "observed_at": self.clock.now().isoformat()}
        scheduler = ReadingScheduler(f.life, capabilities, executor=executor)
        self.addAsyncCleanup(scheduler.close)
        local = self.clock.now().astimezone(ZoneInfo(snapshot["timezone"]))
        slot = choice_slot(snapshot["records"], local.date().isoformat()) + ":0"
        schedule = reading_window(snapshot["calendar"], self.clock.now(), local, None)
        return SimpleNamespace(scheduler=scheduler, planner=SimpleNamespace(_windows={}), slot=slot,
                               schedule=schedule, contexts={f.context.key: digest(f.routine.owner)})

    async def choose_tick(self, f, setup):
        setup.scheduler.advance(setup.contexts, self.clock.now(), setup.planner)
        result = await setup.scheduler.workers[f.context.key]
        setup.scheduler.collect(self.clock.now())
        return result

    async def test_repeated_transient_choice_checks_wait_for_definite_failure_before_rechoice(self):
        f = await self.fixture(start=False)
        setup = self.choice_scheduler(f, f.executor)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"continue"}'))
        with patch.object(f.ports, "asset", side_effect=TimeoutError("temporary recheck failure")), \
                patch.object(setup.scheduler, "monotonic_clock", return_value=100):
            for attempt in range(1, 4):
                self.assertEqual(await self.choose_tick(f, setup), {"state": "unknown"})
                state = f.executor.store.get(f.context.key, choice_key(setup.slot))
                self.assertEqual((state["phase"], state["result_check_attempts"]), ("unknown", attempt))
                self.assertEqual(setup.scheduler.slots[f.context.key]["retry"], 0)
                delay = 30 * 2 ** (attempt - 1)
                self.assertEqual(datetime.fromisoformat(state["result_check_after"]), self.clock.now() + timedelta(seconds=delay))
                self.clock.advance(timedelta(seconds=delay))
            with f.app.runtime.transaction() as db:
                db.execute("UPDATE execution_tasks SET state='failed' WHERE task_id=?", (state["task_id"],))
            self.assertEqual(await self.choose_tick(f, setup), {"state": "failed"})
        state = f.executor.store.get(f.context.key, choice_key(setup.slot))
        self.assertEqual(state["phase"], "failed")
        self.assertEqual(len(self.called), 1)
        with patch.object(setup.scheduler, "monotonic_clock", return_value=130):
            self.clock.advance(timedelta(seconds=30))
            setup.scheduler.advance(setup.contexts, self.clock.now(), setup.planner)
            self.assertEqual(setup.scheduler.slots[f.context.key]["retry"], 0)
            self.clock.advance(timedelta(seconds=30))
            setup.scheduler.advance(setup.contexts, self.clock.now(), setup.planner)
            self.assertEqual(setup.scheduler.slots[f.context.key]["retry"], 1)
            self.assertEqual((await setup.scheduler.workers[f.context.key])["state"], "committed")

    async def test_choice_retry_recovers_original_and_publishes_without_resubmission(self):
        f = await self.fixture(start=False)
        setup = self.choice_scheduler(f, f.executor)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        with patch.object(f.ports, "asset", side_effect=ConnectionError("temporary")):
            self.assertEqual(await self.choose_tick(f, setup), {"state": "unknown"})
        original = f.executor.store.get(f.context.key, choice_key(setup.slot))
        self.clock.advance(timedelta(seconds=30))
        with patch.object(f.ports, "run_model_task", side_effect=AssertionError("must inspect original")):
            result = await self.choose_tick(f, setup)
        self.assertEqual(result, {"state": "committed", "value": {
            "source_id": f.source.source_id, "reason": "original", "starts": 0, "next_at": None}})
        self.assertEqual(setup.scheduler._choice(f.context.key, setup.slot), result["value"])
        self.assertEqual(f.executor.store.get(f.context.key, choice_key(setup.slot))["task_id"], original["task_id"])
        self.assertEqual(setup.scheduler.slots[f.context.key]["retry"], 0)
        self.assertEqual(len(self.called), 1)

    async def test_crash_mid_choice_recheck_preserves_deadline_budget_and_original_task(self):
        f = await self.fixture(start=False)
        setup = self.choice_scheduler(f, f.executor)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        with patch.object(f.ports, "asset", side_effect=ConnectionError("temporary")):
            self.assertEqual(await self.choose_tick(f, setup), {"state": "unknown"})
        original = f.executor.store.get(f.context.key, choice_key(setup.slot))
        self.clock.advance(timedelta(seconds=30))
        entered = asyncio.Event()
        async def interrupted(_request):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(f.ports, "inspect_task", interrupted):
            setup.scheduler.advance(setup.contexts, self.clock.now(), setup.planner)
            await asyncio.wait_for(entered.wait(), timeout=5)
            await setup.scheduler.close()
        interrupted_state = f.executor.store.get(f.context.key, choice_key(setup.slot))
        self.assertEqual(interrupted_state["result_check_attempts"], 2)
        self.assertEqual(datetime.fromisoformat(interrupted_state["result_check_after"]), self.clock.now() + timedelta(seconds=60))
        # Disabling A3 after a crash still recovers life-owned choices.
        recovered = ReadingExecutor(f.life, f.ports, enabled=False)
        self.clock.advance(timedelta(seconds=50))
        restarted = self.choice_scheduler(f, recovered)
        with patch.object(restarted.scheduler, "monotonic_clock", return_value=100) as elapsed:
            with patch.object(f.ports, "inspect_task", side_effect=AssertionError("backoff must survive restart")):
                restarted.scheduler.advance(restarted.contexts, self.clock.now(), restarted.planner)
                self.assertNotIn(f.context.key, restarted.scheduler.workers)
            self.clock.advance(timedelta(seconds=10))
            elapsed.return_value = 110  # Only 10 seconds since the restart's snapshot.
            with patch.object(f.ports, "run_model_task", side_effect=AssertionError("must inspect original")):
                self.assertEqual((await self.choose_tick(f, restarted))["state"], "committed")
        state = recovered.store.get(f.context.key, choice_key(setup.slot))
        self.assertEqual((state["phase"], state["task_id"]), ("completed", original["task_id"]))
        self.assertEqual(len(self.called), 1)

    async def test_crash_in_first_inspection_of_submitted_choice_reserves_retry(self):
        f = await self.fixture(start=False)
        setup = self.choice_scheduler(f, f.executor)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        submit = f.ports.run_model_task
        async def pending(request):
            receipt = await submit(request)
            return replace(receipt, receipt=replace(receipt.receipt, status=m.ReceiptStatus.PENDING), result_ref=None)
        with patch.object(f.ports, "run_model_task", pending):
            self.assertEqual(await self.choose_tick(f, setup), {"state": "pending"})
        self.assertEqual(f.executor.store.get(f.context.key, choice_key(setup.slot))["phase"], "submitted")
        self.assertEqual(setup.scheduler.next_check[f.context.key], self.clock.now() + timedelta(seconds=30))
        self.clock.advance(timedelta(seconds=30))
        entered = asyncio.Event()
        async def interrupted(_request):
            entered.set()
            await asyncio.Event().wait()
        with patch.object(f.ports, "inspect_task", interrupted):
            setup.scheduler.advance(setup.contexts, self.clock.now(), setup.planner)
            await asyncio.wait_for(entered.wait(), timeout=5)
            await setup.scheduler.close()
        state = f.executor.store.get(f.context.key, choice_key(setup.slot))
        self.assertEqual((state["phase"], state["result_check_attempts"]), ("unknown", 1))
        self.assertEqual(datetime.fromisoformat(state["result_check_after"]), self.clock.now() + timedelta(seconds=30))
        restarted = self.choice_scheduler(f, ReadingExecutor(f.life, f.ports, enabled=False))
        restarted.scheduler.advance(restarted.contexts, self.clock.now(), restarted.planner)
        self.assertNotIn(f.context.key, restarted.scheduler.workers)
        self.clock.advance(timedelta(seconds=30))
        self.assertEqual((await self.choose_tick(f, restarted))["state"], "committed")
        self.assertEqual(len(self.called), 1)

    async def test_choice_recheck_exhaustion_logs_failure_ignores_late_result_and_rechooses(self):
        f = await self.fixture(start=False)
        setup = self.choice_scheduler(f, f.executor)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        with self.assertLogs("good_life.reading_execution", level="WARNING") as logs:
            with patch.object(f.ports, "asset", side_effect=ConnectionError("temporary")):
                for attempt in range(MAX_RESULT_CHECKS):
                    expected = "failed" if attempt == MAX_RESULT_CHECKS - 1 else "unknown"
                    self.assertEqual(await self.choose_tick(f, setup), {"state": expected})
                    self.clock.advance(timedelta(seconds=min(30 * 2 ** attempt, 900)))
        state = f.executor.store.get(f.context.key, choice_key(setup.slot))
        self.assertEqual((state["phase"], state["result_check_attempts"], state["reason_code"]),
                         ("failed", MAX_RESULT_CHECKS, "result_check_exhausted"))
        self.assertIn(state["task_id"], logs.output[-1])
        self.assertIn("result_check_exhausted", logs.output[-1])
        await setup.scheduler.close()
        # Use production binding after disabling A3, rather than constructing
        # an executor that the runtime might otherwise omit for failed history.
        f.api.unregister_life_provider(f.token)
        with patch.object(DueSleepScheduler, "start") as start, patch.object(
                f.api, "life_async_capabilities", side_effect=AssertionError("rolled-back host has no async API")):
            await f.provider.bind(f.api)
        start.assert_called_once()
        self.addAsyncCleanup(f.provider.unbind)
        reading = f.provider.scheduler.reading_scheduler
        self.assertIsNone(reading.executor)
        self.assertFalse(ReadingExecutionStore(f.life).has_life_runs())
        reading.capabilities["reading_snapshot"] = setup.scheduler.capabilities["reading_snapshot"]
        restarted = SimpleNamespace(scheduler=reading, planner=setup.planner, contexts=setup.contexts)
        with patch.object(f.ports, "inspect_task", side_effect=AssertionError("late result must be ignored")), patch.object(
                f.ports, "run_model_task", side_effect=AssertionError("original task must not be resubmitted")):
            result = await choose(f.executor, f.context.key, [json.loads(encode(f.activity))], setup.slot, setup.schedule)
        self.assertEqual(result, {"state": "failed"})
        with patch.dict(restarted.scheduler.capabilities, {"reading_snapshot": lambda _key: (_ for _ in ()).throw(AssertionError("failed choice is terminal"))}):
            self.assertEqual(await restarted.scheduler._choose(f.context.key, [f.activity.activity_id], setup.slot, setup.schedule, None), {"state": "failed"})
        restarted.scheduler.advance(restarted.contexts, self.clock.now(), restarted.planner)
        self.clock.advance(timedelta(seconds=30))
        restarted.scheduler.advance(restarted.contexts, self.clock.now(), restarted.planner)
        self.assertEqual(restarted.scheduler.slots[f.context.key]["retry"], 1)
        self.assertEqual((await self.choose_tick(f, restarted))["state"], "committed")
        self.assertEqual(f.executor.store.get(f.context.key, choice_key(setup.slot))["task_id"], state["task_id"])
        self.assertEqual(len(self.called), 2)

    async def test_succeeded_choice_without_result_fails_when_completed_candidates_change(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        submit = f.ports.run_model_task
        async def missing_result(request):
            return replace(await submit(request), result_ref=None)
        with patch.object(f.ports, "run_model_task", missing_result):
            self.assertEqual(await choose(f.executor, f.context.key, [activity], "no-result", "window"), {"state": "unknown"})
        scope = await f.executor._scope(f.context.key)
        state = f.executor.store.get(f.context.key, choice_key("no-result"))
        receipt = await f.ports.inspect_task(p.TaskInspection(scope, state["task_id"], state["params_digest"]))
        with patch.object(f.ports, "inspect_task", return_value=replace(receipt, result_ref=None)):
            changed = [activity, {**activity, "activity_id": "another-book"}]
            self.assertEqual(await choose(f.executor, f.context.key, changed, "no-result", "window"), {"state": "failed"})
        self.assertEqual(f.executor.store.get(f.context.key, choice_key("no-result"))["phase"], "failed")
        self.assertEqual((await choose(f.executor, f.context.key, [activity], "no-result", "window"))["state"], "failed")
        self.assertEqual(len(self.called), 1)

    async def test_default_runtime_choice_still_uses_legacy_receipt_and_no_a3_state(self):
        f = await self.fixture(life_executor=False, start=False)
        scheduler = await bound_default_scheduler(f.provider, f.api, f.token)
        self.addAsyncCleanup(scheduler.close)
        reading = scheduler.reading_scheduler
        snapshot = reading.capabilities["reading_snapshot"](f.context.key)
        local = self.clock.now().astimezone(ZoneInfo(snapshot["timezone"]))
        slot = choice_slot(snapshot["records"], local.date().isoformat()) + ":0"
        schedule = reading_window(snapshot["calendar"], self.clock.now(), local, None)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"legacy"}'))
        with patch.object(ReadingExecutionStore, "save", side_effect=AssertionError("default A3 write")):
            result = await reading._choose(f.context.key, [f.activity.activity_id], slot, schedule, None)
        self.assertEqual(result, {"state": "committed", "value": {
            "source_id": f.source.source_id, "reason": "legacy", "starts": 0, "next_at": None}})
        self.assertEqual(reading.capabilities["reading_choice_result"](f.context.key, slot), result)
        self.assert_no_a3(f)
        self.assertEqual(len(self.called), 1)

    async def test_choice_result_read_failure_after_successful_inspection_remains_recoverable(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        asset = f.ports.asset
        checks = 0
        def interrupted_read(scope, result_ref):
            nonlocal checks
            checks += 1
            if checks == 2:
                raise TimeoutError("temporary result read failure")
            return asset(scope, result_ref)
        with patch.object(f.ports, "asset", interrupted_read):
            self.assertEqual(await choose(f.executor, f.context.key, [activity], "read-race", "window"), {"state": "unknown"})
        self.assertEqual(checks, 2)
        self.assertEqual(f.executor.store.get(f.context.key, choice_key("read-race"))["phase"], "unknown")
        self.assertEqual((await choose(f.executor, f.context.key, [activity], "read-race", "window"))["state"], "committed")
        self.assertEqual(len(self.called), 1)

    async def test_succeeded_reading_without_result_remains_recoverable(self):
        f = await self.fixture()
        submit = f.ports.run_model_task
        async def missing_result(request):
            return replace(await submit(request), result_ref=None)
        with patch.object(f.ports, "run_model_task", missing_result):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
        self.assertEqual(f.executor.store.get(f.context.key, f.activity.activity_id)["phase"], "unknown")
        self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)

    async def test_permanent_result_rejections_fail_and_never_reinspect_or_resubmit_old_choice(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        with patch.object(f.ports, "asset", side_effect=TimeoutError("temporary")):
            await choose(f.executor, f.context.key, [activity], "permanent", "window")
        original = f.executor.store.get(f.context.key, choice_key("permanent"))
        for code in ("life_model_input_changed", "reading_source_changed", "source_audience_mismatch", "source_reference_invalid", "invalid_config"):
            with self.subTest(code=code):
                f.executor.store.save(f.context.key, choice_key("permanent"), original)
                with patch.object(f.ports, "asset", side_effect=TaskError(code, "permanent result rejection")):
                    self.assertEqual(await choose(f.executor, f.context.key, [activity], "permanent", "window"), {"state": "failed"})
                failed = f.executor.store.get(f.context.key, choice_key("permanent"))
                self.assertEqual((failed["phase"], failed["task_id"]), ("failed", original["task_id"]))
                self.assertEqual(failed["result_check_attempts"], original["result_check_attempts"])
                with patch.object(f.ports, "inspect_task", side_effect=AssertionError("late result must be ignored")), patch.object(
                        f.ports, "run_model_task", side_effect=AssertionError("old task must never be resubmitted")):
                    self.assertEqual(await choose(f.executor, f.context.key, [activity], "permanent", "window"), {"state": "failed"})
        self.assertEqual(len(self.called), 1)

    async def test_corrupt_stored_result_is_definite_failure_without_rewriting_task_audit(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        with patch.object(f.ports, "asset", side_effect=TimeoutError("temporary")):
            await choose(f.executor, f.context.key, [activity], "corrupt-result", "window")
        original = f.executor.store.get(f.context.key, choice_key("corrupt-result"))
        key = "life.model.result.v1:" + original["task_id"]
        valid = f.app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"]
        for damaged in ("not JSON", "{}", json.dumps({**json.loads(valid), "params_digest": "wrong"}),
                        json.dumps({**json.loads(valid), "context_key": "another-context"})):
            with self.subTest(damaged=damaged[:20]):
                f.executor.store.save(f.context.key, choice_key("corrupt-result"), original)
                with f.app.runtime.transaction() as db:
                    db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (damaged, key))
                self.assertEqual(await choose(f.executor, f.context.key, [activity], "corrupt-result", "window"), {"state": "failed"})
                self.assertEqual(f.app.runtime.read_one("SELECT state FROM execution_tasks WHERE task_id=?", (original["task_id"],))["state"], "succeeded")
        with f.app.runtime.transaction() as db:
            db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (valid, key))
        self.assertEqual(await choose(f.executor, f.context.key, [activity], "corrupt-result", "window"), {"state": "failed"})
        self.assertEqual(len(self.called), 1)

    async def test_permanent_result_read_rejection_fails_after_successful_inspection(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        asset, checks = f.ports.asset, 0
        def rejected_read(scope, result_ref):
            nonlocal checks
            checks += 1
            if checks == 2:
                raise TaskError("reading_source_changed", "permanent source change")
            return asset(scope, result_ref)
        with patch.object(f.ports, "asset", rejected_read):
            self.assertEqual(await choose(f.executor, f.context.key, [activity], "permanent-read", "window"), {"state": "failed"})
        self.assertEqual(checks, 2)
        self.assertEqual(await choose(f.executor, f.context.key, [activity], "permanent-read", "window"), {"state": "failed"})
        self.assertEqual(len(self.called), 1)

    async def test_in_flight_choice_inspections_do_not_spend_result_retry_budget(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(_env):
            entered.set()
            await release.wait()
            return answered('{"choice":"1","reason":"slow original"}')
        self.model_behavior = slow
        worker = asyncio.create_task(choose(f.executor, f.context.key, [activity], "in-flight", "window"))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            for _ in range(MAX_RESULT_CHECKS + 2):
                self.assertEqual(await choose(f.executor, f.context.key, [activity], "in-flight", "window"), {"state": "pending"})
                state = f.executor.store.get(f.context.key, choice_key("in-flight"))
                self.assertEqual(state.get("result_check_attempts", 0), 0)
                self.assertNotIn("result_check_after", state)
                scope = await f.executor._scope(f.context.key)
                receipt = await f.ports.inspect_task(p.TaskInspection(scope, state["task_id"], state["params_digest"]))
                self.assertEqual(receipt.receipt.status, m.ReceiptStatus.RUNNING)
                self.clock.advance(timedelta(seconds=10))
        finally:
            release.set()
            result = await asyncio.wait_for(worker, timeout=10)
        self.assertEqual(result["state"], "committed")
        self.assertEqual(len(self.called), 1)

    async def test_in_flight_reading_stays_active_without_consuming_result_budget(self):
        f = await self.fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow(_env):
            entered.set()
            await release.wait()
            return answered('{"reflection":"original reflection"}')
        self.model_behavior = slow
        worker = asyncio.create_task(f.executor.execute(f.context.key, f.activity.activity_id))
        try:
            await asyncio.wait_for(entered.wait(), timeout=5)
            for _ in range(MAX_RESULT_CHECKS + 2):
                self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "pending"})
                state = f.executor.store.get(f.context.key, f.activity.activity_id)
                self.assertEqual(state.get("result_check_attempts", 0), 0)
                self.assertNotIn("result_check_after", state)
                self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "active")
        finally:
            release.set()
            result = await asyncio.wait_for(worker, timeout=10)
        self.assertEqual(result, {"state": "completed"})
        self.assertEqual(len(self.called), 1)

    async def test_scope_and_original_errors_do_not_consume_result_budget(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        with patch.object(f.ports, "asset", side_effect=TimeoutError("temporary")):
            await choose(f.executor, f.context.key, [activity], "non-result-error", "window")
        original = f.executor.store.get(f.context.key, choice_key("non-result-error"))
        for target, method, error in ((f.executor, "_original", TimeoutError("source read timed out")),
                                      (f.executor, "_scope", TaskError("provider_unavailable", "offline"))):
            with self.subTest(method=method, code=getattr(error, "code", "timeout")):
                with patch.object(target, method, side_effect=error):
                    with self.assertRaises(type(error)):
                        await choose(f.executor, f.context.key, [activity], "non-result-error", "window")
                self.assertEqual(f.executor.store.get(f.context.key, choice_key("non-result-error")), original)
        self.assertEqual(len(self.called), 1)

    async def test_first_result_backoff_starts_after_slow_model_returns(self):
        f = await self.fixture(start=False)
        setup = self.choice_scheduler(f, f.executor)
        observed = self.clock.now()
        async def delayed(_env):
            self.clock.advance(timedelta(minutes=5))
            return answered('{"choice":"1","reason":"original"}')
        self.model_behavior = delayed
        with patch.object(f.ports, "asset", side_effect=TimeoutError("temporary")):
            self.assertEqual(await self.choose_tick(f, setup), {"state": "unknown"})
        state = f.executor.store.get(f.context.key, choice_key(setup.slot))
        self.assertEqual(datetime.fromisoformat(state["result_check_after"]), observed + timedelta(minutes=5, seconds=30))
        self.assertEqual(state["result_check_attempts"], 1)
        setup.scheduler.advance(setup.contexts, self.clock.now(), setup.planner)
        self.assertNotIn(f.context.key, setup.scheduler.workers)

    async def test_reading_result_retry_exhaustion_pauses_host_and_releases_foreground(self):
        f = await self.fixture()
        capabilities = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        scheduler = ReadingScheduler(f.life, capabilities, executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        with self.assertLogs("good_life.reading_execution", level="WARNING") as logs:
            with patch.object(f.ports, "asset", side_effect=ConnectionError("temporary")):
                for attempt in range(MAX_RESULT_CHECKS):
                    expected = "paused" if attempt == MAX_RESULT_CHECKS - 1 else "unknown"
                    self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": expected})
                    self.clock.advance(timedelta(seconds=min(30 * 2 ** attempt, 900)))
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual((state["phase"], state["reason_code"], state["result_check_attempts"]),
                         ("paused", "result_check_exhausted", MAX_RESULT_CHECKS))
        self.assertIn("result_check_exhausted", logs.output[-1])
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "paused")
        self.assertEqual(f.executor.store.pending(f.context.key), [])
        self.assertFalse(capabilities["reading_snapshot"](f.context.key)["busy"])
        with patch.object(f.ports, "inspect_task", side_effect=AssertionError("late result must not restart paused activity")):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "paused"})
        self.assertEqual(len(self.called), 1)

    async def test_unknown_reading_not_yet_due_yields_to_manual(self):
        f = await self.fixture()
        capabilities = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        intent = {"context_key": f.context.key, "owner_key": digest(f.routine.owner),
                  "activity_id": "manual-book", "operation_id": "manual-priority"}
        capabilities["reading_manual_pending"] = lambda: [intent]
        scheduler = ReadingScheduler(f.life, capabilities, executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        state.update(phase="unknown", task_id="original-reading-task", result_check_attempts=1,
                     result_check_after=(self.clock.now() + timedelta(minutes=5)).isoformat())
        f.executor.store.save(f.context.key, f.activity.activity_id, state)
        called = []
        async def manual(context, request):
            called.append((context, request))
            return {"state": "rejected"}
        with patch.object(scheduler, "_manual", manual), patch.object(scheduler, "_run_life", side_effect=AssertionError("retry overtook manual")):
            scheduler.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), SimpleNamespace(_windows={}))
            self.assertEqual(await scheduler.workers[f.context.key], {"state": "rejected"})
        self.assertEqual(called, [(f.context.key, intent)])
        self.assertEqual(len(self.called), 0)

    async def test_default_legacy_manual_reading_keeps_existing_scheduler_backoff(self):
        f = await self.fixture(life_executor=False, start=False)
        scheduler = await bound_default_scheduler(f.provider, f.api, f.token)
        self.addAsyncCleanup(scheduler.close)
        reading = scheduler.reading_scheduler
        reading.capabilities["reading_manual_pending"] = lambda: [{
            "context_key": f.context.key, "owner_key": digest(f.routine.owner),
            "activity_id": f.activity.activity_id, "operation_id": "legacy-manual"}]
        reading.next_check[f.context.key] = self.clock.now() + timedelta(seconds=30)
        reading.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), SimpleNamespace(_windows={}))
        self.assertNotIn(f.context.key, reading.workers)
        self.assertIsNone(reading.executor)
        self.assert_no_a3(f)

    async def test_refused_checks_preserve_unknown_calls_and_changed_candidates_never_resend(self):
        f = await self.fixture(start=False)
        original = json.loads(encode(f.activity))
        captured = []
        submit = f.ports.run_model_task
        async def capture(request):
            captured.append(request)
            return await submit(request)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=ProviderResponse("unknown"))
        with patch.object(f.ports, "run_model_task", capture):
            self.assertEqual((await choose(f.executor, f.context.key, [original], "unknown-choice", "window"))["state"], "unknown")
        request = captured[0]
        count = len(self.called)
        for call_state in ("reserved", "dispatched", "unknown"):
            with self.subTest(call_state=call_state):
                with f.app.runtime.transaction() as db:
                    db.execute("UPDATE model_calls SET state=? WHERE task_id=?", (call_state, request.task_id))
                bad = signed("model_task", replace(request, task_type="unsupported", params_digest=""))
                from astrbot_host.life_async_capabilities import HostPortError
                with self.assertRaises(HostPortError) as refused:
                    await submit(bad)
                self.assertEqual(refused.exception.code, "invalid_config")
                with patch.object(f.app.role_reader.port_tasks, "recover_result", side_effect=TaskError("reading_source_changed", "refused check")):
                    receipt = await f.ports.inspect_task(p.TaskInspection(request.scope, request.task_id, request.params_digest))
                self.assertEqual(receipt.receipt.status, m.ReceiptStatus.UNKNOWN)
        changed = [original, {**original, "activity_id": "new-book"}]
        self.assertEqual((await choose(f.executor, f.context.key, changed, "unknown-choice", "window"))["state"], "unknown")
        self.assertEqual(len(self.called), count)

    async def test_completion_is_delivered_with_more_than_one_page_of_pending_events(self):
        f = await self.fixture()
        with f.life.transaction():
            for i in range(105):
                f.life.record_event(event_id=f"older-{i}", owner_key=digest(f.routine.owner), context_key=f.context.key,
                    kind="activity.completed", payload={"operation_id": f"old-op-{i}"}, occurred_at=self.clock.now(), consumers=("journal", "interest"))
        await f.executor.execute(f.context.key, f.activity.activity_id)
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        operation = "reading-complete-" + digest(state["task_id"])
        event_id = "life-activity-" + hashlib.sha256(operation.encode()).hexdigest()
        for consumer in ("journal", "interest"):
            event = f.life.event_for_consumer(event_id, consumer)
            self.assertNotIn(event_id, [item["event_id"] for item in f.life.pending_events(consumer, limit=100)])
            self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))
        self.assertEqual(state["phase"], "completed")
        self.assertEqual(len(self.called), 1)

    async def test_revoked_aborted_proposal_discards_text_for_non_source_error(self):
        f = await self.fixture()
        original = f.ports.execute_platform_action
        async def revoke_and_reject(request):
            row = f.app.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id=?", (f.source.source_id,))
            f.app.role_journal.sources.revoke(f.source, expected_governance_revision=row["governance_revision"], observed_at=self.clock.now()).unwrap()
            with patch.object(ModelCompletionHandoff, "_authorize", side_effect=TaskError("life_revision_conflict", "revision rejected")):
                return await original(request)
        f.ports.execute_platform_action = revoke_and_reject
        self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "paused"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertNotIn("command", state)
        self.assertNotIn("proof", state)
        self.assertEqual(state["phase"], "pausing")
        row = f.life._connection.execute("SELECT state,payload_json,result_json FROM life_prepared_commands WHERE command_id LIKE 'activity-model-complete-%'").fetchone()
        self.assertEqual(tuple(row), ("aborted", "{}", None))
        self.assertNotIn("片段的读后感", json.dumps(state, ensure_ascii=False))
        scope = await f.executor._scope(f.context.key)
        ref = m.SourceRef("life.model.result.v1:" + state["task_id"], "1", None, None, None, self.clock.now(), None, "asset")
        with self.assertRaises(ValueError):
            await f.ports.read_material(p.MaterialRequest(scope, ref, "inspect"))
        self.assertEqual(len(self.called), 1)

    async def test_completed_choice_publishes_after_disable_without_ports(self):
        f = await self.fixture(start=False)
        activities = [json.loads(encode(f.activity))]
        context, slot = f.context.key, "recover-choice"
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"continue"}'))
        chosen = await choose(f.executor, context, activities, slot, "window")
        checkpoint = f.executor.store.get(context, choice_key(slot))
        receipt_query = "SELECT * FROM execution_tasks WHERE task_id=?"
        receipt = tuple(f.app.runtime.read_one(receipt_query, (checkpoint["task_id"],)))
        self.assertTrue(f.executor.store.has_life_runs())
        f.api.unregister_life_provider(f.token)
        with patch.object(DueSleepScheduler, "start"), patch.object(
                f.api, "life_async_capabilities", side_effect=AttributeError("rolled-back host has no async API")):
            await f.provider.bind(f.api)
        self.addAsyncCleanup(f.provider.unbind)
        reading = f.provider.scheduler.reading_scheduler
        self.assertIsNone(reading.executor)
        self.assertIsNone(reading._choice(context, slot))
        self.assertEqual(await reading._choose(context, [f.activity.activity_id], slot, "window", None), chosen)
        self.assertEqual(reading._choice(context, slot), chosen["value"])
        self.assertEqual(reading.store.get(context, choice_key(slot)), checkpoint)
        self.assertEqual(tuple(f.app.runtime.read_one(receipt_query, (checkpoint["task_id"],))), receipt)
        self.assertFalse(reading.store.has_life_runs())
        self.assertEqual(len(self.called), 1)

    async def test_advance_publishes_completed_choice_before_source_recheck_without_ports(self):
        f = await self.fixture(start=False)
        setup = self.choice_scheduler(f, f.executor)
        context = f.context.key
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"continue"}'))
        chosen = await choose(f.executor, context, [json.loads(encode(f.activity))], setup.slot, setup.schedule)
        self.assertEqual(chosen["value"]["source_id"], f.source.source_id)
        reading = setup.scheduler
        reading.executor = None
        snapshot = reading.capabilities["reading_snapshot"](context)
        self.assertIn(f.source.source_id, snapshot["authorized_sources"])
        reading.capabilities["reading_snapshot"] = lambda _context: {**snapshot, "authorized_sources": []}
        self.assertIsNone(reading._choice(context, setup.slot))

        reading.advance(setup.contexts, self.clock.now(), setup.planner)

        self.assertEqual(reading._choice(context, setup.slot), chosen["value"])
        self.assertNotIn(context, reading.workers)
        self.assertEqual(reading.slots[context]["retry"], 1)
        self.assertEqual(len(self.called), 1)

    async def test_scope_actions_and_model_kinds_are_restricted(self):
        f = await self.fixture()
        with self.assertRaises(ValueError):
            await f.ports.resolve_authority(p.AuthorityRequest(f.context.key, "role.life.run", ()))
        scope = await f.executor._scope(f.context.key)
        f.ports.authority_actions[scope.authority_ref] = "role.life.run"
        with self.assertRaises(ValueError):
            await f.executor._original(scope, json.loads(encode(f.started)))
        f.ports.authority_actions[scope.authority_ref] = "role.reading.record"
        original = await f.executor._original(scope, json.loads(encode(f.started)))
        request = signed("model_task", p.ModelTaskRequest(scope, "wrong-kind", "", "calendar.plan", m.TemplateRef("reading", 1),
            {}, (original.source_ref,), original.audience_bound, p.ModelBudget(8, 8000000, 524288)))
        with self.assertRaises(ValueError):
            await f.ports.run_model_task(request)
        self.assertEqual(len(self.called), 0)

    async def test_invalid_action_does_not_poison_id_and_conflict_is_explicit_rejection(self):
        f = await self.fixture(start=False)
        intent = await self.manual_intent(f)
        scope = await f.executor._scope(f.context.key)
        audience = m.AudienceBound("context:" + digest([scope.owner_key, scope.context_key]), (scope.context_key,))
        request = p.PlatformActionRequest(scope, intent["operation_id"], "", "life.intent.start", {"intent_ref": "wrong"}, (), audience)
        with self.assertRaises(ValueError):
            await f.ports.execute_platform_action(signed("platform_action", request))
        key = "life.async.action.v1:" + intent["operation_id"]
        self.assertIsNone(f.app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,)))
        valid = signed("platform_action", replace(request, payload={"intent_ref": manual_reading_queue.PREFIX + intent["operation_id"]}))
        self.assertEqual((await f.ports.execute_platform_action(valid)).status, m.ReceiptStatus.SUCCEEDED)
        original = f.app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"]
        rejected = await f.ports.execute_platform_action(signed("platform_action", request))
        self.assertEqual(rejected.status, m.ReceiptStatus.REJECTED)
        self.assertEqual(f.app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"], original)
        self.assertEqual(len(self.called), 0)
