"""Atomic resource preparation and cleanup on the enabled local kernel path."""
from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
import json
import unittest

from test_kernel_support import Access, KernelCase, NOW, ROOT, SimulationClock, m
from good_life.activities.buy_drink import AVAILABLE, register_buy_drink
from good_life.economy import Evidence, WalletAccess
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig, KernelError
from good_life.kernel.codec import encode, freeze
from good_life.kernel.resources import ResourcePreparation
from good_life.storage import LifeStorage


class KernelResourceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix=".kernel-resources-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.storage = LifeStorage.open(self.temporary.name)
        self.addCleanup(lambda: self.storage.close())
        self.host = Access()
        self.scope = self.host.scope
        self.registry = ComponentRegistry()
        self.wallet, self.template = register_buy_drink(self.registry, configured_currency="CNY", reserve_minor=36,
            payments=({"at_s": 60, "amount_minor": 6}, {"at_s": 120, "amount_minor": 20}, {"at_s": 180, "amount_minor": 6}))
        self.access = WalletAccess(self.host, self.wallet)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True),
                                     access=self.access, clock=SimulationClock())
        self.addCleanup(lambda: self.kernel.close())
        self.executor = self.registry.executors["purchase_local@2"]
        self.wallet.record_income(self.scope.owner_key, "initial", m.Money(100, "CNY"), operation_id="initial",
            expected_revision=0, evidence=Evidence("initial", NOW, m.Provenance.SIMULATED))

    def prepared(self, version=2):
        # Build @1 historical fixtures under the original binary's creation
        # policy, then immediately restore the current recovery-only policy.
        executor = self.registry.executors["purchase_local@" + str(version)]
        with patch.object(executor, "accepts_new_instances", True):
            candidate = self.kernel.propose(self.scope, m.TemplateRef("buy_drink", m.TemplateVersion(version)),
                opportunity_key="drink", reason="drink_requested", now=NOW, expires_at=NOW + timedelta(hours=1))
            return self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="prepare", now=NOW).instance

    def start(self, instance, command="start", now=NOW):
        return self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                                 command_id=command, now=now)

    def advance(self, instance, seconds):
        return self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                  command_id="advance-" + str(seconds), now=NOW + timedelta(seconds=seconds))

    def quote(self):
        # Assertions can inspect the persisted wallet without installing the
        # fresh domain object used by disabled kernel safety settlement.
        with self.storage._lock:
            q = self.wallet._quote(self.storage._connection, self.scope.owner_key)
        return q.balance_minor, q.reserved_minor, q.available_minor

    def reservation(self, instance):
        return self.wallet.reservation(self.scope.owner_key, instance.instance_id + ":budget")

    def count(self, table):
        return self.kernel.repo.reader.one("SELECT COUNT(*) FROM " + table, ())[0]

    def pending(self, instance, seconds=180):
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("interrupted terminal commit")):
            with self.assertRaisesRegex(RuntimeError, "interrupted terminal commit"):
                self.advance(instance, seconds)
        return self.kernel.repo.instance(self.scope.owner_key, instance.instance_id)

    def reopen(self, enabled=True):
        self.kernel.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temporary.name)
        if not enabled:
            self.registry = ComponentRegistry()
            self.wallet, self.template = register_buy_drink(self.registry, configured_currency="CNY", reserve_minor=36,
                payments=({"at_s": 60, "amount_minor": 6}, {"at_s": 120, "amount_minor": 20}, {"at_s": 180, "amount_minor": 6}))
            self.access = WalletAccess(self.host, self.wallet)
            self.executor = self.registry.executors["purchase_local@2"]
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=enabled),
                                     access=self.access, clock=SimulationClock())

    def test_v1_is_readable_for_recovery_but_cannot_create_new_instances(self):
        ref = m.TemplateRef("buy_drink", m.TemplateVersion(1))
        self.assertEqual(self.registry.template(ref).executor, "purchase_local@1")
        with self.assertRaises(KernelError) as error:
            self.kernel.propose(self.scope, ref, opportunity_key="new-v1", reason="drink_requested",
                                now=NOW, expires_at=NOW + timedelta(hours=1))
        self.assertEqual(error.exception.code, "missing_dependency")
        # An old pending candidate must not bypass the creation fence either.
        with patch.object(self.registry.executors["purchase_local@1"], "accepts_new_instances", True):
            candidate = self.kernel.propose(self.scope, ref, opportunity_key="old-v1", reason="drink_requested",
                                            now=NOW, expires_at=NOW + timedelta(hours=1))
        rejected = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="retired-prepare", now=NOW)
        self.assertFalse(rejected.gate.allowed)
        self.assertEqual(rejected.gate.reason_code, m.ReasonCode.MISSING_DEPENDENCY)
        self.assertEqual(self.count("life_kernel_instances"), 0)
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, candidate.candidate_id)[1].status, m.CandidateStatus.REJECTED)

    def test_tick_expires_or_rejects_old_v1_candidates_then_starts_v2(self):
        from types import SimpleNamespace
        retired = []
        for suffix, expiry in (("aaa-expired", 1), ("aab-retired", 3600)):
            with patch.object(self.registry.executors["purchase_local@1"], "accepts_new_instances", True), \
                 patch("good_life.kernel.engine.uuid4", return_value=SimpleNamespace(hex=suffix)):
                retired.append(self.kernel.propose(self.scope, m.TemplateRef("buy_drink", m.TemplateVersion(1)),
                    opportunity_key=suffix, reason="drink_requested", now=NOW, expires_at=NOW + timedelta(seconds=expiry)))
        with patch("good_life.kernel.engine.uuid4", return_value=SimpleNamespace(hex="zzz-current")):
            current = self.kernel.propose(self.scope, m.TemplateRef("buy_drink", m.TemplateVersion(2)),
                opportunity_key="current", reason="drink_requested", now=NOW, expires_at=NOW + timedelta(hours=1))
        report = self.kernel.tick(now=NOW + timedelta(seconds=2))
        self.assertEqual((report["started"], report["failures"]), (1, ()))
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, retired[0].candidate_id)[1].status, m.CandidateStatus.EXPIRED)
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, retired[1].candidate_id)[1].status, m.CandidateStatus.REJECTED)
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, current.candidate_id)[1].status, m.CandidateStatus.CONSUMED)
        active, = self.kernel.repo.active(self.scope.owner_key)
        self.assertEqual((active.executor_version, self.quote()), ("2", (100, 36, 64)))

    def test_unstable_start_is_bounded_and_releases_the_foreground(self):
        prepared = self.prepared()
        original = self.host.snapshot
        def unstable(scope, now):
            self.host.revision += 1
            return original(scope, now)
        with patch.object(self.host, "snapshot", side_effect=unstable):
            for _ in range(2):
                with self.assertRaises(KernelError) as error:
                    self.start(prepared)
                self.assertEqual(error.exception.code, "stale_version")
            failed = self.start(prepared)
        self.assertEqual((failed.status, failed.reason_code), (m.ActivityStatus.FAILED, m.ReasonCode.STALE_VERSION))
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.assertIsNone(self.reservation(failed))
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual(self.start(prepared), failed)

    def test_unstable_recovery_is_bounded_paused_and_can_resume_later(self):
        started = self.start(self.prepared())
        self.reopen()
        original = self.host.snapshot
        def unstable(scope, now):
            self.host.revision += 1
            return original(scope, now)
        with patch.object(self.host, "snapshot", side_effect=unstable):
            for seconds in (10, 11):
                with self.assertRaises(KernelError):
                    self.kernel.recover(self.scope, now=NOW + timedelta(seconds=seconds))
            paused, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=12))
        self.assertEqual((paused.status, paused.reason_code), (m.ActivityStatus.PAUSED, m.ReasonCode.STALE_VERSION))
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.assertEqual((paused.checkpoint, paused.elapsed_s, self.quote()), (started.checkpoint, 0, (100, 36, 64)))
        resumed = self.start(paused, "stable-resume", NOW + timedelta(seconds=20))
        self.assertEqual((resumed.status, resumed.started_at, self.quote()), (m.ActivityStatus.RUNNING, NOW, (100, 36, 64)))

    def _resume_resource_denial(self, *, paused, can_pause=True):
        from good_life.kernel.registry import ModuleSpec
        started = self.start(self.prepared())
        if paused:
            old = self.kernel.pause(self.scope, started.instance_id, expected_revision=started.revision,
                                    command_id="pause-before-resource-denial", now=NOW)
        else:
            self.reopen()
            old = started
        host = self.host
        class Claims:
            def apply_effect(self, db, instance, effect):
                db.execute("UPDATE test_resume_claims SET held=held+1")
                host.free_s = 179
                return True
        with self.storage.transaction() as db:
            db.execute("CREATE TABLE test_resume_claims (held INTEGER NOT NULL)")
            db.execute("INSERT INTO test_resume_claims VALUES (0)")
        self.registry.register_module(ModuleSpec("claims", "1", ("claims.held",), consumes=("claims.hold",)), Claims())
        if not can_pause:
            self.executor.pausable_phases = ()
        original = self.executor.prepare_resources
        def prepare(instance, now, snapshot):
            plan = original(instance, now, snapshot)
            return replace(plan, effects=plan.effects + (m.Effect(instance.instance_id + ":resume-claim",
                instance.instance_id, "claims", "claims.hold", freeze({}), now, ()),),
                reservation_refs=plan.reservation_refs + (m.RecordRef("claims.hold", instance.instance_id, None),))
        with patch.object(self.executor, "prepare_resources", side_effect=prepare):
            if paused:
                blocked = self.start(old, "blocked-resource-resume", NOW + timedelta(seconds=1))
            elif can_pause:
                blocked, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=1))
            else:
                with self.assertRaises(KernelError) as error:
                    self.kernel.recover(self.scope, now=NOW + timedelta(seconds=1))
                self.assertEqual(error.exception.code, "no_time_window")
                blocked = self.kernel.repo.instance(self.scope.owner_key, old.instance_id)
                self.assertEqual(blocked, old)
        self.assertEqual(blocked.status, m.ActivityStatus.PAUSED if paused or can_pause else m.ActivityStatus.RUNNING)
        self.assertEqual((blocked.checkpoint, blocked.reservation_refs), (old.checkpoint, old.reservation_refs))
        self.assertEqual(self.kernel.repo.reader.one("SELECT held FROM test_resume_claims", ())[0], 0)
        self.assertEqual(self.count("life_kernel_effects"), 1)
        self.assertEqual(self.quote(), (100, 36, 64))
        if not paused and can_pause:
            self.assertEqual(blocked.reason_code, m.ReasonCode.NO_TIME_WINDOW)

    def test_nonpausable_recovery_gate_denial_keeps_phase_and_rolls_back_resources(self):
        self._resume_resource_denial(paused=False, can_pause=False)

    def test_nonpausable_stale_recovery_never_claims_a_safe_pause(self):
        started = self.start(self.prepared())
        self.reopen()
        self.executor.pausable_phases = ()
        original = self.host.snapshot
        def unstable(scope, now):
            self.host.revision += 1
            return original(scope, now)
        with patch.object(self.host, "snapshot", side_effect=unstable):
            for seconds in (10, 11, 12):
                with self.assertRaises(KernelError) as error:
                    self.kernel.recover(self.scope, now=NOW + timedelta(seconds=seconds))
                self.assertEqual(error.exception.code, "stale_version")
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, started.instance_id), started)
        self.assertEqual(self.quote(), (100, 36, 64))
        cancelled = self.kernel.cancel(self.scope, started.instance_id, expected_revision=started.revision,
                                      command_id="explicit-cancel", now=NOW + timedelta(seconds=20))
        self.assertEqual((cancelled.status, self.quote()), (m.ActivityStatus.CANCELLED, (100, 0, 100)))

    def test_recovery_post_preparation_gate_denial_pauses_and_undoes_resources(self):
        self._resume_resource_denial(paused=False)

    def test_paused_resume_post_preparation_gate_denial_undoes_resources(self):
        self._resume_resource_denial(paused=True)

    def test_atomic_start_has_reservation_reference_and_no_waiting_phase(self):
        prepared = self.prepared()
        started = self.start(prepared)
        self.assertEqual((started.status, started.phase, started.executor_version),
                         (m.ActivityStatus.RUNNING, "purchasing", "2"))
        self.assertEqual(self.quote(), (100, 36, 64))
        self.assertEqual(started.reservation_refs, (m.RecordRef("economy.reservation", started.instance_id + ":budget", None),))
        self.assertEqual(self.start(prepared), started)
        self.assertEqual(self.count("life_wallet_operations"), 2)
        row = self.kernel.repo.reader.one("SELECT expected_revision FROM life_wallet_operations WHERE operation_id=?",
                                         (started.instance_id + ":reserve",))
        self.assertEqual(row[0], 1)
        gates = {row[0]: json.loads(row[1]) for row in self.kernel.repo.reader.all(
            "SELECT stage,gate_json FROM life_kernel_gates WHERE command_id='start'", ())}
        self.assertGreater(gates["after_resources"]["snapshot_revision"], gates["before_start"]["snapshot_revision"])
        at60 = self.advance(started, 60)
        self.assertEqual((at60.elapsed_s, self.quote()), (60, (94, 30, 64)))
        final = self.advance(at60, 180)
        self.assertEqual((final.status, self.quote()), (m.ActivityStatus.COMPLETED, (68, 0, 68)))

    def test_reserving_all_available_funds_does_not_require_a_second_budget(self):
        self.wallet.reserve(self.scope.owner_key, "other", m.Money(64, "CNY"), operation_id="other",
            expected_revision=1, review_at=NOW + timedelta(hours=1),
            evidence=Evidence("other", NOW, m.Provenance.SIMULATED, "other-instance"))
        started = self.start(self.prepared())
        self.assertEqual((started.status, self.quote()), (m.ActivityStatus.RUNNING, (100, 100, 0)))

    def test_snapshot_changes_after_final_gate_roll_back_the_prepared_reservation(self):
        prepared = self.prepared()
        original = self.host.snapshot
        calls = 0
        def changes_on_commit(scope, now):
            nonlocal calls
            calls += 1
            if calls == 4:
                self.host.busy = True
            return original(scope, now)
        with patch.object(self.host, "snapshot", side_effect=changes_on_commit):
            with self.assertRaises(KernelError) as error:
                self.start(prepared)
        self.assertEqual(error.exception.code, "stale_version")
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, prepared.instance_id), prepared)
        self.assertIsNone(self.reservation(prepared))
        self.assertEqual(self.quote(), (100, 0, 100))

    def test_domain_refusal_rolls_back_instance_effect_refs_and_command(self):
        prepared = self.prepared()
        original = self.wallet.apply_effect
        def refuse(db, instance, effect):
            original(db, instance, effect)
            raise KernelError(m.ReasonCode.INSUFFICIENT_RESOURCE, "domain refused after staged reservation")
        with patch.object(self.wallet, "apply_effect", side_effect=refuse):
            with self.assertRaises(KernelError) as error:
                self.start(prepared)
        self.assertEqual(error.exception.error.reason_code, m.ReasonCode.INSUFFICIENT_RESOURCE)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, prepared.instance_id), prepared)
        self.assertIsNone(self.reservation(prepared))
        self.assertEqual((self.count("life_kernel_effects"), self.count("life_wallet_operations")), (0, 1))
        self.assertIsNone(self.kernel.repo.reader.one("SELECT 1 FROM life_kernel_commands WHERE command_id='start'", ()))
        self.assertEqual(self.start(prepared).status, m.ActivityStatus.RUNNING)

    def test_pre_start_gate_refusal_never_creates_domain_facts(self):
        prepared = self.prepared()
        self.host.free_s = 179
        failed = self.start(prepared)
        self.assertEqual((failed.status, failed.reason_code), (m.ActivityStatus.FAILED, m.ReasonCode.NO_TIME_WINDOW))
        self.assertIsNone(self.reservation(failed))
        self.assertEqual(self.quote(), (100, 0, 100))

    def test_complete_window_is_rechecked_after_first_resource_write(self):
        prepared = self.prepared()
        original = self.wallet.apply_effect
        def shorten_window(db, instance, effect):
            answer = original(db, instance, effect)
            self.host.free_s = 179
            return answer
        with patch.object(self.wallet, "apply_effect", side_effect=shorten_window):
            with self.assertRaises(KernelError) as error:
                self.start(prepared)
        self.assertEqual(error.exception.error.reason_code, m.ReasonCode.NO_TIME_WINDOW)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, prepared.instance_id), prepared)
        self.assertIsNone(self.reservation(prepared))
        self.assertEqual(self.quote(), (100, 0, 100))

    def test_snapshot_change_during_pure_preparation_is_stale_and_atomic(self):
        prepared = self.prepared()
        original = self.executor.prepare_resources
        def changed(instance, now, snapshot):
            plan = original(instance, now, snapshot)
            self.host.revision += 1
            return plan
        with patch.object(self.executor, "prepare_resources", side_effect=changed):
            with self.assertRaises(KernelError) as error:
                self.start(prepared)
        self.assertEqual(error.exception.error.reason_code, m.ReasonCode.STALE_VERSION)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, prepared.instance_id), prepared)
        self.assertIsNone(self.reservation(prepared))

    def test_expected_wallet_revision_is_fixed_by_the_decision_snapshot(self):
        prepared = self.prepared()
        original = self.wallet.apply_effect
        def race(db, instance, effect):
            # A same-transaction competing domain change after the final view
            # must not be hidden by taking a fresh quote in apply_effect.
            db.execute("UPDATE life_wallet_accounts SET revision=revision+1 WHERE owner_key=?", (instance.owner_key,))
            return original(db, instance, effect)
        with patch.object(self.wallet, "apply_effect", side_effect=race):
            with self.assertRaises(KernelError) as error:
                self.start(prepared)
        self.assertEqual(error.exception.error.reason_code, m.ReasonCode.STALE_VERSION)
        self.assertEqual(self.wallet.quote_available(self.scope.owner_key).revision, 1)
        self.assertIsNone(self.reservation(prepared))

    def test_composed_revision_tracks_wallet_and_host_and_survives_restart(self):
        before = self.kernel._snapshot(self.scope, NOW)
        same = self.kernel._snapshot(self.scope, NOW)
        self.assertEqual(before.state.revision, same.state.revision)
        self.wallet.record_income(self.scope.owner_key, "more", m.Money(1, "CNY"), operation_id="more",
            expected_revision=1, evidence=Evidence("more", NOW, m.Provenance.SIMULATED))
        wallet_changed = self.kernel._snapshot(self.scope, NOW)
        self.assertGreater(wallet_changed.state.revision, before.state.revision)
        self.assertEqual(self.host.revision, 1)
        self.host.revision += 1
        host_changed = self.kernel._snapshot(self.scope, NOW)
        self.assertGreater(host_changed.state.revision, wallet_changed.state.revision)
        self.reopen()
        self.assertEqual(self.kernel._snapshot(self.scope, NOW).state.revision, host_changed.state.revision)

    def test_resume_regates_and_keeps_existing_reservation(self):
        started = self.start(self.prepared())
        paused = self.kernel.pause(self.scope, started.instance_id, expected_revision=started.revision,
                                   command_id="pause", now=NOW + timedelta(seconds=10))
        self.host.free_s = 169
        self.assertEqual(self.start(paused, "blocked-resume", NOW + timedelta(seconds=20)), paused)
        self.host.free_s = 170
        resumed = self.start(paused, "resume", NOW + timedelta(seconds=20))
        self.assertEqual((resumed.status, resumed.elapsed_s, resumed.started_at), (m.ActivityStatus.RUNNING, 10, NOW))
        self.assertEqual((self.quote(), self.wallet.quote_available(self.scope.owner_key).revision), ((100, 36, 64), 2))
        self.reopen()
        self.host.free_s = 169
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(hours=1))
        self.assertEqual(recovered.status, m.ActivityStatus.PAUSED)
        self.assertEqual(self.quote(), (100, 36, 64))

    def test_restart_rechecks_the_recover_authority_action(self):
        started = self.start(self.prepared())
        self.reopen()
        with patch.object(self.host, "authorize", side_effect=lambda scope, action: action == "kernel.recover"):
            recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(hours=1))
        self.assertEqual((recovered.status, recovered.started_at, self.quote()), (m.ActivityStatus.RUNNING, started.started_at, (100, 36, 64)))

    def test_abandon_releases_actual_unpaid_remainder_without_proposed_spending(self):
        started = self.start(self.prepared())
        paid = self.advance(started, 60)
        pending = self.pending(paid)
        result_id = "kernel-result-" + pending.instance_id
        proposal, _ = self.kernel.repo.result(self.scope.owner_key, result_id)
        failed = self.kernel.abandon_result(self.scope, result_id, expected_revision=pending.revision,
            command_id="abandon", reason="unrecoverable local proposal", now=NOW + timedelta(seconds=200))
        self.assertEqual((failed.status, self.quote()), (m.ActivityStatus.FAILED, (94, 0, 94)))
        self.assertEqual(failed.checkpoint, pending.checkpoint)
        self.assertEqual(self.kernel.repo.result(self.scope.owner_key, result_id)[0], proposal)
        audit, = self.wallet.recovery_audit(self.scope.owner_key)
        self.assertEqual((audit["actual_paid_minor"], audit["released_minor"]), (6, 30))
        self.assertEqual(self.kernel.abandon_result(self.scope, result_id, expected_revision=pending.revision,
            command_id="abandon", reason="unrecoverable local proposal", now=NOW + timedelta(seconds=200)), failed)
        self.assertEqual(self.quote(), (94, 0, 94))
        # The strictly checked A2 manual entry remains a safe no-op after auto cleanup.
        fallback = dict(abandon_command_id="abandon", abandon_reason="unrecoverable local proposal",
            expected_instance_revision=failed.revision, operation_id="manual-fallback",
            expected_revision=self.wallet.quote_available(self.scope.owner_key).revision,
            evidence=Evidence("manual", NOW + timedelta(seconds=201), m.Provenance.SIMULATED, failed.instance_id))
        receipt = self.wallet.release_abandoned(self.scope.owner_key, result_id, **fallback)
        self.assertEqual(self.quote(), (94, 0, 94))
        with self.assertRaises(KernelError):
            self.wallet.release_abandoned(self.scope.owner_key, result_id, **{**fallback, "operation_id": "initial"})
        with self.assertRaises(KernelError):
            self.wallet.release_abandoned(self.scope.owner_key, result_id, **{**fallback, "expected_revision": fallback["expected_revision"] + 1})
        self.wallet.record_income(self.scope.owner_key, "after-cleanup", m.Money(1, "CNY"), operation_id="after-cleanup",
            expected_revision=self.wallet.quote_available(self.scope.owner_key).revision,
            evidence=Evidence("after-cleanup", NOW + timedelta(seconds=202), m.Provenance.SIMULATED))
        self.assertEqual(self.wallet.release_abandoned(self.scope.owner_key, result_id, **fallback), receipt)
        self.assertEqual(self.quote(), (95, 0, 95))

    def test_abandon_cleanup_failure_rolls_back_failed_status_and_release(self):
        started = self.start(self.prepared())
        pending = self.pending(started)
        original = self.wallet.apply_effect
        def interrupted(db, instance, effect):
            original(db, instance, effect)
            raise RuntimeError("cleanup interrupted")
        with patch.object(self.wallet, "apply_effect", side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, "cleanup interrupted"):
                self.kernel.abandon_result(self.scope, "kernel-result-" + pending.instance_id,
                    expected_revision=pending.revision, command_id="abandon", reason="reviewed", now=NOW + timedelta(seconds=200))
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, pending.instance_id), pending)
        self.assertEqual(self.quote(), (100, 36, 64))
        self.assertEqual(self.wallet.recovery_audit(self.scope.owner_key), ())
        self.assertIsNone(self.kernel.repo.result(self.scope.owner_key, "kernel-result-" + pending.instance_id)[1])

    def test_legacy_pending_proposal_also_uses_automatic_cleanup(self):
        old = self.start(self.prepared(version=1))
        funded = self.advance(old, 0)
        pending = self.pending(funded)
        failed = self.kernel.abandon_result(self.scope, "kernel-result-" + pending.instance_id,
            expected_revision=pending.revision, command_id="abandon-old", reason="reviewed", now=NOW + timedelta(seconds=200))
        self.assertEqual((failed.executor_version, failed.status, self.quote()), ("1", m.ActivityStatus.FAILED, (100, 0, 100)))

    def test_disabled_safety_settlement_also_cleans_up(self):
        pending = self.pending(self.start(self.prepared()))
        self.reopen(enabled=False)
        self.assertIsNone(self.wallet.storage)
        failed = self.kernel.abandon_result(self.scope, "kernel-result-" + pending.instance_id,
            expected_revision=pending.revision, command_id="abandon-disabled", reason="reviewed", now=NOW + timedelta(seconds=200))
        self.assertEqual((failed.status, self.quote()), (m.ActivityStatus.FAILED, (100, 0, 100)))

    def test_disabled_fresh_wallet_also_cleans_up_reserved_v1(self):
        funded = self.advance(self.start(self.prepared(version=1)), 0)
        pending = self.pending(funded)
        self.reopen(enabled=False)
        self.assertIsNone(self.wallet.storage)
        failed = self.kernel.abandon_result(self.scope, "kernel-result-" + pending.instance_id,
            expected_revision=pending.revision, command_id="disabled-v1-abandon", reason="reviewed", now=NOW + timedelta(seconds=200))
        self.assertEqual((failed.status, failed.executor_version, self.quote()), (m.ActivityStatus.FAILED, "1", (100, 0, 100)))
        self.assertIsNone(self.wallet.storage)

    def test_schema_one_upgrade_continues_above_legacy_snapshot_revisions(self):
        self.start(self.prepared(version=1))
        self.kernel.close()
        with self.storage.transaction() as db:
            # A schema-1 fixture whose host version was much higher than the
            # current host counter. Historical evidence must keep its number.
            db.execute("DROP TABLE life_kernel_snapshots")
            db.execute("UPDATE life_metadata SET value='1' WHERE key='kernel_schema_version'")
            db.execute("UPDATE life_kernel_gates SET gate_json=json_set(gate_json,'$.snapshot_revision',500)")
            db.execute("UPDATE life_kernel_candidates SET record_json=json_set(record_json,'$.snapshot_revision',700)")
        self.reopen()
        current = self.kernel._snapshot(self.scope, NOW)
        self.assertEqual(current.state.revision, 701)
        self.assertEqual(self.kernel._snapshot(self.scope, NOW).state.revision, 701)
        self.assertEqual(self.kernel.repo.reader.one(
            "SELECT MAX(json_extract(gate_json,'$.snapshot_revision')) FROM life_kernel_gates", ())[0], 500)
        self.assertEqual(self.kernel.repo.reader.one(
            "SELECT MAX(json_extract(record_json,'$.snapshot_revision')) FROM life_kernel_candidates", ())[0], 700)

    def test_schema_one_upgrade_keeps_old_awaiting_instance_and_original_facts(self):
        old = self.start(self.prepared(version=1))
        self.assertEqual((old.phase, self.quote()), ("awaiting_reservation", (100, 0, 100)))
        original_record = encode(old)
        self.kernel.close()
        with self.storage.transaction() as db:
            db.execute("DROP TABLE life_kernel_snapshots")
            db.execute("UPDATE life_metadata SET value='1' WHERE key='kernel_schema_version'")
        self.reopen()
        self.assertEqual(encode(self.kernel.repo.instance(self.scope.owner_key, old.instance_id)), original_record)
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=120))
        funded = self.advance(recovered, 120)
        self.assertEqual((funded.phase, funded.elapsed_s, funded.executor_version), ("purchasing", 0, "1"))
        self.assertEqual(self.quote(), (100, 36, 64))
        self.assertEqual(self.reservation(funded).review_at, NOW + timedelta(seconds=300))
        self.assertEqual(self.advance(funded, 300).status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (68, 0, 68))
        self.assertEqual(self.kernel.repo.reader.one("SELECT value FROM life_metadata WHERE key='kernel_schema_version'", ())[0], "2")


