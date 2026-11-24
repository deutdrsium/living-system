import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.prepared_commands import PreparedCommands
from good_life.reading_scheduler import ReadingScheduler
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage


class SleepSchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_scheduler_snapshots_are_checked_once_per_context_every_thirty_seconds(self):
        with TemporaryDirectory() as directory, closing(LifeStorage.open(directory)) as storage:
            PreparedCommands(storage).install()
            now = datetime(2026, 11, 19, tzinfo=timezone.utc)
            owner = {"installation_id": "install", "bot_id": "bot", "persona_id": "persona"}
            contexts = [{"context_key": "first", "owner_key": "owner"}]

            async def unused(*_args):
                self.fail("no activity should start")

            sleep = Mock(side_effect=lambda key: {"context_key": key, "observed_at": now.isoformat(),
                "owner": owner, "settings": {"enabled": False}, "config_revision": 1, "profile": {},
                "busy": False, "selected_executor": True, "active_reading": False, "napped_today": False})
            social = Mock(return_value={"observed_at": now.isoformat(), "owner": owner, "timezone": "UTC",
                                        "interests": [], "records": [], "busy": True})
            reading = Mock(return_value={"observed_at": now.isoformat(), "reading_enabled": False, "busy": True})
            pending = []
            async def run_manual(key, operation_id):
                self.assertEqual((key, operation_id), ("first", "manual"))
                pending.clear()
                return {"state": "completed"}

            elapsed = Mock(return_value=100)
            scheduler = DueSleepScheduler(storage, {"api_version": "companion.life-host.v1",
                "contexts": lambda: contexts, "wake": unused, "recover": unused,
                "sleep_snapshot": sleep, "start_sleep": unused, "recover_sleep_creation": unused,
                "social_snapshot": social, "start_social": unused, "execute_social": unused, "recover_social": unused,
                "reading_snapshot": reading, "reading_choice_result": unused, "choose_reading": unused,
                "start_reading": unused, "execute_reading": unused, "recover_reading": unused,
                "reading_manual_pending": lambda: pending, "execute_manual_reading": run_manual}, monotonic_clock=elapsed)
            with patch.object(scheduler.records, "dispatch", return_value=()) as records:
                await scheduler.tick(now=now)
                pending.append({"context_key": "first", "owner_key": "owner", "activity_id": "book", "operation_id": "manual"})
                elapsed.return_value = 101
                await scheduler.tick(now=now + timedelta(seconds=1))
                self.assertEqual(await scheduler.reading_scheduler.workers["first"], {"state": "completed"})
                for snapshot in (sleep, social, reading):
                    self.assertEqual(snapshot.call_args_list, [call("first")])
                self.assertEqual(records.call_count, 2)

                contexts.append({"context_key": "second", "owner_key": "owner"})
                elapsed.return_value = 129
                await scheduler.tick(now=now + timedelta(seconds=29))
                elapsed.return_value = 130
                await scheduler.tick(now=now + timedelta(seconds=30))
                for snapshot in (sleep, social, reading):
                    self.assertEqual(snapshot.call_args_list, [call("first"), call("second"), call("first")])
                self.assertIsNone(scheduler.last_failure)
            await scheduler.close()

    async def test_reading_cadence_uses_elapsed_time_at_the_snapshot_not_the_tick_time(self):
        with TemporaryDirectory() as directory, closing(LifeStorage.open(directory)) as storage:
            now = datetime(2026, 11, 19, tzinfo=timezone.utc)
            snapshot = Mock(return_value={"observed_at": now.isoformat(), "reading_enabled": False, "busy": True})
            elapsed = Mock(return_value=500)
            scheduler = ReadingScheduler(storage, {"reading_snapshot": snapshot}, monotonic_clock=elapsed)
            contexts, planner = {"context": "owner"}, SimpleNamespace(_windows={})
            scheduler.advance(contexts, now, planner)
            # This tick was delayed before the actual check. Even though the
            # next tick's wall time is much later, only one second elapsed.
            elapsed.return_value = 501
            scheduler.advance(contexts, now + timedelta(seconds=61), planner)
            self.assertEqual(snapshot.call_count, 1)
            elapsed.return_value = 529
            scheduler.advance(contexts, now - timedelta(hours=1), planner)
            self.assertEqual(snapshot.call_count, 1)
            elapsed.return_value = 530
            scheduler.advance(contexts, now - timedelta(hours=1), planner)
            self.assertEqual(snapshot.call_count, 2)

    async def test_unresolved_interruption_blocks_waking_and_new_sleep_for_that_role(self):
        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            try:
                PreparedCommands(storage).install()
                async def unavailable(*_args):
                    raise ValueError("interruption has not been committed")
                async def must_not_run(*_args):
                    self.fail("sleep execution overtook an unresolved interruption")
                capabilities = {"api_version": "companion.life-host.v1",
                    "contexts": lambda: [{"context_key": "context", "owner_key": "owner"}],
                    "wake": must_not_run, "recover": must_not_run,
                    "sleep_interruptions": lambda: [{"id": "pending", "context_key": "context", "owner_key": "owner"}],
                    "interrupt_sleep": unavailable, "sleep_snapshot": lambda *_: self.fail("role must wait"),
                    "start_sleep": must_not_run, "recover_sleep_creation": must_not_run}
                scheduler = DueSleepScheduler(storage, capabilities)
                self.assertEqual(await scheduler.tick(), {"recovered": 0, "woken": 0})
                self.assertEqual(scheduler.last_failure, "life_interruption_unavailable")
                await scheduler.close()
            finally:
                storage.close()

    async def test_start_is_single_and_close_prevents_restart(self):
        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            try:
                PreparedCommands(storage).install()
                async def unused(*_args):
                    self.fail("no context should execute a sleep")
                capabilities = {"api_version": "companion.life-host.v1", "contexts": lambda: [],
                                "wake": unused, "recover": unused}
                scheduler = DueSleepScheduler(storage, capabilities)
                first = scheduler.start()
                self.assertIs(scheduler.start(), first)
                await asyncio.sleep(0)
                await scheduler.close()
                self.assertTrue(first.done())
                self.assertEqual(await scheduler.tick(), {"recovered": 0, "woken": 0})
                with self.assertRaises(ValueError):
                    scheduler.start()
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
