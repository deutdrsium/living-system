"""Regression cases for the independent A1 review, using temporary SQLite."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
import time
from unittest.mock import patch

from test_kernel_support import CountingExecutor, KernelCase, NOW, m
from good_life.activities.local_order import LocalTimedUnits, SPEC, TEMPLATE
from good_life.kernel import ActivityKernel, ComponentRegistry, KernelError
from good_life.kernel.clock import MonotonicClock
from good_life.kernel.codec import freeze
from good_life.kernel.gating import PreparedActivity
from good_life.kernel.lifecycle import ExecutorStep


class OpaqueCheckpointExecutor(LocalTimedUnits):
    name = "opaque_checkpoint"

    def prepare(self, template, now):
        return PreparedActivity("tidying", 3, freeze({"counter": 0}), freeze({}))

    def start(self, instance, now):
        return instance.checkpoint

    def resume(self, instance, now):
        return instance.checkpoint

    def advance(self, instance, now, audience, *, elapsed_delta_s):
        elapsed = min(3, instance.elapsed_s + elapsed_delta_s)
        units = freeze({"generic_pieces": int(elapsed == 3)})
        outcome = m.Outcome(m.OutcomeStatus.SUCCEEDED, False, "local opaque rule done") if elapsed == 3 else None
        return ExecutorStep("tidying", freeze({"counter": elapsed}), elapsed, units, outcome=outcome)

    def validate_result(self, instance, result):
        return instance.checkpoint["counter"] == 3 and result.progress.completed_units == {"generic_pieces": 1}


class KernelReviewTests(KernelCase):
    def test_replayed_start_after_restart_cannot_bypass_recovery_gate(self):
        running = self.running()
        progressed = self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
            command_id="known-progress", now=NOW + timedelta(seconds=65))
        self.reopen()
        self.access.free_s = 10
        resumes = self.executor.resumes
        replay = self.kernel.start(self.scope, running.instance_id, expected_revision=1,
                                  command_id="start-opportunity-1", now=NOW)
        self.assertEqual(replay, running)  # An original receipt, not live state.
        self.assertIsNone(self.kernel._anchor(progressed))
        with self.assertRaises(KernelError) as caught:
            self.kernel.advance(self.scope, running.instance_id, expected_revision=progressed.revision,
                command_id="must-recover", now=NOW + timedelta(hours=1))
        self.assertEqual(caught.exception.code, "stale_version")
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(hours=1))
        self.assertEqual(recovered.status, m.ActivityStatus.PAUSED)
        self.assertEqual(recovered.reason_code, m.ReasonCode.NO_TIME_WINDOW)
        self.assertEqual(recovered.elapsed_s, 65)
        self.assertEqual(self.executor.resumes, resumes)
        self.assertIsNone(self.kernel._anchor(recovered))

    def test_wrong_revision_anchor_requires_recovery(self):
        running = self.running()
        key = (self.scope.owner_key, running.instance_id)
        self.kernel._session_instances[key] = (running.revision - 1, 0)
        with self.assertRaises(KernelError):
            self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
                                command_id="stale-anchor", now=NOW + timedelta(seconds=10))
        resumes = self.executor.resumes
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=10))
        self.assertEqual(self.executor.resumes, resumes + 1)
        self.assertEqual(recovered.elapsed_s, 0)
        self.assertEqual(self.kernel._anchor(recovered)[0], recovered.revision)

    def test_replaying_start_does_not_modify_a_live_anchor(self):
        running = self.running()
        current = self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
            command_id="progress", now=NOW + timedelta(seconds=65))
        before = dict(self.kernel._session_instances)
        self.kernel.start(self.scope, running.instance_id, expected_revision=1,
                          command_id="start-opportunity-1", now=NOW)
        self.assertEqual(self.kernel._session_instances, before)
        after = self.kernel.advance(self.scope, current.instance_id, expected_revision=current.revision,
                                   command_id="continued", now=NOW + timedelta(seconds=70))
        self.assertEqual(after.elapsed_s, 70)

    def test_running_can_cancel_directly_after_restart_without_offline_progress(self):
        running = self.running()
        current = self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
                                     command_id="progress", now=NOW + timedelta(seconds=65))
        self.reopen()
        self.access.free_s = 0
        cancelled = self.kernel.cancel(self.scope, current.instance_id, expected_revision=current.revision,
                                      command_id="cancel-without-recovery", now=NOW + timedelta(days=2))
        self.assertEqual(cancelled.status, m.ActivityStatus.CANCELLED)
        self.assertEqual((cancelled.elapsed_s, self.units()), (65, 1))
        self.assertTrue(cancelled.outcome.partial)
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))

    def test_nonpausable_phase_with_failed_recovery_gate_can_still_cancel(self):
        running = self.running()
        self.reopen()
        self.executor.pausable_phases = ()
        self.access.free_s = 0
        with self.assertRaises(KernelError) as caught:
            self.kernel.recover(self.scope, now=NOW + timedelta(seconds=30))
        self.assertEqual(caught.exception.code, "no_time_window")
        current = self.kernel.repo.instance(self.scope.owner_key, running.instance_id)
        self.assertEqual(current, running)
        cancelled = self.kernel.cancel(self.scope, current.instance_id, expected_revision=current.revision,
                                      command_id="cancel-blocked-phase", now=NOW + timedelta(seconds=30))
        self.assertEqual(cancelled.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(cancelled.elapsed_s, 0)
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))

    def test_empty_ticks_append_neither_revisions_nor_commands(self):
        running = self.running()
        counts = (self.count("life_kernel_revisions"), self.count("life_kernel_commands"))
        for fraction in (0, 0.1, 0.25, 0.5, 0.75, 0.99):
            result = self.kernel.tick(now=NOW + timedelta(seconds=fraction))
            self.assertEqual(result, {"advanced": 0, "started": 0, "failures": ()})
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, running.instance_id), running)
        self.assertEqual((self.count("life_kernel_revisions"), self.count("life_kernel_commands")), counts)
        changed = self.kernel.tick(now=NOW + timedelta(seconds=1))
        self.assertEqual(changed["advanced"], 1)
        self.assertEqual((self.count("life_kernel_revisions"), self.count("life_kernel_commands")),
                         (counts[0] + 1, counts[1] + 1))

    def test_completed_units_does_not_require_a_named_checkpoint_key(self):
        executor = OpaqueCheckpointExecutor()
        self.registry.register_executor(executor)
        template = self.registry.register_template(replace(TEMPLATE, id="opaque", executor="opaque_checkpoint@1",
            params=freeze({"duration_s": 3, "unit_s": 1, "label": "opaque local rule"})))
        candidate = self.propose("opaque", template=template)
        prepared = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="opaque-prepare", now=NOW).instance
        running = self.kernel.start(self.scope, prepared.instance_id, expected_revision=1, command_id="opaque-start", now=NOW)
        final = self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
                                   command_id="opaque-finish", now=NOW + timedelta(seconds=3))
        self.assertEqual(final.status, m.ActivityStatus.COMPLETED)
        self.assertNotIn("completed_units", final.checkpoint)
        with self.storage._lock:
            row = self.storage._connection.execute("SELECT result_id FROM life_kernel_results WHERE instance_id=?",
                                                   (final.instance_id,)).fetchone()
        proposal, _final = self.kernel.repo.result(self.scope.owner_key, row[0])
        self.assertEqual(proposal.progress.completed_units, {"generic_pieces": 1})

    def test_unexpected_scope_exception_is_reported_and_other_owner_still_starts(self):
        other = replace(self.scope, owner_key=m.OwnerKey("z-other-owner"), context_key=m.ContextKey("z-context"))
        self.access.inventory += (other,)
        self.propose("healthy", scope=other)
        authorize = self.access.authorize
        def fail_one(scope, action):
            if scope == self.scope:
                raise RuntimeError("private diagnostic must not leak")
            return authorize(scope, action)
        with patch.object(self.access, "authorize", side_effect=fail_one):
            outcome = self.kernel.tick(now=NOW)
        self.assertEqual(outcome["failures"], ("unexpected_error:RuntimeError",))
        self.assertEqual(outcome["started"], 1)
        active, = self.kernel.repo.active(other.owner_key)
        self.assertEqual(active.status, m.ActivityStatus.RUNNING)

    def test_settle_only_uses_pinned_dynamic_executor_version(self):
        paused = self.running("paused-dynamic")
        paused = self.kernel.pause(self.scope, paused.instance_id, expected_revision=paused.revision,
                                   command_id="pause-dynamic", now=NOW)
        running = self.running("running-dynamic")
        pending = self.propose("pending-dynamic")

        self.registry = ComponentRegistry()
        self.registry.register_module(SPEC, self.domain)
        replacement = CountingExecutor()
        replacement.version = "2"
        self.registry.register_executor(replacement)
        staged = self.registry._stage_group(settlement_only=True)
        staged.register_executor(self.executor)
        self.registry._publish_group(staged)
        self.reopen()
        later = NOW + timedelta(seconds=65)
        resumes, advances = self.executor.resumes, self.executor.advances

        self.assertEqual(self.kernel.recover(self.scope, now=later), ())
        self.assertEqual(self.kernel.tick(now=later), {"advanced": 0, "started": 0, "failures": ()})
        for command, instance in ((self.kernel.start, paused), (self.kernel.advance, running)):
            with self.assertRaisesRegex(KernelError, "capability_disabled") as denied:
                command(self.scope, instance.instance_id, expected_revision=instance.revision,
                        command_id="blocked-" + instance.instance_id, now=later)
            self.assertEqual(denied.exception.code, "missing_dependency")
            self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id), instance)
        self.assertEqual((self.executor.resumes, self.executor.advances), (resumes, advances))
        cancelled = self.kernel.cancel(self.scope, running.instance_id, expected_revision=running.revision,
                                       command_id="cancel-dynamic", now=later)
        self.assertEqual((cancelled.status, cancelled.elapsed_s), (m.ActivityStatus.CANCELLED, running.elapsed_s))

        self.registry.register_template(TEMPLATE)
        with self.assertRaisesRegex(KernelError, "capability_disabled"):
            self.propose("new-disabled-dynamic", now=later)
        prepares = self.executor.prepares
        denied = self.kernel.prepare_candidate(self.scope, pending.candidate_id, command_id="prepare-disabled", now=later)
        self.assertEqual(denied.gate.reason_code, m.ReasonCode.MISSING_DEPENDENCY)
        self.assertIsNone(denied.instance)
        self.assertEqual(self.executor.prepares, prepares)

        template = self.registry.register_template(replace(TEMPLATE, version=m.TemplateVersion(2),
                                                           executor=f"{replacement.name}@2"))
        self.kernel.propose(self.scope, m.TemplateRef(template.id, template.version), opportunity_key="enabled-version",
                            reason="tidy_requested", now=later, expires_at=later + timedelta(hours=1))
        self.assertEqual(self.kernel.tick(now=later), {"advanced": 0, "started": 1, "failures": ()})
        self.assertEqual((replacement.prepares, replacement.starts), (1, 1))

    def _two_pending_cancellations(self, prefix):
        paused = []
        for suffix in ("first", "second"):
            key = prefix + "-" + suffix
            running = self.running(key)
            paused.append(self.kernel.pause(self.scope, running.instance_id, expected_revision=running.revision,
                                            command_id="pause-" + key, now=NOW))
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash after durable proposal")):
            for instance in paused:
                with self.assertRaisesRegex(RuntimeError, "crash after durable proposal"):
                    self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                       command_id="cancel-" + instance.instance_id, now=NOW)
        return tuple(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id)
                     for instance in sorted(paused, key=lambda item: item.instance_id))

    def test_explicit_recovery_attempts_later_proposals_before_raising(self):
        for method in (self.kernel.recover_pending, self.kernel.recover):
            with self.subTest(method=method.__name__):
                bad, good = self._two_pending_cancellations(method.__name__)
                original = self.executor.validate_result
                with patch.object(self.executor, "validate_result", side_effect=lambda instance, result:
                                  instance.instance_id != bad.instance_id and original(instance, result)):
                    with self.assertRaises(KernelError) as caught:
                        method(self.scope, now=NOW)
                self.assertEqual(caught.exception.code, "invalid_config")
                self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, bad.instance_id), bad)
                self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, good.instance_id).status,
                                 m.ActivityStatus.CANCELLED)
                bad_id, = self.kernel.repo.pending_results(self.scope.owner_key, self.scope.context_key)
                self.assertEqual(bad_id, "kernel-result-" + bad.instance_id)
                self.kernel.commit_result(self.scope, bad_id, now=NOW)

    def test_tick_isolates_result_rollback_and_continues_same_scope(self):
        bad, good = self._two_pending_cancellations("tick")
        self.propose("next-healthy-candidate")
        original = self.kernel._effects_events

        def fail_one(db, instance, *args, **kwargs):
            if instance.instance_id == bad.instance_id:
                raise RuntimeError("private diagnostic must not leak")
            return original(db, instance, *args, **kwargs)

        with patch.object(self.kernel, "_effects_events", side_effect=fail_one):
            outcome = self.kernel.tick(now=NOW)
        self.assertEqual(outcome, {"advanced": 0, "started": 1, "failures": ("unexpected_error:RuntimeError",)})
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, bad.instance_id), bad)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, good.instance_id).status, m.ActivityStatus.CANCELLED)
        self.assertEqual(self.kernel.repo.pending_results(self.scope.owner_key, self.scope.context_key),
                         ("kernel-result-" + bad.instance_id,))

    def _pending_terminal(self):
        running = self.running()
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash after durable proposal")):
            with self.assertRaises(RuntimeError):
                self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
                    command_id="pending-finish", now=NOW + timedelta(seconds=180))
        current = self.kernel.repo.instance(self.scope.owner_key, running.instance_id)
        return current, self.kernel.repo.pending_result(self.scope.owner_key, running.instance_id)

    def test_missing_executor_pending_result_can_be_explicitly_abandoned(self):
        current, result_id = self._pending_terminal()
        self.reopen()
        self.registry.executors.clear()
        with self.assertRaises(KernelError) as caught:
            self.kernel.recover(self.scope, now=NOW + timedelta(seconds=240))
        self.assertEqual(caught.exception.code, "missing_dependency")
        with self.assertRaises(KernelError):
            self.kernel.cancel(self.scope, current.instance_id, expected_revision=current.revision,
                               command_id="blocked-cancel", now=NOW + timedelta(seconds=240))
        # An administrator can perform this safety action even with new work off.
        self.kernel.close()
        self.kernel = ActivityKernel(self.storage, self.registry, access=self.access)
        kwargs = dict(expected_revision=current.revision, command_id="admin-abandon", reason="pinned executor removed",
                      now=NOW + timedelta(seconds=240))
        final = self.kernel.abandon_result(self.scope, result_id, **kwargs)
        self.assertEqual((final.status, final.reason_code), (m.ActivityStatus.FAILED, m.ReasonCode.INVALID_CONFIG))
        self.assertTrue(final.outcome.partial)
        self.assertEqual(final.elapsed_s, 180)
        self.assertEqual((self.units(), self.count("life_kernel_effects"), self.count("life_events")), (0, 0, 0))
        self.assertFalse(self.kernel.repo.foreground_busy(self.scope.owner_key))
        proposal, saved_final = self.kernel.repo.result(self.scope.owner_key, result_id)
        self.assertEqual(proposal.outcome.status, m.OutcomeStatus.SUCCEEDED)
        self.assertEqual(saved_final, final)
        self.assertEqual(self.kernel.abandon_result(self.scope, result_id, **kwargs), final)
        self.assertEqual(self.kernel.commit_result(self.scope, result_id, now=NOW + timedelta(seconds=300)), final)
        self.assertEqual(self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=300)), ())

    def test_abandonment_checks_distinct_permission_context_revision_and_unknown_fence(self):
        current, result_id = self._pending_terminal()
        authorize = self.access.authorize
        kwargs = dict(expected_revision=current.revision, command_id="admin", reason="explicit local abandonment",
                      now=NOW + timedelta(seconds=240))
        with patch.object(self.access, "authorize", side_effect=lambda scope, action:
                          action != "kernel.abandon_result" and authorize(scope, action)):
            with self.assertRaises(KernelError) as caught:
                self.kernel.abandon_result(self.scope, result_id, **kwargs)
        self.assertEqual(caught.exception.code, "unauthorized")
        other = replace(self.scope, context_key=m.ContextKey("other-private"))
        self.access.inventory += (other,)
        with self.assertRaises(KernelError):
            self.kernel.abandon_result(other, result_id, **kwargs)
        with self.assertRaises(KernelError):
            self.kernel.abandon_result(self.scope, result_id, **{**kwargs, "expected_revision": current.revision + 1})
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, current.instance_id), current)
        # Simulate an external operation discovered in audit. Neither a caller's
        # reason string nor abandonment is evidence for leaving UNKNOWN.
        unknown = replace(current, revision=m.Revision(current.revision + 1), status=m.ActivityStatus.UNKNOWN,
                          reason_code=m.ReasonCode.RESULT_UNKNOWN, operation_refs=("original-operation",))
        with self.storage.transaction() as db:
            self.kernel.repo.save(db, current, unknown)
        with self.assertRaises(KernelError) as caught:
            self.kernel.abandon_result(self.scope, result_id, **{**kwargs, "expected_revision": unknown.revision})
        self.assertEqual(caught.exception.code, "result_unknown")
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, current.instance_id), unknown)

    def test_real_monotonic_clock_path_uses_system_interval_not_simulation_time(self):
        self.kernel.clock = MonotonicClock()
        before = time.monotonic()
        running = self.running()
        anchor = self.kernel._anchor(running)[1]
        self.assertLessEqual(before, anchor)
        self.assertLessEqual(anchor, time.monotonic())
        progressed = self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
            command_id="real-clock", now=NOW + timedelta(hours=2))
        after = time.monotonic()
        self.assertLessEqual(progressed.elapsed_s, int(after - anchor))
        self.assertLess(progressed.elapsed_s, 180)
        self.assertNotEqual(progressed.status, m.ActivityStatus.COMPLETED)

    def _two_paused_contexts(self):
        other = replace(self.scope, context_key=m.ContextKey("other-private"))
        self.access.inventory += (other,)
        first = self.running("first")
        first = self.kernel.pause(self.scope, first.instance_id, expected_revision=first.revision, command_id="pause-first", now=NOW)
        second = self.running("second", scope=other)
        second = self.kernel.pause(other, second.instance_id, expected_revision=second.revision, command_id="pause-second", now=NOW)
        return other, first, second

    def test_start_foreground_mutex_across_two_paused_contexts(self):
        other, first, second = self._two_paused_contexts()
        def start(item):
            scope, instance = item
            return self.kernel.start(scope, instance.instance_id, expected_revision=instance.revision,
                                     command_id="resume-" + instance.instance_id, now=NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = tuple(pool.map(start, ((self.scope, first), (other, second))))
        self.assertEqual(sum(item.status == m.ActivityStatus.RUNNING for item in outcomes), 1)
        self.assertEqual(sum(item.status == m.ActivityStatus.PAUSED for item in outcomes), 1)

    def test_recover_foreground_mutex_with_other_context_start(self):
        other, first, second = self._two_paused_contexts()
        first = self.kernel.start(self.scope, first.instance_id, expected_revision=first.revision, command_id="resume-first", now=NOW)
        self.reopen()
        def recover():
            return self.kernel.recover(self.scope, now=NOW + timedelta(seconds=30))[0]
        def start_other():
            return self.kernel.start(other, second.instance_id, expected_revision=second.revision,
                                     command_id="try-other-start", now=NOW + timedelta(seconds=30))
        with ThreadPoolExecutor(max_workers=2) as pool:
            recovery = pool.submit(recover)
            contender = pool.submit(start_other)
            recovered, refused = recovery.result(), contender.result()
        self.assertEqual((recovered.status, refused.status), (m.ActivityStatus.RUNNING, m.ActivityStatus.PAUSED))
        self.assertEqual(recovered.elapsed_s, first.elapsed_s)
        self.assertEqual(recovered.started_at, first.started_at)
        self.assertEqual(self.kernel._anchor(recovered)[0], recovered.revision)


if __name__ == "__main__":
    import unittest
    unittest.main()
