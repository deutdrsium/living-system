from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.role_work.life import scheduling as original
from good_life import sleep_schedule as migrated


class SleepScheduleCompatibilityTests(unittest.TestCase):
    def test_bedtime_variation_and_timezone_match_original(self):
        for values in (("installation", "bot", "role"), ("安装", "机器人", "角色")):
            owner = SimpleNamespace(installation_id=values[0], bot_id=values[1], persona_id=values[2])
            wire_owner = vars(owner)
            for day in (date(2024, 2, 29), date(2026, 3, 8), date(2026, 11, 1)):
                for maximum in (0, 1, 20, 120):
                    with self.subTest(owner=values, day=day, maximum=maximum):
                        self.assertEqual(migrated.variation_for(wire_owner, day, maximum),
                                         original.variation_for(owner, day, maximum))
                        for zone in ("Asia/Shanghai", "America/New_York"):
                            try:
                                ZoneInfo(zone)
                            except ZoneInfoNotFoundError:
                                continue
                            local_time = time(2, 30)
                            expected = original.bedtime_at(owner, day, zone, local_time, maximum)
                            self.assertEqual(migrated.bedtime_at(wire_owner, day, zone, local_time, maximum), expected)
                            for now in (expected - timedelta(seconds=1), expected, expected + timedelta(seconds=1)):
                                self.assertEqual(migrated.next_bedtime(now, wire_owner, zone, local_time, maximum),
                                                 original.next_bedtime(now, owner, zone, local_time, maximum))

    def test_sleep_phase_matches_original_at_boundaries(self):
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        episodes = (None, {}, {"state": "sleeping", "started_at": now.isoformat(),
                              "planned_wake_at": (now + timedelta(hours=8)).isoformat()},
                    {"state": "interrupted", "started_at": now.isoformat(),
                     "wake_until": (now + timedelta(minutes=5)).isoformat()},
                    {"state": "awake", "started_at": (now - timedelta(hours=6)).isoformat(),
                     "ended_at": (now - timedelta(minutes=29)).isoformat(),
                     "planned_wake_at": (now + timedelta(hours=2)).isoformat()},
                    {"state": "awake", "ended_at": (now - timedelta(minutes=30)).isoformat(),
                     "sleep_debt_minutes": 90})
        for episode in episodes:
            for minutes in (None, -1, 0, 20, 21, 60, 120, 121):
                next_sleep = now + timedelta(minutes=minutes) if minutes is not None else None
                with self.subTest(episode=episode, minutes=minutes):
                    self.assertEqual(migrated.sleep_rhythm(now, next_sleep, episode),
                                     original.sleep_rhythm(now, next_sleep, episode))


if __name__ == "__main__":
    unittest.main()
