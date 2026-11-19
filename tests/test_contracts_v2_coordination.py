"""Real host/life recovery using explicitly negotiated local coordination v2."""
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from test_reading_extraction import ReadingExtractionFixture
from astrbot_host.life_async_capabilities import HostPortError
from astrbot_host.life_async_coordination_v2 import COMMAND_REF_PREFIX
from companion_v2.execution.models import TaskError, encode
from companion_v2.integration import life_commands, manual_reading_queue
from companion_v2.integration.life_async_codec import params_digest
from companion_v2.integration.model_completion_handoff import ModelCompletionHandoff
from companion_v2.integration.reading_handoff import ReadingStartHandoff
from datetime import timedelta
from astrbot_host.life_async_coordination_v2 import _IntentStartHandoff
from companion_v2.model_gateway.life_tasks import PREFIX
from companion_v2.role_work.life.management import RoleLifeManagement
from good_life.contracts import models as m, ports as p, v2 as v
from good_life.reading_execution import response_types
from good_life.storage import StorageReader


def constructors():
    return {**response_types(), **{name: getattr(v, name) for name in (
        "CommandDecision", "CommandSettlement", "LifeCommandReceipt", "IntentStartReceipt")}}


class CoordinationV2Tests(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    def negotiate(self, f, groups=(v.ABORT_OWN_CAPABILITY, v.COMMAND_RECONCILE_CAPABILITY, v.AUTHORIZED_INTENT_CAPABILITY)):
        return f.api.life_async_capabilities(plugin=f.plugin, registration_id=f.token,
            contract_version=2, response_types=constructors(), requested_capabilities=groups)

    async def prepared(self):
        f = await self.fixture()
        with patch.object(f.ports, "execute_platform_action", side_effect=ValueError("before host decision")):
            with self.assertRaisesRegex(ValueError, "before host decision"):
                await f.executor.execute(f.context.key, f.activity.activity_id)
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        f.command, f.proof = state["command"], state["proof"]
        f.v2 = self.negotiate(f)["ports"]
        scope = await f.v2.resolve_authority(p.AuthorityRequest(m.ContextKey(f.context.key), "role.reading.record", ()))
        _, request_hash = life_commands._request(f.context, f.command)
        f.abort = v.AbortPreparedCommandRequest(scope, f.command["command_id"], request_hash,
            COMMAND_REF_PREFIX + f.command["command_id"])
        f.reconcile = v.LifeCommandRequest(scope, f.abort.command_id, request_hash, f.abort.command_ref,
            PREFIX + f.proof["task_id"] + ":manifest")
        return f

    def state(self, f):
        return StorageReader(f.life).one("SELECT state FROM life_prepared_commands WHERE command_id=?",
                                         (f.abort.command_id,))[0]

    async def test_default_off_and_v1_still_negotiates_unchanged(self):
        f = await self.fixture()
        result = self.negotiate(f, ())
        self.assertEqual(result["capability_groups"], ())
        self.assertEqual(result["capabilities"], ("resolve_authority",))
        self.assertFalse(hasattr(result["ports"], "run_model_task"))
        self.assertFalse(hasattr(result["ports"], "execute_platform_action"))
        original = f.api.life_async_capabilities(plugin=f.plugin, registration_id=f.token,
            contract_version=1, response_types=response_types())
        self.assertEqual(original["api_version"], "companion.life-host.async.v1")
        self.assertEqual(original["local_actions"], ("life.command.commit", "life.intent.start"))
        scope = await result["ports"].resolve_authority(p.AuthorityRequest(m.ContextKey(f.context.key), "role.reading.record", ()))
        with self.assertRaises(HostPortError) as denied:
            await result["ports"].abort_own_prepared_command(v.AbortPreparedCommandRequest(scope, "x", "hash", "ref"))
        self.assertEqual(denied.exception.code, "unauthorized")
        with self.assertRaises(TaskError):
            self.negotiate(f, (v.MODEL_TASK_CAPABILITY,))

    async def test_abort_original_then_independent_pause_releases_role(self):
        f = await self.prepared()
        receipt = await f.v2.abort_own_prepared_command(f.abort)
        self.assertEqual((receipt.decision, receipt.settlement), (v.CommandDecision.ABORT, v.CommandSettlement.CONFIRMED))
        self.assertEqual(self.state(f), "aborted")
        self.assertEqual(await f.v2.abort_own_prepared_command(f.abort), receipt)
        self.assertEqual(await f.v2.reconcile_life_command(f.reconcile), receipt)
        # A new, independently audited pause can now reserve the same owner.
        from companion_v2.integration.interrupted_work_handoff import InterruptedWorkHandoff
        await InterruptedWorkHandoff(f.app, f.routine, f.app.role_life.transition_provider()).execute(
            f.activity.activity_id, expected_revision=f.started.revision)
        current = f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner)
        self.assertEqual(current.state.value, "paused")
        self.assertEqual(len(self.called), 1)

    async def test_abort_after_source_withdrawal_does_not_read_model_content(self):
        f = await self.prepared()
        with patch.object(f.app.role_journal.sources, "read", side_effect=AssertionError("no model/source reread")):
            receipt = await f.v2.abort_own_prepared_command(f.abort)
        self.assertEqual(receipt.settlement, v.CommandSettlement.CONFIRMED)
        self.assertEqual(self.state(f), "aborted")
        self.assertEqual(len(self.called), 1)

    async def test_lost_abort_resolve_response_replays_original_decision_after_rebind(self):
        f = await self.prepared()
        provider = f.app.role_life.transition_provider()
        resolve = provider.resolve
        def lost(*args, **kwargs):
            resolve(*args, **kwargs)
            raise TimeoutError("lost abort response")
        with patch.object(f.app.role_life, "transition_provider", return_value=provider), patch.object(provider, "resolve", side_effect=lost):
            first = await f.v2.abort_own_prepared_command(f.abort)
        self.assertEqual((first.decision, first.settlement), (v.CommandDecision.ABORT, v.CommandSettlement.UNKNOWN))
        self.assertEqual(self.state(f), "aborted")
        fresh = self.negotiate(f)["ports"]
        scope = await fresh.resolve_authority(p.AuthorityRequest(m.ContextKey(f.context.key), "role.reading.record", ()))
        second = await fresh.abort_own_prepared_command(replace(f.abort, scope=scope))
        self.assertEqual((second.decision_id, second.settlement), (first.decision_id, v.CommandSettlement.CONFIRMED))
        self.assertEqual(len(self.called), 1)

    async def test_preexisting_commit_is_recovered_never_aborted(self):
        f = await self.prepared()
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.proof)
        decision = handoff.decide(f.command, observed_at=f.app.now())
        self.assertEqual(self.state(f), "prepared")
        with patch.object(ModelCompletionHandoff, "abort", side_effect=AssertionError("commit must never abort")):
            receipt = await f.v2.abort_own_prepared_command(f.abort)
        self.assertEqual((receipt.decision.value, receipt.settlement.value, receipt.decision_id),
                         ("commit", "confirmed", decision["decision_id"]))
        self.assertEqual(self.state(f), "acknowledged")
        self.assertEqual(await f.v2.abort_own_prepared_command(f.abort), receipt)
        self.assertEqual(len(self.called), 1)

    async def test_commit_wins_race_with_abort_and_original_commit_recovers(self):
        f = await self.prepared()
        decide = life_commands.decide
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.proof)
        def race(*args, **kwargs):
            if kwargs["outcome"] == "abort":
                handoff.decide(f.command, observed_at=f.app.now())
            return decide(*args, **kwargs)
        with patch.object(life_commands, "decide", side_effect=race):
            receipt = await f.v2.abort_own_prepared_command(f.abort)
        self.assertEqual((receipt.decision.value, receipt.settlement.value), ("commit", "confirmed"))
        self.assertEqual(self.state(f), "acknowledged")
        self.assertEqual(len(self.called), 1)

    async def test_known_commit_with_failed_settlement_preserves_barrier(self):
        f = await self.prepared()
        handoff = ModelCompletionHandoff(f.app, f.routine, f.app.role_life.transition_provider(), f.proof)
        handoff.decide(f.command, observed_at=f.app.now())
        with patch.object(ModelCompletionHandoff, "apply", side_effect=TimeoutError("life store unavailable")):
            receipt = await f.v2.abort_own_prepared_command(f.abort)
        self.assertEqual((receipt.decision.value, receipt.settlement.value, receipt.reason_code),
                         ("commit", "unknown", m.ReasonCode.RESULT_UNKNOWN))
        self.assertEqual(self.state(f), "prepared")
        recovered = await f.v2.reconcile_life_command(f.reconcile)
        self.assertEqual((recovered.decision_id, recovered.settlement.value), (receipt.decision_id, "confirmed"))
        self.assertEqual(len(self.called), 1)

    async def test_reconcile_uses_original_proof_and_commit_without_extra_model(self):
        f = await self.prepared()
        receipt = await f.v2.reconcile_life_command(f.reconcile)
        self.assertEqual((receipt.decision.value, receipt.settlement.value), ("commit", "confirmed"))
        self.assertEqual(receipt.result_ref, f.abort.command_ref)
        self.assertEqual(self.state(f), "acknowledged")
        self.assertEqual(await f.v2.reconcile_life_command(f.reconcile), receipt)
        self.assertEqual(len(self.called), 1)

    async def test_reconcile_reports_specific_permanent_refusals(self):
        f = await self.prepared()
        for code, expected in (("life_activity_evidence_required", "invalid_config"),
                               ("life_revision_conflict", "stale_version"),
                               ("life_command_denied", "unauthorized")):
            with self.subTest(code=code), patch.object(ModelCompletionHandoff, "_authorize", side_effect=TaskError(code, "refused")):
                with self.assertRaises(HostPortError) as refused:
                    await f.v2.reconcile_life_command(f.reconcile)
                self.assertEqual(refused.exception.code, expected)
                self.assertEqual(refused.exception.error.operation_id, f.abort.command_id)
                self.assertEqual(self.state(f), "prepared")
        self.assertEqual(len(self.called), 1)

    async def test_cross_owner_context_hash_and_kind_rejected_without_new_decision(self):
        f = await self.prepared()
        for bad in (replace(f.abort, request_hash="0" * 64),
                    replace(f.abort, scope=replace(f.abort.scope, owner_key=m.OwnerKey("other"))),
                    replace(f.abort, scope=replace(f.abort.scope, context_key=m.ContextKey("other"))),
                    replace(f.abort, command_id="activity-reading-complete-other"),
                    replace(f.abort, command_ref="arbitrary")):
            with self.subTest(request=bad), self.assertRaises(HostPortError):
                await f.v2.abort_own_prepared_command(bad)
        self.assertIsNone(life_commands.lookup(f.app.runtime, context_key=f.context.key,
            command_id=f.abort.command_id, request_hash=f.abort.request_hash))
        self.assertEqual(self.state(f), "prepared")
        self.assertEqual(len(self.called), 1)

    async def test_missing_command_preserves_identity_without_fabricating_abort(self):
        f = await self.prepared()
        missing_id = "activity-model-complete-absent"
        request = replace(f.abort, command_id=missing_id, command_ref=COMMAND_REF_PREFIX + missing_id)
        with self.assertRaises(HostPortError) as error:
            await f.v2.abort_own_prepared_command(request)
        self.assertEqual(error.exception.code, "missing_dependency")
        self.assertEqual(error.exception.error.operation_id, missing_id)
        self.assertEqual(self.state(f), "prepared")
        with self.assertRaises(HostPortError) as mismatch:
            await f.v2.abort_own_prepared_command(replace(f.abort, request_hash="0" * 64))
        self.assertEqual(mismatch.exception.code, "invalid_config")
        self.assertEqual(mismatch.exception.error.operation_id, f.abort.command_id)
        self.assertEqual(len(self.called), 1)

    async def test_expired_provider_lease_cannot_abort(self):
        f = await self.prepared()
        f.api.unregister_life_provider(f.token)
        with self.assertRaises(HostPortError):
            await f.v2.abort_own_prepared_command(f.abort)
        self.assertEqual(self.state(f), "prepared")

    async def queued_intent(self):
        f = await self.fixture(start=False)
        manager = RoleLifeManagement(f.app, f.context)
        item = manager.status(self.core)["activities"][0]
        scheduled = await manager.command(self.core, {"action": "read", "operation_key": "v2-manual",
            "activity_id": f.activity.activity_id, "expected_revision": 1, "view_token": item["view_token"]})
        self.assertTrue(scheduled.is_success, scheduled)
        # A3 scheduler is opt-in; enqueue directly via the management helper if
        # the legacy management command started immediately in this fixture.
        f.v2 = self.negotiate(f)["ports"]
        scope = await f.v2.resolve_authority(p.AuthorityRequest(m.ContextKey(f.context.key), "role.reading.record", ()))
        pending = manual_reading_queue.pending(f.app)
        self.assertEqual(len(pending), 1)
        operation_id = pending[0]["operation_id"]
        request = v.AuthorizedIntentRequest(scope, operation_id, "", manual_reading_queue.PREFIX + operation_id)
        f.intent = replace(request, params_digest=params_digest("authorized_intent", request))
        return f

    async def test_intent_success_inspects_after_queue_delete_and_adapter_restart(self):
        f = await self.queued_intent()
        receipt = await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(receipt.status, m.ReceiptStatus.SUCCEEDED)
        self.assertIsNotNone(receipt.start_record_ref)
        self.assertEqual(manual_reading_queue.pending(f.app), [])
        fresh = self.negotiate(f)["ports"]
        scope = await fresh.resolve_authority(p.AuthorityRequest(m.ContextKey(f.context.key), "role.reading.record", ()))
        inspection = v.IntentInspection(scope, f.intent.operation_id, f.intent.params_digest)
        self.assertEqual(await fresh.inspect_authorized_intent(inspection), receipt)
        self.assertEqual(await fresh.start_authorized_intent(replace(f.intent, scope=scope)), receipt)
        self.assertEqual(len(self.called), 0)

    async def assert_interrupted_start_recovers(self, stage):
        from datetime import timedelta
        f = await self.queued_intent()
        original = getattr(_IntentStartHandoff, stage)

        def interrupted(handoff, *args, **kwargs):
            original(handoff, *args, **kwargs)
            raise TimeoutError("lost start " + stage)

        with patch.object(_IntentStartHandoff, stage, interrupted):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
        self.clock.advance(timedelta(seconds=5))
        fresh = self.negotiate(f)["ports"]
        scope = await fresh.resolve_authority(p.AuthorityRequest(m.ContextKey(f.context.key), "role.reading.record", ()))
        receipt = await fresh.start_authorized_intent(replace(f.intent, scope=scope))
        self.assertEqual(receipt.status, m.ReceiptStatus.SUCCEEDED)
        self.assertEqual(manual_reading_queue.pending(f.app), [])
        rows = StorageReader(f.life).all("SELECT state FROM life_prepared_commands WHERE command_id LIKE 'activity-reading-start-%'", ())
        self.assertEqual([row[0] for row in rows], ["acknowledged"])
        self.assertEqual(len(self.called), 0)

    async def test_intent_recovers_after_preparation_without_replacing_command(self):
        await self.assert_interrupted_start_recovers("prepare")

    async def test_intent_recovers_after_host_decision(self):
        await self.assert_interrupted_start_recovers("decide")

    async def test_intent_recovers_after_life_apply(self):
        await self.assert_interrupted_start_recovers("apply")

    async def test_intent_recovers_after_host_complete_before_life_ack(self):
        await self.assert_interrupted_start_recovers("complete")

    async def test_intent_recovers_after_life_ack_before_queue_delete(self):
        await self.assert_interrupted_start_recovers("acknowledge")

    async def assert_permanent_intent_rejection(self, code, reason):
        f = await self.queued_intent()
        with patch.object(ReadingStartHandoff, "_authorize", side_effect=TaskError(code, "permanent refusal")):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
        receipt = await f.v2.start_authorized_intent(f.intent)
        self.assertEqual((receipt.status, receipt.reason_code), (m.ReceiptStatus.REJECTED, reason))
        self.assertIsNone(receipt.start_record_ref)
        self.assertEqual(await f.v2.start_authorized_intent(f.intent), receipt)
        self.assertEqual(await f.v2.inspect_authorized_intent(v.IntentInspection(
            f.intent.scope, f.intent.operation_id, f.intent.params_digest)), receipt)
        self.assertEqual(manual_reading_queue.pending(f.app), [])
        rows = StorageReader(f.life).all("SELECT state FROM life_prepared_commands WHERE command_id LIKE 'activity-reading-start-%'", ())
        self.assertEqual([row[0] for row in rows], ["aborted"])
        self.assertEqual(len(self.called), 0)

    async def test_intent_own_authorization_abort_keeps_matching_durable_reason(self):
        await self.assert_permanent_intent_rejection("life_command_denied", m.ReasonCode.UNAUTHORIZED)

    async def test_intent_own_revision_abort_keeps_matching_durable_reason(self):
        await self.assert_permanent_intent_rejection("life_revision_conflict", m.ReasonCode.STALE_VERSION)

    async def test_intent_transient_decide_abort_can_start_later(self):
        f = await self.queued_intent()
        with patch.object(ReadingStartHandoff, "_authorize", side_effect=TaskError("reading_source_unavailable", "temporary")):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(len(manual_reading_queue.pending(f.app)), 1)
        self.clock.advance(timedelta(seconds=1))
        receipt = await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(receipt.status, m.ReceiptStatus.SUCCEEDED)
        rows = StorageReader(f.life).all("SELECT state FROM life_prepared_commands ORDER BY prepared_at", ())
        self.assertEqual([row[0] for row in rows], ["aborted", "acknowledged"])
        self.assertEqual(len(self.called), 0)

    async def test_intent_preprepare_pacing_does_not_pin_failed_observation(self):
        f = await self.queued_intent()
        ready_at = self.clock.now() + timedelta(hours=1)
        expected_start = ReadingStartHandoff._expected_start
        def paced(handoff, activity, request_id, at):
            if at < ready_at:
                raise TaskError("life_reading_paced", "wait for original pace")
            return expected_start(handoff, activity, request_id, at)
        with patch.object(ReadingStartHandoff, "_expected_start", paced):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
            self.assertEqual(StorageReader(f.life).all("SELECT state FROM life_prepared_commands", ()), [])
            self.clock.advance(timedelta(days=1))
            receipt = await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(receipt.status, m.ReceiptStatus.SUCCEEDED)
        self.assertEqual(len(self.called), 0)

    async def test_intent_repin_during_preflight_prevents_stale_prepare(self):
        f = await self.queued_intent()
        prepare = _IntentStartHandoff.prepare
        def repinned(handoff, *args, **kwargs):
            key = f.v2.intent_key(f.intent.scope, f.intent.operation_id)
            record = json.loads(f.app.runtime.read_one(
                "SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"])
            self.clock.advance(timedelta(seconds=1))
            f.v2.repin_intent(f.app, f.intent, record)
            return prepare(handoff, *args, **kwargs)
        with patch.object(_IntentStartHandoff, "prepare", repinned):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(StorageReader(f.life).all("SELECT state FROM life_prepared_commands", ()), [])
        self.assertEqual((await f.v2.start_authorized_intent(f.intent)).status, m.ReceiptStatus.SUCCEEDED)
        self.assertEqual(len(self.called), 0)

    async def interrupted_preparation(self):
        f = await self.queued_intent()
        prepare = _IntentStartHandoff.prepare
        def interrupted(handoff, *args, **kwargs):
            prepare(handoff, *args, **kwargs)
            raise TimeoutError("prepare response lost")
        with patch.object(_IntentStartHandoff, "prepare", interrupted):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
        command_id = StorageReader(f.life).one("SELECT command_id FROM life_prepared_commands", ())[0]
        f.original = f.app.role_life.transition_provider().request_for_replay(command_id=command_id,
            owner_key=f.intent.scope.owner_key, context_key=f.intent.scope.context_key)
        return f

    async def test_intent_retries_after_v1_recovery_sweep_aborts_lost_prepare(self):
        f = await self.interrupted_preparation()
        capabilities = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        result = await capabilities["recover_reading"](f.context.key, f.original)
        self.assertEqual(result["state"], "aborted")
        self.clock.advance(timedelta(seconds=1))
        receipt = await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(receipt.status, m.ReceiptStatus.SUCCEEDED)
        self.assertEqual(manual_reading_queue.pending(f.app), [])
        self.assertEqual(len(self.called), 0)

    async def test_intent_recovery_decide_refusal_aborts_prepared_command(self):
        f = await self.interrupted_preparation()
        with patch.object(ReadingStartHandoff, "_authorize", side_effect=TaskError("reading_source_unavailable", "temporary")):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(StorageReader(f.life).one("SELECT state FROM life_prepared_commands", ())[0], "aborted")
        self.assertEqual(len(manual_reading_queue.pending(f.app)), 1)
        self.clock.advance(timedelta(seconds=1))
        receipt = await f.v2.start_authorized_intent(f.intent)
        self.assertEqual(receipt.status, m.ReceiptStatus.SUCCEEDED)
        self.assertEqual(len(self.called), 0)

    async def test_v1_and_v2_start_the_same_queue_item_once(self):
        f = await self.queued_intent()
        with patch.object(_IntentStartHandoff, "prepare", side_effect=TaskError("life_reading_paced", "temporary")):
            with self.assertRaises(HostPortError):
                await f.v2.start_authorized_intent(f.intent)
        self.clock.advance(timedelta(seconds=1))
        scope = await f.ports.resolve_authority(p.AuthorityRequest(m.ContextKey(f.context.key), "role.reading.record", ()))
        request = p.PlatformActionRequest(scope, f.intent.operation_id, "", "life.intent.start",
            {"intent_ref": f.intent.intent_ref}, (), f.ports.audience(scope))
        request = replace(request, params_digest=params_digest("platform_action", request))
        self.assertEqual((await f.ports.execute_platform_action(request)).status, m.ReceiptStatus.SUCCEEDED)
        self.assertEqual((await f.v2.start_authorized_intent(f.intent)).status, m.ReceiptStatus.SUCCEEDED)
        rows = StorageReader(f.life).all("SELECT state FROM life_prepared_commands", ())
        self.assertEqual([row[0] for row in rows], ["acknowledged"])
        self.assertEqual(len(self.called), 0)

    async def test_intent_stale_revision_keeps_durable_rejection_after_queue_delete(self):
        f = await self.queued_intent()
        # Change the expected revision in its original persisted intent so the
        # authorized management request is stale against the activity.
        with f.app.runtime.transaction() as db:
            row = db.execute("SELECT value FROM runtime_metadata WHERE key=?", (f.intent.intent_ref,)).fetchone()
            data = json.loads(row["value"])
            data["revision"] += 1
            db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (encode(data), f.intent.intent_ref))
        receipt = await f.v2.start_authorized_intent(f.intent)
        self.assertEqual((receipt.status, receipt.reason_code), (m.ReceiptStatus.REJECTED, m.ReasonCode.STALE_VERSION))
        inspection = v.IntentInspection(f.intent.scope, f.intent.operation_id, f.intent.params_digest)
        self.assertEqual(await f.v2.inspect_authorized_intent(inspection), receipt)
        self.assertEqual(manual_reading_queue.pending(f.app), [])
        self.assertEqual(len(self.called), 0)


if __name__ == "__main__":
    unittest.main()