class GenericResourceTests(KernelCase):
    def test_hooks_work_for_an_independent_non_wallet_domain(self):
        from good_life.kernel.registry import ModuleSpec

        class Tokens:
            def apply_effect(self, db, instance, effect):
                if effect.kind == "tokens.claim":
                    db.execute("INSERT INTO test_token_claims VALUES (?,1)", (instance.instance_id,))
                else:
                    db.execute("UPDATE test_token_claims SET held=0 WHERE instance_id=?", (instance.instance_id,))
                return True

        with self.storage.transaction() as db:
            db.execute("CREATE TABLE test_token_claims (instance_id TEXT PRIMARY KEY, held INTEGER NOT NULL)")
        self.registry.register_module(ModuleSpec("tokens", "1", ("tokens.held",),
                                                consumes=("tokens.claim", "tokens.release")), Tokens())
        def prepare(instance, now, snapshot):
            return ResourcePreparation(instance.phase, instance.checkpoint,
                (m.Effect(instance.instance_id + ":claim", instance.instance_id, "tokens", "tokens.claim", freeze({}), now, ()),),
                (m.RecordRef("tokens.claim", instance.instance_id, None),))
        with patch.object(self.executor, "prepare_resources", side_effect=prepare, create=True):
            started = self.running()
        self.assertEqual(started.reservation_refs, (m.RecordRef("tokens.claim", started.instance_id, None),))
        self.assertEqual(self.kernel.repo.reader.one("SELECT held FROM test_token_claims", ())[0], 1)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("pending")):
            with self.assertRaisesRegex(RuntimeError, "pending"):
                self.kernel.advance(self.scope, started.instance_id, expected_revision=started.revision,
                                    command_id="finish", now=NOW + timedelta(seconds=180))
        pending = self.kernel.repo.instance(self.scope.owner_key, started.instance_id)
        def cleanup(instance, result, now):
            return (m.Effect(instance.instance_id + ":cleanup", instance.instance_id, "tokens", "tokens.release", freeze({}), now, ()),)
        with patch.object(self.executor, "abandon_resources", side_effect=cleanup, create=True):
            failed = self.kernel.abandon_result(self.scope, "kernel-result-" + pending.instance_id,
                expected_revision=pending.revision, command_id="abandon", reason="reviewed", now=NOW + timedelta(seconds=200))
        self.assertEqual(failed.status, m.ActivityStatus.FAILED)
        self.assertEqual(self.kernel.repo.reader.one("SELECT held FROM test_token_claims", ())[0], 0)
        self.assertEqual(self.units(), 0)


if __name__ == "__main__":
    unittest.main()
