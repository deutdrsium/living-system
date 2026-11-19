"""W6 study rules, durable cursor and the real artifact ledger."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_academics_domain as domain
import test_study as fixtures
from test_kernel_support import Access, NOW, SimulationClock
from good_life.academics import Academics, AcademicsAccess, AcademicsConfig, register_academics
from good_life.activities.study_work import allocate_window, generate_v1
from good_life.contracts.models import AudienceBound, ReasonCode, SourceRef
from good_life.contracts.ports import AuthorizedMaterial
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig
from good_life.kernel.codec import KernelError, wire
from good_life.storage import LifeStorage


class Arrived:
    async def arrived(self, owner_key, context_key, cursor):
        return cursor["step"] == "walk"


class StudyArtifactsTests(unittest.TestCase):
    write, seed = domain.DomainCase.write, domain.DomainCase.seed
    put = fixtures.StudyCase.put

    def setUp(self):
        domain.DomainCase.setUp(self)
        self.session = self.seed()
        self.put("syllabus", fixtures.syllabus(questions=["比较两组读者的理由。\n需要哪些证据？"]))
        self.put("assignment", fixtures.assignment(chunk_work_s=60, estimated_work_s=120))
        self.assertIsNone(self.storage._connection.execute(
            "SELECT value FROM life_metadata WHERE key='academics_study_work_schema_version'").fetchone())
        self.enable()

    def enable(self):
        self.academics = Academics(config=AcademicsConfig(enabled=True, study_enabled=True))
        self.academics.install(self.storage)
        self.study = self.academics.study_artifacts

    def plan(self, window_s=100, **options):
        return self.study.plan(self.owner, self.context, window_s=window_s, **options)

    def advance(self, cursor=None, *, work_s=0, allocated_s=60, **options):
        cursor = cursor or self.plan()["cursor"]
        return asyncio.run(self.study.advance(self.owner, self.context, cursor["assignment_id"], cursor["chunk_index"],
            expected_revision=cursor["revision"], work_s=work_s, allocated_s=allocated_s, now=NOW, **options))

    def read(self, kind="study_artifacts", *, owner=None, context=None, ports=None, **query):
        return asyncio.run(self.study.read(owner or self.owner, context or self.context, kind,
            query or {"assignment_id": "essay"}, ports=ports))

    def progress(self):
        return self.academics.read(self.owner, self.context, "study_progress", {"term_id": "fall", "course_id": "ethics"})["items"]

    def test_two_deadlines_overhead_and_zero_windows(self):
        self.put("assignment", fixtures.assignment(assignment_id="a-later", due_at="2026-11-22T08:00:00Z",
                                                   chunk_work_s=60, estimated_work_s=60))
        plan = self.plan(180, travel_s=25, departure_s=15)
        self.assertEqual([(item["assignment_id"], item["chunk_index"], item["allocated_s"], item["shortfall_s"])
                          for item in plan["items"]], [("essay", 0, 60, 0), ("essay", 1, 60, 0), ("a-later", 0, 20, 40)])
        self.assertEqual(plan["shortfall_s"], 40)
        self.assertEqual(sum(plan[key] for key in ("allocated_s", "travel_reserved_s", "departure_reserved_s", "unused_s")), 180)
        zero = self.plan(0, travel_s=20, departure_s=5)
        self.assertTrue(zero["zero_window"])
        self.assertEqual((zero["overhead_shortfall_s"], zero["shortfall_s"], zero["allocated_s"]), (25, 180, 0))
        exhausted = self.plan(22, travel_s=20, departure_s=5)
        self.assertFalse(exhausted["zero_window"])
        self.assertTrue(exhausted["zero_work_window"])
        self.assertEqual(exhausted["overhead_shortfall_s"], 3)
        self.assertEqual(self.advance()["step"], "walk")

    def test_allocator_ties_and_utc_deadline_order(self):
        tied = [{"assignment_id": assn, "chunk_index": idx, "due_at": due_at, "remaining_work_s": 10}
                for assn, idx, due_at in (("b", 0, "2026-11-21T06:00:00Z"), ("a", 1, "2026-11-21T14:00:00+08:00"),
                                          ("a", 0, "2026-11-21T06:00:00Z"), ("z", 0, "2026-11-21T07:00:00+02:00"))]
        result = allocate_window(tied, window_s=35)
        self.assertEqual([(i["assignment_id"], i["chunk_index"], i["allocated_s"]) for i in result["items"]],
                         [("z", 0, 10), ("a", 0, 10), ("a", 1, 10), ("b", 0, 5)])

    def test_restart_continues_work_and_checkpoint_on_same_chunk(self):
        walk = self.advance(walk=Arrived())
        self.assertEqual(self.advance(walk, work_s=500, allocated_s=0)["revision"], walk["revision"])
        partial = self.advance(walk, work_s=500, allocated_s=13)
        self.assertEqual(partial["used_s"], 13)
        self.assertEqual(self.progress()[0]["completed_work_s"], 0)
        self.assertEqual(self.plan()["items"][0]["allocated_s"], 47)
        self.storage.close()
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)
        self.enable()
        cursor = self.plan()["cursor"]
        self.assertEqual(cursor, {key: value for key, value in partial.items() if key != "used_s"})
        self.assertEqual((cursor["assignment_id"], cursor["chunk_index"], cursor["step"]), ("essay", 0, "work"))
        with self.assertRaisesRegex(KernelError, "cursor changed"):
            self.advance(walk, work_s=7)
        checkpoint = self.advance(cursor, work_s=500, allocated_s=50)
        self.assertEqual((checkpoint["used_s"], checkpoint["step"], self.progress()[0]["completed_work_s"]), (47, "checkpoint", 60))
        self.enable()
        cursor = self.plan()["cursor"]
        self.assertEqual((cursor["chunk_index"], cursor["step"]), (0, "checkpoint"))
        self.assertEqual(self.advance()["step"], "done")
        self.assertEqual((self.plan()["cursor"]["chunk_index"], self.plan()["cursor"]["step"]), (1, "walk"))

    def test_legacy_completion_after_restart_preserves_pending_artifacts(self):
        walk = self.advance(walk=Arrived())
        partial = self.advance(walk, work_s=21)
        self.assertEqual(self.progress()[0]["completed_work_s"], 20)
        self.assertEqual([item["kind"] for item in self.read()["items"]], ["draft"])

        self.storage.close()
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)
        self.academics = Academics(config=AcademicsConfig(enabled=True))
        self.academics.install(self.storage)
        host = Access()
        self.scope = host.scope = replace(host.scope, owner_key=self.owner, context_key=self.context)
        host.inventory = (self.scope,)
        registry = ComponentRegistry()
        register_academics(registry, self.academics)
        self.kernel = ActivityKernel(self.storage, registry, config=KernelConfig(enabled=True),
                                     access=AcademicsAccess(host, self.academics, registry), clock=SimulationClock())
        self.addCleanup(self.kernel.close)
        legacy = fixtures.StudyCase.study(self, "legacy-after-restart", session_s=40)
        self.assertEqual(legacy.target_ref.id, "essay:0")
        fixtures.StudyCase.advance(self, legacy, 40)
        self.assertEqual(self.progress()[0]["completed_work_s"], 60)

        self.enable()
        plan = self.plan()
        self.assertEqual((plan["cursor"]["chunk_id"], plan["cursor"]["step"], plan["cursor"]["revision"]),
                         ("essay:0", "work", partial["revision"]))
        self.assertEqual(plan["items"][0]["allocated_s"], 39)
        checkpoint = self.advance(plan["cursor"], work_s=39, allocated_s=39)
        self.assertEqual((checkpoint["step"], checkpoint["used_s"]), ("checkpoint", 39))
        self.assertCountEqual([item["kind"] for item in self.read()["items"]], ["draft", "note", "review"])
        self.assertEqual(self.progress()[0]["completed_work_s"], 60)
        self.assertEqual(self.advance(checkpoint)["step"], "done")
        self.assertEqual(self.plan()["cursor"]["chunk_id"], "essay:1")

    def test_resume_specific_cursor_after_deadline_order_changes(self):
        walk = self.advance(walk=Arrived())
        saved = self.advance(walk, work_s=21)
        self.put("assignment", fixtures.assignment(assignment_id="earlier", due_at="2026-11-19T08:00:00Z"))
        self.assertEqual(self.plan()["cursor"]["assignment_id"], "earlier")
        resumed = self.study.cursor(self.owner, self.context, "essay", 0)
        self.assertEqual((resumed["step"], resumed["revision"], resumed["elapsed_work_s"]), ("work", saved["revision"], 21))
        legacy = Academics(config=AcademicsConfig(enabled=True))
        legacy.install(self.storage)
        self.assertFalse(hasattr(legacy, "study_artifacts"))
        progress = legacy.read(self.owner, self.context, "study_progress", {"term_id": "fall", "course_id": "ethics"})
        essay = next(item for item in progress["items"] if item["chunk_id"] == "essay:0")
        self.assertEqual(essay["completed_work_s"], 20)

    def test_generation_rollback_keeps_artifacts_cursor_and_progress_atomic(self):
        walk = self.advance(walk=Arrived())
        with self.storage.transaction() as db:
            db.execute("""CREATE TEMP TRIGGER reject_study_progress BEFORE UPDATE ON life_study_chunks
                       BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test rollback"):
            self.advance(walk, work_s=20)
        self.assertEqual(self.read()["items"], [])
        self.assertEqual(self.progress()[0]["completed_work_s"], 0)
        self.assertEqual(self.plan()["cursor"]["revision"], walk["revision"])

    def test_withdrawal_before_generation_commit_and_read_keeps_original_step(self):
        ref = SourceRef("book", "v1", None, 0, 7, NOW, None, "private-ref")
        bound = AudienceBound("scope-permission", (self.context,))
        self.put("syllabus", fixtures.syllabus(materials=[{"source_ref": wire(ref), "audience_bound": wire(bound)}]), action="update")
        case = self

        class Ports:
            calls, allowed_through = 0, 0

            async def resolve_authority(self, request):
                return SimpleNamespace(owner_key=case.owner, context_key=case.context)

            async def read_material(self, request):
                case.assertFalse(case.storage._connection.in_transaction)
                self.calls += 1
                if self.calls > self.allowed_through:
                    raise KernelError(ReasonCode.SOURCE_UNAVAILABLE, "withdrawn")
                return AuthorizedMaterial(ref, "PRIVATE SOURCE TEXT", bound)

        ports = Ports()
        walk = self.advance(walk=Arrived())
        for allowed_through in (0, 2):  # Reject the first check, then reject the check after generation.
            ports.allowed_through = allowed_through
            with patch("good_life.academics.study_artifacts.generate_v1", wraps=generate_v1) as generate:
                with self.assertRaisesRegex(KernelError, "withdrawn"):
                    self.advance(walk, work_s=20, ports=ports)
                self.assertEqual(generate.call_count, int(allowed_through != 0))
            self.assertEqual(self.plan()["cursor"]["revision"], walk["revision"])
            self.assertEqual(self.progress()[0]["completed_work_s"], 0)
        ports.allowed_through = ports.calls + 3
        checkpoint = self.advance(walk, work_s=60, ports=ports)
        listing = self.read(ports=ports)
        self.assertTrue(all(item["availability"] == "available" for item in listing["items"]))
        artifact_id = listing["items"][0]["artifact_id"]
        blocked = self.read("study_artifact", artifact_id=artifact_id, ports=ports)
        self.assertEqual((blocked["availability"], blocked["text"], blocked["next_step"]), ("blocked", None, None))
        with self.assertRaisesRegex(KernelError, "withdrawn"):
            self.advance(checkpoint, ports=ports)
        self.assertEqual(self.plan()["cursor"]["step"], "checkpoint")
        ports.allowed_through = ports.calls + 2
        detail = self.read("study_artifact", artifact_id=artifact_id, ports=ports)
        self.assertIn("虚构材料", detail["text"])
        self.assertNotIn("PRIVATE SOURCE TEXT", detail["text"])
        self.assertEqual(self.advance(checkpoint, ports=ports)["step"], "done")

    def test_followups_are_idempotent_across_attendance_delivery_and_reopen(self):
        payload = {"schema_version": 1, "session_id": self.session["session_id"], "action": "borrow_notes",
                   "assignment_id": "essay", "extra_work_s": 600, "provenance": "simulated"}
        first = self.study.consume_attendance(self.owner, self.context, payload, now=NOW)
        self.assertEqual([(item["kind"], item["extra_work_s"]) for item in first], [("borrow_notes", 0), ("extra_load", 600)])
        self.enable()
        second = self.study.consume_attendance(self.owner, self.context, payload, now=NOW + timedelta(seconds=10))
        self.assertEqual(first, second)
        other = self.study.accept_followup(self.owner, self.context, session_id=payload["session_id"], kind="catch_up_notes", now=NOW)
        self.assertNotIn(other["followup_id"], {item["followup_id"] for item in first})
        self.assertEqual(self.storage._connection.execute("SELECT COUNT(*) FROM life_study_work_followups").fetchone()[0], 3)
