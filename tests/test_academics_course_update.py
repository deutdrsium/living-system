"""Course metadata update: every slot of one term course, same course_id, one transaction and revision."""
from copy import deepcopy
import json

import unittest

import test_academics_domain as domain
from test_academics_domain import entry, term
from test_kernel_support import NOW
from good_life.kernel.codec import KernelError


class DomainCase(unittest.TestCase):
    """Reuse only the domain fixtures, not the domain test methods."""
    setUp = domain.DomainCase.setUp
    write = domain.DomainCase.write


COURSE = {"term_id": "fall", "course_id": "ethics", "name": "伦理学（新）", "kind": "elective", "credits": 3, "teacher_ref": "王老师"}


class CourseUpdateTests(DomainCase):
    def seed_two_slots(self):
        self.write("term", term())
        self.write("timetable", entry())
        self.write("timetable", entry(slot_id="ethics-wed", weekday=3))
        self.write("timetable", entry(slot_id="logic-tue", weekday=2, course={"course_id": "logic", "name": "逻辑学",
            "kind": "required", "credits": 2, "teacher_ref": None}))

    def course_update(self, record=COURSE, *, action="update", record_id=None):
        return self.write("course", deepcopy(record), action=action, record_id=record_id or record["course_id"])

    def slots(self):
        return {item["slot_id"]: item for item in self.academics.read(self.owner, self.context, "timetable", {"term_id": "fall"})["entries"]}

    def sessions(self):
        return {item["session_id"]: item for item in json.loads(json.dumps(
            self.academics.read(self.owner, self.context, "occurrences", {"term_id": "fall", "start": "2026-11-16", "end": "2026-11-30"})["items"]))}

    def test_rewrites_all_slots_of_the_course_in_one_revision_and_keeps_the_id(self):
        self.seed_two_slots()
        before, sessions = self.revision, self.sessions()
        receipt = self.course_update()
        self.assertEqual(receipt, {"schema_version": 1, "revision": before + 1, "kind": "course", "id": "ethics", "deleted": False})
        slots = self.slots()
        expected = {key: COURSE[key] for key in ("course_id", "name", "kind", "credits", "teacher_ref")}
        for slot_id in ("ethics-mon", "ethics-wed"):
            self.assertEqual(slots[slot_id]["course"], expected)
            self.assertEqual(slots[slot_id]["revision"], before + 1)
        self.assertEqual(slots["logic-tue"]["course"]["name"], "逻辑学", "其他课程不动")
        self.assertLess(slots["logic-tue"]["revision"], before + 1)
        after = self.sessions()
        changed = [sid for sid, item in after.items() if item["course_id"] == "ethics" and item["end_at"] > NOW.isoformat()]
        self.assertTrue(changed)
        for session_id in changed:
            self.assertEqual(after[session_id]["course_name"], "伦理学（新）")
            self.assertEqual(after[session_id]["course_id"], "ethics", "课程编号不变")
            self.assertEqual(after[session_id]["revision"], sessions[session_id]["revision"] + 1, "课次换新版本，旧提醒作废")

    def test_invalid_metadata_changes_nothing(self):
        self.seed_two_slots()
        before, slots = self.revision, self.slots()
        with self.assertRaisesRegex(KernelError, "credits"):
            self.course_update({**COURSE, "credits": 101})
        self.assertEqual(self.slots(), slots)
        self.assertEqual(self.academics.read(self.owner, self.context, "term", {})["revision"], before)

    def test_update_only_exact_fields_existing_course_and_revision_fence(self):
        self.seed_two_slots()
        for action in ("create", "delete"):
            with self.subTest(action=action), self.assertRaisesRegex(KernelError, "courses can only be updated"):
                self.course_update(action=action)
        with self.assertRaisesRegex(KernelError, "course update"):
            self.course_update({**COURSE, "slot_id": "ethics-mon"})
        with self.assertRaisesRegex(KernelError, "record id mismatch"):
            self.course_update(record_id="logic")
        for record in ({**COURSE, "course_id": "missing"}, {**COURSE, "term_id": "spring"}):
            with self.subTest(record=record), self.assertRaises(KernelError) as caught:
                self.course_update(record)
            self.assertEqual(caught.exception.code, "stale_version")
        self.revision -= 1
        with self.assertRaises(KernelError) as caught:
            self.course_update()
        self.assertEqual(caught.exception.code, "stale_version")
