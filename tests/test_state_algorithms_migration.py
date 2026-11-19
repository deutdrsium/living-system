from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from companion_v2.role_work.state import cycle, cycle_feelings, mood
from good_life.state_records import StagedStateRecords


class StateAlgorithmMigrationTests(unittest.TestCase):
    def test_mood_and_cycle_computations_preserve_the_original_values(self):
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        records = StagedStateRecords(db)
        def compute(domain, function, *args, **options):
            return records.dispatch("compute", owner_key="role", domain=domain, function=function,
                                    args=args, options=options)
        now = datetime(2026, 10, 28, tzinfo=timezone.utc)
        current = {"baseline": 6, "joy": 6, "anger": 2, "sorrow": 2, "fear": 1,
                   "intensity": "mild", "description": "平常"}
        dynamics = {"updated_at": "2026-10-27T00:00:00+00:00", "baseline": 6}
        for function, args, options in (
            ("validate", (current,), {}),
            ("previous_axes", (current, dynamics), {}),
            ("project", (current, dynamics), {"at": now, "half_life_minutes": 90, "band": 3, "cross_window": True}),
        ):
            self.assertEqual(compute("mood", function, *args, **options), getattr(mood, function)(*args, **options))
        settings = {**cycle.DEFAULTS, "cycle_enabled": True, "cycle_variation_enabled": True}
        assertion = {"applicability": "applicable", "description": "平稳"}
        seed = {"installation_id": "i", "bot_id": "b", "persona_id": "p"}
        expected = cycle.evolve(None, assertion, settings, now, seed=seed)
        actual = compute("cycle", "evolve", None, assertion, settings, now, seed=seed)
        self.assertEqual(actual, expected)
        self.assertEqual(compute("cycle", "project", actual, now, discomfort_enabled=True),
                         cycle.project(expected, now, discomfort_enabled=True))
        feeling = {"applicability": "applicable", "description": "有点累",
                   "feeling": {"episode_id": None, "trend": "continuing"}}
        self.assertEqual(compute("feelings", "update", None, feeling, now, key=["ctx", "op"]),
                         cycle_feelings.update(None, feeling, now, key=["ctx", "op"]))


if __name__ == "__main__":
    unittest.main()
