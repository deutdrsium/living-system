from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.agenda.calendar import CalendarService
from companion_v2.execution.models import encode
from companion_v2.foundation.refs import ScopeKind, ScopeRef, SourceRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.identity_policy.private_domain import source_ref
from good_life.calendar_changes import change


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def wire(value):
    return json.loads(encode(value))


class Records:
    def __init__(self, current):
        self.current = current

    def get(self, _context, _identifier):
        return self.current

    def storage_key_for_change(self, _context, _identifier, _kind):
        return "owner"

    def change(self, _context, _identifier, *, transform, **_kwargs):
        return transform(deepcopy(self.current))


class CalendarChangesCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        self.role = SubjectRef("installation", SubjectKind.BOT, "role")
        self.human = SubjectRef("installation", SubjectKind.HUMAN, "human")
        self.context = SimpleNamespace(role_binding=SimpleNamespace(role=self.role), key="context")
        self.operator = SimpleNamespace(subject=self.human)
        self.source1 = SourceRef("source-1", "calendar", self.scope, VersionRef("source-1", 1), NOW)
        self.source2 = SourceRef("source-2", "calendar", self.scope, VersionRef("source-2", 1), NOW)
        self.data = {"kind": "role_plan", "title": "午后散步", "detail": "在附近走走",
                     "schedule": {"anchor": "2026-01-01T08:00:00+08:00", "timezone_name": "Asia/Shanghai",
                                  "frequency": "daily", "interval": 1, "count": 3},
                     "duration_minutes": 30, "presence": "散步"}
        self.evidence = []
        self.cancelled = []

    def old_change(self, current, *, action, data=None, sources=(), operator=None, planning=None):
        service = CalendarService.__new__(CalendarService)
        service.runtime = SimpleNamespace(transaction=lambda: nullcontext(), read_one=lambda *_args: None)
        service.tasks = SimpleNamespace(now=lambda: NOW)
        service.access = SimpleNamespace(check=lambda *_args, **_kwargs: None,
            evidence=lambda _context, sources, **_kwargs: self.evidence.extend(wire(tuple(sources))))
        service.records = Records(current)
        service._evidence = lambda _context, sources, **_kwargs: self.evidence.extend(wire(tuple(sources)))
        service._cancel_reminder = lambda _context, record, _key: self.cancelled.append(record)
        return service.command(self.context, identifier="event", action=action, expected_revision=1,
                               operation_key="operation", data=data, sources=sources, operator=operator, planning=planning)

    def compare(self, current, *, action, data=None, sources=(), operator=None, planning=None):
        self.evidence = []
        self.cancelled = []
        original = self.old_change(deepcopy(current), action=action, data=data, sources=sources,
                                   operator=operator, planning=planning)
        moved = change(deepcopy(current), action=action, data=data, sources=wire(sources),
                       actor=wire(operator.subject if operator else self.role),
                       operator_authenticated=operator is not None, planning=planning)
        self.assertEqual(moved["record"], original)
        self.assertEqual(moved["cancel_reminder"], self.cancelled[0] if self.cancelled else None)
        expected_sources = {json.dumps(wire(source_ref(item)), sort_keys=True) for item in self.evidence}
        self.assertEqual({json.dumps(item, sort_keys=True) for item in moved["evidence"]}, expected_sources)
        return original

    def test_create_revise_and_each_existing_action_match_original(self):
        created = self.compare(None, action="create", data=self.data, sources=(self.source1,))
        current = {**created, "id": "event", "revision": 1, "updated_at": "2026-01-01T00:00:00.000000+00:00",
                   "reminder_id": "reminder", "reminder_context_key": "context"}
        cases = (
            ("revise", {**self.data, "title": "傍晚散步"}, (self.source2,), None),
            ("refine", {"sequence": 1, "detail": "带上水"}, (self.source1, self.source2), None),
            ("presence", {"presence": " 在散步 "}, (), None),
            ("accept", None, (), self.operator),
            ("cancel", None, (), None),
            ("exception", {"sequence": 1, "start": "2026-01-03T09:00:00+08:00"}, (), None),
            ("exception", {"sequence": 1, "start": None}, (), None),
        )
        for action, data, sources, operator in cases:
            with self.subTest(action=action, data=data):
                self.compare(current, action=action, data=data, sources=sources, operator=operator,
                             planning={"origin": "test"})
        commitment = {**current, "kind": "commitment", "state": "accepted", "accepted_by": [wire(self.human)]}
        moved = self.compare(commitment, action="exception",
                             data={"sequence": 1, "start": "2026-01-03T09:00:00+08:00"})
        self.assertEqual((moved["state"], moved["accepted_by"]), ("proposed", []))
        legacy = deepcopy(current)
        legacy["sources"][0]["issued_at"] = "2026-01-01T08:00:00+08:00"
        refined = self.compare(legacy, action="refine", data={"sequence": 0, "detail": "慢慢走"},
                               sources=(self.source1,))
        self.assertEqual(len(refined["sources"]), 1)

    def test_rejections_match_original_codes(self):
        created = self.old_change(None, action="create", data=self.data)
        current = {**created, "id": "event", "revision": 1}
        cases = (
            (current, "create", self.data, None),
            (None, "cancel", None, None),
            (current, "accept", None, None),
            (current, "revise", {**self.data, "kind": "commitment"}, None),
            (current, "refine", {"sequence": 8, "detail": "不存在"}, None),
            (current, "presence", {"presence": "123456789"}, None),
            (current, "exception", {"sequence": 9, "start": None}, None),
            (current, "other", None, None),
        )
        for old, action, data, operator in cases:
            with self.subTest(action=action):
                with self.assertRaises(Exception) as original:
                    self.old_change(deepcopy(old), action=action, data=data, operator=operator)
                with self.assertRaises(Exception) as moved:
                    change(deepcopy(old), action=action, data=data, sources=[],
                           actor=wire(operator.subject if operator else self.role),
                           operator_authenticated=operator is not None)
                self.assertEqual(getattr(moved.exception, "code", None), getattr(original.exception, "code", None))
                self.assertEqual(str(moved.exception), str(original.exception))


if __name__ == "__main__":
    unittest.main()
