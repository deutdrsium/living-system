"""Host rechecks preserve the original task audit and unsettled model calls."""
import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from test_reading_extraction import ReadingExtractionFixture
from tests.test_role_tool_loop import answered
from astrbot_host.life_async_capabilities import HostPortError
from companion_v2.execution.models import TaskError, encode
from good_life.contracts import models as m, ports as p
from good_life.reading_execution import signed
from good_life.reading_selection import choose


class ReadingHostRecheckTests(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    async def choice_task(self):
        f = await self.fixture(start=False)
        captured = []
        submit = f.ports.run_model_task

        async def capture(request):
            captured.append(request)
            return await submit(request)

        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"original"}'))
        with patch.object(f.ports, "run_model_task", capture), patch.object(
                f.ports, "asset", side_effect=TimeoutError("temporary result recheck")):
            self.assertEqual(await choose(f.executor, f.context.key, [json.loads(encode(f.activity))],
                                          "host-recheck", "window"), {"state": "unknown"})
        return f, captured[0]

    def set_audit_states(self, f, request, task_state, call_state="generated"):
        with f.app.runtime.transaction() as db:
            db.execute("UPDATE execution_tasks SET state=? WHERE task_id=?", (task_state, request.task_id))
            db.execute("UPDATE model_calls SET state=? WHERE task_id=?", (call_state, request.task_id))

    def audit(self, f, request):
        return (dict(f.app.runtime.read_one("SELECT * FROM execution_tasks WHERE task_id=?", (request.task_id,))),
                tuple(dict(row) for row in f.app.runtime.read_all(
                    "SELECT * FROM model_calls WHERE task_id=? ORDER BY call_id", (request.task_id,))))

    async def test_preadmission_rejections_raise_without_receipts_or_audit_changes(self):
        f, request = await self.choice_task()
        invalid = (
            (signed("model_task", replace(request, task_type="unsupported", params_digest="")), "invalid_config"),
            (replace(request, params_digest="changed"), "stale_version"),
            (signed("model_task", replace(request, schema_version=2, params_digest="")), "invalid_config"),
            (signed("model_task", replace(request, source_refs=(), params_digest="")), "stale_version"),
            (signed("model_task", replace(request, inputs={**request.inputs, "instruction_version": "changed"},
                                           params_digest="")), "stale_version"),
        )
        for task_state in ("created", "running", "uncertain", "succeeded"):
            for call_state in ("generated", "reserved", "dispatched", "unknown"):
                self.set_audit_states(f, request, task_state, call_state)
                original = self.audit(f, request)
                for bad, reason in invalid:
                    with self.subTest(task_state=task_state, call_state=call_state, reason=reason,
                                      task_type=bad.task_type), patch.object(
                            f.ports, "inspect", side_effect=AssertionError("submission rejection is not a receipt")):
                        with self.assertRaises(HostPortError) as raised:
                            await f.ports.run_model_task(bad)
                        self.assertEqual(raised.exception.code, reason)
                        self.assertEqual(self.audit(f, request), original)
        with self.assertRaises(HostPortError) as raised:
            await f.ports.run_model_task(replace(invalid[0][0], task_id="absent-task"))
        self.assertEqual(raised.exception.code, "invalid_config")
        self.assertIsNone(f.app.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id='absent-task'", ()))
        self.assertEqual(len(self.called), 1)

    async def test_permanent_recheck_only_fails_succeeded_settled_tasks(self):
        f, request = await self.choice_task()
        inspection = p.TaskInspection(request.scope, request.task_id, request.params_digest)
        expected = {"created": "pending", "admitted": "pending", "retry_scheduled": "pending",
                    "running": "running", "cancelling": "running", "uncertain": "unknown",
                    "failed": "failed", "cancelled": "cancelled", "expired": "failed", "succeeded": "failed"}
        for task_state, status in expected.items():
            with self.subTest(task_state=task_state):
                self.set_audit_states(f, request, task_state)
                original = self.audit(f, request)
                with patch.object(f.app.role_reader.port_tasks, "recover_result", side_effect=TaskError(
                        "reading_source_changed", "permanent result recheck")):
                    receipt = await f.ports.inspect_task(inspection)
                self.assertEqual(receipt.receipt.status.value, status)
                if task_state == "succeeded":
                    self.assertEqual(receipt.receipt.reason_code, m.ReasonCode.STALE_VERSION)
                elif task_state in ("created", "admitted", "retry_scheduled", "running", "cancelling"):
                    self.assertIsNone(receipt.receipt.reason_code)
                elif task_state == "uncertain":
                    self.assertEqual(receipt.receipt.reason_code, m.ReasonCode.RESULT_UNKNOWN)
                self.assertIsNone(receipt.result_ref)
                self.assertEqual(self.audit(f, request), original)
        self.assertEqual(len(self.called), 1)

    async def test_result_error_never_overrides_unsettled_calls(self):
        f, request = await self.choice_task()
        error = f.ports.make("ContractError", reason_code=m.ReasonCode.SOURCE_UNAVAILABLE,
                             message="permanent result recheck", operation_id=request.task_id)
        for task_state in ("created", "running", "uncertain", "succeeded"):
            for call_state in ("reserved", "dispatched", "unknown"):
                with self.subTest(task_state=task_state, call_state=call_state):
                    self.set_audit_states(f, request, task_state, call_state)
                    original = self.audit(f, request)
                    with patch.object(f.app.role_reader.port_tasks, "recover_result", side_effect=AssertionError(
                            "unsettled calls cannot be recovered")):
                        receipt = f.ports.inspect(request.scope, request.task_id, request.params_digest,
                                                  include_result=False, result_error=error)
                    expected = "running" if task_state in ("created", "running") and call_state != "unknown" else "unknown"
                    self.assertEqual(receipt.receipt.status.value, expected)
                    self.assertEqual(receipt.receipt.reason_code,
                                     m.ReasonCode.RESULT_UNKNOWN if expected == "unknown" else None)
                    self.assertIsNone(receipt.result_ref)
                    self.assertEqual(self.audit(f, request), original)
        self.assertEqual(len(self.called), 1)

    async def test_revision_conflict_is_unknown_then_original_result_recovers(self):
        f, request = await self.choice_task()
        self.set_audit_states(f, request, "uncertain")
        inspection = p.TaskInspection(request.scope, request.task_id, request.params_digest)
        original = self.audit(f, request)
        with patch.object(f.app.role_reader.port_tasks, "recover_result", side_effect=TaskError(
                "task_revision_conflict", "another inspector settled the task")) as recover:
            receipt = await f.ports.inspect_task(inspection)
        self.assertEqual(recover.call_count, 1)
        self.assertEqual((receipt.receipt.status, receipt.receipt.reason_code, receipt.result_ref),
                         (m.ReceiptStatus.UNKNOWN, m.ReasonCode.RESULT_UNKNOWN, None))
        self.assertEqual(self.audit(f, request), original)
        recovered = await f.ports.inspect_task(inspection)
        self.assertEqual(recovered.receipt.status, m.ReceiptStatus.SUCCEEDED)
        self.assertEqual(recovered.result_ref, "life.model.result.v1:" + request.task_id)
        self.assertEqual(len(self.called), 1)

    async def test_competing_reconciliation_keeps_winner_audit_and_recovers(self):
        f, request = await self.choice_task()
        self.set_audit_states(f, request, "uncertain")
        inspection = p.TaskInspection(request.scope, request.task_id, request.params_digest)
        reconcile = f.app.tasks.reconcile

        def race(*args, **kwargs):
            winner = reconcile(*args, **kwargs)
            self.assertTrue(winner.is_success, winner)
            return reconcile(*args, **kwargs)

        with patch.object(f.app.tasks, "reconcile", side_effect=race) as competing:
            receipt = await f.ports.inspect_task(inspection)
        self.assertEqual(competing.call_count, 1)
        self.assertEqual((receipt.receipt.status, receipt.receipt.reason_code),
                         (m.ReceiptStatus.UNKNOWN, m.ReasonCode.RESULT_UNKNOWN))
        self.assertEqual(f.app.tasks.read(f.context, request.task_id).unwrap().state, "succeeded")
        with patch.object(f.ports, "run_model_task", side_effect=AssertionError("must recover original result")):
            result = await choose(f.executor, f.context.key, [json.loads(encode(f.activity))], "host-recheck", "window")
        self.assertEqual(result["state"], "committed")
        self.assertEqual(len(self.called), 1)

    async def test_final_submission_recheck_rejects_permanent_result_without_audit_rewrite(self):
        f, request = await self.choice_task()
        original = self.audit(f, request)
        with patch.object(f.ports, "asset", side_effect=TaskError("source_reference_invalid", "bad result source")):
            receipt = await f.ports.run_model_task(request)
        self.assertEqual((receipt.receipt.status, receipt.receipt.reason_code, receipt.result_ref),
                         (m.ReceiptStatus.FAILED, m.ReasonCode.SOURCE_UNAVAILABLE, None))
        self.assertEqual(self.audit(f, request), original)
        self.assertEqual(len(self.called), 1)
