"""A4 manual validation, materialization, migration and command fencing."""
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins/astrbot_plugin_good_life"), str(ROOT / "tests")]
from test_kernel_support import NOW
from good_life.academics import Academics, AcademicsConfig
from good_life.academics.models import expand, validate_term
from good_life.kernel.codec import KernelError
from good_life.storage import LifeStorage


def term(**changes):
    return {"term_id": "fall", "academic_year": "2026-2027", "timezone": "Asia/Shanghai",
        "start_date": "2026-11-16", "end_date": "2026-11-30", "week_starts": ["2026-11-16", "2026-11-23"],
        "holidays": [], "makeup_days": [], "source": "manual", **changes}


def entry(**changes):
    return {"slot_id": "ethics-mon", "term_id": "fall", "course": {"course_id": "ethics", "name": "伦理学",
        "kind": "required", "credits": 2, "teacher_ref": None}, "weekday": 1, "start_time": "16:10", "end_time": "16:20",
        "weeks": [1, 2], "week_rule": "all", "place_id": "classroom", "exceptions": [], "source": "manual", **changes}


class DomainCase(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix=".academics-", dir=ROOT)
        self.addCleanup(self.temp.cleanup)
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)
        self.academics = Academics(config=AcademicsConfig(enabled=True))
        self.academics.install(self.storage)
        self.owner, self.context, self.revision = "owner", "context", 0
        self.counter = 0

    def write(self, kind, record=None, *, action="create", record_id=None, now=NOW):
        self.counter += 1
        body = {"command_id": "manual-" + str(self.counter), "expected_revision": self.revision, "action": action,
            "kind": kind, "id": record_id or record["term_id" if kind == "term" else "slot_id"]}
        if action != "delete":
            body["record"] = record
        result = self.academics.command(self.owner, self.context, body, now=now)
        self.revision = result["revision"]
        return result

    def seed(self):
        self.write("term", term())
        self.write("timetable", entry())
        return self.academics.next_sessions(self.owner, self.context, now=NOW)[0]

    def test_week_rules_holidays_makeup_and_stable_identity(self):
        original = expand(term(), [entry()])
        self.assertEqual(len(original), 2)
        self.assertEqual(original[0]["start_at"], "2026-11-16T08:10:00Z")
        self.assertEqual(len(expand(term(), [entry(week_rule="odd")])), 1)
        even = expand(term(), [entry(week_rule="even")])
        self.assertEqual(even[0]["local_date"], "2026-11-23")
        holiday = term(holidays=["2026-11-16"])
        self.assertEqual(len(expand(holiday, [entry()])), 1)
        holiday["makeup_days"] = [{"date": "2026-11-21", "teaching_date": "2026-11-16"}]
        moved = expand(holiday, [entry()])
        self.assertEqual(moved[0]["session_id"], original[0]["session_id"])
        self.assertEqual(moved[0]["local_date"], "2026-11-21")
        self.assertEqual(expand(term(), [entry(place_id="other-room")])[0]["session_id"], original[0]["session_id"])

    def test_cancellation_reschedule_and_partial_final_week(self):
        exceptions = [
            {"date": "2026-11-16", "cancelled": True, "start_time": None, "end_time": None, "place_id": None},
            {"date": "2026-11-23", "cancelled": False, "start_time": "18:00", "end_time": "18:30", "place_id": "room2"},
        ]
        sessions = expand(term(), [entry(exceptions=exceptions)])
        self.assertTrue(sessions[0]["cancelled"])
        self.assertEqual(sessions[1]["start_at"], "2026-11-23T10:00:00Z")
        self.assertEqual(sessions[1]["place_id"], "room2")
        same_time_disjoint_weeks = expand(term(), [entry(week_rule="odd"), entry(slot_id="even", week_rule="even")])
        self.assertEqual(len(same_time_disjoint_weeks), 2)
        partial = term(end_date="2026-11-25")
        self.assertEqual(len(expand(partial, [entry()])), 2)
        self.assertEqual(expand(partial, [entry(weekday=5)])[-1]["local_date"], "2026-11-20")

    def test_timezone_local_date_and_dst_rejection(self):
        midnight = expand(term(), [entry(start_time="00:10", end_time="00:20")])[0]
        self.assertEqual(midnight["start_at"], "2026-11-15T16:10:00Z")
        self.assertEqual(midnight["local_date"], "2026-11-16")
        eastern = term(timezone="America/New_York", start_date="2026-10-26", end_date="2026-11-09", week_starts=["2026-10-26", "2026-11-02"])
        with self.assertRaisesRegex(KernelError, "ambiguous"):
            expand(eastern, [entry(weekday=7, start_time="01:10", end_time="01:20")])
        spring = term(timezone="America/New_York", start_date="2026-03-02", end_date="2026-03-16", week_starts=["2026-03-02", "2026-03-09"])
        with self.assertRaisesRegex(KernelError, "nonexistent"):
            expand(spring, [entry(weekday=7, start_time="02:10", end_time="02:20")])
        # UTC instants across DST differ, even though the local timetable stays fixed.
        times = expand(eastern, [entry(start_time="09:00", end_time="10:00")])
        self.assertEqual([item["start_at"][11:16] for item in times], ["13:00", "14:00"])

    def test_validation_matrix(self):
        bad_terms = [term(source="scraped"), term(timezone="Invalid/Zone"), term(end_date="2026-11-16"),
            term(week_starts=["2026-11-16", "2026-11-17"]), term(holidays=["2027-01-01"]),
            term(makeup_days=[{"date": "2026-11-17", "teaching_date": "2026-11-17"}])]
        for value in bad_terms:
            with self.subTest(term=value), self.assertRaises(KernelError):
                validate_term(value)
        for changes in ({"weeks": [0]}, {"weeks": [3]}, {"weeks": [True]}, {"weeks": [1, 1]}, {"weekday": 0},
            {"week_rule": "sometimes"}, {"start_time": "16:20"}, {"place_id": ""}, {"source": "generated"}):
            with self.subTest(slot=changes), self.assertRaises(KernelError):
                expand(term(), [entry(**changes)])
        with self.assertRaisesRegex(KernelError, "overlap"):
            expand(term(), [entry(), entry(slot_id="second")])
        with self.assertRaisesRegex(KernelError, "exception"):
            expand(term(), [entry(exceptions=[{"date": "2026-11-17", "cancelled": True, "start_time": None, "end_time": None, "place_id": None}])])

    def test_atomic_validation_revision_and_replay(self):
        self.seed()
        request = {"command_id": "update", "expected_revision": 2, "action": "update", "kind": "timetable", "id": "ethics-mon", "record": entry(place_id="new-room")}
        receipt = self.academics.command(self.owner, self.context, request, now=NOW)
        self.assertEqual(receipt["revision"], 3)
        self.assertEqual(self.academics.command(self.owner, self.context, request, now=NOW + timedelta(days=2)), receipt)
        with self.assertRaisesRegex(KernelError, "different parameters"):
            self.academics.command(self.owner, self.context, {**request, "record": entry(place_id="other-room")}, now=NOW)
        with self.assertRaises(KernelError) as stale:
            self.academics.command(self.owner, self.context, {**request, "command_id": "stale"}, now=NOW)
        self.assertEqual(stale.exception.code, "stale_version")
        self.revision = 3
        with self.assertRaisesRegex(KernelError, "overlap"):
            self.write("timetable", entry(slot_id="conflict"))
        self.assertEqual(self.academics.read(self.owner, self.context, "term", {})["revision"], 3)
        with self.assertRaises(KernelError) as denied:
            self.academics.read(self.owner, "other-context", "term", {})
        self.assertEqual(denied.exception.code, "unauthorized")

    def test_revisions_history_deletion_and_pure_reads(self):
        first = self.seed()
        self.write("timetable", entry(place_id="room2"), action="update")
        changed = self.academics.next_sessions(self.owner, self.context, now=NOW)[0]
        self.assertEqual(changed["session_id"], first["session_id"])
        self.assertEqual(changed["revision"], first["revision"] + 1)
        self.write("timetable", entry(place_id="room3"), action="update", now=NOW + timedelta(hours=1))
        historic = self.academics.session_context(self.owner, self.context, first["session_id"])
        self.assertEqual(historic, changed)
        self.write("timetable", action="delete", record_id="ethics-mon", now=NOW + timedelta(hours=1))
        sessions = self.academics.read(self.owner, self.context, "occurrences", {"term_id": "fall", "start": "2026-11-16", "end": "2026-11-30"})
        self.assertFalse(sessions["items"][0]["cancelled"])
        self.assertTrue(sessions["items"][1]["cancelled"])
        with self.storage._lock:
            before = self.storage._connection.total_changes
            self.academics.read(self.owner, self.context, "term", {})
            self.academics.read(self.owner, self.context, "timetable", {"term_id": "fall"})
            self.assertEqual(self.storage._connection.total_changes, before)
        # Explicit install is idempotent and verifies ownership/version.
        self.academics.install(self.storage)
        with self.storage.transaction() as db:
            db.execute("UPDATE life_metadata SET value='999' WHERE key='academics_schema_version'")
        with self.assertRaisesRegex(KernelError, "unsupported"):
            self.academics.install(self.storage)

    def test_timezone_change_is_fenced_after_history(self):
        first = self.seed()
        with self.assertRaisesRegex(KernelError, "timezone cannot change"):
            self.write("term", term(timezone="America/New_York"), action="update", now=NOW + timedelta(hours=1))
        stored = self.academics.read(self.owner, self.context, "term", {})
        self.assertEqual(stored["revision"], 2)
        self.assertEqual(stored["terms"][0]["timezone"], "Asia/Shanghai")
        self.assertEqual(self.academics.session_context(self.owner, self.context, first["session_id"]), first)
        self.write("term", term(timezone="America/New_York"), action="update", now=NOW - timedelta(days=1))
        revised = self.academics.session_context(self.owner, self.context, first["session_id"])
        self.assertEqual(revised["start_at"], "2026-11-16T21:10:00Z")

    def test_runtime_explicit_enabled_embedding_and_missing_adapter(self):
        import asyncio
        from good_life.runtime import LifeRuntime
        from good_life.academics import Campus, ClassActivities
        from good_life.kernel import KernelConfig
        from test_kernel_support import Access
        enabled_dir = TemporaryDirectory(prefix=".academics-runtime-", dir=ROOT)
        self.addCleanup(enabled_dir.cleanup)
        storage = LifeStorage.open(enabled_dir.name)
        self.addCleanup(storage.close)
        access = Access()
        campus = Campus(("dorm", "classroom"), (("dorm", "classroom", 300),))
        runtime = LifeRuntime(storage, object(), academics_config=AcademicsConfig(enabled=True),
            kernel_config=KernelConfig(enabled=True), kernel_access=access, campus=campus)
        self.addCleanup(lambda: asyncio.run(runtime.close()))
        self.assertIsInstance(runtime.kernel, ClassActivities)
        self.assertEqual(runtime.academics.read(access.scope.owner_key, access.scope.context_key, "term", {})["terms"], [])
        self.assertEqual(runtime.advance_kernel(now=NOW)["started"], 0)
        self.assertNotIn("academics", runtime.handlers)
        with self.assertRaisesRegex(KernelError, "host-verified"):
            from good_life.academics import AcademicsAccess
            AcademicsAccess(None, self.academics, object())

    def test_disabled_has_no_schema_or_behavior_change(self):
        from good_life.runtime import LifeRuntime
        from types import SimpleNamespace
        other = TemporaryDirectory(prefix=".academics-off-", dir=ROOT)
        self.addCleanup(other.cleanup)
        storage = LifeStorage.open(other.name)
        self.addCleanup(storage.close)
        baseline_dir = TemporaryDirectory(prefix=".academics-baseline-", dir=ROOT)
        self.addCleanup(baseline_dir.cleanup)
        baseline_storage = LifeStorage.open(baseline_dir.name)
        self.addCleanup(baseline_storage.close)
        baseline = LifeRuntime(baseline_storage, SimpleNamespace())
        runtime = LifeRuntime(storage, SimpleNamespace(), academics_config=AcademicsConfig(enabled=False))
        self.assertEqual(set(runtime.handlers), set(baseline.handlers))
        schema = lambda store: [tuple(row) for row in store._connection.execute("SELECT name,sql FROM sqlite_master ORDER BY name")]
        self.assertEqual(schema(storage), schema(baseline_storage))
        self.assertEqual([tuple(row) for row in storage._connection.execute("SELECT * FROM life_metadata ORDER BY key")],
                         [tuple(row) for row in baseline_storage._connection.execute("SELECT * FROM life_metadata ORDER BY key")])
        self.assertIsNone(runtime.academics)
        self.assertEqual(runtime.advance_kernel(now=NOW), {"advanced": 0, "started": 0, "failures": ()})
        # Legacy runtime installs its own prepared/configuration schema; academic/kernel objects never appear.
        objects = [row[0] for row in storage._connection.execute("SELECT name FROM sqlite_master")]
        self.assertFalse(any(name.startswith(("life_academic_", "life_campus_", "life_kernel_")) for name in objects))
        disabled = Academics()
        with self.assertRaises(KernelError) as error:
            disabled.read("owner", "context", "term", {})
        self.assertEqual(error.exception.code, "missing_dependency")


if __name__ == "__main__":
    unittest.main()
