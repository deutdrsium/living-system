from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import digest, encode
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.role_work.life.smart_social import social_window as original_window
from good_life.social_schedule import browsing_ready, social_first, social_window


NOW = datetime(2026, 1, 1, 12, tzinfo=timezone.utc)


def item(kind, state, *, seconds=0, identifier="activity"):
    return {"activity_id": identifier, "state": state, "work": {"kind": kind},
            "updated_at": (NOW + timedelta(seconds=seconds)).isoformat()}


class SocialScheduleCompatibilityTests(unittest.TestCase):
    def test_calendar_windows_match_original_for_overlap_sleep_and_partial_results(self):
        active = {"state": "accepted", "kind": "role_plan", "title": "刷视频", "detail": "",
                  "start": (NOW - timedelta(minutes=1)).isoformat(), "end": (NOW + timedelta(minutes=1)).isoformat()}
        conflict = {**active, "kind": "commitment", "title": "约定"}
        cases = [
            ({"items": []}, None),
            ({"items": [active]}, None),
            ({"items": [active, conflict]}, None),
            ({"items": [{**active, "start": "invalid"}]}, None),
            ({"items": [active], "partial": True}, None),
            ({"items": []}, NOW),
            ({"items": [active]}, NOW + timedelta(hours=1)),
        ]
        for calendar, sleep in cases:
            with self.subTest(calendar=calendar, sleep=sleep):
                self.assertEqual(social_window(calendar, NOW, NOW, sleep),
                                 original_window(calendar, NOW, NOW, sleep))

    def test_browsing_interval_includes_paused_and_cancelled_sessions(self):
        records = [item("social", "completed", seconds=-3600), item("social", "paused", seconds=-1200),
                   item("reading", "completed", seconds=-10), item("social", "active", seconds=-1)]
        self.assertFalse(browsing_ready(records, NOW, 30))
        self.assertTrue(browsing_ready(records, NOW + timedelta(minutes=10), 30))
        self.assertTrue(browsing_ready(records, NOW, 0))
        records.append(item("social", "cancelled", seconds=-30))
        self.assertFalse(browsing_ready(records, NOW, 1))

    def test_last_completed_activity_alternates_reading_and_browsing(self):
        owner = RoleOwnerRef("installation", "bot", "role")
        wire_owner = json.loads(encode(owner))
        day = "2026-01-01"
        expected_initial = int(digest([owner, day])[:8], 16) % 5 < 2
        self.assertEqual(social_first(wire_owner, day, ["interest"], []), expected_initial)
        self.assertFalse(social_first(wire_owner, day, [], []))
        reading = item("reading", "completed", seconds=-10, identifier="r")
        browsing = item("social", "completed", seconds=-20, identifier="s")
        self.assertTrue(social_first(wire_owner, day, [], [reading, browsing]))
        browsing["updated_at"] = NOW.isoformat()
        self.assertFalse(social_first(wire_owner, day, ["interest"], [reading, browsing]))
        browsing["state"] = "paused"
        self.assertTrue(social_first(wire_owner, day, [], [reading, browsing]))


if __name__ == "__main__":
    unittest.main()
