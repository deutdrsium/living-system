from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.role_work.life import smart_reading as original
from companion_v2.role_work.models import RoleActivityState
from good_life import reading_choices as moved


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def activity(identifier, source, offset, state="planned", kind="reading"):
    source_ref = SimpleNamespace(source_id=source)
    work = SimpleNamespace(kind=kind, source=source_ref, start_offset=offset)
    host = SimpleNamespace(activity_id=identifier, revision=1, work=work, state=RoleActivityState(state))
    document = {"activity_id": identifier, "revision": 1, "state": state,
                "work": {"kind": kind, "source": {"source_id": source}, "start_offset": offset}}
    return host, document


class ReadingChoicesCompatibilityTests(unittest.TestCase):
    def test_candidates_preserve_paused_spans_rotation_and_authorization(self):
        entries = [activity(f"book-{i}", f"source-{i:02}", 0) for i in range(12)]
        entries += [activity("later-span", "source-01", 20), activity("paused-first", "source-01", 0, "paused")]
        # Put the paused earlier span first for the original equal-offset tie rule.
        entries[-1], entries[1] = entries[1], entries[-1]
        denied = {"source-03", "source-06"}
        sources = SimpleNamespace(validate_reference_for_audience=lambda source, **_kwargs:
                                  SimpleNamespace(is_success=source.source_id not in denied))
        app = SimpleNamespace(role_journal=SimpleNamespace(sources=sources), now=lambda: NOW)
        routine = SimpleNamespace(app=app, runtime=object(),
                                  context=SimpleNamespace(actor=SimpleNamespace(subject="audience")))
        reader = original.SmartReading(routine)
        host, documents = zip(*entries)
        for day in ("2026-01-01", "2026-06-30"):
            for selected in (None, "source-01", "source-02", "source-03", "missing"):
                with self.subTest(day=day, selected=selected):
                    before = reader.candidates(host, day, selected)
                    after = moved.candidates(documents, day, selected_source=selected,
                                             authorize_source=lambda source: source["source_id"] not in denied)
                    self.assertEqual([a.activity_id for a in before], [a["activity_id"] for a in after])
                    self.assertNotIn("later-span", [a["activity_id"] for a in after])

    def test_choice_slot_and_reading_windows_match_original(self):
        entries = (activity("read", "book", 0, "completed"), activity("browse", "unused", 0, "completed", "social"),
                   activity("waiting", "other", 0, "planned"))
        host, documents = zip(*entries)
        self.assertEqual(moved.choice_slot(documents, "2026-01-01"), original.choice_slot(host, "2026-01-01"))
        start, end = (NOW - timedelta(minutes=10)).isoformat(), (NOW + timedelta(minutes=10)).isoformat()
        reading = {"state": "accepted", "kind": "role_plan", "title": "读书", "detail": "", "start": start, "end": end}
        meeting = {**reading, "kind": "commitment", "title": "约会"}
        for calendar in ({}, {"partial": True}, {"items": [reading]}, {"items": [meeting]},
                         {"items": [reading, meeting]}, {"items": [{**reading, "start": "invalid"}]}):
            for next_sleep in (None, NOW - timedelta(seconds=1), NOW + timedelta(hours=1)):
                with self.subTest(calendar=calendar, next_sleep=next_sleep):
                    self.assertEqual(moved.reading_window(calendar, NOW, NOW, next_sleep),
                                     original.reading_window(calendar, NOW, NOW, next_sleep))


if __name__ == "__main__":
    unittest.main()
