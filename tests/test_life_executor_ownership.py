from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
from companion_v2.execution.models import TaskError
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.life.coordinator import RoleLifeCoordinator
from tests.test_role_state import StateFixture
from tests.test_role_life_runtime import LifeFixture


class CoordinatorOwnershipTests(unittest.TestCase):
    def test_transferred_or_invalid_ownership_stops_the_old_executor(self):
        selected = ["legacy"]
        def owner():
            if selected[0] == "invalid":
                raise TaskError("life_activity_owner_invalid", "invalid")
            return selected[0]
        calls = []
        routine = SimpleNamespace(advance=lambda: calls.append("advance"), cancel_running=lambda: calls.append("cancel"),
                                  advance_host_tasks=lambda: calls.append("host-only"))
        coordinator = RoleLifeCoordinator(SimpleNamespace(role_life=SimpleNamespace(activity_owner=owner)))
        coordinator.routines["routine"] = routine
        coordinator.advance()
        selected[0] = "good_life"
        coordinator.advance()
        selected[0] = "invalid"
        coordinator.advance()
        self.assertEqual(calls, ["advance", "cancel", "host-only", "cancel"])


class RuntimeOwnershipTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def test_host_starts_before_missing_life_provider_after_transfer(self):
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        host, app, context, _ = self.setup_state()
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        app.life_runtime.advance()
        self.assertEqual(app.state, "running")
        with self.assertRaises(TaskError) as missing:
            app.role_life.activity("activity", owner=routine.owner)
        self.assertEqual(missing.exception.code, "life_provider_unavailable")
        self.assertEqual(self.called, [])

    async def test_direct_legacy_sleep_and_reading_do_not_start_after_transfer(self):
        host, app, context, _ = self.setup_state()
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        cancelled = []
        routine.cancel_running = lambda: cancelled.append(True)
        routine.advance()
        self.assertEqual(cancelled, [True])
        refreshed = []
        routine.refresh = lambda: refreshed.append(True)
        app.life_runtime.advance()
        self.assertEqual(refreshed, [True])
        calls = (
            lambda: routine.start_sleep("manual", 10),
            lambda: routine.wake("sleep", expected_revision=1, operation_id="wake"),
            lambda: routine.start_reading("book", expected_revision=1, request_id="read"),
        )
        for call in calls:
            with self.assertRaises(TaskError) as error:
                call()
            self.assertEqual(error.exception.code, "life_executor_transferred")
        with self.assertRaises(TaskError) as read:
            await routine._read(SimpleNamespace(activity_id="book"))
        self.assertEqual(read.exception.code, "life_executor_transferred")
        with self.assertRaises(TaskError) as wake:
            await routine._wake_shared_due()
        self.assertEqual(wake.exception.code, "life_executor_transferred")
        self.assertEqual(self.sent, [])


class DiaryOwnershipTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_host_diary_registration_continues_after_life_transfer(self):
        await self.setup_life()
        await self.configure(diary_enabled=True)
        routine = self.manager.life
        self.assertIn(routine.schedule_id, self.app.journal_schedule._schedules)
        self.app.journal_schedule.remove(routine.schedule_id)
        routine._revision = None
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        self.app.life_runtime.advance()
        self.assertIn(routine.schedule_id, self.app.journal_schedule._schedules)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
