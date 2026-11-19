"""Pinned purchase_local@1 compatibility, policy recovery and durable replay.

New atomic @2 start behavior is covered in test_kernel_resources.py.
"""
from dataclasses import replace
from datetime import timedelta
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from test_kernel_support import Access, NOW, ROOT, SimulationClock, m
from good_life.activities.buy_drink import AVAILABLE, PURCHASE_BUDGET, register_buy_drink
from good_life.activities.local_order import register_local_order
from good_life.economy import Evidence, WalletAccess, WalletActivities
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelConfig, KernelError
from good_life.kernel.codec import freeze, wire
from good_life.overview_reads import read_activities
from good_life.storage import LifeStorage


class WalletPurchaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory(prefix=".wallet-purchase-", dir=ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.storage = LifeStorage.open(self.temporary.name)
        self.addCleanup(lambda: self.storage.close())
        self.host = Access()
        self.scope = self.host.scope
        self.registry = ComponentRegistry()
        self.wallet, self.template = register_buy_drink(self.registry, configured_currency="CNY", reserve_minor=36, executor_version="1",
            payments=({"at_s": 60, "amount_minor": 6}, {"at_s": 120, "amount_minor": 20}, {"at_s": 180, "amount_minor": 6}))
        # Historical fixtures are produced as the old binary did. Production
        # registration keeps @1 recovery-only; new-instance rejection is tested
        # separately with the normal registry in test_kernel_resources.py.
        self.registry.executors["purchase_local@1"].accepts_new_instances = True
        self.tidy = register_local_order(self.registry)
        self.access = WalletAccess(self.host, self.wallet)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True),
                                     access=self.access, clock=SimulationClock())
        self.activities = WalletActivities(self.kernel, self.wallet)
        self.addCleanup(lambda: self.kernel.close())
        self.income(100, "initial")

    def income(self, minor, key):
        quote = self.wallet.quote_available(self.scope.owner_key)
        return self.wallet.record_income(self.scope.owner_key, key, m.Money(minor, "CNY"), operation_id=key,
            expected_revision=quote.revision, evidence=Evidence(key, NOW, m.Provenance.SIMULATED))

    def propose(self, opportunity="drink", now=NOW):
        return self.kernel.propose(self.scope, m.TemplateRef(self.template.id, self.template.version),
            opportunity_key=opportunity, reason="drink_requested", now=now, expires_at=now + timedelta(hours=1))

    def prepared(self, opportunity="drink", now=NOW):
        candidate = self.propose(opportunity, now)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id,
                                                 command_id="prepare-" + opportunity, now=now)
        self.assertTrue(prepared.gate.allowed)
        return prepared.instance

    def running(self):
        instance = self.prepared()
        return self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                                 command_id="start", now=NOW)

    def advance(self, instance, seconds, command_id=None):
        return self.activities.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                       command_id=command_id or "step-" + str(seconds), now=NOW + timedelta(seconds=seconds))

    def quote(self):
        # Inspect persisted facts without enabling the fresh wallet used by
        # disabled-kernel safety settlement.
        with self.storage._lock:
            q = self.wallet._quote(self.storage._connection, self.scope.owner_key)
        return q.balance_minor, q.reserved_minor, q.available_minor

    def reservation(self, instance):
        return self.wallet.reservation(self.scope.owner_key, instance.instance_id + ":budget")

    def reopen(self, *, enabled=True):
        self.kernel.close()
        self.storage.close()
        self.storage = LifeStorage.open(self.temporary.name)
        if not enabled:
            params = self.template.params
            self.registry = ComponentRegistry()
            self.wallet, self.template = register_buy_drink(self.registry,
                configured_currency=params["currency"], reserve_minor=params["reserve_minor"],
                payments=params["payments"], executor_version="1")
            self.tidy = register_local_order(self.registry)
            self.access = WalletAccess(self.host, self.wallet)
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=enabled),
                                     access=self.access, clock=SimulationClock())
        self.activities = WalletActivities(self.kernel, self.wallet)

    def test_normal_purchase_and_final_release_use_kernel_effect_transaction(self):
        started = self.running()
        self.assertEqual((started.phase, self.quote()), ("awaiting_reservation", (100, 0, 100)))
        instance = self.advance(started, 0, "reserve")
        self.assertEqual((instance.phase, self.quote()), ("purchasing", (100, 36, 64)))
        instance = self.advance(instance, 60)
        self.assertEqual(self.quote(), (94, 30, 64))
        instance = self.advance(instance, 120)
        self.assertEqual(self.quote(), (74, 10, 64))
        completed = self.advance(instance, 180, "finish")
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (68, 0, 68))
        self.assertEqual(self.reservation(completed).status, "released")
        replay = self.advance(instance, 180, "finish")
        self.assertEqual(replay, completed)
        self.assertEqual(self.quote(), (68, 0, 68))
        self.assertEqual([t.amount_minor for t in self.wallet.transactions(self.scope.owner_key)], [100, 6, 20, 6])
        for event in self.storage.pending_events("kernel.audit.v1"):
            envelope = self.kernel.repo.envelope(event["event_id"])
            self.assertEqual(envelope.provenance, m.Provenance.SIMULATED)
            self.assertEqual(envelope.audience_bound.context_keys, (self.scope.context_key,))

    def test_cancellation_releases_only_unused_reservation_and_keeps_paid_stage(self):
        instance = self.advance(self.running(), 0, "reserve")
        instance = self.advance(instance, 60)
        cancelled = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                       command_id="cancel", now=NOW + timedelta(seconds=75))
        self.assertEqual(cancelled.status, m.ActivityStatus.CANCELLED)
        self.assertTrue(cancelled.outcome.partial)
        self.assertEqual(self.quote(), (94, 0, 94))
        self.assertEqual([t.kind for t in self.wallet.transactions(self.scope.owner_key)], ["income", "spend"])
        self.assertEqual(self.reservation(cancelled).amount_remaining_minor, 0)

    def test_cancel_reserved_purchase_before_payment_releases_everything(self):
        instance = self.advance(self.running(), 0, "reserve")
        cancelled = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                       command_id="cancel-funded", now=NOW + timedelta(seconds=10))
        self.assertEqual(cancelled.status, m.ActivityStatus.CANCELLED)
        self.assertTrue(cancelled.outcome.partial)
        self.assertEqual(cancelled.elapsed_s, 10)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual(len(self.wallet.transactions(self.scope.owner_key)), 1)

    def test_domain_effect_replay_itself_is_idempotent(self):
        instance = self.advance(self.running(), 0, "reserve")
        command = instance.checkpoint["commands"][0]
        effect = m.Effect(command["effect_id"], instance.instance_id, "economy", "economy.purchase",
                          command["payload"], NOW, ())
        with self.storage.transaction() as db:
            self.assertTrue(self.wallet.apply_effect(db, instance, effect))
            self.assertTrue(self.wallet.apply_effect(db, instance, effect))
        self.assertEqual(self.quote(), (100, 36, 64))
        self.assertEqual(self.wallet.quote_available(self.scope.owner_key).revision, 2)

    def test_cancel_before_reservation_or_before_start_creates_no_money_fact(self):
        instance = self.prepared()
        cancelled = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                       command_id="cancel-before-start", now=NOW)
        self.assertFalse(cancelled.outcome.partial)
        self.assertIsNone(self.reservation(cancelled))
        candidate = self.prepared("another")
        instance = self.kernel.start(self.scope, candidate.instance_id, expected_revision=candidate.revision,
                                     command_id="another-start", now=NOW)
        cancelled = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                       command_id="cancel-before-reserve", now=NOW + timedelta(seconds=70))
        self.assertFalse(cancelled.outcome.partial)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertIsNone(self.reservation(cancelled))

    def test_reservation_tick_never_counts_unfunded_purchase_time(self):
        instance = self.advance(self.running(), 120, "late-reserve")
        self.assertEqual(instance.elapsed_s, 0)
        self.assertEqual(self.quote(), (100, 36, 64))
        completed = self.advance(instance, 300, "finish-after-funding")
        self.assertEqual(completed.elapsed_s, 180)
        self.assertEqual(self.quote(), (68, 0, 68))

    def test_insufficient_funds_and_missing_wallet_fact_are_gate_rejections(self):
        # Another activity legitimately holds most funds.
        self.wallet.reserve(self.scope.owner_key, "other-reservation", m.Money(70, "CNY"),
            operation_id="other-reserve", expected_revision=1, review_at=NOW + timedelta(hours=1),
            evidence=Evidence("other-quote", NOW, m.Provenance.SIMULATED, "other-instance"))
        candidate = self.propose()
        preparation = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="rejected", now=NOW)
        self.assertFalse(preparation.gate.allowed)
        self.assertEqual(preparation.gate.reason_code, m.ReasonCode.INSUFFICIENT_RESOURCE)
        self.assertIsNone(preparation.instance)
        unknown_scope = replace(self.scope, owner_key=m.OwnerKey("unknown-wallet"))
        self.host.inventory = (unknown_scope,)
        snapshot = self.access.snapshot(unknown_scope, NOW)
        self.assertIsNone(snapshot.state.values[AVAILABLE].value)
        self.assertEqual(snapshot.state.values[AVAILABLE].quality, m.StateQuality.UNKNOWN)

    def test_last_gate_rechecks_current_funds_and_first_reserve_rechecks_again(self):
        instance = self.prepared()
        self.wallet.reserve(self.scope.owner_key, "other-reservation", m.Money(70, "CNY"),
            operation_id="other-reserve", expected_revision=1, review_at=NOW + timedelta(hours=1),
            evidence=Evidence("other-quote", NOW, m.Provenance.SIMULATED, "other-instance"))
        failed = self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="start-rejected", now=NOW)
        self.assertEqual(failed.status, m.ActivityStatus.FAILED)
        self.assertEqual(failed.reason_code, m.ReasonCode.INSUFFICIENT_RESOURCE)
        self.assertIsNone(self.reservation(failed))
        # A fresh funded start can also lose its available quote before its
        # reservation phase; no payment or checkpoint may escape that rollback.
        self.income(100, "top-up")
        prepared = self.prepared("second")
        started = self.kernel.start(self.scope, prepared.instance_id, expected_revision=prepared.revision,
                                    command_id="second-start", now=NOW)
        self.wallet.reserve(self.scope.owner_key, "third-reservation", m.Money(100, "CNY"),
            operation_id="third-reserve", expected_revision=self.wallet.quote_available(self.scope.owner_key).revision,
            review_at=NOW + timedelta(hours=1), evidence=Evidence("third", NOW, m.Provenance.SIMULATED, "third-instance"))
        cancelled = self.advance(started, 0, "reserve-after-race")
        self.assertEqual(cancelled.status, m.ActivityStatus.CANCELLED)
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.assertIsNone(self.reservation(started))
        audit = self.wallet.recovery_audit(self.scope.owner_key)[0]
        self.assertEqual(audit["reason_code"], "insufficient_resource")
        self.assertTrue(audit["completed"])
        overview = read_activities(self.storage, owner_key=self.scope.owner_key, context_key=self.scope.context_key,
                                   now=NOW, kernel=True, academics=False)
        states = {item["state"]: item["reason_code"] for item in overview["items"]}
        self.assertEqual(states, {"failed": "insufficient_resource", "cancelled": "insufficient_resource"})

    def test_restart_keeps_progress_and_does_not_charge_reserved_budget_twice(self):
        # Leave only 36 available so recovery must use the original secured
        # budget rather than require an additional fresh 36 after payment.
        self.wallet.reserve(self.scope.owner_key, "other-reservation", m.Money(64, "CNY"),
            operation_id="other-reserve", expected_revision=1, review_at=NOW + timedelta(days=3),
            evidence=Evidence("other", NOW, m.Provenance.SIMULATED, "other-instance"))
        instance = self.advance(self.running(), 0, "reserve")
        instance = self.advance(instance, 60)
        self.assertEqual(self.quote(), (94, 94, 0))
        self.reopen()
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(days=2))
        self.assertEqual((recovered.status, recovered.elapsed_s), (m.ActivityStatus.RUNNING, 60))
        snapshot = self.access.snapshot(self.scope, NOW + timedelta(days=2))
        self.assertEqual(snapshot.state.values[AVAILABLE].value, 0)
        self.assertEqual(snapshot.state.values[PURCHASE_BUDGET].value, 36)
        completed = self.kernel.advance(self.scope, recovered.instance_id, expected_revision=recovered.revision,
            command_id="after-restart", now=NOW + timedelta(days=2, seconds=120))
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (68, 64, 4))
        self.assertEqual(len(self.wallet.transactions(self.scope.owner_key)), 4)

    def test_crash_after_terminal_proposal_replays_payment_and_release_once(self):
        instance = self.advance(self.running(), 0, "reserve")
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash after proposal")):
            with self.assertRaisesRegex(RuntimeError, "crash after proposal"):
                self.advance(instance, 180, "finish-crash")
        self.assertEqual(self.quote(), (100, 36, 64))
        previous = (self.storage, self.registry, self.wallet, self.access, self.kernel, self.activities)
        self.reopen(enabled=False)
        for old, new in zip(previous, (self.storage, self.registry, self.wallet, self.access, self.kernel, self.activities)):
            self.assertIsNot(old, new)
        self.assertIsNone(self.wallet.storage)
        completed, = self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=300))
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (68, 0, 68))
        self.assertEqual(self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=400)), ())
        self.assertIsNone(self.wallet.storage)
        with self.storage._lock:
            transactions = self.storage._connection.execute("""SELECT kind,amount_minor FROM life_wallet_transactions
                WHERE owner_key=? ORDER BY rowid""", (self.scope.owner_key,)).fetchall()
            reservation = self.storage._connection.execute("""SELECT amount_remaining_minor,status
                FROM life_wallet_reservations WHERE owner_key=? AND reservation_id=?""",
                (self.scope.owner_key, instance.instance_id + ":budget")).fetchone()
        self.assertEqual([tuple(row) for row in transactions], [("income", 100), ("spend", 6), ("spend", 20), ("spend", 6)])
        self.assertEqual(tuple(reservation), (0, "released"))

    def test_payment_failure_rolls_back_all_domain_effects_and_terminal_event(self):
        instance = self.advance(self.running(), 0, "reserve")
        original = self.wallet.apply_effect
        def fail_release(db, instance, effect):
            if effect.effect_id.endswith(":release"):
                raise RuntimeError("interrupted release")
            return original(db, instance, effect)
        event_count = len(self.storage.pending_events("kernel.audit.v1"))
        with patch.object(self.wallet, "apply_effect", side_effect=fail_release):
            with self.assertRaisesRegex(RuntimeError, "interrupted release"):
                self.advance(instance, 180, "finish-rollback")
        self.assertEqual(self.quote(), (100, 36, 64))
        self.assertEqual(len(self.wallet.transactions(self.scope.owner_key)), 1)
        self.assertEqual(len(self.storage.pending_events("kernel.audit.v1")), event_count)
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=200))
        self.assertEqual(recovered.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(self.quote(), (68, 0, 68))

    def test_unknown_keeps_reservation_and_pause_resume_retains_original_quote(self):
        instance = self.advance(self.running(), 0, "reserve")
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="pause", now=NOW + timedelta(seconds=60))
        resumed = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
                                    command_id="resume", now=NOW + timedelta(seconds=100))
        self.assertEqual(self.quote(), (94, 30, 64))
        unknown = self.kernel.mark_unknown(self.scope, resumed.instance_id, expected_revision=resumed.revision,
                                           command_id="unknown", operation_id="original", now=NOW + timedelta(seconds=101))
        self.reopen()
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(days=5))
        self.assertEqual(recovered, unknown)
        with self.assertRaises(KernelError):
            self.kernel.cancel(self.scope, unknown.instance_id, expected_revision=unknown.revision,
                               command_id="cannot-cancel", now=NOW + timedelta(days=5))
        self.assertEqual(self.quote(), (94, 30, 64))

    def occupy_funds(self):
        self.wallet.reserve(self.scope.owner_key, "racing-budget", m.Money(70, "CNY"),
            operation_id="racing-reserve", expected_revision=self.wallet.quote_available(self.scope.owner_key).revision,
            review_at=NOW + timedelta(hours=1), evidence=Evidence("racing-quote", NOW, m.Provenance.SIMULATED, "racing-instance"))

    def propose_tidy(self):
        return self.kernel.propose(self.scope, m.TemplateRef(self.tidy.id, self.tidy.version),
            opportunity_key="next-tidy", reason="tidy_requested", now=NOW, expires_at=NOW + timedelta(hours=1))

    def test_runtime_explicit_embedding_drives_purchase_recovery_policy(self):
        import asyncio
        from good_life.runtime import LifeRuntime
        self.kernel.close()
        runtime = LifeRuntime(self.storage, object(), kernel_config=KernelConfig(enabled=True),
                              kernel_registry=self.registry, kernel_access=self.access)
        self.assertIsInstance(runtime.kernel, WalletActivities)
        self.activities = runtime.kernel
        self.kernel = runtime.kernel.kernel
        try:
            instance = self.running()
            self.occupy_funds()
            stats = runtime.advance_kernel(now=NOW)
            self.assertEqual(stats["cancelled_unfunded"], 1)
            self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
            self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id).status, m.ActivityStatus.CANCELLED)
        finally:
            asyncio.run(runtime.close())

    def test_tick_cancels_unfunded_purchase_and_starts_pending_candidate(self):
        instance = self.running()
        self.occupy_funds()
        candidate = self.propose_tidy()
        stats = self.activities.tick(now=NOW)
        self.assertEqual(stats["cancelled_unfunded"], 1)
        self.assertEqual(stats["started"], 1)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, candidate.candidate_id)[1].status, m.CandidateStatus.CONSUMED)
        self.assertEqual(self.activities.tick(now=NOW + timedelta(seconds=60))["failures"], ())
        self.assertEqual(self.quote(), (100, 70, 30))

    def test_tick_handles_reservation_race_inside_kernel_tick(self):
        instance = self.running()
        self.propose_tidy()
        advance = self.kernel.advance
        def race(*args, **kwargs):
            self.occupy_funds()
            return advance(*args, **kwargs)
        with patch.object(self.kernel, "advance", side_effect=race):
            stats = self.activities.tick(now=NOW)
        self.assertEqual(stats["cancelled_unfunded"], 1)
        self.assertIn("insufficient_resource", stats["failures"])
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        following = self.activities.tick(now=NOW + timedelta(seconds=1))
        self.assertEqual(following["failures"], ())
        self.assertEqual(following["started"], 1)

    def test_pause_unfunded_purchase_uses_same_audited_cancellation_policy(self):
        instance = self.running()
        self.occupy_funds()
        final = self.activities.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
                                       command_id="pause-refused-reserve", now=NOW)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.assertEqual(self.wallet.recovery_audit(self.scope.owner_key)[0]["reason_code"], "insufficient_resource")

    def assert_refused_command_replays(self, method):
        instance = self.running()
        self.occupy_funds()
        command = dict(expected_revision=instance.revision, command_id="original-refused-command", now=NOW)
        invoke = getattr(self.activities, method)
        final = invoke(self.scope, instance.instance_id, **command)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(invoke(self.scope, instance.instance_id, **command), final)
        self.reopen()
        invoke = getattr(self.activities, method)
        self.assertEqual(invoke(self.scope, instance.instance_id, **command), final)
        for change in ({"now": NOW + timedelta(seconds=1)}, {"expected_revision": final.revision}):
            with self.subTest(change=change), self.assertRaises(KernelError) as caught:
                invoke(self.scope, instance.instance_id, **{**command, **change})
            self.assertEqual(caught.exception.code, "invalid_config")
        other_method = "pause" if method == "advance" else "advance"
        with self.assertRaises(KernelError) as caught:
            getattr(self.activities, other_method)(self.scope, instance.instance_id, **command)
        self.assertEqual(caught.exception.code, "invalid_config")
        with self.assertRaises(KernelError) as caught:
            invoke(self.scope, instance.instance_id, **{**command, "command_id": "start"})
        self.assertEqual(caught.exception.code, "invalid_config")
        self.assertEqual(self.quote(), (100, 70, 30))
        self.assertIsNone(self.reservation(final))

    def test_refused_advance_replays_original_response_and_rejects_changed_request(self):
        self.assert_refused_command_replays("advance")

    def test_refused_pause_replays_original_response_and_rejects_changed_request(self):
        self.assert_refused_command_replays("pause")

    def test_direct_advance_retry_cannot_bypass_decided_cancellation_after_income(self):
        instance = self.running()
        self.occupy_funds()
        with patch.object(self.kernel, "cancel", side_effect=RuntimeError("cancel interrupted")):
            with self.assertRaisesRegex(RuntimeError, "cancel interrupted"):
                self.advance(instance, 0, "original-advance")
        self.reopen()
        self.income(100, "restored-funds")
        final = self.advance(instance, 0, "original-advance")
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertIsNone(self.reservation(final))
        self.assertEqual(self.advance(instance, 0, "original-advance"), final)
        self.assertTrue(self.wallet.recovery_audit(self.scope.owner_key)[0]["completed"])
        self.assertEqual(self.activities.tick(now=NOW + timedelta(seconds=1))["failures"], ())
        self.assertEqual(self.quote(), (200, 70, 130))

    def test_recover_cannot_reanchor_or_reserve_before_decided_cancellation(self):
        instance = self.running()
        self.occupy_funds()
        with patch.object(self.kernel, "cancel", side_effect=RuntimeError("cancel interrupted")):
            with self.assertRaisesRegex(RuntimeError, "cancel interrupted"):
                self.advance(instance, 0, "original-advance")
        self.reopen()
        self.income(100, "restored-funds")
        self.assertEqual(self.activities.recover(self.scope, now=NOW + timedelta(seconds=100)), ())
        final = self.advance(instance, 0, "original-advance")
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertIsNone(self.reservation(final))
        self.assertEqual(self.activities.tick(now=NOW + timedelta(seconds=101))["failures"], ())
        self.assertTrue(self.wallet.recovery_audit(self.scope.owner_key)[0]["completed"])

    def test_refusal_decision_resumes_after_crash_even_if_income_arrives(self):
        instance = self.running()
        self.occupy_funds()
        with patch.object(self.kernel, "cancel", side_effect=RuntimeError("crash before cancellation")):
            with self.assertRaisesRegex(RuntimeError, "crash before cancellation"):
                self.advance(instance, 0, "refused-reserve")
        self.assertFalse(self.wallet.recovery_audit(self.scope.owner_key)[0]["completed"])
        self.reopen()
        self.income(100, "restored-funds")
        stats = self.activities.tick(now=NOW + timedelta(seconds=1))
        self.assertEqual(stats["cancelled_unfunded"], 1)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertTrue(self.wallet.recovery_audit(self.scope.owner_key)[0]["completed"])
        self.assertEqual(self.quote(), (200, 70, 130))

    def test_refusal_audit_finishes_after_crash_following_committed_cancellation(self):
        instance = self.running()
        self.occupy_funds()
        with patch.object(self.wallet, "complete_refusal", side_effect=RuntimeError("crash after cancellation")):
            with self.assertRaisesRegex(RuntimeError, "crash after cancellation"):
                self.advance(instance, 0, "refused-reserve")
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertFalse(self.wallet.recovery_audit(self.scope.owner_key)[0]["completed"])
        self.reopen()
        self.assertEqual(self.activities.tick(now=NOW + timedelta(seconds=1))["failures"], ())
        self.assertTrue(self.wallet.recovery_audit(self.scope.owner_key)[0]["completed"])
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))

    def test_review_deadline_is_created_at_first_reservation_and_validated_in_effect(self):
        instance = self.running()
        self.assertIsNone(instance.checkpoint["review_at"])
        step = self.registry.executor(self.template).advance(instance, NOW + timedelta(seconds=500),
            self.host.audience(self.scope), elapsed_delta_s=500)
        # A structurally consistent proposal still cannot bypass the wallet's
        # independent review deadline check. No wallet SQL is forged here.
        cp = dict(step.checkpoint)
        cp["review_at"] = NOW.isoformat()
        command = dict(cp["commands"][0])
        command["payload"] = {**command["payload"], "review_at": cp["review_at"]}
        cp["commands"] = (command,)
        forged = replace(instance, checkpoint=freeze(cp))
        effect = m.Effect(command["effect_id"], instance.instance_id, "economy", "economy.purchase",
                          freeze(command["payload"]), NOW + timedelta(seconds=500), ())
        with self.storage.transaction() as db:
            with self.assertRaisesRegex(KernelError, "review must follow"):
                self.wallet.apply_effect(db, forged, effect)
        self.assertEqual(self.quote(), (100, 0, 100))
        instance = self.advance(instance, 500, "late-first-reserve")
        self.assertEqual(self.reservation(instance).review_at, NOW + timedelta(seconds=680))
        self.assertEqual(instance.checkpoint["review_at"], self.reservation(instance).review_at.isoformat())

    def test_external_release_of_active_purchase_is_rejected_then_cancel_succeeds(self):
        instance = self.advance(self.running(), 0, "reserve")
        with self.assertRaises(KernelError) as caught:
            self.wallet.release(self.scope.owner_key, instance.instance_id + ":budget", operation_id="external-release",
                expected_revision=self.wallet.quote_available(self.scope.owner_key).revision,
                evidence=Evidence("external", NOW, m.Provenance.SIMULATED, instance.instance_id))
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(self.quote(), (100, 36, 64))
        final = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="cancel-after-rejected-release", now=NOW)
        self.assertEqual(final.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.quote(), (100, 0, 100))

    def test_paused_purchase_ambiguity_reports_missing_dependency_not_insufficient_cash(self):
        instance = self.advance(self.running(), 0, "reserve")
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="pause-purchase", now=NOW)
        next_instance = self.prepared("second-purchase")
        failed = self.kernel.start(self.scope, next_instance.instance_id, expected_revision=next_instance.revision,
                                   command_id="second-purchase-start", now=NOW)
        self.assertEqual(failed.status, m.ActivityStatus.FAILED)
        self.assertEqual(failed.reason_code, m.ReasonCode.MISSING_DEPENDENCY)
        self.assertEqual(self.quote(), (100, 36, 64))
        resumed = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
                                    command_id="resume-first", now=NOW)
        self.assertEqual(resumed.status, m.ActivityStatus.RUNNING)

    def abandon_cancel(self, instance, seconds):
        at = NOW + timedelta(seconds=seconds)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("cancel proposal persisted")):
            with self.assertRaisesRegex(RuntimeError, "cancel proposal persisted"):
                self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="cancel-before-abandon", now=at)
        result_id = self.kernel.repo.pending_result(self.scope.owner_key, instance.instance_id)
        proposal, _ = self.kernel.repo.result(self.scope.owner_key, result_id)
        # Simulate an already persisted pre-extension abandonment, so the A2
        # administrative fallback still exercises its actual release path.
        with patch.object(self.registry.executor(self.template), "abandon_resources", None):
            final = self.kernel.abandon_result(self.scope, result_id, expected_revision=proposal.expected_revision,
                command_id="abandon-local-cancel", reason="explicit local abandonment", now=at + timedelta(seconds=1))
        return result_id, final

    def cleanup_abandoned(self, result_id, final, **overrides):
        command = dict(abandon_command_id="abandon-local-cancel", abandon_reason="explicit local abandonment",
            expected_instance_revision=final.revision, operation_id="cleanup-abandoned",
            expected_revision=self.wallet.quote_available(self.scope.owner_key).revision,
            evidence=Evidence("checked-abandon-cleanup", final.updated_at, m.Provenance.SIMULATED, final.instance_id))
        command.update(overrides)
        return self.wallet.release_abandoned(self.scope.owner_key, result_id, **command)

    def test_abandoned_cancel_releases_actual_reservation_without_paying_proposal(self):
        instance = self.advance(self.running(), 0, "reserve")
        result_id, final = self.abandon_cancel(instance, 60)
        self.assertTrue(final.checkpoint["released"])
        self.assertEqual(final.checkpoint["completed_units"]["paid_steps"], 1)
        self.assertEqual(self.quote(), (100, 36, 64))
        revision = self.wallet.quote_available(self.scope.owner_key).revision
        result = self.cleanup_abandoned(result_id, final)
        self.assertEqual(self.quote(), (100, 0, 100))
        self.assertEqual(self.cleanup_abandoned(result_id, final, expected_revision=revision), result)
        self.assertEqual(len(self.wallet.transactions(self.scope.owner_key)), 1)
        audit = self.wallet.recovery_audit(self.scope.owner_key)[0]
        self.assertEqual((audit["actual_paid_minor"], audit["released_minor"], audit["actual_remaining_minor"]), (0, 36, 0))
        self.assertEqual(audit["abandoned_checkpoint"], wire(final.checkpoint))

    def test_abandon_cleanup_rejects_pending_proposal_without_failed_abandonment(self):
        instance = self.advance(self.running(), 0, "reserve")
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("pending cancel")):
            with self.assertRaisesRegex(RuntimeError, "pending cancel"):
                self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="pending-cancel", now=NOW + timedelta(seconds=60))
        result_id = self.kernel.repo.pending_result(self.scope.owner_key, instance.instance_id)
        current = self.kernel.repo.instance(self.scope.owner_key, instance.instance_id)
        with self.assertRaises(KernelError) as caught:
            self.cleanup_abandoned(result_id, current)
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(self.quote(), (100, 36, 64))
        self.assertEqual(self.wallet.recovery_audit(self.scope.owner_key), ())

    def test_abandon_cleanup_requires_durable_matching_command_and_keeps_prior_payment(self):
        instance = self.advance(self.running(), 0, "reserve")
        instance = self.advance(instance, 60)
        result_id, final = self.abandon_cancel(instance, 120)
        for override in ({"abandon_command_id": "not-an-abandon-command"}, {"abandon_reason": "forged reason"},
                         {"expected_instance_revision": final.revision - 1},
                         {"evidence": Evidence("other", final.updated_at, m.Provenance.SIMULATED, "other-instance")}):
            with self.subTest(override=override), self.assertRaises(KernelError):
                self.cleanup_abandoned(result_id, final, **override)
        self.assertEqual(self.quote(), (94, 30, 64))
        self.cleanup_abandoned(result_id, final)
        self.assertEqual(self.quote(), (94, 0, 94))
        self.assertEqual([t.amount_minor for t in self.wallet.transactions(self.scope.owner_key)], [100, 6])
        self.assertEqual(self.wallet.recovery_audit(self.scope.owner_key)[0]["actual_paid_minor"], 6)

    def test_domain_rejects_forged_effect_and_terminal_omission(self):
        instance = self.advance(self.running(), 0, "reserve")
        fake = m.Effect(instance.instance_id + ":pay:1", instance.instance_id, "economy", "economy.purchase",
                        freeze({"reservation_id": instance.instance_id + ":budget", "step_id": "1", "amount_minor": 6}), NOW, ())
        with self.storage.transaction() as db:
            with self.assertRaises(KernelError):
                self.wallet.apply_effect(db, instance, fake)
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("stop")):
            with self.assertRaises(RuntimeError):
                self.advance(instance, 180, "proposal")
        result_id = self.kernel.repo.pending_result(self.scope.owner_key, instance.instance_id)
        result, _ = self.kernel.repo.result(self.scope.owner_key, result_id)
        current = self.kernel.repo.instance(self.scope.owner_key, instance.instance_id)
        executor = self.registry.executor(self.template)
        with self.assertRaises(KernelError):
            executor.validate_result(current, replace(result, effects=()))
        self.assertEqual(self.quote(), (100, 36, 64))


if __name__ == "__main__":
    unittest.main()
