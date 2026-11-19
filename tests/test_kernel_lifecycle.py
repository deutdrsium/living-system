from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from test_kernel_support import KernelCase, NOW, m
from good_life.kernel import KernelError
from good_life.kernel.lifecycle import terminal_status, validate_transition


class KernelLifecycleTests(KernelCase):
    def test_normal_completion_commits_owned_facts_and_global_events_once(self):
        instance = self.running()
        self.assertEqual(instance.status, m.ActivityStatus.RUNNING)
        self.assertEqual(instance.started_at, NOW)
        instance = self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="step-60", now=NOW + timedelta(seconds=60))
        self.assertEqual((instance.elapsed_s, self.units()), (60, 1))
        revision = instance.revision
        completed = self.kernel.advance(self.scope, instance.instance_id, expected_revision=revision,
            command_id="finish", now=NOW + timedelta(seconds=180))
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(completed.outcome.status, m.OutcomeStatus.SUCCEEDED)
        self.assertFalse(completed.outcome.partial)
        self.assertEqual((completed.elapsed_s, self.units()), (180, 3))
        self.assertEqual(self.count("life_kernel_effects"), 3)
        events = self.storage.pending_events("kernel.audit.v1")
        self.assertEqual(len(events), 2)
        event_ids = tuple(event["event_id"] for event in events)
        self.assertEqual(len(set(event_ids)), 2)
        for event in events:
            self.assertEqual(set(event), {"sequence", "event_id", "owner_key", "context_key", "kind", "payload", "content_hash", "occurred_at", "consumers"})
            envelope = self.kernel.repo.envelope(event["event_id"])
            self.assertEqual(envelope.provenance, m.Provenance.SIMULATED)
            self.assertEqual(envelope.audience_bound.context_keys, (self.scope.context_key,))
        replay = self.kernel.advance(self.scope, completed.instance_id, expected_revision=revision,
            command_id="finish", now=NOW + timedelta(seconds=180))
        self.assertEqual(replay, completed)
        self.assertEqual(self.units(), 3)
        self.assertEqual(tuple(event["event_id"] for event in self.storage.pending_events("kernel.audit.v1")), event_ids)
        self.assertEqual(self.executor.advances, 2)
        self.assertEqual(completed.started_at, NOW)

    def test_activity_result_is_only_a_proposal_not_fabricated_completion(self):
        instance = self.running()
        proposal = m.ActivityResult(instance.instance_id, instance.revision,
            m.Outcome(m.OutcomeStatus.SUCCEEDED, False, "unverified completion"), NOW,
            m.Progress(0, instance.checkpoint["completed_units"], False), (), (), (), (), ())
        with self.storage.transaction() as db:
            result_id = self.kernel.repo.stage_result(db, self.scope, proposal)
        with self.assertRaises(KernelError):
            self.kernel.commit_result(self.scope, result_id, now=NOW)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id), instance)
        self.assertEqual((self.units(), self.count("life_events")), (0, 0))

    def test_cancel_preserves_partial_progress_and_only_actual_effects(self):
        instance = self.running()
        cancelled = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="cancel", now=NOW + timedelta(seconds=75))
        self.assertEqual(cancelled.status, m.ActivityStatus.CANCELLED)
        self.assertEqual(cancelled.reason_code, m.ReasonCode.CANCELLED)
        self.assertEqual(cancelled.outcome.status, m.OutcomeStatus.CANCELLED)
        self.assertTrue(cancelled.outcome.partial)
        self.assertEqual((cancelled.elapsed_s, self.units()), (75, 1))
        self.assertEqual(cancelled.checkpoint["completed_units"]["tidied_units"], 1)
        with self.assertRaises(KernelError):
            self.kernel.start(self.scope, cancelled.instance_id, expected_revision=cancelled.revision,
                              command_id="reopen-terminal", now=NOW + timedelta(seconds=80))
        self.assertEqual(self.units(), 1)

    def test_cancel_preparing_keeps_no_start_or_fictional_facts(self):
        instance = self.prepared()
        final = self.kernel.cancel(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="cancel-before-start", now=NOW)
        self.assertIsNone(final.started_at)
        self.assertFalse(final.outcome.partial)
        self.assertEqual(final.elapsed_s, 0)
        self.assertEqual(self.units(), 0)

    def test_restart_reanchors_without_offline_work_and_finishes_once(self):
        instance = self.running()
        partial = self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="before-crash", now=NOW + timedelta(seconds=65))
        starts = self.executor.starts
        self.reopen()
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(days=2))
        self.assertEqual(recovered.elapsed_s, 65)
        self.assertEqual(recovered.started_at, NOW)
        self.assertEqual(self.executor.starts, starts)
        self.assertEqual(self.units(), 1)
        completed = self.kernel.advance(self.scope, recovered.instance_id, expected_revision=recovered.revision,
            command_id="after-restart", now=NOW + timedelta(days=2, seconds=115))
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(completed.elapsed_s, 180)
        self.assertEqual(self.units(), 3)
        self.assertEqual(self.count("life_kernel_effects"), 3)

    def test_restart_replays_durable_terminal_proposal_not_executor(self):
        instance = self.running()
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash after proposal commit")):
            with self.assertRaisesRegex(RuntimeError, "crash after proposal"):
                self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                    command_id="crash-at-finish", now=NOW + timedelta(seconds=180))
        self.assertEqual(self.units(), 0)
        self.assertEqual(self.count("life_events"), 0)
        self.assertEqual(self.count("life_kernel_results"), 1)
        advances = self.executor.advances
        self.reopen()
        completed, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=300))
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual(completed.updated_at, NOW + timedelta(seconds=300))
        self.assertEqual(self.executor.advances, advances)
        self.assertEqual((self.units(), self.count("life_events")), (3, 1))
        event, = self.storage.pending_events("kernel.audit.v1")
        envelope = self.kernel.repo.envelope(event["event_id"])
        self.assertEqual(envelope.occurred_at, NOW + timedelta(seconds=180))
        self.assertEqual(envelope.recorded_at, NOW + timedelta(seconds=300))
        self.kernel.recover(self.scope, now=NOW + timedelta(seconds=400))
        self.assertEqual((self.units(), self.count("life_events")), (3, 1))
        original = self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="crash-at-finish", now=NOW + timedelta(seconds=180))
        self.assertEqual(original, completed)

    def test_switching_off_still_allows_explicit_decided_result_settlement_only(self):
        from good_life.kernel import ActivityKernel
        instance = self.running()
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                    command_id="decided-before-disable", now=NOW + timedelta(seconds=180))
        advances = self.executor.advances
        self.kernel.close()
        self.kernel = ActivityKernel(self.storage, self.registry, access=self.access)
        self.assertEqual(self.kernel.tick(now=NOW + timedelta(seconds=300))["advanced"], 0)
        completed, = self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=300))
        self.assertEqual(completed.status, m.ActivityStatus.COMPLETED)
        self.assertEqual((self.units(), self.count("life_events"), self.executor.advances), (3, 1, advances))
        self.assertEqual(self.kernel.recover_pending(self.scope, now=NOW + timedelta(seconds=400)), ())
        with self.assertRaises(KernelError):
            self.propose("must-stay-off")
        self.assertEqual(self.count("life_kernel_instances"), 1)

    def test_domain_failure_rolls_back_instance_effects_and_outbox(self):
        instance = self.running()
        original_apply = self.domain.apply_effect
        calls = []
        def fail_second(db, instance, effect):
            calls.append(effect.effect_id)
            if len(calls) == 2:
                raise RuntimeError("domain transaction interrupted")
            return original_apply(db, instance, effect)
        with patch.object(self.domain, "apply_effect", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "domain transaction"):
                self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                    command_id="finish-rollback", now=NOW + timedelta(seconds=180))
        self.assertEqual((self.units(), self.count("life_kernel_effects"), self.count("life_events")), (0, 0, 0))
        current = self.kernel.repo.instance(self.scope.owner_key, instance.instance_id)
        self.assertEqual(current.status, m.ActivityStatus.RUNNING)
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=240))
        self.assertEqual(recovered.status, m.ActivityStatus.COMPLETED)
        self.assertEqual((self.units(), self.count("life_events")), (3, 1))

    def test_unknown_never_restarts_reissues_or_cancels_original_operation(self):
        instance = self.running()
        unknown = self.kernel.mark_unknown(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="unknown", operation_id="original-operation", now=NOW + timedelta(seconds=10))
        starts, advances = self.executor.starts, self.executor.advances
        self.reopen()
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(hours=1))
        self.assertEqual(recovered, unknown)
        self.assertEqual(recovered.operation_refs, ("original-operation",))
        for action in (self.kernel.cancel, self.kernel.advance, self.kernel.start):
            with self.subTest(action=action.__name__), self.assertRaises(KernelError):
                action(self.scope, unknown.instance_id, expected_revision=unknown.revision,
                       command_id="try-" + action.__name__, now=NOW + timedelta(hours=1))
        self.assertEqual((self.executor.starts, self.executor.advances), (starts, advances))
        self.assertTrue(self.kernel.repo.foreground_busy(self.scope.owner_key))
        self.assertEqual(self.units(), 0)

    def test_pause_resume_rechecks_gate_and_does_not_count_waiting(self):
        instance = self.running()
        paused = self.kernel.pause(self.scope, instance.instance_id, expected_revision=instance.revision,
                                   command_id="pause", now=NOW + timedelta(seconds=65))
        self.assertEqual((paused.status, paused.elapsed_s, self.units()), (m.ActivityStatus.PAUSED, 65, 1))
        self.access.free_s = 10
        refused = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
                                   command_id="resume-refused", now=NOW + timedelta(hours=1))
        self.assertEqual(refused, paused)
        self.access.free_s = 3600
        running = self.kernel.start(self.scope, paused.instance_id, expected_revision=paused.revision,
                                   command_id="resume", now=NOW + timedelta(hours=1))
        completed = self.kernel.advance(self.scope, running.instance_id, expected_revision=running.revision,
            command_id="after-wait", now=NOW + timedelta(hours=1, seconds=115))
        self.assertEqual((completed.elapsed_s, self.units()), (180, 3))
        self.assertEqual(completed.started_at, NOW)

    def test_revision_command_digest_and_terminal_fences(self):
        instance = self.running()
        with self.assertRaises(KernelError) as caught:
            self.kernel.advance(self.scope, instance.instance_id, expected_revision=1, command_id="stale", now=NOW)
        self.assertEqual(caught.exception.code, "stale_version")
        self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                            command_id="same", now=NOW + timedelta(seconds=1))
        with self.assertRaises(KernelError) as caught:
            self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                                command_id="same", now=NOW + timedelta(seconds=2))
        self.assertEqual(caught.exception.code, "invalid_config")

    def test_transition_table_is_exhaustive_and_unknown_has_additional_fence(self):
        sample = self.prepared()
        graph = dict(m.ACTIVITY_TRANSITIONS)
        for source in m.ActivityStatus:
            for target in m.ActivityStatus:
                old = replace(sample, status=source)
                with self.subTest(source=source, target=target):
                    if target in graph[source] and source != m.ActivityStatus.UNKNOWN:
                        validate_transition(old, target)
                    else:
                        with self.assertRaises(KernelError):
                            validate_transition(old, target)
        for outcome in (m.Outcome(m.OutcomeStatus.SUCCEEDED, False, "ok", m.ReasonCode.CANCELLED),
                        m.Outcome(m.OutcomeStatus.FAILED, False, "missing reason"),
                        m.Outcome(m.OutcomeStatus.CANCELLED, False, "missing reason")):
            with self.assertRaises(KernelError):
                terminal_status(outcome)

    def test_only_one_foreground_across_two_authorized_contexts(self):
        other = replace(self.scope, context_key=m.ContextKey("other-private-context"))
        self.access.inventory = (self.scope, other)
        first = self.propose("first", scope=self.scope)
        second = self.propose("second", scope=other)
        def prepare(item):
            scope, candidate, command_id = item
            return self.kernel.prepare_candidate(scope, candidate.candidate_id, command_id=command_id, now=NOW)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = tuple(pool.map(prepare, ((self.scope, first, "first"), (other, second, "second"))))
        self.assertEqual(sum(result.instance is not None for result in results), 1)
        self.assertEqual(self.count("life_kernel_instances"), 1)

    def test_single_coordinator_lifetime_rejects_double_activation_and_old_callbacks(self):
        from good_life.kernel import ActivityKernel, KernelConfig
        with self.assertRaises(KernelError):
            ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access)
        old = self.kernel
        old.close()
        self.kernel = ActivityKernel(self.storage, self.registry, config=KernelConfig(enabled=True), access=self.access)
        self.assertEqual(old.tick(now=NOW)["started"], 0)
        with self.assertRaises(KernelError):
            old.propose(self.scope, m.TemplateRef("tidy_desk", m.TemplateVersion(1)), opportunity_key="old-callback",
                        reason="tidy_requested", now=NOW, expires_at=NOW + timedelta(hours=1))

    def test_shutdown_fences_a_callback_authorized_before_close(self):
        candidate = self.propose()
        snapshot = self.access.snapshot
        def close_after_authorization(scope, now):
            value = snapshot(scope, now)
            self.kernel.close()
            return value
        with patch.object(self.access, "snapshot", side_effect=close_after_authorization):
            with self.assertRaises(KernelError):
                self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="late-callback", now=NOW)
        self.assertEqual(self.count("life_kernel_instances"), 0)
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, candidate.candidate_id)[1].status, m.CandidateStatus.PENDING)

    def test_wall_clock_jump_does_not_invent_online_progress(self):
        monotonic = [100.0]
        self.kernel.clock.stamp = lambda now: monotonic[0]
        instance = self.running()
        monotonic[0] = 105.0
        progressed = self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="wall-jump", now=NOW + timedelta(hours=2))
        self.assertEqual(progressed.elapsed_s, 5)
        self.assertEqual(self.units(), 0)
        with self.assertRaises(KernelError):
            self.kernel.advance(self.scope, progressed.instance_id, expected_revision=progressed.revision,
                command_id="wall-backwards", now=NOW + timedelta(hours=1))
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id), progressed)

    def test_subsecond_ticks_do_not_lose_duration(self):
        instance = self.running()
        for index in range(1, 9):
            instance = self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                command_id="fraction-" + str(index), now=NOW + timedelta(seconds=index / 4))
        self.assertEqual(instance.elapsed_s, 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
