"""Regression scenarios from the A4 integration review."""
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch

import test_academics_domain as domain
import test_academics_lifecycle as life
from test_kernel_support import NOW
from good_life.academics import AcademicsAccess, Campus, ClassActivities, register_academics
from good_life.activities.class_session import propose_class
from good_life.activities.local_order import register_local_order
from good_life.contracts.models import ActivityStatus, CandidateStatus, ContextKey, TemplateRef
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig
from good_life.kernel.codec import KernelError
from test_kernel_support import SimulationClock


class ClassReview(unittest.TestCase):
    setUp = life.ClassLifecycle.setUp
    write = life.ClassLifecycle.write
    seed = life.ClassLifecycle.seed
    start = life.ClassLifecycle.start
    advance = life.ClassLifecycle.advance
    participation = life.ClassLifecycle.participation
    reopen = life.ClassLifecycle.reopen

    def test_same_place_revision_and_changed_graph_do_not_strand_arrival(self):
        instance = self.start()
        self.campus.locate(self.owner, "dorm", expected_revision=1)
        # Already pinned travel must retain the original 300-second quote.
        self.campus.paths["gate", "classroom"] = 900
        final = self.advance(instance, 1200)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.campus.location(self.owner), ("classroom", 3))
        self.assertEqual(self.participation()["attended_s"], 600)

    def test_same_place_revision_allows_legacy_recovery(self):
        instance = self.start()
        self.campus.locate(self.owner, "dorm", expected_revision=1)
        self.reopen()
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator.tick(now=self.depart + timedelta(seconds=30))["failures"], ())
        resumed = self.kernel.repo.instance(self.owner, instance.instance_id)
        self.assertEqual(resumed.status, ActivityStatus.RUNNING)
        self.assertEqual(resumed.checkpoint["location_revision"], 1)
        final = self.advance(resumed, 1200)
        self.assertEqual(final.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.campus.location(self.owner), ("classroom", 3))

    def test_changed_origin_has_explicit_cancel_exit(self):
        instance = self.start()
        self.campus.locate(self.owner, "other-room", expected_revision=1)
        result = ClassActivities(self.kernel, self.academics, self.campus).tick(now=NOW + timedelta(seconds=800))
        self.assertEqual(result["failures"], ())
        self.assertEqual(self.kernel.repo.instance(self.owner, instance.instance_id).status, ActivityStatus.CANCELLED)
        self.assertEqual(self.campus.location(self.owner)[0], "other-room")
        self.assertEqual(self.participation()["attended_s"], 0)
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))

    def test_pending_arrival_conflict_abandons_only_uncommitted_proposal(self):
        instance = self.start()
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.advance(instance, 1200)
        self.campus.locate(self.owner, "other-room", expected_revision=1)
        result = ClassActivities(self.kernel, self.academics, self.campus).tick(now=NOW + timedelta(seconds=1300))
        self.assertEqual(result["failures"], ())
        self.assertEqual(self.kernel.repo.instance(self.owner, instance.instance_id).status, ActivityStatus.FAILED)
        self.assertEqual(self.participation()["status"], "abandoned")
        self.assertEqual(self.participation()["attended_s"], 0)

    def test_missing_dynamic_candidate_is_rejected_without_blocking_other_activity(self):
        candidate = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        self.kernel.close()
        self.registry = ComponentRegistry()
        register_academics(self.registry, self.academics, campus=self.campus)
        local = register_local_order(self.registry)
        self.host.capabilities = ("local_order",)
        self.access = AcademicsAccess(self.host, self.academics, self.registry, self.campus)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access, clock=SimulationClock())
        good = self.kernel.propose(self.scope, TemplateRef(local.id, local.version), opportunity_key="other", reason="tidy_requested", now=NOW + timedelta(seconds=1), expires_at=NOW + timedelta(hours=1))
        with patch.object(self.kernel.repo, "pending_candidates", return_value=(candidate, good)):
            result = self.kernel.tick(now=NOW + timedelta(seconds=2))
        self.assertEqual(result["started"], 1)
        self.assertIn("missing_dependency", result["failures"])
        self.assertEqual(self.kernel.repo.candidate(self.owner, candidate.candidate_id)[1].status, CandidateStatus.REJECTED)

    def test_midclass_recovery_gates_actual_wall_time_remaining(self):
        instance = self.start()
        instance = self.advance(instance, 700)
        self.reopen()
        self.host.free_s = 300  # End at 1200, recover at 900; online-plan remainder was 500.
        resumed = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=900))[0]
        self.assertEqual(resumed.status, ActivityStatus.RUNNING)
        self.assertEqual(self.advance(resumed, 1200).status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["attended_s"], 400)

    def test_recovery_time_window_pause_resumes_before_class_end(self):
        instance = self.start()
        instance = self.advance(instance, 700)
        self.reopen()
        self.host.free_s = 100
        paused = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=900))[0]
        self.assertEqual(paused.status, ActivityStatus.PAUSED)
        self.host.free_s = 200
        result = ClassActivities(self.kernel, self.academics, self.campus).tick(now=NOW + timedelta(seconds=1000))
        self.assertEqual(result["failures"], ())
        resumed = self.kernel.repo.instance(self.owner, instance.instance_id)
        self.assertEqual(resumed.status, ActivityStatus.RUNNING)
        self.assertEqual(self.advance(resumed, 1200).status, ActivityStatus.COMPLETED)
        self.assertEqual(self.participation()["attended_s"], 300)

    def test_sweep_snapshot_failure_keeps_wrapped_tick_and_other_owner_running(self):
        instance = self.advance(self.start(), 700)
        self.reopen()
        self.host.free_s = 60
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        coordinator.tick(now=NOW + timedelta(seconds=800))
        self.assertEqual(self.kernel.repo.instance(self.owner, instance.instance_id).status, ActivityStatus.PAUSED)
        other = replace(self.scope, owner_key="zz-another-owner", context_key=ContextKey("another-context"))
        self.host.inventory = (self.scope, other)
        self.host.capabilities = ("local_order",)
        self.host.free_s = 3600
        template = register_local_order(self.registry)
        self.kernel.propose(other, TemplateRef(template.id, template.version), opportunity_key="other-owner",
            reason="tidy_requested", now=NOW + timedelta(seconds=810), expires_at=NOW + timedelta(hours=1))
        original = self.host.snapshot

        def flaky(scope, now):
            if scope.owner_key == self.owner:
                raise RuntimeError("private provider details")
            return original(scope, now)

        with patch.object(self.host, "snapshot", side_effect=flaky), patch.object(
                self.kernel, "tick", wraps=self.kernel.tick) as wrapped:
            result = coordinator.tick(now=NOW + timedelta(seconds=860))
        wrapped.assert_called_once()
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["failures"], ("unexpected_error:RuntimeError",))
        self.assertEqual(self.kernel.repo.instance(self.owner, instance.instance_id).status, ActivityStatus.PAUSED)

    def test_waiting_window_does_not_append_denied_start_receipts(self):
        instance = self.advance(self.start(), 700)
        self.reopen()
        self.host.free_s = 60
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        coordinator.tick(now=NOW + timedelta(seconds=800))
        db = self.storage._connection
        counts = lambda: tuple(db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
            for table in ("life_kernel_gates", "life_kernel_commands"))
        before = counts()
        with patch.object(self.kernel, "start", wraps=self.kernel.start) as start:
            for seconds in range(830, 1140, 30):
                self.assertEqual(coordinator.tick(now=NOW + timedelta(seconds=seconds))["failures"], ())
            start.assert_not_called()
        self.assertEqual(counts(), before)
        self.assertEqual(self.kernel.repo.instance(self.owner, instance.instance_id).status, ActivityStatus.PAUSED)
        coordinator.tick(now=NOW + timedelta(seconds=1140))
        self.assertEqual(self.kernel.repo.instance(self.owner, instance.instance_id).status, ActivityStatus.RUNNING)
        coordinator.tick(now=NOW + timedelta(seconds=1201))
        self.assertEqual(self.kernel.repo.instance(self.owner, instance.instance_id).status, ActivityStatus.COMPLETED)
        self.assertNotEqual(self.participation()["status"], "active")

    def _ended_busy(self, paused):
        instance = self.start()
        if paused:
            instance = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
                command_id="pause-ended", now=NOW + timedelta(seconds=700))
        self.reopen()
        self.host.free_s, self.host.busy = 0, True
        result = ClassActivities(self.kernel, self.academics, self.campus).tick(now=NOW + timedelta(seconds=1300))
        self.assertEqual(result["failures"], ())
        self.assertIn(self.kernel.repo.instance(self.owner, instance.instance_id).status,
                      (ActivityStatus.COMPLETED, ActivityStatus.CANCELLED))
        self.assertNotEqual(self.participation()["status"], "active")
        self.assertFalse(self.kernel.repo.foreground_busy(self.owner))

    def test_ended_running_closes_even_when_host_is_busy(self):
        self._ended_busy(False)

    def test_ended_paused_closes_even_when_host_is_busy(self):
        self._ended_busy(True)

    def test_rejected_class_does_not_hide_the_next_due_class(self):
        self.write("timetable", domain.entry(slot_id="second", start_time="16:20", end_time="16:30"))
        self.campus.paths["gate", "classroom"] = 1100
        first = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        self.host.busy = True
        self.kernel.prepare_candidate(self.scope, first.candidate_id, command_id="reject-first", now=NOW)
        self.host.busy = False
        result = ClassActivities(self.kernel, self.academics, self.campus).tick(now=NOW)
        self.assertEqual(result["started"], 1)
        active = self.kernel.repo.active(self.owner)[0]
        self.assertEqual(active.checkpoint["session"]["slot_id"], "second")

    def test_templates_are_indexed_per_owner_even_with_same_session_ids(self):
        from good_life.activities.class_session import eligibility_key
        other = replace(self.scope, owner_key="another-owner", context_key=ContextKey("another-context"))
        self.host.inventory = (*self.host.inventory, other)
        self.campus.locate(other.owner_key, "dorm")
        for revision, kind, record in ((0, "term", domain.term()), (1, "timetable", domain.entry(place_id="other-room"))):
            self.academics.command(other.owner_key, other.context_key, {"command_id": "other-" + kind,
                "expected_revision": revision, "action": "create", "kind": kind,
                "id": record["term_id" if kind == "term" else "slot_id"], "record": record}, now=NOW)
        own = propose_class(self.kernel, self.academics, self.scope, self.session["session_id"], now=NOW, campus=self.campus)
        other_candidate = propose_class(self.kernel, self.academics, other, self.session["session_id"], now=NOW, campus=self.campus)
        self.assertNotEqual(own.template_ref, other_candidate.template_ref)
        self.assertEqual(self.access.snapshot(self.scope, NOW).state.values[eligibility_key(self.session)].value, 1)
        self.assertEqual(self.access.snapshot(other, NOW).state.values[eligibility_key(self.session)].value, 1)
        self.assertEqual(len(self.academics._template_refs[(self.owner, self.context)]), 1)

    def test_unknown_origin_is_quiet_and_does_not_create_candidates(self):
        with self.storage.transaction() as db:
            db.execute("DELETE FROM life_campus_locations WHERE owner_key=?", (self.owner,))
        coordinator = ClassActivities(self.kernel, self.academics, self.campus)
        self.assertEqual(coordinator.tick(now=self.depart)["failures"], ())
        self.assertEqual(coordinator.tick(now=self.depart)["started"], 0)
        self.assertEqual(self.kernel.repo.pending_candidates(self.owner), ())

    def test_other_context_keeps_host_snapshot_and_can_run_other_domain(self):
        other = replace(self.scope, context_key=ContextKey("another-private-context"))
        self.host.inventory = (*self.host.inventory, other)
        self.host.capabilities = ("local_order",)
        template = register_local_order(self.registry)
        snapshot = self.access.snapshot(other, NOW)
        self.assertEqual(snapshot, self.host.snapshot(other, NOW))
        self.kernel.propose(other, TemplateRef(template.id, template.version), opportunity_key="other-context", reason="tidy_requested", now=NOW, expires_at=NOW + timedelta(hours=1))
        result = ClassActivities(self.kernel, self.academics, self.campus).tick(now=NOW)
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["failures"], ())


