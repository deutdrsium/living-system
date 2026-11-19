"""Post-review recovery regressions through the contract-v1 host adapter."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from test_reading_extraction import ReadingExtractionFixture
from tests.test_role_tool_loop import answered
from companion_v2.execution.models import TaskError, digest, encode
from companion_v2.integration import life_event_inbox
from companion_v2.integration.model_completion_handoff import ModelCompletionHandoff
from companion_v2.model_gateway.life_tasks import MODEL_TASK_TIMEOUT_SECONDS as HOST_TASK_TIMEOUT_SECONDS
from good_life.contracts import models as m, ports as p
from good_life.reading_choices import choice_slot, reading_window
from good_life.reading_execution import ReadingExecutor, MODEL_TASK_TIMEOUT_SECONDS, MAX_RESULT_CHECKS, signed
from good_life.reading_scheduler import ReadingScheduler
from good_life.reading_selection import choose, choice_key


class ReadingFollowupTests(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    async def fixture(self, **kwargs):
        f = await super().fixture(**kwargs)
        f.executor.clock = self.clock.now
        return f

    def scheduler(self, f):
        scheduler = ReadingScheduler(f.life, f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token),
                                     executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        return scheduler

    async def assert_absent_resubmission_refreshes_deadline(self, kind):
        f = await self.fixture(start=kind == "activity")
        activity = json.loads(encode(f.activity))
        key = choice_key("absent-retry") if kind == "choice" else f.activity.activity_id
        if kind == "choice":
            self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        async def execute(executor):
            return (await choose(executor, f.context.key, [activity], "absent-retry", "window")
                    if kind == "choice" else await executor.execute(f.context.key, key))
        with patch.object(f.ports, "run_model_task", side_effect=ConnectionError("before admission")):
            self.assertEqual(await execute(f.executor), {"state": "unknown"})
        old = f.executor.store.get(f.context.key, key)
        self.assertIsNone(f.app.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id=?", (old["task_id"],)))
        self.clock.advance(timedelta(seconds=MODEL_TASK_TIMEOUT_SECONDS + 30))
        self.assertLess(datetime.fromisoformat(old["task_deadline"]), self.clock.now())
        submit = f.ports.run_model_task
        receipts = []
        async def pending(request):
            receipt = await submit(request)
            receipts.append(receipt)
            return replace(receipt, receipt=replace(receipt.receipt, status=m.ReceiptStatus.PENDING), result_ref=None)
        with patch.object(f.ports, "run_model_task", pending):
            self.assertEqual(await execute(f.executor), {"state": "pending"})
        rebuilt = f.executor.store.get(f.context.key, key)
        self.assertEqual(rebuilt["task_id"], old["task_id"])
        self.assertEqual(datetime.fromisoformat(rebuilt["task_deadline"]),
                         self.clock.now() + timedelta(seconds=MODEL_TASK_TIMEOUT_SECONDS))
        self.assertEqual(rebuilt["result_check_attempts"], old["result_check_attempts"])
        recovered = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
        running = replace(receipts[0], receipt=replace(receipts[0].receipt, status=m.ReceiptStatus.RUNNING), result_ref=None)
        with patch.object(f.ports, "inspect_task", return_value=running), patch.object(
                f.ports, "run_model_task", side_effect=AssertionError("existing task must only be inspected")):
            self.assertEqual(await execute(recovered), {"state": "pending"})
        self.assertEqual(recovered.store.get(f.context.key, key)["task_deadline"], rebuilt["task_deadline"])
        self.assertEqual((await execute(recovered))["state"], "committed" if kind == "choice" else "completed")
        self.assertEqual(len(self.called), 1)

    async def test_absent_reading_resubmission_refreshes_expired_deadline(self):
        await self.assert_absent_resubmission_refreshes_deadline("activity")

    async def test_absent_choice_resubmission_refreshes_expired_deadline(self):
        await self.assert_absent_resubmission_refreshes_deadline("choice")

    async def assert_permanent_admission_rejection(self, kind, code):
        f = await self.fixture(start=kind == "activity")
        scheduler = self.scheduler(f)
        key = choice_key("admission-refused") if kind == "choice" else f.activity.activity_id
        activity = json.loads(encode(f.activity))
        async def execute():
            return (await choose(f.executor, f.context.key, [activity], "admission-refused", "window")
                    if kind == "choice" else await scheduler._run_life(f.context.key, key))
        # Use the real capability decorator to translate a pre-admission host
        # failure to HostPortError (invalid_config/stale_version).
        with patch.object(f.app.role_reader.port_tasks, "submit", side_effect=TaskError(code, "admission refused")) as submit:
            self.assertEqual(await execute(), {"state": "failed" if kind == "choice" else "paused"})
            state = f.executor.store.get(f.context.key, key)
            self.assertEqual(state["phase"], "failed" if kind == "choice" else "paused")
            self.assertIn(state["reason_code"], ("invalid_config", "stale_version", "unauthorized"))
            self.assertIsNone(f.app.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id=?", (state["task_id"],)))
            self.assertEqual(f.executor.store.pending(f.context.key), [])
            self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
            self.assertEqual(await execute(), {"state": "failed" if kind == "choice" else "paused"})
            self.assertEqual(submit.call_count, 1)
        if kind == "choice":
            # A new scheduler slot remains eligible after the rejected slot.
            self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"retry"}'))
            self.assertEqual((await choose(f.executor, f.context.key, [activity], "admission-refused:1", "window"))["state"], "committed")
        self.assertEqual(len(self.called), 1 if kind == "choice" else 0)

    async def test_invalid_reading_admission_pauses_and_releases_foreground(self):
        await self.assert_permanent_admission_rejection("activity", "life_model_task_invalid")

    async def test_changed_reading_admission_pauses_and_releases_foreground(self):
        await self.assert_permanent_admission_rejection("activity", "life_model_input_changed")

    async def test_invalid_choice_admission_fails_and_allows_rechoice(self):
        await self.assert_permanent_admission_rejection("choice", "life_model_task_invalid")

    async def test_changed_choice_admission_fails_and_allows_rechoice(self):
        await self.assert_permanent_admission_rejection("choice", "life_model_input_changed")

    async def test_unauthorized_reading_admission_pauses_and_releases_foreground(self):
        await self.assert_permanent_admission_rejection("activity", "life_authority_denied")

    async def test_unauthorized_choice_admission_fails_and_allows_rechoice(self):
        await self.assert_permanent_admission_rejection("choice", "life_authority_denied")

    async def assert_transient_admission_is_bounded(self, kind, code="result_unknown"):
        f = await self.fixture(start=kind == "activity")
        scheduler = self.scheduler(f)
        key = choice_key("admission-transient") if kind == "choice" else f.activity.activity_id
        activity = json.loads(encode(f.activity))
        # Each next inspect confirms absence; resubmission must retain the
        # uncertainty budget even while refreshing its provisional deadline.
        with patch.object(f.app.role_reader.port_tasks, "submit", side_effect=TaskError(code, "temporary")) as submit:
            for attempt in range(1, MAX_RESULT_CHECKS + 1):
                result = (await choose(f.executor, f.context.key, [activity], "admission-transient", "window")
                          if kind == "choice" else await scheduler._run_life(f.context.key, key))
                terminal = "failed" if kind == "choice" else "paused"
                self.assertEqual(result, {"state": terminal if attempt == MAX_RESULT_CHECKS else "unknown"})
                state = f.executor.store.get(f.context.key, key)
                self.assertEqual(state["result_check_attempts"], attempt)
                if attempt < MAX_RESULT_CHECKS:
                    self.assertEqual(datetime.fromisoformat(state["result_check_after"]),
                                     self.clock.now() + timedelta(seconds=30 * 2 ** (attempt - 1)))
                self.clock.advance(timedelta(seconds=30 * 2 ** (attempt - 1)))
            self.assertEqual(submit.call_count, MAX_RESULT_CHECKS)
        self.assertEqual(state["reason_code"], "result_check_exhausted")
        self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
        self.assertEqual(len(self.called), 0)

    async def test_transient_reading_admission_uses_bounded_retry(self):
        await self.assert_transient_admission_is_bounded("activity")

    async def test_transient_choice_admission_uses_bounded_retry_across_absence(self):
        await self.assert_transient_admission_is_bounded("choice")

    async def test_unavailable_provider_reading_admission_is_bounded(self):
        await self.assert_transient_admission_is_bounded("activity", "life_provider_unavailable")

    async def test_unavailable_provider_choice_admission_is_bounded(self):
        await self.assert_transient_admission_is_bounded("choice", "life_provider_unavailable")

    async def assert_commit_error_preserves_reconciliation(self, phase):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        method = {"prepared": "apply", "committed": "complete", "acknowledged": "acknowledge"}[phase]
        original = getattr(ModelCompletionHandoff, method)
        def interrupted(handoff, *args, **kwargs):
            if phase == "acknowledged":
                original(handoff, *args, **kwargs)
            code = "life_revision_conflict" if phase == "committed" else "reading_source_changed"
            raise TaskError(code, "interrupted after durable decision")
        with patch.object(ModelCompletionHandoff, method, interrupted):
            result = await scheduler._run_life(f.context.key, f.activity.activity_id)
        if phase == "acknowledged":
            # An acknowledged local row is stronger evidence than a lost/error
            # response, and already has the original completion events.
            self.assertEqual(result, {"state": "completed"})
            self.assertEqual(len(self.called), 1)
            for consumer in ("journal", "interest"):
                event = f.life.pending_events(consumer)[0]
                self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))
            return
        self.assertEqual(result, {"state": "commit_unresolved"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["phase"], "commit_unresolved")
        self.assertIn("command", state)
        self.assertIn("proof", state)
        row = f.life._connection.execute("SELECT state FROM life_prepared_commands WHERE command_id=?",
                                          (state["command"]["command_id"],)).fetchone()
        self.assertEqual(row[0], phase)
        self.assertTrue(f.executor.is_pending(f.context.key, f.activity.activity_id))
        self.assertEqual(len(self.called), 1)
        # Simulate explicit host reconciliation of the original durable decision.
        # A permanent contract error cannot keep replaying it automatically.
        scope = await f.executor._scope(f.context.key)
        audience = m.AudienceBound("context:" + digest([scope.owner_key, scope.context_key]), (scope.context_key,))
        await f.ports.execute_platform_action(signed("platform_action", p.PlatformActionRequest(scope,
            state["command"]["payload"]["operation_id"], "", "life.command.commit",
            {"command": state["command"], "proof": state["proof"]}, (), audience)))
        # Recovery adopts the same decision and delivers both original events.
        scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
        with patch.object(f.ports, "run_model_task", side_effect=AssertionError("commit recovery must not regenerate")):
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
        completed = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(completed["phase"], "completed")
        self.assertNotIn("command", completed)
        self.assertNotIn("proof", completed)
        for consumer in ("journal", "interest"):
            event = f.life.pending_events(consumer)[0]
            self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))
        self.assertEqual(len(self.called), 1)

    async def test_decided_prepared_command_survives_source_error_before_apply(self):
        await self.assert_commit_error_preserves_reconciliation("prepared")

    async def test_committed_command_survives_stale_version_before_confirmation(self):
        await self.assert_commit_error_preserves_reconciliation("committed")

    async def test_acknowledged_command_survives_source_error_and_delivers_events(self):
        await self.assert_commit_error_preserves_reconciliation("acknowledged")

    async def test_both_sides_match_host_registration_and_actual_task_deadline(self):
        f = await self.fixture()
        self.assertEqual(MODEL_TASK_TIMEOUT_SECONDS, HOST_TASK_TIMEOUT_SECONDS)
        self.assertTrue(all(reg.definition.timeout_seconds == HOST_TASK_TIMEOUT_SECONDS
                            for reg in f.app.role_reader.registrations))
        self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        task = f.app.tasks.read(f.context, state["task_id"]).unwrap()
        self.assertEqual(task.spec.deadline_at - task.spec.not_before, timedelta(seconds=MODEL_TASK_TIMEOUT_SECONDS))
        self.assertEqual(datetime.fromisoformat(state["task_deadline"]), task.spec.deadline_at)

    async def assert_failed_choice_admission_scheduler_advances(self, code):
        f = await self.fixture(start=False)
        scheduler = self.scheduler(f)
        snapshot = scheduler.capabilities["reading_snapshot"](f.context.key)
        snapshot["sleep_enabled"] = False
        scheduler.capabilities["reading_snapshot"] = lambda _key: {**snapshot, "observed_at": self.clock.now().isoformat()}
        local = self.clock.now().astimezone(ZoneInfo(snapshot["timezone"]))
        slot = choice_slot(snapshot["records"], local.date().isoformat()) + ":0"
        schedule = reading_window(snapshot["calendar"], self.clock.now(), local, None)
        with patch.object(f.app.role_reader.port_tasks, "submit", side_effect=TaskError(code, "refused")):
            self.assertEqual(await scheduler._choose(f.context.key, [f.activity.activity_id], slot, schedule, None), {"state": "failed"})
        contexts, planner = {f.context.key: digest(f.routine.owner)}, SimpleNamespace(_windows={})
        scheduler.advance(contexts, self.clock.now(), planner)
        self.assertNotIn(f.context.key, scheduler.workers)
        self.clock.advance(timedelta(seconds=30))
        scheduler.advance(contexts, self.clock.now(), planner)
        self.assertEqual(scheduler.slots[f.context.key]["retry"], 1)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"retry"}'))
        scheduler.advance(contexts, self.clock.now(), planner)
        self.assertEqual((await scheduler.workers[f.context.key])["state"], "committed")
        self.assertEqual(len(self.called), 1)

    async def test_failed_choice_admission_scheduler_advances_to_next_slot(self):
        await self.assert_failed_choice_admission_scheduler_advances("life_model_task_invalid")

    async def test_unauthorized_choice_admission_scheduler_advances_to_next_slot(self):
        await self.assert_failed_choice_admission_scheduler_advances("life_authority_denied")

    async def test_absent_choice_rebuild_clears_old_backoff_before_submission(self):
        f = await self.fixture(start=False)
        activity = json.loads(encode(f.activity))
        with patch.object(f.ports, "run_model_task", side_effect=ConnectionError("before admission")):
            self.assertEqual(await choose(f.executor, f.context.key, [activity], "clear-backoff", "window"), {"state": "unknown"})
        old = f.executor.store.get(f.context.key, choice_key("clear-backoff"))
        submit = f.ports.run_model_task
        async def rebuilt(request):
            state = f.executor.store.get(f.context.key, choice_key("clear-backoff"))
            self.assertEqual(state["phase"], "submitted")
            self.assertEqual(state["result_check_attempts"], old["result_check_attempts"])
            self.assertNotIn("reason_code", state)
            self.assertNotIn("result_check_after", state)
            return await submit(request)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"retry"}'))
        with patch.object(f.ports, "run_model_task", rebuilt):
            self.assertEqual((await choose(f.executor, f.context.key, [activity], "clear-backoff", "window"))["state"], "committed")
        self.assertEqual(len(self.called), 1)

    async def test_absent_reading_rebuild_clears_old_backoff_before_submission(self):
        f = await self.fixture()
        with patch.object(f.ports, "run_model_task", side_effect=ConnectionError("before admission")):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
        old = f.executor.store.get(f.context.key, f.activity.activity_id)
        submit = f.ports.run_model_task
        async def rebuilt(request):
            state = f.executor.store.get(f.context.key, f.activity.activity_id)
            self.assertEqual(state["phase"], "submitted")
            self.assertEqual(state["result_check_attempts"], old["result_check_attempts"])
            self.assertNotIn("reason_code", state)
            self.assertNotIn("result_check_after", state)
            return await submit(request)
        with patch.object(f.ports, "run_model_task", rebuilt):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)

    async def test_synthetic_rejected_commit_receipt_checks_abort_and_releases_foreground(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        commit = f.ports.execute_platform_action
        async def rejected(request):
            with patch.object(ModelCompletionHandoff, "_authorize", side_effect=TaskError("life_revision_conflict", "rejected")):
                with self.assertRaises(ValueError):
                    await commit(request)
            return m.OperationReceipt(request.operation_id, request.action, m.ReceiptStatus.REJECTED,
                                      request.operation_id, m.ReasonCode.STALE_VERSION)
        with patch.object(f.ports, "execute_platform_action", rejected):
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["phase"], "paused")
        self.assertNotIn("command", state)
        self.assertNotIn("proof", state)
        row = f.life._connection.execute("SELECT state,payload_json,result_json FROM life_prepared_commands WHERE command_id LIKE 'activity-model-complete-%'").fetchone()
        self.assertEqual(tuple(row), ("aborted", "{}", None))
        self.assertEqual(f.executor.store.pending(f.context.key), [])
        self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
        self.assertEqual(len(self.called), 1)

    async def test_real_host_abort_then_exception_pauses_and_releases_foreground(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        # Exercise the actual decorator and recover/abort exception path, without
        # replacing the raised HostPortError with a synthetic rejected receipt.
        with patch.object(ModelCompletionHandoff, "_authorize", side_effect=TaskError("life_revision_conflict", "rejected")):
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["phase"], "paused")
        self.assertNotIn("command", state)
        self.assertNotIn("proof", state)
        row = f.life._connection.execute("SELECT state,payload_json,result_json FROM life_prepared_commands WHERE command_id LIKE 'activity-model-complete-%'").fetchone()
        self.assertEqual(tuple(row), ("aborted", "{}", None))
        self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
        self.assertEqual(len(self.called), 1)

    async def assert_lost_last_commit_response_recovers(self, *, parked=False, abort=False):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        commit, calls = f.ports.execute_platform_action, []
        async def interrupted(request):
            calls.append(request.operation_id)
            if len(calls) < MAX_RESULT_CHECKS:
                return m.OperationReceipt(request.operation_id, request.action, m.ReceiptStatus.UNKNOWN, request.operation_id)
            if abort:
                with patch.object(ModelCompletionHandoff, "_authorize", side_effect=TaskError("life_revision_conflict", "rejected")):
                    with self.assertRaises(ValueError):
                        await commit(request)
            else:
                await commit(request)
            raise asyncio.CancelledError("last response lost before checkpoint")
        with patch.object(f.ports, "execute_platform_action", interrupted):
            for attempt in range(1, MAX_RESULT_CHECKS):
                self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "unknown"})
                self.clock.advance(timedelta(seconds=30 * 2 ** (attempt - 1)))
                scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
            with self.assertRaises(asyncio.CancelledError):
                await scheduler._run_life(f.context.key, f.activity.activity_id)
        state = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["result_check_attempts"], MAX_RESULT_CHECKS)
        self.assertNotIn("commit_decision", state)
        row = f.life._connection.execute("SELECT state FROM life_prepared_commands WHERE command_id=?",
                                         (state["command"]["command_id"],)).fetchone()
        self.assertEqual(row[0], "aborted" if abort else "acknowledged")
        if parked:
            # This checkpoint was already parked by the old cap-before-probe code.
            scheduler.executor._stop_unconfirmed_commit(state)
        scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
        scheduler.capabilities["reading_snapshot"] = lambda _key: {"reading_enabled": False, "busy": True}
        contexts, planner = {f.context.key: digest(f.routine.owner)}, SimpleNamespace(_windows={})
        with patch.object(f.ports, "execute_platform_action", side_effect=AssertionError("durable decision must not recommit")), patch.object(
                f.ports, "run_model_task", side_effect=AssertionError("recovery must not regenerate")):
            scheduler.advance(contexts, self.clock.now(), planner)
            self.assertIn(f.context.key, scheduler.workers)
            self.assertEqual(await scheduler.workers[f.context.key], {"state": "paused" if abort else "completed"})
        self.assertEqual(len(calls), MAX_RESULT_CHECKS)
        self.assertEqual(len(self.called), 1)
        completed = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(completed["phase"], "paused" if abort else "completed")
        self.assertNotIn("command", completed)
        if not abort:
            self.assertNotIn("pause_blocked_reason", completed)
            for consumer in ("journal", "interest"):
                event = f.life.pending_events(consumer)[0]
                self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))

    async def test_last_commit_response_cancelled_recovers_durable_acknowledgement(self):
        await self.assert_lost_last_commit_response_recovers()

    async def test_parked_last_commit_recovers_durable_acknowledgement(self):
        await self.assert_lost_last_commit_response_recovers(parked=True)

    async def test_parked_last_commit_recovers_durable_abort(self):
        await self.assert_lost_last_commit_response_recovers(parked=True, abort=True)

    async def test_parked_last_commit_finishes_durable_commit_before_delivering_events(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        commit, calls = f.ports.execute_platform_action, []
        original = ModelCompletionHandoff.apply
        def cancelled(handoff, *args, **kwargs):
            original(handoff, *args, **kwargs)
            raise asyncio.CancelledError("lost before host confirmation")
        async def uncertain(request):
            calls.append(request.operation_id)
            return m.OperationReceipt(request.operation_id, request.action, m.ReceiptStatus.UNKNOWN, request.operation_id)
        with patch.object(f.ports, "execute_platform_action", uncertain):
            for attempt in range(1, MAX_RESULT_CHECKS):
                self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "unknown"})
                self.clock.advance(timedelta(seconds=30 * 2 ** (attempt - 1)))
        with patch.object(ModelCompletionHandoff, "apply", cancelled):
            with self.assertRaises(asyncio.CancelledError):
                await scheduler._run_life(f.context.key, f.activity.activity_id)
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["result_check_attempts"], MAX_RESULT_CHECKS)
        self.assertEqual(f.executor._local_commit(state)[0], "committed")
        self.assertEqual(f.life.pending_events("interest"), ())
        f.executor._stop_unconfirmed_commit(state)
        self.assertEqual(len(f.executor.store.pending(f.context.key)), 1)
        scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
        self.clock.advance(datetime.fromisoformat(state["result_check_after"]) - self.clock.now())
        with patch.object(f.ports, "execute_platform_action", wraps=commit) as recover, patch.object(
                f.ports, "run_model_task", side_effect=AssertionError("recovery must not regenerate")):
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
            self.assertEqual(recover.call_count, 1)
        self.assertEqual(len(calls), MAX_RESULT_CHECKS - 1)
        self.assertEqual(len(self.called), 1)
        for consumer in ("journal", "interest"):
            event = f.life.pending_events(consumer)[0]
            self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))

    async def test_committed_command_confirmation_unknown_is_bounded_across_restarts(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        # Leave a real local commit waiting for host confirmation.
        with patch.object(ModelCompletionHandoff, "complete", side_effect=TaskError("result_unknown", "confirmation lost")):
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "unknown"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(f.executor._local_commit(state)[0], "committed")
        with patch.object(ModelCompletionHandoff, "recover", side_effect=TaskError("result_unknown", "still uncertain")) as recover, patch.object(
                ModelCompletionHandoff, "abort", side_effect=TaskError("result_unknown", "abort unavailable")):
            for attempt in range(1, MAX_RESULT_CHECKS + 1):
                scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
                self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id),
                                 {"state": "commit_unresolved" if attempt == MAX_RESULT_CHECKS else "unknown"})
                state = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
                self.assertEqual(state["result_check_attempts"], attempt)
                self.clock.advance(timedelta(seconds=30 * 2 ** (attempt - 1)))
            self.assertEqual(len(scheduler.executor.store.pending(f.context.key)), 1)
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            self.assertEqual(recover.call_count, MAX_RESULT_CHECKS + 1)
            self.assertEqual(scheduler.executor.store.get(f.context.key, f.activity.activity_id)["result_check_attempts"], MAX_RESULT_CHECKS)
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            self.assertEqual(recover.call_count, MAX_RESULT_CHECKS + 1)
        self.assertEqual(len(self.called), 1)

    async def assert_contract_commit_error_is_bounded(self, code):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        with patch.object(ModelCompletionHandoff, "recover", side_effect=TaskError(code, "uncertain recovery")) as recover, patch.object(
                ModelCompletionHandoff, "abort", side_effect=TaskError(code, "abort unavailable")):
            for attempt in range(1, MAX_RESULT_CHECKS + 1):
                self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id),
                                 {"state": "commit_unresolved" if attempt == MAX_RESULT_CHECKS else "unknown"})
                state = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
                self.assertEqual(state["result_check_attempts"], attempt)
                if attempt < MAX_RESULT_CHECKS:
                    delay = 30 * 2 ** (attempt - 1)
                    self.assertEqual(datetime.fromisoformat(state["result_check_after"]), self.clock.now() + timedelta(seconds=delay))
                    scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
                    self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "unknown"})
                    self.assertEqual(recover.call_count, attempt)
                    self.clock.advance(timedelta(seconds=delay))
            self.clock.advance(timedelta(hours=1))
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            self.assertEqual(recover.call_count, MAX_RESULT_CHECKS)
        self.assertEqual(len(self.called), 1)
        self.assertTrue(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])

    async def test_result_unknown_host_error_consumes_persisted_commit_budget(self):
        await self.assert_contract_commit_error_is_bounded("result_unknown")

    async def test_provider_unavailable_host_error_backs_off_without_consuming_commit_budget(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        with patch.object(ModelCompletionHandoff, "recover", side_effect=TaskError("life_provider_unavailable", "provider offline")) as recover, patch.object(
                ModelCompletionHandoff, "abort", side_effect=TaskError("life_provider_unavailable", "abort unavailable")):
            for attempt in range(1, MAX_RESULT_CHECKS + 2):
                self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "unknown"})
                state = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
                self.assertEqual(state["result_check_attempts"], 0)
                self.assertEqual(state["reason_code"], "provider_unavailable")
                delay = min(30 * 2 ** (attempt - 1), 900)
                self.assertEqual(state["provider_commit_attempts"], attempt)
                self.assertEqual(datetime.fromisoformat(state["result_check_after"]), self.clock.now() + timedelta(seconds=delay))
                scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
                self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "unknown"})
                self.assertEqual(recover.call_count, attempt)
                self.clock.advance(timedelta(seconds=delay))
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)

    async def assert_permanent_host_commit_error(self, code, reason):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        with patch.object(ModelCompletionHandoff, "recover", side_effect=TaskError(code, "permanent refusal")) as recover, patch.object(
                ModelCompletionHandoff, "abort", side_effect=TaskError(code, "abort unavailable")), self.assertLogs("good_life.reading_execution", level="WARNING") as logs:
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            state = f.executor.store.get(f.context.key, f.activity.activity_id)
            self.assertEqual(state["reason_code"], reason)
            self.assertIn(state["command"]["command_id"], "\n".join(logs.output))
            self.assertEqual(f.executor.store.pending(f.context.key), [])
            self.clock.advance(timedelta(hours=1))
            recovered = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
            self.assertEqual(await recovered.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            self.assertEqual(recover.call_count, 1)

    async def test_invalid_config_commit_error_parks_with_command_diagnostics(self):
        await self.assert_permanent_host_commit_error("invalid_config", "invalid_config")

    async def test_unauthorized_commit_error_parks_with_command_diagnostics(self):
        await self.assert_permanent_host_commit_error("life_authority_denied", "unauthorized")

    async def test_invalid_action_commit_error_parks_with_command_diagnostics(self):
        await self.assert_permanent_host_commit_error("life_action_invalid", "invalid_config")

    async def test_digest_mismatch_commit_error_parks_with_command_diagnostics(self):
        await self.assert_permanent_host_commit_error("digest_mismatch", "invalid_config")

    async def test_plain_commit_value_error_restores_reservation_and_surfaces(self):
        f = await self.fixture()
        with patch.object(f.ports, "execute_platform_action", side_effect=ValueError("implementation bug")):
            with self.assertRaisesRegex(ValueError, "implementation bug"):
                await f.executor.execute(f.context.key, f.activity.activity_id)
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual((state["phase"], state["result_check_attempts"]), ("committing", 0))
        self.assertNotIn("result_check_after", state)
        recovered = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
        self.assertEqual(await recovered.execute(f.context.key, f.activity.activity_id), {"state": "completed"})

    async def test_model_polling_leaves_a_full_restart_safe_commit_budget(self):
        f = await self.fixture()
        submit = f.ports.run_model_task
        unknown = []
        async def submitted(request):
            receipt = await submit(request)
            unknown.append(replace(receipt, receipt=replace(receipt.receipt, status=m.ReceiptStatus.UNKNOWN), result_ref=None))
            return unknown[0]
        with patch.object(f.ports, "run_model_task", submitted):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
        self.clock.advance(timedelta(seconds=30))
        with patch.object(f.ports, "inspect_task", return_value=unknown[0]):
            for attempt in range(2, MAX_RESULT_CHECKS):
                self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
                self.clock.advance(timedelta(seconds=30 * 2 ** (attempt - 1)))
        self.assertEqual(f.executor.store.get(f.context.key, f.activity.activity_id)["result_check_attempts"], MAX_RESULT_CHECKS - 1)
        commit, calls = f.ports.execute_platform_action, []
        async def transient(request):
            calls.append(request.operation_id)
            if len(calls) < MAX_RESULT_CHECKS:
                return m.OperationReceipt(request.operation_id, request.action, m.ReceiptStatus.UNKNOWN, request.operation_id)
            return await commit(request)
        with patch.object(f.ports, "execute_platform_action", transient):
            for attempt in range(1, MAX_RESULT_CHECKS + 1):
                f.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
                self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id),
                                 {"state": "completed" if attempt == MAX_RESULT_CHECKS else "unknown"})
                if attempt < MAX_RESULT_CHECKS:
                    self.assertEqual(f.executor.store.get(f.context.key, f.activity.activity_id)["result_check_attempts"], attempt)
                    self.clock.advance(timedelta(seconds=30 * 2 ** (attempt - 1)))
        self.assertEqual(len(calls), MAX_RESULT_CHECKS)
        self.assertEqual(len(self.called), 1)

    async def assert_unconfirmed_commit_is_bounded(self, status):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        snapshot = scheduler.capabilities["reading_snapshot"]
        scheduler.capabilities["reading_snapshot"] = lambda key: {**snapshot(key), "reading_enabled": False}
        contexts, planner = {f.context.key: digest(f.routine.owner)}, SimpleNamespace(_windows={})
        async def unconfirmed(request):
            return m.OperationReceipt(request.operation_id, request.action, status, request.operation_id)
        with patch.object(f.ports, "execute_platform_action", side_effect=unconfirmed) as commit:
            for attempt in range(1, MAX_RESULT_CHECKS + 1):
                scheduler.advance(contexts, self.clock.now(), planner)
                self.assertEqual(await scheduler.workers[f.context.key],
                                 {"state": "commit_unresolved" if attempt == MAX_RESULT_CHECKS else "unknown"})
                scheduler.collect(self.clock.now())
                state = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
                self.assertEqual(state["result_check_attempts"], attempt)
                if attempt < MAX_RESULT_CHECKS:
                    delay = 30 * 2 ** (attempt - 1)
                    self.assertEqual(datetime.fromisoformat(state["result_check_after"]), self.clock.now() + timedelta(seconds=delay))
                    # Direct recovery and scheduler ticks share the persisted
                    # backoff, including after A3 is disabled on restart.
                    scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
                    self.clock.advance(timedelta(seconds=delay - 1))
                    self.assertEqual(await scheduler.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
                    scheduler.advance(contexts, self.clock.now(), planner)
                    self.assertNotIn(f.context.key, scheduler.workers)
                    self.assertEqual(commit.call_count, attempt)
                    self.clock.advance(timedelta(seconds=1))
            self.assertEqual(commit.call_count, MAX_RESULT_CHECKS)
            for _ in range(3):
                self.clock.advance(timedelta(minutes=30))
                scheduler.advance(contexts, self.clock.now(), planner)
                self.assertNotIn(f.context.key, scheduler.workers)
            self.assertEqual(commit.call_count, MAX_RESULT_CHECKS)
        state = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual((state["phase"], state["reason_code"]), ("commit_unresolved", "commit_retry_exhausted"))
        self.assertEqual(state["pause_blocked_reason"], "host_abort_unavailable")
        self.assertIn("result_check_after", state)
        # Unconfirmed decisions retain their original recovery evidence and
        # foreground fence. Contract v1 cannot abort this reservation safely.
        self.assertIn("command", state)
        self.assertIn("proof", state)
        self.assertEqual(scheduler.executor.store.pending(f.context.key), [])
        self.assertTrue(scheduler.executor.is_pending(f.context.key, f.activity.activity_id))
        self.assertTrue(snapshot(f.context.key)["busy"])
        self.assertEqual(f.life.pending_events("interest"), ())
        self.assertEqual(len(self.called), 1)
        # The generic restart recovery does not recognize A3 model-completion
        # command IDs. It must leave the original row and decision evidence alone.
        from good_life.sleep_scheduler import DueSleepScheduler
        timer = DueSleepScheduler(f.life, scheduler.capabilities, reading_executor=scheduler.executor)
        self.addAsyncCleanup(timer.close)
        pause = AsyncMock(side_effect=AssertionError("unresolved command still fences pause"))
        with patch.object(f.ports, "execute_platform_action", side_effect=AssertionError("terminal commit must not replay")), patch.dict(
                timer.capabilities, {"pause_interrupted_work": pause}):
            self.assertEqual(await timer.reading_scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            self.assertEqual((await timer.tick(now=self.clock.now()))["recovered"], 0)
        pause.assert_not_awaited()
        row = f.life._connection.execute("SELECT state,decision_id FROM life_prepared_commands WHERE command_id=?",
                                         (state["command"]["command_id"],)).fetchone()
        self.assertEqual(tuple(row), ("prepared", None))
        parked = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
        for field in ("command", "proof", "phase", "reason_code", "result_check_attempts"):
            self.assertEqual(parked[field], state[field])
        self.assertTrue(snapshot(f.context.key)["busy"])

    async def test_rejected_unconfirmed_commit_exhausts_budget_without_fabricating_abort(self):
        await self.assert_unconfirmed_commit_is_bounded(m.ReceiptStatus.REJECTED)

    async def test_successful_receipt_without_verified_commit_exhausts_budget_without_fabricating_abort(self):
        await self.assert_unconfirmed_commit_is_bounded(m.ReceiptStatus.SUCCEEDED)

    async def test_unconfirmed_commit_recovers_original_command_without_regeneration(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        commit = f.ports.execute_platform_action
        requests = []
        async def transient(request):
            requests.append(request)
            if len(requests) == 1:
                return m.OperationReceipt(request.operation_id, request.action, m.ReceiptStatus.UNKNOWN, request.operation_id)
            return await commit(request)
        with patch.object(f.ports, "execute_platform_action", transient):
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "unknown"})
            self.clock.advance(timedelta(seconds=30))
            scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
            self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(requests[0].payload, requests[1].payload)
        self.assertEqual(requests[0].operation_id, requests[1].operation_id)
        self.assertEqual(scheduler.executor.store.get(f.context.key, f.activity.activity_id)["result_check_attempts"], 0)
        self.assertEqual(len(self.called), 1)

    async def test_transient_delivery_exhaustion_is_terminal_and_does_not_recommit(self):
        f = await self.fixture()
        scheduler = self.scheduler(f)
        snapshot = scheduler.capabilities["reading_snapshot"]
        scheduler.capabilities["reading_snapshot"] = lambda key: {**snapshot(key), "reading_enabled": False}
        contexts, planner = {f.context.key: digest(f.routine.owner)}, SimpleNamespace(_windows={})
        accept = f.ports.accept_life_event
        calls = []
        async def unavailable(request):
            consumer = request.event.payload["consumer"]
            calls.append(consumer)
            if consumer == "interest":
                raise TimeoutError("event delivery temporarily unavailable")
            return await accept(request)
        with patch.object(f.ports, "accept_life_event", unavailable), patch.object(
                f.ports, "execute_platform_action", wraps=f.ports.execute_platform_action) as commit:
            for attempt in range(1, MAX_RESULT_CHECKS + 1):
                scheduler.advance(contexts, self.clock.now(), planner)
                self.assertEqual(await scheduler.workers[f.context.key],
                                 {"state": "completed" if attempt == MAX_RESULT_CHECKS else "unknown"})
                scheduler.collect(self.clock.now())
                state = f.executor.store.get(f.context.key, f.activity.activity_id)
                self.assertEqual(state["result_check_attempts"], attempt)
                if attempt < MAX_RESULT_CHECKS:
                    delay = 30 * 2 ** (attempt - 1)
                    self.assertEqual(datetime.fromisoformat(state["result_check_after"]), self.clock.now() + timedelta(seconds=delay))
                    # Restart keeps the original committed decision and retry
                    # deadline; an early tick cannot write another event.
                    scheduler.executor = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
                    before = list(calls)
                    self.clock.advance(timedelta(seconds=delay - 1))
                    scheduler.advance(contexts, self.clock.now(), planner)
                    self.assertNotIn(f.context.key, scheduler.workers)
                    self.assertEqual(calls, before)
                    self.clock.advance(timedelta(seconds=1))
            self.assertEqual(commit.call_count, 1)
        state = scheduler.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["reason_code"], "events_withheld_retry_exhausted")
        self.assertEqual(state["events_delivered_consumers"], ["journal"])
        self.assertEqual(state["events_withheld_consumers"], ["interest"])
        self.assertNotIn("command", state)
        self.assertNotIn("proof", state)
        self.assertEqual(scheduler.executor.store.pending(f.context.key), [])
        self.assertEqual(calls, ["journal"] + ["interest"] * MAX_RESULT_CHECKS)
        self.assertEqual(len(self.called), 1)
        # A later manual dispatch gets the context instead of another commit.
        intent = {"owner_key": digest(f.routine.owner), "context_key": f.context.key,
                  "activity_id": "later-book", "operation_id": "later-intent"}
        scheduler.capabilities["reading_manual_pending"] = lambda: [intent]
        async def manual(_context, _intent):
            return {"state": "rejected"}
        with patch.object(scheduler, "_manual", manual), patch.object(
                f.ports, "execute_platform_action", side_effect=AssertionError("terminal run must not recommit")):
            scheduler.advance(contexts, self.clock.now(), planner)
            self.assertEqual(scheduler.activity_ids[f.context.key], "later-book")
            self.assertEqual(await scheduler.workers[f.context.key], {"state": "rejected"})

    async def test_delivery_retry_recovers_without_replaying_received_consumer(self):
        f = await self.fixture()
        accept = f.ports.accept_life_event
        calls = []
        async def transient(request):
            consumer = request.event.payload["consumer"]
            calls.append(consumer)
            if calls == ["journal", "interest"]:
                raise ConnectionError("temporary interest delivery failure")
            return await accept(request)
        with patch.object(f.ports, "accept_life_event", transient), patch.object(
                f.ports, "execute_platform_action", wraps=f.ports.execute_platform_action) as commit:
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
            state = f.executor.store.get(f.context.key, f.activity.activity_id)
            self.assertEqual(state["events_delivered_consumers"], ["journal"])
            self.clock.advance(timedelta(seconds=30))
            recovered = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
            self.assertEqual(await recovered.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
            self.assertEqual(commit.call_count, 1)
        state = recovered.store.get(f.context.key, f.activity.activity_id)
        self.assertNotIn("reason_code", state)
        self.assertEqual(state["events_delivered_consumers"], ["journal", "interest"])
        self.assertEqual(calls, ["journal", "interest", "interest"])
        self.assertEqual(len(self.called), 1)

    async def test_stale_delivery_withholds_only_the_unreceived_consumer(self):
        f = await self.fixture()
        accept = f.ports.accept_life_event
        async def stale(request):
            if request.event.payload["consumer"] == "interest":
                raise TaskError("stale_version", "source changed after journal acceptance")
            return await accept(request)
        with patch.object(f.ports, "accept_life_event", stale):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["reason_code"], "events_withheld_source_unavailable")
        self.assertEqual(state["events_delivered_consumers"], ["journal"])
        self.assertEqual(state["events_withheld_consumers"], ["interest"])
        self.assertEqual(f.executor.store.pending(f.context.key), [])
        for consumer in ("journal", "interest"):
            event = f.life.pending_events(consumer)[0]
            self.assertEqual(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer), consumer == "journal")

    async def test_source_rejected_delivery_receipt_finishes_without_marking_received(self):
        f = await self.fixture()
        async def rejected(request):
            return p.LifeEventReceipt(request.event.event_id, request.event.payload["consumer"], request.scope.owner_key,
                m.ReceiptStatus.REJECTED, None, None, m.ReasonCode.SOURCE_UNAVAILABLE)
        with patch.object(f.ports, "accept_life_event", rejected):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(state["reason_code"], "events_withheld_source_unavailable")
        self.assertEqual(state["events_withheld_consumers"], ["journal", "interest"])
        self.assertEqual(state.get("events_delivered_consumers", []), [])
        self.assertEqual(f.executor.store.pending(f.context.key), [])
