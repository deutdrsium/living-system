from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.sleep_planner import SleepPlanner


OWNER = {"installation_id": "installation", "bot_id": "bot", "persona_id": "role"}
SETTINGS = {"enabled": True, "sleep_enabled": True, "sleep_timezone": "UTC", "sleep_time": "22:00",
            "sleep_variation_minutes": 0, "sleep_window_minutes": 30, "sleep_minutes": 480}
NOW = datetime(2026, 1, 2, 21, tzinfo=timezone.utc)


class SleepPlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = SleepPlanner()

    def plan(self, now, **changes):
        options = {"context_key": "context", "owner": OWNER, "settings": SETTINGS, "config_revision": "1",
                   "now": now, "profile": {"sleep_pressure": None}, "busy": False,
                   "selected_executor": True, "active_reading": False, "napped_today": False}
        options.update(changes)
        return self.planner.plan(**options)

    def test_nightly_window_advances_only_after_confirmed_sleep(self):
        self.assertIsNone(self.plan(NOW))
        due = NOW + timedelta(hours=1)
        plan = self.plan(due)
        self.assertEqual((plan["kind"], plan["request_id"]), ("night", "daily-sleep-2026-01-02"))
        self.assertEqual(self.plan(due), plan)
        self.planner.confirm(plan, owner=OWNER, settings=SETTINGS, now=due)
        self.assertIsNone(self.plan(due + timedelta(minutes=1)))

    def test_pressure_and_busy_hold_until_the_window_closes(self):
        self.assertIsNone(self.plan(NOW))
        due = NOW + timedelta(hours=1)
        profile = {"sleep_pressure": 0, "sleep_debt_minutes": 0, "workload_score": 0}
        self.assertIsNone(self.plan(due, profile=profile))
        self.assertIsNone(self.plan(due + timedelta(minutes=30), profile=profile, busy=True))
        self.assertIsNone(self.plan(due + timedelta(minutes=30), profile=profile, active_reading=True))
        self.assertEqual(self.plan(due + timedelta(minutes=30), profile=profile)["kind"], "night")

    def test_restart_after_bedtime_and_expired_window_skip_old_sleep(self):
        self.assertIsNone(self.plan(NOW + timedelta(hours=3)))
        other = SleepPlanner()
        options = {"context_key": "other", "owner": OWNER, "settings": SETTINGS, "config_revision": "1",
                   "profile": {"sleep_pressure": None}, "busy": False, "selected_executor": True,
                   "active_reading": False, "napped_today": False}
        self.assertIsNone(other.plan(now=NOW, **options))
        self.assertIsNone(other.plan(now=NOW + timedelta(hours=10), **options))

    def test_nap_retains_original_pressure_window_and_daily_limit(self):
        now = NOW.replace(hour=12)
        profile = {"sleep_pressure": 50, "sleep_debt_minutes": 120, "workload_score": 20,
                   "awake_minutes": 360, "has_sleep_history": True}
        nap = self.plan(now, profile=profile, selected_executor=False)
        self.assertEqual((nap["kind"], nap["minutes"], nap["request_id"]), ("nap", 25, "daily-nap-2026-01-02"))
        self.assertIsNone(self.plan(now, profile=profile, napped_today=True))
        self.assertIsNone(self.plan(now, profile=profile, busy=True))
        self.assertIsNone(self.plan(now.replace(hour=10), profile=profile))


if __name__ == "__main__":
    unittest.main()