class ManualReview(unittest.TestCase):
    setUp = domain.DomainCase.setUp
    write = domain.DomainCase.write
    seed = domain.DomainCase.seed

    def test_midterm_slot_replacement_does_not_invent_overlapping_history(self):
        original = self.seed()
        now = NOW + timedelta(days=3)
        self.write("timetable", action="delete", record_id="ethics-mon", now=now)
        self.write("timetable", domain.entry(slot_id="replacement"), now=now)
        rows = self.academics.read(self.owner, self.context, "occurrences", {"term_id": "fall", "start": "2026-11-16", "end": "2026-11-30"})["items"]
        self.assertEqual(len([item for item in rows if not item["cancelled"]]), 2)
        self.assertEqual(len([item for item in rows if item["local_date"] == "2026-11-16"]), 1)
        self.assertEqual(self.academics.session_context(self.owner, self.context, original["session_id"]), original)

    def test_started_unclaimed_class_cancellation_is_applied(self):
        original = self.seed()
        exception = {"date": "2026-11-16", "cancelled": True, "start_time": None, "end_time": None, "place_id": None}
        self.write("timetable", domain.entry(exceptions=[exception]), action="update", now=NOW + timedelta(seconds=700))
        revised = self.academics.session_context(self.owner, self.context, original["session_id"])
        self.assertTrue(revised["cancelled"])
        self.assertEqual(revised["revision"], original["revision"] + 1)

    def test_deleted_and_unknown_terms_are_missing_for_both_reads(self):
        self.seed()
        self.write("timetable", action="delete", record_id="ethics-mon")
        self.write("term", action="delete", record_id="fall")
        for term_id in ("fall", "nonexistent"):
            for kind, query in (("timetable", {"term_id": term_id}),
                ("occurrences", {"term_id": term_id, "start": "2026-11-16", "end": "2026-11-30"})):
                with self.subTest(kind=kind, term_id=term_id), self.assertRaises(KernelError) as error:
                    self.academics.read(self.owner, self.context, kind, query)
                self.assertEqual(error.exception.code, "missing_dependency")

    def test_wallet_page_stays_enabled_through_class_coordinator(self):
        from good_life.academics import AcademicsConfig
        from good_life.activities.buy_drink import register_buy_drink
        from good_life.economy import WalletAccess
        from good_life.runtime import LifeRuntime
        from test_kernel_support import Access
        registry = ComponentRegistry()
        wallet, _ = register_buy_drink(registry, configured_currency="CNY", reserve_minor=10,
            payments=({"at_s": 60, "amount_minor": 10},))
        host = Access()
        runtime = LifeRuntime(self.storage, object(), academics_config=AcademicsConfig(enabled=True),
            kernel_config=KernelConfig(enabled=True), kernel_registry=registry,
            kernel_access=WalletAccess(host, wallet))
        self.addCleanup(runtime.kernel.close)
        self.assertIsInstance(runtime.kernel, ClassActivities)
        before = self.storage._connection.total_changes
        result = runtime.read_wallet_page(owner_key=host.scope.owner_key, context_key=host.scope.context_key)
        self.assertTrue(result["enabled"])
        self.assertEqual(result["currency"], "CNY")
        self.assertEqual(result["transactions"], [])
        self.assertEqual(self.storage._connection.total_changes, before)
        runtime.kernel.close()
        self.assertEqual(runtime.read_wallet_page(owner_key=host.scope.owner_key,
            context_key=host.scope.context_key), {"enabled": False})

    def test_unchanged_occurrences_are_not_rewritten(self):
        self.seed()
        statements = []
        self.storage._connection.set_trace_callback(statements.append)
        try:
            self.write("term", domain.term(academic_year="renamed"), action="update")
        finally:
            self.storage._connection.set_trace_callback(None)
        self.assertFalse(any(sql.startswith("INSERT INTO life_academic_sessions") for sql in statements))
