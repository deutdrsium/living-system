"""Generic optional duration quotes and per-candidate exception isolation."""
from dataclasses import replace
from datetime import timedelta
from sqlite3 import OperationalError
from unittest.mock import patch

from test_kernel_support import KernelCase, NOW
from good_life.contracts.models import ActivityStatus, CandidateStatus, ReasonCode
from good_life.kernel.codec import KernelError


class ComponentBoundaryTests(KernelCase):
    def test_existing_executor_recovery_still_uses_online_plan_remainder(self):
        instance = self.running()
        self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                            command_id="known-minute", now=NOW + timedelta(seconds=60))
        self.reopen()
        self.access.free_s = 40
        recovered = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=150))[0]
        self.assertEqual(recovered.status, ActivityStatus.PAUSED)
        self.assertEqual(recovered.reason_code.value, "no_time_window")

    def test_opted_in_wall_deadline_quote_is_used_for_recovery_and_final_gate(self):
        self.executor.remaining_duration_s = lambda instance, now: max(0, 180 - int((now - NOW).total_seconds()))
        instance = self.running()
        self.kernel.advance(self.scope, instance.instance_id, expected_revision=instance.revision,
                            command_id="known-minute", now=NOW + timedelta(seconds=60))
        self.reopen()
        self.access.free_s = 40
        recovered = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=150))[0]
        self.assertEqual(recovered.status, ActivityStatus.RUNNING)
        self.assertEqual(recovered.elapsed_s, 60)
        self.assertEqual(recovered.started_at, NOW)

    def test_invalid_remaining_quote_rolls_back_before_start(self):
        instance = self.prepared()
        self.executor.remaining_duration_s = lambda _instance, _now: True
        with self.assertRaises(KernelError):
            self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                              command_id="invalid-quote", now=NOW)
        self.assertEqual(self.kernel.repo.instance(self.scope.owner_key, instance.instance_id), instance)

    def test_missing_template_does_not_poison_the_scope_queue(self):
        bad = self.registry.register_template(replace(self.template, id="retired_template"))
        first = self.propose("bad", template=bad)
        good = self.propose("good", now=NOW + timedelta(seconds=1))
        del self.registry.templates[(bad.id, bad.version)]
        with patch.object(self.kernel.repo, "pending_candidates", return_value=(first, good)):
            result = self.kernel.tick(now=NOW + timedelta(seconds=2))
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["failures"], ("missing_dependency",))
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, first.candidate_id)[1].status, CandidateStatus.REJECTED)
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, good.candidate_id)[1].status, CandidateStatus.CONSUMED)

    def test_declared_prepare_config_error_rejects_only_that_candidate(self):
        bad = self.registry.register_template(replace(self.template, id="broken_template"))
        first = self.propose("bad", template=bad)
        good = self.propose("good", now=NOW + timedelta(seconds=1))
        original = self.executor.prepare

        def prepare(template, now):
            if template.id == bad.id:
                raise KernelError(ReasonCode.INVALID_CONFIG, "private component failure")
            return original(template, now)

        self.executor.prepare = prepare
        with patch.object(self.kernel.repo, "pending_candidates", return_value=(first, good)):
            result = self.kernel.tick(now=NOW + timedelta(seconds=2))
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["failures"], ("invalid_config",))
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, first.candidate_id)[1].status, CandidateStatus.REJECTED)

    def _retry_transient(self, target, method, error):
        first, second = self.propose("first"), self.propose("second")
        other = replace(self.scope, owner_key="zz-other-owner", context_key="another-context")
        self.access.inventory = (self.scope, other)
        third = self.propose("other", scope=other)
        original = getattr(target, method)
        failed = False

        def flaky(*args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise error
            return original(*args, **kwargs)

        def pending(owner):
            return (first, second) if owner == self.scope.owner_key else (third,)

        with patch.object(target, method, side_effect=flaky), patch.object(
                self.kernel.repo, "pending_candidates", side_effect=pending):
            result = self.kernel.tick(now=NOW + timedelta(seconds=5))
            self.assertEqual(result["started"], 1)  # The other owner still runs.
            self.assertEqual(len(result["failures"]), 1)
            for candidate in (first, second):
                self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key,
                    candidate.candidate_id)[1].status, CandidateStatus.PENDING)
            self.assertEqual(self.count("life_kernel_candidates"), 3)
            again = self.propose("first", now=NOW + timedelta(seconds=6))
            self.assertEqual(again.candidate_id, first.candidate_id)
            self.assertEqual(again.status, CandidateStatus.PENDING)
            resumed = self.kernel.tick(now=NOW + timedelta(seconds=10))
            self.assertEqual(resumed["started"], 1)
            self.assertEqual(resumed["failures"], ())
            self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key,
                first.candidate_id)[1].status, CandidateStatus.CONSUMED)
            self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key,
                second.candidate_id)[1].status, CandidateStatus.PENDING)

    def test_transient_snapshot_failure_retries_and_leaves_other_scope_running(self):
        self._retry_transient(self.access, "snapshot", RuntimeError("provider unavailable"))

    def test_host_config_error_is_not_a_candidate_component_defect(self):
        self._retry_transient(self.access, "snapshot", KernelError(ReasonCode.INVALID_CONFIG, "bad host view"))

    def test_transient_audience_failure_retries(self):
        self._retry_transient(self.access, "audience", RuntimeError("permissions provider unavailable"))

    def test_transient_storage_failure_retries(self):
        self._retry_transient(self.storage, "transaction", OperationalError("database is locked"))

    def test_transient_prepare_provider_failure_retries(self):
        self._retry_transient(self.executor, "prepare", RuntimeError("provider unavailable"))

    def test_prepare_dependency_outage_retries(self):
        self._retry_transient(self.executor, "prepare", KernelError(ReasonCode.MISSING_DEPENDENCY, "provider unavailable"))

    def test_invalid_prepare_result_rejects_only_the_defective_candidate(self):
        bad = self.registry.register_template(replace(self.template, id="invalid_result"))
        first = self.propose("bad", template=bad)
        good = self.propose("good")
        original = self.executor.prepare
        with patch.object(self.executor, "prepare", side_effect=lambda template, now:
                None if template.id == bad.id else original(template, now)), patch.object(
                self.kernel.repo, "pending_candidates", return_value=(first, good)):
            result = self.kernel.tick(now=NOW + timedelta(seconds=2))
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["failures"], ("invalid_config",))
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key,
            first.candidate_id)[1].status, CandidateStatus.REJECTED)

    def test_missing_executor_rejects_only_its_candidate(self):
        from test_kernel_support import CountingExecutor
        executor = CountingExecutor()
        executor.name = "retired_executor"
        self.registry.register_executor(executor)
        bad = self.registry.register_template(replace(self.template, id="retired_executor_template",
            executor=executor.name + "@" + executor.version))
        first, good = self.propose("bad", template=bad), self.propose("good")
        del self.registry.executors[bad.executor]
        with patch.object(self.kernel.repo, "pending_candidates", return_value=(first, good)):
            result = self.kernel.tick(now=NOW + timedelta(seconds=2))
        self.assertEqual(result["started"], 1)
        self.assertEqual(result["failures"], ("missing_dependency",))
        self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key,
            first.candidate_id)[1].status, CandidateStatus.REJECTED)
