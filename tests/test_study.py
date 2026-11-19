"""A5 real kernel sessions, persisted chunks and A4 recovery regressions."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import unittest
import test_academics_lifecycle as classes
import test_academics_domain as domain
from test_kernel_support import NOW, SimulationClock
from good_life.activities.study import propose_study
from good_life.academics import AcademicsAccess, ClassActivities, register_academics
from good_life.contracts import models as m, ports as p
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig
from good_life.kernel.codec import KernelError, wire


def syllabus(**changes):
    return {"unit_id": "week1", "term_id": "fall", "course_id": "ethics", "week": 1,
        "topic": "论证", "concepts": ["理由"], "materials": [], "questions": [], "assignment_ids": [], "source": "manual", **changes}


def assignment(**changes):
    return {"assignment_id": "essay", "term_id": "fall", "course_id": "ethics", "unit_id": "week1", "title": "论证练习",
        "due_at": "2026-11-20T08:00:00Z", "chunk_work_s": 2400, "estimated_work_s": 5400, "source": "manual", **changes}


class StudyCase(unittest.TestCase):
    write, seed = domain.DomainCase.write, domain.DomainCase.seed
    advance, fresh_registry = classes.ClassLifecycle.advance, classes.ClassLifecycle.fresh_registry

    def setUp(self):
        classes.ClassLifecycle.setUp(self)
        self.put("syllabus", syllabus())
        self.put("assignment", assignment())

    def put(self, kind, record, action="create"):
        self.counter += 1
        result = self.academics.command(self.owner, self.context, {"command_id": "study-manual-" + str(self.counter),
            "expected_revision": self.revision, "action": action, "kind": kind,
            "id": record["unit_id" if kind == "syllabus" else "assignment_id"], "record": record}, now=NOW)
        self.revision = result["revision"]
        return result

    def study(self, opportunity, *, now=NOW, session_s=1500, ports=None, shadow=None):
        candidate = asyncio.run(propose_study(self.kernel, self.academics, self.scope, now=now,
            opportunity_key=opportunity, session_s=session_s, ports=ports, shadow_state=shadow))
        instance = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="study-prepare-" + opportunity, now=now).instance
        self.assertIsNotNone(instance)
        return self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision, command_id="study-start-" + opportunity, now=now)

    def rows(self):
        return self.academics.read(self.owner, self.context, "study_progress", {"term_id": "fall", "course_id": "ethics"})["items"]

    def test_chunking_and_second_study_continues_same_task(self):
        self.assertEqual([row["required_work_s"] for row in self.rows()], [2400, 2400, 600])
        first = self.study("one")
        final = self.advance(first, 1500, "first-final")
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertTrue(final.outcome.partial)
        second = self.study("two", now=NOW + timedelta(seconds=1500))
        self.assertNotEqual(first.instance_id, second.instance_id)
        self.assertEqual(first.target_ref.id, second.target_ref.id)
        self.assertEqual(second.checkpoint["params"]["target"]["chunk"]["completed_work_s"], 1500)
        final = self.advance(second, 2400, "second-final")
        self.assertEqual(self.rows()[0]["completed_work_s"], 2400)
        self.assertEqual(self.rows()[0]["status"], "completed")
        self.assertFalse(final.outcome.partial)
        third = self.study("three", now=NOW + timedelta(seconds=2400))
        self.assertEqual(third.target_ref.id, "essay:1")

    def test_cancel_and_new_session_keep_partial_progress(self):
        instance = self.study("interrupt")
        final = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="study-interrupt", now=NOW + timedelta(seconds=200))
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.rows()[0]["completed_work_s"], 200)
        next_instance = self.study("after-interrupt", now=NOW + timedelta(seconds=400))
        self.assertEqual(next_instance.target_ref.id, instance.target_ref.id)
        self.assertEqual(next_instance.checkpoint["params"]["target"]["chunk"]["completed_work_s"], 200)

    def test_pause_recovery_never_counts_offline_time(self):
        instance = self.study("crash")
        instance = self.advance(instance, 200, "study-progress")
        self.fresh_registry()
        resumed = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=1000))[0]
        self.assertEqual(self.rows()[0]["completed_work_s"], 200)
        instance = self.advance(resumed, 1100, "post-restart")
        self.assertEqual(self.rows()[0]["completed_work_s"], 300)
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="study-pause", now=NOW + timedelta(seconds=1200))
        self.assertEqual(paused.status, m.ActivityStatus.PAUSED)
        self.assertEqual(self.rows()[0]["completed_work_s"], 400)
        resumed = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
            command_id="study-resume", now=NOW + timedelta(seconds=1800))
        self.advance(resumed, 1900, "after-pause")
        self.assertEqual(self.rows()[0]["completed_work_s"], 500)

    def test_queued_study_cannot_borrow_paused_instances_claim(self):
        self.assert_queued_claim_isolated("queued-first", "queued-second")

    def test_same_opportunity_with_different_pacing_has_isolated_claims(self):
        self.assert_queued_claim_isolated("queued-same", "queued-same", second_session_s=600)

    def assert_queued_claim_isolated(self, first_key, second_key, second_session_s=1500):
        first, second = [asyncio.run(propose_study(self.kernel, self.academics, self.scope, now=NOW,
            opportunity_key=key, session_s=session_s)) for key, session_s in ((first_key, 1500), (second_key, second_session_s))]
        first_template = self.registry.templates[(first.template_ref.id, first.template_ref.version)]
        second_template = self.registry.templates[(second.template_ref.id, second.template_ref.version)]
        self.assertNotEqual(first_template.gating["resources"], second_template.gating["resources"])
        prepared = self.kernel.prepare_candidate(self.scope, first.candidate_id, command_id="queued-prepare-first", now=NOW)
        self.assertTrue(prepared.gate.allowed)
        instance = self.kernel.start(self.scope, prepared.instance.instance_id, expected_revision=prepared.instance.revision,
            command_id="queued-start-first", now=NOW)
        self.assertEqual(instance.status, m.ActivityStatus.RUNNING)
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="queued-pause-first", now=NOW + timedelta(seconds=200))
        result = self.kernel.prepare_candidate(self.scope, second.candidate_id, command_id="queued-prepare-second",
            now=NOW + timedelta(seconds=200))
        self.assertFalse(result.gate.allowed)
        self.assertIsNone(result.instance)
        self.assertEqual(self.kernel.repo.candidate(self.owner, second.candidate_id)[1].status, m.CandidateStatus.REJECTED)
        # Project the rejected template last too: another proposal must not
        # overwrite the paused owner's eligibility when it resumes.
        resumed = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
            command_id="queued-resume-first", now=NOW + timedelta(seconds=200))
        self.assertEqual(resumed.status, m.ActivityStatus.RUNNING)
        self.assertEqual(self.rows()[0]["completed_work_s"], 200)

    def test_pinned_study_without_opportunity_key_can_resume(self):
        from good_life.activities.study import study_template
        def legacy_template(target, **params):
            params.pop("opportunity_key")
            return study_template(target, **params)
        with patch("good_life.activities.study.study_template", side_effect=legacy_template):
            instance = self.study("legacy-template")
        self.assertNotIn("opportunity_key", instance.checkpoint["params"])
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="pause-legacy", now=NOW + timedelta(seconds=200))
        self.fresh_registry()
        resumed = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
            command_id="resume-legacy", now=NOW + timedelta(seconds=400))
        self.assertEqual(resumed.status, m.ActivityStatus.RUNNING)
        self.advance(resumed, 500, "legacy-progress")
        self.assertEqual(self.rows()[0]["completed_work_s"], 300)

    def test_paused_chunk_blocks_its_assignment_but_not_other_assignments(self):
        from good_life.academics.study import next_chunk
        instance = self.study("pause-assignment")
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="pause-assignment", now=NOW + timedelta(seconds=200))
        self.assertIsNone(next_chunk(self.academics, self.owner, self.context))
        self.put("assignment", assignment(assignment_id="later", due_at="2026-11-21T08:00:00Z"))
        other = self.study("other-assignment", now=NOW + timedelta(seconds=200))
        self.assertEqual(other.target_ref.id, "later:0")
        self.assertIsNone(next_chunk(self.academics, self.owner, self.context))
        self.kernel.cancel(self.scope, other.instance_id, expected_revision=other.revision,
            command_id="cancel-other-assignment", now=NOW + timedelta(seconds=200))
        resumed = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
            command_id="resume-assignment", now=NOW + timedelta(seconds=200))
        self.advance(resumed, 1500, "finish-paused-session")
        self.assertEqual(next_chunk(self.academics, self.owner, self.context)["chunk"]["chunk_id"], "essay:0")
        continued = self.study("complete-first-chunk", now=NOW + timedelta(seconds=1500))
        self.advance(continued, 2400, "complete-first-chunk")
        self.assertEqual(next_chunk(self.academics, self.owner, self.context)["chunk"]["chunk_id"], "essay:1")

    def test_terminal_crash_and_idempotent_replay(self):
        instance = self.study("terminal")
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.advance(instance, 1500, "study-final-crash")
        self.assertEqual(self.rows()[0]["completed_work_s"], 0)
        self.fresh_registry()
        recovered = self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=2000))[0]
        self.assertEqual(self.rows()[0]["completed_work_s"], 1500)
        self.assertEqual(self.advance(instance, 1500, "study-final-crash"), recovered)
        self.assertEqual(self.rows()[0]["completed_work_s"], 1500)
        self.assertEqual(self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=2000)), ())

    def test_abandon_only_releases_unsettled_work(self):
        instance = self.study("abandon-study")
        instance = self.advance(instance, 200, "known-work")
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.advance(instance, 1500, "unknown-local-work")
        pending = self.kernel.repo.instance(self.owner, instance.instance_id)
        self.kernel.abandon_result(self.scope, self.kernel.repo.pending_result(self.owner, instance.instance_id),
            expected_revision=pending.revision, command_id="abandon-study-result", reason="test", now=NOW + timedelta(seconds=1800))
        self.assertEqual(self.rows()[0]["completed_work_s"], 200)
        next_instance = self.study("after-abandon", now=NOW + timedelta(seconds=1800))
        self.assertEqual(next_instance.target_ref.id, instance.target_ref.id)

    def test_assignment_id_reserves_space_for_chunk_suffix(self):
        with self.assertRaises(KernelError):
            self.put("assignment", assignment(assignment_id="a" * 253))
        self.put("assignment", assignment(assignment_id="a" * 252, estimated_work_s=2400 * 128))
        from good_life.academics.study import chunks
        items = chunks(assignment(assignment_id="a" * 252, estimated_work_s=2400 * 128))
        self.assertEqual(len(items[-1]["chunk_id"]), 256)
        self.assertEqual(items[-1]["chunk_index"], 127)

    def test_manual_validation_revision_and_replay(self):
        for changes in ({"chunk_work_s": 0}, {"estimated_work_s": True}, {"estimated_work_s": 400000}, {"due_at": "tomorrow"}, {"unit_id": "missing"}, {"source": "scraped"}):
            with self.subTest(changes=changes), self.assertRaises((KernelError, ValueError)):
                self.put("assignment", assignment(assignment_id="bad", **changes))
        body = {"command_id": "replay-manual", "expected_revision": self.revision, "action": "create", "kind": "assignment", "id": "second", "record": assignment(assignment_id="second")}
        first = self.academics.command(self.owner, self.context, body, now=NOW)
        self.assertEqual(self.academics.command(self.owner, self.context, body, now=NOW), first)
        with self.assertRaises(KernelError) as error:
            self.academics.command(self.owner, self.context, {**body, "command_id": "stale"}, now=NOW)
        self.assertEqual(error.exception.code, "stale_version")

    def test_claimed_target_update_is_fenced(self):
        self.study("claimed")
        with self.assertRaises(KernelError) as error:
            self.put("assignment", assignment(title="changed"), action="update")
        self.assertEqual(error.exception.code, "stale_version")

    def test_studied_course_can_be_retired_and_links_can_grow_during_study(self):
        instance = self.study("retire")
        self.put("assignment", assignment(assignment_id="later"))
        self.put("syllabus", syllabus(assignment_ids=["essay", "later"]), action="update")
        self.fresh_registry()
        instance = self.kernel.recover(self.scope, now=NOW)[0]
        self.assertEqual(instance.status, m.ActivityStatus.RUNNING)
        final = self.advance(instance, 1500, "retire-finish")
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.put("assignment", assignment(title="revised title", due_at="2026-11-22T08:00:00Z"), action="update")
        self.assertEqual(next(row for row in self.rows() if row["assignment_id"] == "essay")["due_at"], "2026-11-22T08:00:00Z")
        self.put("syllabus", syllabus(), action="update")
        for kind, record_id in (("assignment", "essay"), ("assignment", "later"), ("syllabus", "week1"), ("timetable", "ethics-mon"), ("term", "fall")):
            self.counter += 1
            result = self.academics.command(self.owner, self.context, {"command_id": "retire-" + str(self.counter),
                "expected_revision": self.revision, "action": "delete", "kind": kind, "id": record_id}, now=NOW + timedelta(seconds=1500))
            self.revision = result["revision"]
        self.assertEqual(self.kernel.repo.pinned(self.owner, instance.instance_id)[0].params["target"]["assignment"]["title"], "论证练习")
        self.assertEqual(self.storage._connection.execute("SELECT COUNT(*) FROM life_study_chunks").fetchone()[0], 1)

    def test_material_preflight_uses_a3_permission_and_versions(self):
        ref = m.SourceRef("text", "v1", "hash", 0, 20, NOW, None, "permission-private")
        bound = self.host.audience(self.scope)
        self.put("syllabus", syllabus(materials=[{"source_ref": wire(ref), "audience_bound": wire(bound)}]), action="update")
        with self.assertRaises(KernelError) as error:
            self.study("without-port")
        self.assertEqual(error.exception.code, "missing_dependency")
        from dataclasses import replace
        port_scope = replace(self.scope, authority_ref="port-authority")
        class Ports:
            async def resolve_authority(inner, request):
                self.assertEqual(request.context_key, self.context)
                return port_scope
            async def read_material(inner, request):
                self.assertEqual(request.scope, port_scope)
                self.assertEqual(request.source_ref, ref)
                return p.AuthorizedMaterial(ref, "PRIVATE source text", bound)
        self.study("with-material", ports=Ports())
        self.assertNotIn("PRIVATE source text", self.storage._connection.execute("SELECT record_json FROM life_kernel_instances").fetchone()[0])

    def test_material_revision_and_revocation_fail_before_candidate_creation(self):
        from dataclasses import replace
        ref = m.SourceRef("text", "v1", "hash", 0, 20, NOW, None, "permission-private")
        bound = self.host.audience(self.scope)
        self.put("syllabus", syllabus(materials=[{"source_ref": wire(ref), "audience_bound": wire(bound)}]), action="update")
        class Changed:
            async def resolve_authority(inner, request):
                return self.scope
            async def read_material(inner, request):
                return p.AuthorizedMaterial(replace(ref, version="v2"), "changed", bound)
        with self.assertRaises(KernelError) as error:
            self.study("changed-material", ports=Changed())
        self.assertEqual(error.exception.code, "stale_version")
        class Revoked:
            async def resolve_authority(inner, request):
                return self.scope
            async def read_material(inner, request):
                raise KernelError(m.ReasonCode.UNAUTHORIZED, "material revoked")
        with self.assertRaises(KernelError) as error:
            self.study("revoked-material", ports=Revoked())
        self.assertEqual(error.exception.code, "unauthorized")
        class WrongOwner:
            async def resolve_authority(inner, request):
                return replace(self.scope, owner_key="another-owner")
            async def read_material(inner, request):
                self.fail("wrong owner must fail before material access")
        with self.assertRaises(KernelError) as error:
            self.study("wrong-material-owner", ports=WrongOwner())
        self.assertEqual(error.exception.code, "unauthorized")
        self.assertEqual(self.storage._connection.execute("SELECT COUNT(*) FROM life_kernel_candidates").fetchone()[0], 0)
        self.assertEqual(self.rows()[0]["completed_work_s"], 0)

    def test_shadow_energy_controls_load_and_evaluation(self):
        shadow = SimpleNamespace(config=SimpleNamespace(enabled=True, energy=True), snapshot=lambda _owner:
            m.StateSnapshot(self.scope.owner_key, NOW, m.Revision(1), {"energy": m.StateValue(0.5, NOW, m.Revision(1), (), m.StateQuality.VALID)}))
        instance = self.study("shadow", shadow=shadow)
        params = instance.checkpoint["params"]
        self.assertEqual((params["efficiency"], params["load"], params["evaluation"]), (500, 0.75, "shadow_energy_v1"))
        self.advance(instance, 1500, "shadow-progress")
        self.assertEqual(self.rows()[0]["completed_work_s"], 750)

    def test_real_a6_shadow_is_read_without_advancing_or_mutating_it(self):
        from test_state_model_support import StateModelCase
        state = StateModelCase()
        state.setUp()
        self.addCleanup(state.doCleanups)
        with state.storage.transaction() as db:
            state.add_role(db, self.owner)
        state.model.initialize(self.owner, at=NOW)
        before = state.model.snapshot(self.owner)
        instance = self.study("real-shadow", shadow=state.model)
        self.assertEqual(instance.checkpoint["params"]["efficiency"], 370)
        self.assertEqual(state.model.snapshot(self.owner), before)

    def test_disabled_shadow_uses_fixed_values_and_enabled_missing_state_rejects(self):
        def never(_owner):
            self.fail("disabled shadow must not be read")
        disabled = SimpleNamespace(config=SimpleNamespace(enabled=False, energy=False), snapshot=never)
        instance = self.study("shadow-off", shadow=disabled)
        self.assertEqual(instance.checkpoint["params"]["efficiency"], 1000)
        self.assertEqual(instance.checkpoint["params"]["evaluation"], "fixed_rule_v1")
        self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="off-cancel", now=NOW)
        missing = SimpleNamespace(config=SimpleNamespace(enabled=True, energy=True), snapshot=lambda _owner: None)
        with self.assertRaises(KernelError) as error:
            self.study("missing-shadow", shadow=missing)
        self.assertEqual(error.exception.code, "missing_dependency")

    def test_missing_dependency_stays_missing_without_candidate_growth(self):
        from dataclasses import replace
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        snapshot = self.access.snapshot
        def without_campus(scope, now):
            current = snapshot(scope, now)
            return replace(current, capabilities=tuple(item for item in current.capabilities if item != "campus"))
        with patch.object(self.access, "snapshot", side_effect=without_campus):
            for tick in range(5):
                self.assertEqual(coordinator.tick(now=self.depart + timedelta(seconds=tick))["started"], 0)
            rows = self.storage._connection.execute("SELECT candidate_id,status FROM life_kernel_candidates").fetchall()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][1], m.CandidateStatus.REJECTED.value)
            self.assertEqual(self.kernel.repo.candidate(self.owner, rows[0][0])[1].status, m.CandidateStatus.REJECTED)
        result = coordinator.tick(now=self.depart + timedelta(seconds=5))
        self.assertEqual(result["started"], 1)
        self.assertEqual(self.academics.participation(self.owner, self.context, self.session["session_id"])["status"], "active")
        self.assertEqual(self.storage._connection.execute("SELECT COUNT(*) FROM life_kernel_candidates").fetchone()[0], 2)
        self.assertEqual(coordinator.tick(now=self.depart + timedelta(seconds=6))["started"], 0)
        self.assertEqual(self.storage._connection.execute("SELECT COUNT(*) FROM life_kernel_candidates").fetchone()[0], 2)

    def test_a4_missing_template_rejection_can_be_reproposed(self):
        from good_life.activities.class_session import propose_class
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        self.fresh_registry()
        result = self.kernel.tick(now=NOW)
        self.assertIn("missing_dependency", result["failures"])
        self.assertEqual(self.kernel.repo.candidate(self.owner, candidate.candidate_id)[1].status, m.CandidateStatus.REJECTED)
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        result = coordinator.tick(now=self.depart)
        self.assertEqual(result["started"], 1)
        self.assertEqual(coordinator.tick(now=self.depart)["started"], 0)

    def test_gc_keeps_pending_candidates_and_nonterminal_instances(self):
        from good_life.activities.class_session import propose_class
        instance = self.study("gc-live")
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        coordinator._gc_templates()
        self.assertIn((instance.template_id, instance.template_version), self.registry.templates)
        self.assertIn((candidate.template_ref.id, candidate.template_ref.version), self.registry.templates)
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision, command_id="gc-pause", now=NOW)
        coordinator._gc_templates()
        self.assertIn((paused.template_id, paused.template_version), self.registry.templates)

    def test_terminal_dynamic_templates_collected_but_pins_retained(self):
        instance = self.study("gc")
        final = self.advance(instance, 1500, "gc-final")
        key = (instance.template_id, instance.template_version)
        self.assertIn(key, self.registry.templates)
        ClassActivities(self.kernel, self.academics, self.campus)._gc_templates()
        self.assertNotIn(key, self.registry.templates)
        self.assertEqual(self.kernel.repo.pinned(self.owner, final.instance_id)[0].id, instance.template_id)
        self.assertEqual(self.advance(instance, 1500, "gc-final"), final)


class StudyOff(unittest.TestCase):
    def test_runtime_keeps_timetable_only_host_bound_without_async_capabilities(self):
        from tempfile import TemporaryDirectory
        from test_life_runtime import Host
        from good_life.runtime import LifeRuntime
        from good_life.storage import LifeStorage
        from good_life.academics import AcademicsConfig
        class NoAsyncHost(Host):
            pass
        async def exercise(storage):
            runtime = LifeRuntime(storage, object(), academics_config=AcademicsConfig(enabled=True))
            await runtime.bind(NoAsyncHost())
            self.assertIsNone(runtime.study_ports)
            await runtime.close()
        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            try:
                asyncio.run(exercise(storage))
            finally:
                storage.close()

    def test_runtime_negotiates_material_ports_without_enabling_a3(self):
        from tempfile import TemporaryDirectory
        from test_life_runtime import Host
        from good_life.runtime import LifeRuntime
        from good_life.storage import LifeStorage
        from good_life.academics import AcademicsConfig
        class StudyHost(Host):
            def life_async_capabilities(inner, **request):
                self.assertEqual(request["contract_version"], 1)
                return {"api_version": "companion.life-host.async.v1", "contract_version": 1,
                    "capabilities": ("resolve_authority", "read_material"), "ports": inner}

            async def resolve_authority(self, request):
                raise AssertionError("binding must not resolve material authority")

            async def read_material(self, request):
                raise AssertionError("binding must not read study material")

        async def exercise(storage):
            runtime = LifeRuntime(storage, object(), academics_config=AcademicsConfig(enabled=True, study_enabled=True))
            host = StudyHost()
            await runtime.bind(host)
            self.assertIs(runtime.study_ports, host)
            self.assertFalse(runtime.reading_executor_enabled)
            await runtime.unbind()
            self.assertIsNone(runtime.study_ports)
            await runtime.close()
        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            try:
                asyncio.run(exercise(storage))
            finally:
                storage.close()

    def test_default_runtime_installs_no_academic_or_study_schema(self):
        from tempfile import TemporaryDirectory
        from good_life.runtime import LifeRuntime
        from good_life.storage import LifeStorage
        from good_life.academics import AcademicsConfig
        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            try:
                runtime = LifeRuntime(storage, object(), academics_config=AcademicsConfig())
                self.assertIsNone(runtime.academics)
                self.assertIsNone(runtime.kernel)
                self.assertIsNone(runtime.study_ports)
                tables = storage._connection.execute("SELECT name FROM sqlite_master WHERE name GLOB 'life_study_*' OR name GLOB 'life_academic_*'").fetchall()
                self.assertEqual(tables, [])
                with self.assertRaises(KernelError) as error:
                    asyncio.run(runtime.propose_study(None, opportunity_key="off", now=NOW))
                self.assertEqual(error.exception.code, "missing_dependency")
                asyncio.run(runtime.close())
            finally:
                storage.close()
