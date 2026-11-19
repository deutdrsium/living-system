"""A3 v2 recovery through real negotiated adapters and both durable journals.

Faults are injected at host decision/provider/store boundaries, never by
mocking the coordination adapter. The model fixture is the usual local double.
"""
import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_reading_extraction import ReadingExtractionFixture
from companion_v2.execution.models import TaskError, digest
from companion_v2.integration import life_commands, life_event_inbox
from companion_v2.integration.model_completion_handoff import ModelCompletionHandoff
from companion_v2.integration.reading_handoff import ReadingStartHandoff
from good_life.contracts import v2 as v
from good_life.prepared_commands import _hash
from good_life.reading_execution import (MAX_RESULT_CHECKS,
    coordination_response_types, retry_delay)
from good_life.reading_scheduler import ReadingScheduler
from good_life.runtime import LifeRuntime
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage


class ReadingAbortOwnTests(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    def state(self, f):
        return f.executor.store.get(f.context.key, f.activity.activity_id)

    def negotiate(self, f, *, pending=False):
        constructors = coordination_response_types()
        if pending:
            # Exercise a future host's pending wire settlement using the response
            # codec seam. The actual decision/settlement still uses real stores.
            constructors["CommandSettlement"] = lambda value: (
                v.CommandSettlement.PENDING if value == "unknown" else v.CommandSettlement(value))
        return f.api.life_async_capabilities(plugin=f.plugin, registration_id=f.token,
            contract_version=2, response_types=constructors,
            requested_capabilities=(v.ABORT_OWN_CAPABILITY, v.COMMAND_RECONCILE_CAPABILITY))["ports"]

    async def exhausted(self, *, enabled=True):
        f = await self.fixture()
        f.executor.clock = self.clock.now
        with patch.object(ModelCompletionHandoff, "recover", side_effect=TaskError("result_unknown", "offline")), patch.object(
                ModelCompletionHandoff, "abort", side_effect=TaskError("result_unknown", "offline")):
            for attempt in range(1, MAX_RESULT_CHECKS + 1):
                result = await f.executor.execute(f.context.key, f.activity.activity_id)
                self.assertEqual(result["state"], "commit_unresolved" if attempt == MAX_RESULT_CHECKS else "unknown")
                self.clock.advance(timedelta(seconds=retry_delay(attempt)))
        f.original = self.state(f)
        if enabled:
            f.executor.coordination_ports = self.negotiate(f)
        return f

    def scheduler(self, f):
        result = ReadingScheduler(f.life, f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token),
                                  executor=f.executor)
        self.addAsyncCleanup(result.close)
        return result

    async def restart(self, f, *, enabled=True):
        # Reopen the actual life DB and re-register both protocol leases. No
        # in-memory state or old scope is carried into the recovered executor.
        f.api.unregister_life_provider(f.token)
        f.life.close()
        f.life = LifeStorage.open(f.directory)
        self.addCleanup(f.life.close)
        f.provider = LifeRuntime(f.life, f.plugin, reading_abort_own_enabled=enabled)
        with patch.object(DueSleepScheduler, "start"):
            await f.provider.bind(f.api)
        self.addAsyncCleanup(f.provider.unbind)
        f.token = f.provider.token
        f.executor = f.provider.scheduler.reading_scheduler.executor
        if f.executor is not None:
            f.executor.clock = self.clock.now
        return f.provider.scheduler.reading_scheduler

    def assert_barrier(self, f):
        state = self.state(f)
        self.assertEqual(state["phase"], "commit_unresolved")
        self.assertEqual(state["command"], f.original["command"])
        self.assertEqual(state["proof"], f.original["proof"])
        self.assertTrue(f.executor.is_pending(f.context.key, f.activity.activity_id))
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "active")
        self.assertTrue(self.scheduler(f).capabilities["reading_snapshot"](f.context.key)["busy"])
        self.assertEqual(f.life.pending_events("interest"), ())
        self.assertEqual(len(self.called), 1)

    def due(self, f):
        after = datetime.fromisoformat(self.state(f)["result_check_after"])
        if after > self.clock.now():
            self.clock.advance(after - self.clock.now())

    async def test_exhausted_original_aborts_then_separate_pause_releases_foreground(self):
        f = await self.exhausted()
        scheduler = await self.restart(f)
        self.assertFalse(f.executor.enabled)  # Recovery works after A3 creation is disabled.
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})
        state = self.state(f)
        self.assertEqual(state["phase"], "paused")
        self.assertEqual(state["command_coordination"]["request_hash"], _hash(f.original["command"]))
        self.assertEqual(state["command_coordination"]["confirmed_decision"], "abort")
        self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
        rows = f.life._connection.execute("SELECT command_id,state FROM life_prepared_commands WHERE command_id LIKE 'activity-model-complete-%'").fetchall()
        self.assertEqual([tuple(row) for row in rows], [(f.original["command"]["command_id"], "aborted")])
        self.assertEqual(len(self.called), 1)
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})

    async def test_commit_wins_abort_race_and_original_events_are_delivered_once(self):
        f = await self.exhausted()
        decide = life_commands.decide
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.original["proof"])
        def race(*args, **kwargs):
            if kwargs["outcome"] == "abort":
                handoff.decide(f.original["command"], observed_at=f.app.now())
            return decide(*args, **kwargs)
        with patch.object(life_commands, "decide", side_effect=race):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        for consumer in ("journal", "interest"):
            event, = f.life.pending_events(consumer)
            self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))
        receipts = tuple(f.app.runtime.read_all("SELECT key,value FROM runtime_metadata WHERE key LIKE 'life.event.inbox.v1:%' ORDER BY key", ()))
        self.assertEqual(len(receipts), 2)
        await self.restart(f)
        self.assertIsNone(f.executor)  # Completed history needs no recovery ports.
        self.assertEqual(receipts, tuple(f.app.runtime.read_all("SELECT key,value FROM runtime_metadata WHERE key LIKE 'life.event.inbox.v1:%' ORDER BY key", ())))
        self.assertEqual(len(self.called), 1)

    async def assert_unknown(self, *, pending=False):
        f = await self.exhausted()
        f.executor.coordination_ports = self.negotiate(f, pending=pending)
        for attempt in range(1, 8):
            with patch.object(life_commands, "decide", side_effect=TimeoutError("decision unavailable")):
                self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            state = self.state(f)
            self.assertEqual(state["command_coordination"]["attempts"], attempt)
            self.assertEqual(state["command_coordination"]["settlement"], "pending" if pending else "unknown")
            self.assertEqual(datetime.fromisoformat(state["result_check_after"]),
                             self.clock.now() + timedelta(seconds=retry_delay(attempt)))
            self.assert_barrier(f)
            scheduler = await self.restart(f)
            f.executor.coordination_ports = self.negotiate(f, pending=pending)
            scheduler.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), SimpleNamespace(_windows={}))
            self.assertNotIn(f.context.key, scheduler.workers)
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
            self.assertEqual(self.state(f)["command_coordination"]["attempts"], attempt)
            self.due(f)
        self.assertEqual(await self.scheduler(f)._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})

    async def test_slow_unknown_settlement_backoff_starts_after_host_returns(self):
        f = await self.exhausted()
        def unavailable(*args, **kwargs):
            self.clock.advance(timedelta(minutes=5))
            raise TimeoutError("slow decision unavailable")
        with patch.object(life_commands, "decide", side_effect=unavailable):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.assertEqual(datetime.fromisoformat(self.state(f)["result_check_after"]), self.clock.now() + timedelta(seconds=30))
        self.assert_barrier(f)

    async def test_unknown_keeps_original_barrier_with_capped_restart_safe_backoff(self):
        await self.assert_unknown()

    async def test_pending_keeps_original_barrier_with_capped_restart_safe_backoff(self):
        await self.assert_unknown(pending=True)

    async def assert_abort_interruption(self, stage):
        f = await self.exhausted()
        provider = f.app.role_life.transition_provider()
        decide, resolve, save = life_commands.decide, provider.resolve, f.executor.store.save
        def decision(*args, **kwargs):
            if stage == "before_abort":
                raise asyncio.CancelledError("before host decides")
            result = decide(*args, **kwargs)
            if stage == "after_decision":
                raise asyncio.CancelledError("after host decides")
            return result
        def settlement(*args, **kwargs):
            result = resolve(*args, **kwargs)
            if stage == "after_settlement":
                raise asyncio.CancelledError("after life resolves")
            return result
        def checkpoint(context, activity, value, **kwargs):
            coordination = value.get("command_coordination", {})
            if stage == "before_life_save" and "settlement" in coordination:
                raise asyncio.CancelledError("reply arrived before caller save")
            result = save(context, activity, value, **kwargs)
            if stage == "after_confirmed_save" and coordination.get("confirmed_decision") == "abort":
                raise asyncio.CancelledError("confirmed before pause checkpoint")
            if stage == "before_pause" and value.get("phase") == "pausing":
                raise asyncio.CancelledError("before independent pause")
            return result
        with patch.object(life_commands, "decide", side_effect=decision), patch.object(
                f.app.role_life, "transition_provider", return_value=provider), patch.object(
                provider, "resolve", side_effect=settlement), patch.object(f.executor.store, "save", side_effect=checkpoint):
            with self.assertRaises(asyncio.CancelledError):
                await f.executor.execute(f.context.key, f.activity.activity_id)
        scheduler = await self.restart(f)
        state = self.state(f)
        if state.get("command"):
            self.assertEqual(state["command"], f.original["command"])
            self.assertEqual(state["proof"], f.original["proof"])
        self.due(f) if state.get("result_check_after") else None
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})
        self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
        self.assertEqual(self.state(f)["phase"], "paused")
        self.assertEqual(len(self.called), 1)

    async def test_restart_before_host_abort_decision(self):
        await self.assert_abort_interruption("before_abort")

    async def test_restart_after_reservation_before_abort_call(self):
        f = await self.exhausted()
        running = f.app.require_running
        def interrupted():
            running()
            if self.state(f).get("command_coordination"):
                raise asyncio.CancelledError("before abort port")
        with patch.object(f.app, "require_running", side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await f.executor.execute(f.context.key, f.activity.activity_id)
        self.assertEqual(self.state(f)["command_coordination"]["attempts"], 1)
        self.assertIsNone(life_commands.lookup(f.app.runtime, context_key=f.context.key,
            command_id=f.original["command"]["command_id"], request_hash=_hash(f.original["command"])))
        scheduler = await self.restart(f)
        self.due(f)
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})

    async def test_restart_after_independent_pause_before_life_checkpoint(self):
        f = await self.exhausted()
        save = f.executor.store.save
        def interrupted(context, activity, value, **kwargs):
            if value.get("phase") == "paused":
                raise asyncio.CancelledError("host paused before caller save")
            return save(context, activity, value, **kwargs)
        with patch.object(f.executor.store, "save", side_effect=interrupted):
            with self.assertRaises(asyncio.CancelledError):
                await self.scheduler(f)._run_life(f.context.key, f.activity.activity_id)
        self.assertEqual(self.state(f)["phase"], "pausing")
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "paused")
        scheduler = await self.restart(f)
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})
        self.assertEqual(self.state(f)["phase"], "paused")

    async def test_restart_after_host_abort_decision(self):
        await self.assert_abort_interruption("after_decision")

    async def test_restart_after_host_settles_before_reply(self):
        await self.assert_abort_interruption("after_settlement")

    async def test_restart_after_abort_reply_before_life_save(self):
        await self.assert_abort_interruption("before_life_save")

    async def test_restart_after_confirmed_checkpoint_before_pause_checkpoint(self):
        await self.assert_abort_interruption("after_confirmed_save")

    async def test_restart_before_independent_pause(self):
        await self.assert_abort_interruption("before_pause")

    async def test_unknown_abort_with_local_abort_cannot_pause_until_confirmed(self):
        f = await self.exhausted()
        provider = f.app.role_life.transition_provider()
        resolve = provider.resolve
        def lost(*args, **kwargs):
            resolve(*args, **kwargs)
            raise TimeoutError("lost settlement response")
        with patch.object(f.app.role_life, "transition_provider", return_value=provider), patch.object(provider, "resolve", side_effect=lost):
            self.assertEqual(await self.scheduler(f)._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.assertEqual(f.executor._local_commit(self.state(f))[0], "aborted")
        self.assert_barrier(f)
        await self.restart(f, enabled=False)
        self.due(f)
        self.assertEqual(await self.scheduler(f)._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.assert_barrier(f)
        await self.restart(f)
        self.due(f)
        self.assertEqual(await self.scheduler(f)._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})

    async def test_known_commit_uses_reconcile_after_park_without_v1_replay(self):
        f = await self.exhausted()
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.original["proof"])
        decision = handoff.decide(f.original["command"], observed_at=f.app.now())
        handoff.apply(f.original["command"], decision)
        with patch.object(ModelCompletionHandoff, "abort", side_effect=AssertionError("committed command must not abort")):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        state = self.state(f)
        self.assertEqual(state["command_coordination"]["decision"], "commit")
        self.assertEqual(state["commit_decision"], decision)
        self.assertEqual(len(self.called), 1)

    async def test_flag_off_does_not_request_v2_and_keeps_original_park(self):
        f = await self.exhausted(enabled=False)
        negotiate = f.api.life_async_capabilities
        calls = []
        def capture(**kwargs):
            calls.append(kwargs)
            return negotiate(**kwargs)
        with patch.object(f.api, "life_async_capabilities", side_effect=capture):
            scheduler = await self.restart(f, enabled=False)
        self.assertEqual([item["contract_version"] for item in calls], [1])
        self.assertTrue(all("requested_capabilities" not in item for item in calls))
        self.assertIsNone(f.executor.coordination_ports)
        self.assertEqual(f.executor.store.pending(f.context.key), [])
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.assert_barrier(f)

    async def test_v1_only_host_keeps_park_even_with_flag_enabled(self):
        f = await self.exhausted(enabled=False)
        negotiate = f.api.life_async_capabilities
        calls = []
        def old_host(**kwargs):
            calls.append(kwargs)
            if kwargs["contract_version"] != 1:
                raise ValueError("unsupported contract")
            return negotiate(**kwargs)
        with patch.object(f.api, "life_async_capabilities", side_effect=old_host):
            scheduler = await self.restart(f)
        self.assertEqual([item["contract_version"] for item in calls], [1, 2])
        self.assertEqual(set(calls[1]["requested_capabilities"]), {v.ABORT_OWN_CAPABILITY, v.COMMAND_RECONCILE_CAPABILITY})
        self.assertIsNone(f.executor.coordination_ports)
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.assert_barrier(f)

    async def test_parked_invalid_commit_proof_backs_off_and_logs_once(self):
        f = await self.exhausted(enabled=False)
        state = self.state(f)
        state.pop("commit_stop_logged")  # A parked checkpoint from #28.
        f.executor.store.save(f.context.key, f.activity.activity_id, state)
        # A corrupt durable decision makes pending() wake the parked row while
        # verify_commit still rejects it. It must not launch on every tick.
        with f.life.transaction() as db:
            db.execute("UPDATE life_prepared_commands SET state='committed',decision_id='corrupt',decision_hash='bad' WHERE command_id=?",
                       (f.original["command"]["command_id"],))
        scheduler = self.scheduler(f)
        for attempt in range(2):
            self.due(f)
            scheduler.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), SimpleNamespace(_windows={}))
            capture = (self.assertLogs("good_life.reading_execution", level="WARNING") if attempt == 0
                       else self.assertNoLogs("good_life.reading_execution", level="WARNING"))
            with capture:
                self.assertEqual(await scheduler.workers[f.context.key], {"state": "commit_unresolved"})
            scheduler.collect(self.clock.now())
            after = self.state(f)["result_check_after"]
            self.assertGreater(datetime.fromisoformat(after), self.clock.now())
            await self.restart(f, enabled=False)
            scheduler = self.scheduler(f)
            scheduler.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), SimpleNamespace(_windows={}))
            self.assertNotIn(f.context.key, scheduler.workers)
        self.assertTrue(self.state(f)["commit_stop_logged"])

    async def test_parked_fresh_unacknowledged_commit_recovers_without_resetting_budget(self):
        f = await self.exhausted(enabled=False)
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.original["proof"])
        decision = handoff.decide(f.original["command"], observed_at=f.app.now())
        handoff.apply(f.original["command"], decision)
        with patch.object(ModelCompletionHandoff, "complete", side_effect=TimeoutError("confirmation temporarily unavailable")):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        state = self.state(f)
        self.assertEqual(state["result_check_attempts"], MAX_RESULT_CHECKS)
        self.assertEqual(state["commit_decision"], decision)
        self.assertFalse(state["commit_acknowledged"])
        self.assertEqual(len(f.executor.store.pending(f.context.key)), 1)
        scheduler = await self.restart(f, enabled=False)
        self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.due(f)
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)

    async def test_flag_off_48_ticks_recovers_commit_and_delivers_both_events_once(self):
        f = await self.exhausted(enabled=False)
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.original["proof"])
        decision = handoff.decide(f.original["command"], observed_at=f.app.now())
        handoff.apply(f.original["command"], decision)
        # Discover the verified commit before its parked deadline. Persisting
        # commit_acknowledged=false must leave it eligible on the next tick.
        state = self.state(f)
        state["result_check_after"] = (self.clock.now() + timedelta(seconds=30)).isoformat()
        f.executor.store.save(f.context.key, f.activity.activity_id, state)
        self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.assertFalse(self.state(f)["commit_acknowledged"])
        self.assertEqual(len(f.executor.store.pending(f.context.key)), 1)
        scheduler = await self.restart(f, enabled=False)
        complete, confirmations, launches = ModelCompletionHandoff.complete, [], []
        def delayed_confirmation(handoff, *args, **kwargs):
            confirmations.append(True)
            if len(confirmations) <= 2:
                raise TimeoutError("confirmation temporarily unavailable")
            return complete(handoff, *args, **kwargs)
        def disable_automatic(reading):
            snapshot = reading.capabilities["reading_snapshot"]
            reading.capabilities["reading_snapshot"] = lambda key: {**snapshot(key), "reading_enabled": False}
        disable_automatic(scheduler)
        with patch.object(ModelCompletionHandoff, "complete", delayed_confirmation):
            for tick in range(48):
                self.clock.advance(timedelta(minutes=30))
                scheduler.advance({f.context.key: digest(f.routine.owner)}, self.clock.now(), SimpleNamespace(_windows={}))
                if f.context.key in scheduler.workers:
                    result = await scheduler.workers[f.context.key]
                    launches.append(result["state"])
                    scheduler.collect(self.clock.now())
                    if result["state"] == "commit_unresolved":
                        self.assertEqual(self.state(f)["result_check_attempts"], MAX_RESULT_CHECKS)
                        self.assertEqual(len(f.executor.store.pending(f.context.key)), 1)
                        scheduler = await self.restart(f, enabled=False)
                        disable_automatic(scheduler)
        self.assertEqual(launches, ["commit_unresolved", "commit_unresolved", "completed"])
        self.assertEqual(len(confirmations), 3)
        self.assertEqual(self.state(f)["phase"], "completed")
        self.assertFalse(scheduler.capabilities["reading_snapshot"](f.context.key)["busy"])
        self.assertEqual(len(self.called), 1)
        for consumer in ("journal", "interest"):
            event, = f.life.pending_events(consumer)
            self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))
        self.assertEqual(len(tuple(f.app.runtime.read_all(
            "SELECT key FROM runtime_metadata WHERE key LIKE 'life.event.inbox.v1:%'", ()))), 2)

    async def test_parked_v1_recovery_backoff_does_not_depend_on_decision_budget(self):
        f = await self.exhausted(enabled=False)
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.original["proof"])
        decision = handoff.decide(f.original["command"], observed_at=f.app.now())
        handoff.apply(f.original["command"], decision)
        # Permanent refusals can park before exhaustion. Their existing budget
        # also stays fixed while confirmed-decision recovery uses parked backoff.
        state = self.state(f)
        state.update(result_check_attempts=2, reason_code="invalid_config", park_check_attempts=0,
                     result_check_after=self.clock.now().isoformat())
        f.executor.store.save(f.context.key, f.activity.activity_id, state)
        with patch.object(ModelCompletionHandoff, "complete", side_effect=TimeoutError("confirmation unavailable")):
            for delay in (30, 60, 120):
                self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
                self.assertEqual(self.state(f)["result_check_attempts"], 2)
                self.assertEqual(datetime.fromisoformat(self.state(f)["result_check_after"]), self.clock.now() + timedelta(seconds=delay))
                self.clock.advance(timedelta(seconds=delay))
        self.assertEqual(await self.scheduler(f)._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)

    async def assert_provider_outage_resets(self, contact):
        f = await self.fixture()
        f.executor.clock = self.clock.now
        unavailable = TaskError("life_provider_unavailable", "provider offline")
        with patch.object(ModelCompletionHandoff, "recover", side_effect=unavailable), patch.object(
                ModelCompletionHandoff, "abort", side_effect=unavailable):
            for attempt, delay in enumerate((30, 60, 120, 240, 480, 900), 1):
                self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
                self.assertEqual(self.state(f)["provider_commit_attempts"], attempt)
                self.clock.advance(timedelta(seconds=delay))
        error = TaskError("result_unknown", "host reached but result uncertain")
        with patch.object(ModelCompletionHandoff, "recover", side_effect=error if contact == "error" else None,
                          return_value=None), patch.object(ModelCompletionHandoff, "abort", side_effect=error):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
        self.assertNotIn("provider_commit_attempts", self.state(f))
        await self.restart(f, enabled=False)
        self.due(f)
        with patch.object(ModelCompletionHandoff, "recover", side_effect=unavailable), patch.object(
                ModelCompletionHandoff, "abort", side_effect=unavailable):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
        self.assertEqual(self.state(f)["provider_commit_attempts"], 1)
        self.assertEqual(datetime.fromisoformat(self.state(f)["result_check_after"]), self.clock.now() + timedelta(seconds=30))
        self.assertEqual(len(self.called), 1)

    async def test_provider_outage_restarts_at_30_after_host_error_response(self):
        await self.assert_provider_outage_resets("error")

    async def test_provider_outage_restarts_at_30_after_host_receipt(self):
        await self.assert_provider_outage_resets("receipt")

    async def assert_commit_interruption(self, stage):
        f = await self.exhausted()
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.original["proof"])
        decide = life_commands.decide
        original = getattr(ModelCompletionHandoff, stage) if stage in ("apply", "complete", "acknowledge") else None
        save = f.executor.store.save
        def race(*args, **kwargs):
            if kwargs["outcome"] == "abort":
                handoff.decide(f.original["command"], observed_at=f.app.now())
                if stage == "decide":
                    raise asyncio.CancelledError("commit won before abort reply")
            return decide(*args, **kwargs)
        def interrupted(handoff, *args, **kwargs):
            result = original(handoff, *args, **kwargs)
            raise asyncio.CancelledError("commit interrupted after " + stage)
        def checkpoint(context, activity, value, **kwargs):
            if value.get("command_coordination", {}).get("settlement") == "confirmed":
                raise asyncio.CancelledError("commit reply before caller checkpoint")
            return save(context, activity, value, **kwargs)
        with patch.object(life_commands, "decide", side_effect=race):
            if original is not None:
                with patch.object(ModelCompletionHandoff, stage, interrupted):
                    with self.assertRaises(asyncio.CancelledError):
                        await f.executor.execute(f.context.key, f.activity.activity_id)
            else:
                with patch.object(f.executor.store, "save", side_effect=checkpoint):
                    with self.assertRaises(asyncio.CancelledError):
                        await f.executor.execute(f.context.key, f.activity.activity_id)
        scheduler = await self.restart(f)
        self.due(f)
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "completed"})
        for consumer in ("journal", "interest"):
            event, = f.life.pending_events(consumer)
            self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))
        self.assertEqual(len(self.called), 1)

    async def test_commit_race_restart_after_host_decision(self):
        await self.assert_commit_interruption("decide")

    async def test_commit_race_restart_after_life_apply(self):
        await self.assert_commit_interruption("apply")

    async def test_commit_race_restart_after_host_completion(self):
        await self.assert_commit_interruption("complete")

    async def test_commit_race_restart_after_acknowledgement(self):
        await self.assert_commit_interruption("acknowledge")

    async def test_commit_race_restart_after_reply_before_life_save(self):
        await self.assert_commit_interruption("before_life_save")

    async def test_authorized_retry_archives_prior_abort_coordination(self):
        f = await self.exhausted()
        scheduler = self.scheduler(f)
        self.assertEqual(await scheduler._run_life(f.context.key, f.activity.activity_id), {"state": "paused"})
        previous = self.state(f)["command_coordination"]
        # Seed the retryable-audit precondition. The normal succeeded model
        # audit is deliberately NOT made retryable by abort-own itself.
        with f.app.runtime.transaction() as db:
            db.execute("UPDATE execution_tasks SET state='failed' WHERE task_id=?", (f.original["task_id"],))
        self.clock.advance(timedelta(hours=1))
        activity = f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner)
        started = await ReadingStartHandoff(f.app, f.routine, f.app.role_life.transition_provider()).execute(
            activity.activity_id, expected_revision=activity.revision, request_id="authorized-new-attempt")
        self.assertEqual(await f.executor.execute(f.context.key, activity.activity_id, started.revision), {"state": "completed"})
        state = self.state(f)
        self.assertEqual(state["prior_command_coordinations"], [previous])
        self.assertNotIn("command_coordination", state)
        self.assertNotIn("commit_stop_logged", state)
        self.assertNotIn("park_check_attempts", state)
        self.assertNotEqual(state["task_id"], f.original["task_id"])
        self.assertEqual(len(self.called), 2)
        rows = f.life._connection.execute("SELECT state FROM life_prepared_commands WHERE command_id LIKE 'activity-model-complete-%'").fetchall()
        self.assertEqual(sorted(row[0] for row in rows), ["aborted", "acknowledged"])

    async def test_inflight_v2_never_replays_v1_when_disabled(self):
        f = await self.exhausted()
        with patch.object(life_commands, "decide", side_effect=TimeoutError("decision unavailable")):
            await f.executor.execute(f.context.key, f.activity.activity_id)
        await self.restart(f, enabled=False)
        self.due(f)
        with patch.object(ModelCompletionHandoff, "recover", side_effect=AssertionError("v2 must not replay via v1")):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "commit_unresolved"})
        self.assert_barrier(f)
