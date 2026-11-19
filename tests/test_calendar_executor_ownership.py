from pathlib import Path
import sys
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
from companion_v2.agenda.calendar import LIFE_CALENDAR_OWNER_KEY
from companion_v2.agenda.planning import CalendarPlanning
from companion_v2.execution.models import TaskError
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


class CalendarPlannerOwnershipTests(unittest.TestCase):
    def test_transferred_owner_skips_legacy_context_scan(self):
        runtime = SimpleNamespace(read_all=lambda *_: self.fail("legacy planner history must not be read"))
        planner = CalendarPlanning(runtime)
        planner.app = SimpleNamespace(calendar=SimpleNamespace(_life_calendar_owner=lambda: "good_life"))
        self.assertEqual(planner._load_legacy_contexts(), {})

    def test_invalid_owner_stops_the_planner(self):
        def invalid():
            raise TaskError("life_calendar_owner_invalid", "invalid")
        planner = CalendarPlanning(SimpleNamespace())
        planner.app = SimpleNamespace(calendar=SimpleNamespace(_life_calendar_owner=invalid))
        planner.advance()
        self.assertTrue(planner.closed)


class CalendarHostOwnershipTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def test_host_starts_with_both_owners_transferred_before_provider(self):
        with self.runtime.transaction() as db:
            db.executemany("INSERT INTO runtime_metadata VALUES (?, 'good_life')",
                           ((LIFE_CALENDAR_OWNER_KEY,), (LIFE_ACTIVITY_OWNER_KEY,)))
        host, app, context, _ = self.setup_state()
        await self.start(host)
        app.life_runtime.bind(context, "test persona")
        app.life_runtime.advance()
        app.calendar_planning.advance()
        self.assertEqual(app.state, "running")
        self.assertFalse(app.calendar_planning.closed)
        self.assertEqual((self.called, self.sent), ([], []))

    async def test_host_starts_without_provider_and_old_planner_cannot_run(self):
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_CALENDAR_OWNER_KEY,))
        host, app, context, _ = self.setup_state()
        await self.start(host)
        validation = app.calendar_planning.validate(None, NOW)
        self.assertFalse(validation.is_success)
        self.assertEqual(validation.failure.code, "life_calendar_executor_transferred")
        app.calendar_planning.advance()
        self.assertFalse(app.calendar_planning.closed)
        with self.assertRaises(TaskError) as error:
            await app.calendar_planning.run(context, "test persona", mode="daily", slot="daily-test", manual=True)
        self.assertEqual(error.exception.code, "life_calendar_executor_transferred")
        self.assertEqual(self.called, [])
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
