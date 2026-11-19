import asyncio
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.prepared_commands import PreparedCommands
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage


class SleepSchedulerLifecycleTests(unittest.IsolatedAsyncioTestCase):
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
