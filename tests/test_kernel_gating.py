from dataclasses import replace
from datetime import timedelta

from test_kernel_support import KernelCase, NOW, m
from good_life.kernel import KernelError, ModuleSpec
from good_life.kernel.codec import decode, encode, freeze
from good_life.activities.local_order import LocalOrder, LocalTimedUnits, SPEC, TEMPLATE


class KernelRegistrationAndGateTests(KernelCase):
    def test_registration_rejects_duplicate_owns_and_pins_immutable_template(self):
        with self.assertRaises(KernelError):
            self.registry.register_module(ModuleSpec("another", "1", SPEC.owns), LocalOrder())
        self.assertEqual(self.registry.owners, {"local_order.tidied_units": "local_order"})
        with self.assertRaises(KernelError):
            self.registry.register_executor(LocalTimedUnits())
        with self.assertRaises(KernelError):
            self.registry.register_template(TEMPLATE)
        params = {"duration_s": 120, "unit_s": 60, "label": "another local rule"}
        saved = self.registry.register_template(replace(TEMPLATE, id="other-local", params=params))
        params["duration_s"] = 999
        self.assertEqual(saved.params["duration_s"], 120)
        with self.assertRaises(TypeError):
            saved.params["duration_s"] = 5
        with self.assertRaises(KernelError):
            self.registry.register_template(replace(TEMPLATE, id="unknown", executor="not_registered@1"))
        with self.assertRaises(KernelError):
            self.registry.register_template(replace(TEMPLATE, id="arbitrary", gating={"eval": "anything"}))
        self.assertEqual(self.registry.unavailable(TEMPLATE, ()), ("local_order",))

    def test_candidate_dedupe_merges_reasons_but_keeps_opportunity_and_target_version(self):
        expanded = replace(TEMPLATE, id="multi", triggers=(freeze({"id": "one", "type": "intent"}), freeze({"id": "two", "type": "intent"})))
        self.registry.register_template(expanded)
        def propose(reason, opportunity="same", revision=1):
            return self.kernel.propose(self.scope, m.TemplateRef("multi", m.TemplateVersion(1)),
                opportunity_key=opportunity, reason=reason, now=NOW, expires_at=NOW + timedelta(hours=1),
                target_ref=m.RecordRef("local_goal", "desk", m.Revision(revision)))
        first, merged = propose("one"), propose("two")
        self.assertEqual(first.candidate_id, merged.candidate_id)
        self.assertEqual(merged.reasons, ("one", "two"))
        self.assertNotEqual(propose("one", "other").candidate_id, first.candidate_id)
        self.assertNotEqual(propose("one", revision=2).candidate_id, first.candidate_id)
        self.assertEqual(self.count("life_kernel_candidates"), 3)

    def test_planning_gate_refusal_does_not_prepare_or_create_instance(self):
        for field, value, reason in (("free_s", 30, "no_time_window"), ("capabilities", (), "missing_dependency"),
                                     ("busy", True, "insufficient_resource"), ("busy", None, "insufficient_resource"),
                                     ("observed_age_s", 40, "stale_version")):
            with self.subTest(field=field, value=value):
                original = getattr(self.access, field)
                candidate = self.propose("gate-" + field + str(value))
                setattr(self.access, field, value)
                preparation = self.kernel.prepare_candidate(self.scope, candidate.candidate_id,
                    command_id="prepare-" + candidate.candidate_id, now=NOW)
                self.assertFalse(preparation.gate.allowed)
                self.assertEqual(preparation.gate.reason_code.value, reason)
                self.assertIsNone(preparation.instance)
                self.assertEqual(self.kernel.repo.candidate(self.scope.owner_key, candidate.candidate_id)[1].status, m.CandidateStatus.REJECTED)
                setattr(self.access, field, original)
        self.assertEqual(self.executor.prepares, 0)
        self.assertEqual(self.count("life_kernel_instances"), 0)

    def test_second_gate_uses_actual_prepare_duration_and_fresh_snapshot(self):
        # The cheap minimum fits, but actual prepare needs 180s, not 60s.
        self.access.free_s = 90
        instance = self.prepared()
        self.access.revision = 2
        failed = self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
                                  command_id="second-gate", now=NOW)
        self.assertEqual(failed.status, m.ActivityStatus.FAILED)
        self.assertEqual(failed.reason_code, m.ReasonCode.NO_TIME_WINDOW)
        self.assertEqual(failed.outcome.reason_code, failed.reason_code)
        self.assertIsNone(failed.started_at)
        self.assertEqual((self.executor.prepares, self.executor.starts), (1, 0))
        self.assertEqual((self.units(), self.count("life_events")), (0, 0))
        with self.storage._lock:
            rows = self.storage._connection.execute("SELECT stage,gate_json FROM life_kernel_gates ORDER BY stage").fetchall()
        self.assertEqual({row[0] for row in rows}, {"planning", "before_start"})
        import json
        final = next(json.loads(row[1]) for row in rows if row[0] == "before_start")
        self.assertEqual(final["snapshot_revision"], 2)
        self.assertEqual(final["estimated_finish_at"], "2026-11-16T08:03:00Z")

    def test_prepared_opportunity_expiring_before_start_fails_closed(self):
        instance = self.prepared()
        failed = self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision,
            command_id="expired-before-start", now=NOW + timedelta(hours=2))
        self.assertEqual(failed.status, m.ActivityStatus.FAILED)
        self.assertEqual(failed.reason_code, m.ReasonCode.NO_TIME_WINDOW)
        self.assertIsNone(failed.started_at)
        self.assertEqual(self.executor.starts, 0)

    def test_two_gates_pass_and_permission_is_rechecked_before_start(self):
        instance = self.prepared()
        self.access.allowed = False
        with self.assertRaises(KernelError) as caught:
            self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision, command_id="lost-authority", now=NOW)
        self.assertEqual(caught.exception.code, "unauthorized")
        self.assertEqual(self.executor.starts, 0)
        self.access.allowed = True
        self.kernel.start(self.scope, instance.instance_id, expected_revision=instance.revision, command_id="authorized", now=NOW)
        self.assertEqual(self.count("life_kernel_gates"), 3)
        self.assertEqual(self.executor.starts, 1)

    def test_unknown_stale_and_insufficient_resource_are_not_satisfied(self):
        template = self.registry.register_template(replace(TEMPLATE, id="resource-rule", gating={"free_s": 60, "resources": {"local.tokens": 2}}))
        for index, (quality, value, age) in enumerate(((m.StateQuality.UNKNOWN, None, 0), (m.StateQuality.STALE, 10, 0),
                (m.StateQuality.VALID, 1, 0), (m.StateQuality.VALID, 10, 60))):
            self.access.resources = {"local.tokens": m.StateValue(value, NOW - timedelta(seconds=age), m.Revision(1), (), quality)}
            candidate = self.propose("resource-" + str(index), template=template)
            result = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="resource-" + str(index), now=NOW)
            self.assertFalse(result.gate.allowed)
            self.assertEqual(result.gate.reason_code, m.ReasonCode.INSUFFICIENT_RESOURCE)
        self.assertEqual(self.executor.prepares, 0)

    def test_resource_changes_between_gates_are_rechecked(self):
        template = self.registry.register_template(replace(TEMPLATE, id="resource-rule", gating={"free_s": 60, "resources": {"local.tokens": 2}}))
        self.access.resources = {"local.tokens": m.StateValue(10, NOW, m.Revision(1), (), m.StateQuality.VALID)}
        candidate = self.propose("resource", template=template)
        prep = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="resource-prepare", now=NOW)
        self.assertTrue(prep.gate.allowed)
        self.access.resources = {}
        failed = self.kernel.start(self.scope, prep.instance.instance_id, expected_revision=1, command_id="resource-start", now=NOW)
        self.assertEqual(failed.reason_code, m.ReasonCode.INSUFFICIENT_RESOURCE)
        self.assertEqual(self.executor.starts, 0)

    def test_expired_candidate_never_prepares_or_reappears_on_same_opportunity(self):
        candidate = self.propose()
        preparation = self.kernel.prepare_candidate(self.scope, candidate.candidate_id, command_id="expired", now=NOW + timedelta(hours=2))
        self.assertFalse(preparation.gate.allowed)
        stored = self.kernel.repo.candidate(self.scope.owner_key, candidate.candidate_id)[1]
        self.assertEqual(stored.status, m.CandidateStatus.EXPIRED)
        repeated = self.propose(now=NOW + timedelta(hours=3))
        self.assertEqual(repeated.candidate_id, candidate.candidate_id)
        self.assertEqual(repeated.status, m.CandidateStatus.EXPIRED)
        self.assertEqual(self.executor.prepares, 0)

    def test_inflight_parameters_survive_new_template_version(self):
        instance = self.running()
        self.registry.register_template(replace(TEMPLATE, version=m.TemplateVersion(2), params={"duration_s": 600, "unit_s": 60, "label": "v2"}))
        self.reopen()
        recovered, = self.kernel.recover(self.scope, now=NOW + timedelta(seconds=10))
        self.assertEqual(recovered.template_version, 1)
        self.assertEqual(recovered.planned_duration_s, 180)
        self.assertEqual(recovered.checkpoint["duration_s"], 180)

    def test_cross_owner_context_and_revoked_generation_are_rejected(self):
        instance = self.running()
        other_context = replace(self.scope, context_key=m.ContextKey("other-context"))
        other_owner = replace(self.scope, owner_key=m.OwnerKey("other-owner"), context_key=m.ContextKey("other-owner-context"))
        self.access.inventory = (self.scope, other_context, other_owner)
        for scope in (other_context, other_owner, replace(self.scope, registration_generation=m.RegistrationGeneration(2))):
            with self.subTest(scope=scope), self.assertRaises(KernelError):
                self.kernel.cancel(scope, instance.instance_id, expected_revision=instance.revision, command_id="cross-role", now=NOW)
        self.assertEqual(self.count("life_kernel_effects"), 0)

    def test_codec_detaches_maps_rejects_units_and_nonfinite_time(self):
        for value in (float("nan"), float("inf"), {1: "not a string"}):
            with self.assertRaises(KernelError):
                encode(value)
        with self.assertRaises(KernelError):
            encode(NOW.replace(tzinfo=None))
        instance = self.running()
        restored = decode(m.ActivityInstance, encode(instance))
        self.assertEqual(restored, instance)
        self.assertTrue(encode(instance).endswith("}"))
        with self.assertRaises(TypeError):
            restored.checkpoint["completed_units"]["tidied_units"] = 8


if __name__ == "__main__":
    import unittest
    unittest.main()
