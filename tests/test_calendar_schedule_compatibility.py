from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.agenda.models import Schedule as OriginalSchedule
from good_life.calendar_schedule import Schedule as MigratedSchedule


def outcome(callback):
    try:
        return ("ok", callback())
    except (ValueError, AttributeError, OverflowError) as error:
        return ("error", getattr(error, "code", None), str(error))


class CalendarScheduleCompatibilityTests(unittest.TestCase):
    def test_occurrences_and_search_match_original(self):
        scenarios = [
            ("2024-01-31T15:00:00+00:00", "Asia/Shanghai"),
            ("2024-02-29T15:00:00+00:00", "Asia/Shanghai"),
            ("2026-03-07T07:30:00+00:00", "America/New_York"),
            ("2026-10-31T05:30:00+00:00", "America/New_York"),
        ]
        for anchor, zone in scenarios:
            try:
                ZoneInfo(zone)
            except ZoneInfoNotFoundError:
                continue
            for frequency in ("once", "daily", "weekly", "monthly", "yearly"):
                for policy in ("reject", "shift_forward"):
                    for fold in (0, 1):
                        document = {"anchor": anchor, "timezone_name": zone, "frequency": frequency,
                                    "interval": 1, "count": 4, "fold": fold, "gap_policy": policy}
                        original = OriginalSchedule.restore(document)
                        migrated = MigratedSchedule.restore(document)
                        with self.subTest(document=document):
                            for index in (0, 1, 2, 3, 4, 50):
                                self.assertEqual(outcome(lambda: migrated.at(index)), outcome(lambda: original.at(index)))
                            start = datetime.fromisoformat(anchor)
                            for day in (-1, 0, 1, 27, 370):
                                instant = start + timedelta(days=day)
                                for minimum in (0, 2):
                                    self.assertEqual(outcome(lambda: migrated.first_at_or_after(instant, minimum=minimum)),
                                                     outcome(lambda: original.first_at_or_after(instant, minimum=minimum)))

    def test_restore_and_invalid_occurrences_match_original(self):
        base = {"anchor": "2026-01-01T00:00:00+00:00", "timezone_name": "UTC"}
        variants = (base, {**base, "fold": True}, {**base, "fold": 2}, {**base, "count": 0},
                    {**base, "interval": False}, {**base, "anchor": "2026-01-01T00:00:00"},
                    {**base, "timezone_name": "not a zone"}, {**base, "frequency": "other"})
        for document in variants:
            with self.subTest(document=document):
                old = outcome(lambda: OriginalSchedule.restore(document))
                new = outcome(lambda: MigratedSchedule.restore(document))
                if old[0] == "error":
                    self.assertEqual(new, old)
                else:
                    self.assertEqual(new[0], "ok")
                    for sequence in (-1, True, 0, 1):
                        self.assertEqual(outcome(lambda: new[1].at(sequence)), outcome(lambda: old[1].at(sequence)))
                    self.assertEqual(outcome(lambda: new[1].first_at_or_after(datetime.now(timezone.utc), minimum=-1)),
                                     outcome(lambda: old[1].first_at_or_after(datetime.now(timezone.utc), minimum=-1)))


if __name__ == "__main__":
    unittest.main()
