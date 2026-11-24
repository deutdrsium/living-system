"""Deterministic term grades from materialized class and study history."""
from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from test_academics_domain import entry, term
from test_kernel_support import NOW
from test_study import assignment, syllabus

from good_life.academics import Academics, AcademicsConfig
from good_life.academics.grades import EVENT_KIND, RULE_VERSION, VERSION_KEY, letter_grade, release_time
from good_life.academics.models import expand
from good_life.academics.service import timestamp
from good_life.event_delivery import ACADEMIC_NOTICES
from good_life.kernel.codec import encode
from good_life.storage import LifeStorage


@pytest.mark.parametrize("score,letter", [(100, "A"), (90, "A"), (89, "A-"), (85, "A-"),
    (84, "B+"), (82, "B+"), (81, "B"), (78, "B"), (77, "B-"), (75, "B-"), (74, "C+"),
    (71, "C+"), (70, "C"), (66, "C"), (65, "C-"), (62, "C-"), (61, "D"), (60, "D"),
    (59, "F"), (50, "F")])
def test_fudan_letter_grade_boundaries(score, letter):
    assert letter_grade(score) == letter


def test_release_is_stable_and_within_the_daytime_window():
    for tz in (ZoneInfo("Asia/Shanghai"), ZoneInfo("America/New_York")):
        for hour in (0, 12, 23):
            assessment = datetime(2026, 3, 1, hour, 30, tzinfo=tz)
            local_days = set()
            for i in range(300):
                key = ("owner", "fall", f"course-{i}")
                release = release_time(*key, assessment, tz)
                assert release == release_time(*key, assessment, tz)
                assert timedelta(days=7) <= release - assessment <= timedelta(days=21)
                local = release.astimezone(tz)
                assert 10 <= local.hour <= 18 and local.minute == 0
                local_days.add((local.date() - assessment.date()).days)
            assert min(local_days) in (7, 8) and max(local_days) in (20, 21)


class GradeWorkspace:
    owner = "grade-owner"
    context = "private-context"

    def __init__(self, path):
        self.path = path
        self.revision = 0
        self.access = SimpleNamespace(scopes=lambda: (SimpleNamespace(owner_key=self.owner, context_key=self.context),),
            authorize=lambda *_: True, write_guard=lambda *_: nullcontext())
        self.open()

    def open(self):
        self.storage = LifeStorage.open(self.path)
        self.academics = Academics(config=AcademicsConfig(enabled=True, grades_enabled=True))
        self.academics.install(self.storage)

    def put(self, kind, record, *, action="create"):
        key = {"term": "term_id", "timetable": "slot_id", "assignment": "assignment_id", "syllabus": "unit_id"}[kind]
        result = self.academics.command(self.owner, self.context, {"command_id": f"grade-{self.revision}",
            "expected_revision": self.revision, "action": action, "kind": kind, "id": record[key],
            "record": record}, now=NOW)
        self.revision = result["revision"]

    def tick(self, now):
        self.academics.term_grades.tick(now=now, get_access=lambda: self.access)

    def grades(self):
        return self.academics.read(self.owner, self.context, "grades", {"term_id": "fall"})["grades"]

    def events(self):
        return self.storage.pending_events(ACADEMIC_NOTICES, context_key=self.context)

    def release(self, assessment):
        return release_time(self.owner, "fall", "ethics", timestamp(assessment), ZoneInfo("Asia/Shanghai"))


@pytest.fixture
def workspace(tmp_path):
    workspace = GradeWorkspace(tmp_path)
    yield workspace
    workspace.storage.close()


def test_latest_assessment_attendance_review_and_one_release_across_restart(workspace):
    school_term = term(week_starts=["2026-11-16", "2026-11-23", "2026-11-30", "2026-12-07", "2026-12-14"],
                       end_date="2026-12-21", academic_year="2026-2027 学年秋季学期")
    slot = entry(weeks=[1, 2, 3, 4, 5], exceptions=[{"date": "2026-12-14", "cancelled": True,
                                                   "place_id": None, "start_time": None, "end_time": None}])
    workspace.put("term", school_term)
    workspace.put("timetable", slot)
    workspace.put("syllabus", syllabus())
    workspace.put("assignment", assignment(due_at="2026-12-21T16:00:00+08:00"))
    final = assignment(assignment_id="final", title="期末论文", due_at="2026-12-21T07:30:00-01:00",
                       chunk_work_s=600, estimated_work_s=1500)
    workspace.put("assignment", final)
    sessions = expand(school_term, [slot])
    with workspace.storage.transaction() as db:
        for i, outcome in ((0, "on_time"), (1, "late"), (2, "absent"), (4, "on_time")):
            db.execute("INSERT INTO life_academic_participation VALUES (?,?,?,?)",
                       (workspace.owner, sessions[i]["session_id"], f"class-{i}", encode({"attendance": {"outcome": outcome}})))
        for assignment_id, index, work in (("essay", 0, 2400), ("final", 0, 600), ("final", 1, 300)):
            db.execute("INSERT INTO life_study_chunks VALUES (?,?,?,?,?)", (workspace.owner, f"{assignment_id}:{index}",
                assignment_id, 1, encode({"completed_work_s": work})))
        assert db.execute("SELECT value FROM life_metadata WHERE key=?", (VERSION_KEY,)).fetchone()[0] == "1"

    release = workspace.release(final["due_at"])
    workspace.tick(release - timedelta(seconds=1))
    assert workspace.grades() == [] and workspace.events() == ()
    with patch.object(workspace.storage, "record_event", side_effect=RuntimeError("write interrupted")):
        with pytest.raises(RuntimeError, match="write interrupted"):
            workspace.tick(release)
    assert workspace.grades() == [] and workspace.events() == ()

    workspace.tick(release)
    grade, = workspace.grades()
    assert grade["assessment_at"] == "2026-12-21T08:30:00Z"
    assert timestamp(grade["release_at"]) == release
    assert grade["assignment_id"] == "final"
    assert grade["provenance"] == "simulated" and grade["rule_version"] == RULE_VERSION
    assert grade["components"]["attendance"] == 0.5 and grade["components"]["review"] == 0.6
    luck = grade["components"]["luck"]
    assert 0 <= luck < 1
    assert grade["weights"] == {"attendance": 0.3, "review": 0.5, "luck": 0.2}
    assert grade["score"] == round(50 + 50 * (0.3 * 0.5 + 0.5 * 0.6 + 0.2 * luck))
    event, = workspace.events()
    assert event["kind"] == EVENT_KIND
    assert event["payload"]["notice"] == (
        f"【教务通知】2026-2027 学年秋季学期《伦理学》成绩已发布：{grade['score']}（{grade['letter']}）")

    workspace.storage.close()
    workspace.open()
    with workspace.storage.transaction() as db:
        db.execute("DELETE FROM life_academic_participation")
        db.execute("DELETE FROM life_study_chunks")
        workspace.storage.acknowledge(event_id=event["event_id"], consumer=ACADEMIC_NOTICES, acknowledged_at=release)
    with patch("good_life.academics.grades._draw", side_effect=AssertionError("persisted grade was redrawn")):
        workspace.tick(release + timedelta(minutes=1))
        workspace.tick(release + timedelta(days=30))
    assert workspace.grades() == [grade] and workspace.events() == ()
    with workspace.storage.transaction() as db:
        assert db.execute("SELECT COUNT(*) FROM life_events WHERE kind=?", (EVENT_KIND,)).fetchone()[0] == 1


def test_class_end_wins_and_no_participation_or_assignment(workspace):
    school_term, slot = term(), entry()
    workspace.put("term", school_term)
    workspace.put("timetable", slot)
    last_class = expand(school_term, [slot])[-1]["end_at"]
    release = workspace.release(last_class)
    workspace.tick(release)
    grade, = workspace.grades()
    assert grade["assessment_at"] == last_class
    assert grade["assignment_id"] is None
    assert grade["components"] == {"attendance": 1.0, "luck": grade["components"]["luck"]}
    assert grade["weights"] == {"attendance": 0.6, "luck": 0.4}
    assert grade["score"] == round(50 + 50 * (0.6 + 0.4 * grade["components"]["luck"]))


def test_assignment_needs_no_timetable_occurrence(workspace):
    workspace.put("term", term(holidays=["2026-11-16", "2026-11-23"]))
    workspace.put("timetable", entry())
    workspace.put("syllabus", syllabus())
    latest = assignment(title="期末复习")
    workspace.put("assignment", latest)
    workspace.tick(workspace.release(latest["due_at"]))
    grade, = workspace.grades()
    assert grade["assessment_at"] == latest["due_at"]
    assert grade["components"]["attendance"] == 1.0 and grade["components"]["review"] == 0
