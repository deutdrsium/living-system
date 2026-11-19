"""V2 batch 1 declaration/serialization conformance; runtime tests are separate."""
from dataclasses import fields, is_dataclass, replace
from datetime import timedelta
from enum import Enum
import inspect
import json
import unittest
from typing import get_type_hints

import test_contracts_v1 as v1
from test_contracts_v1 import from_wire, to_wire
from good_life.contracts import models as m, ports as p, v2 as v


class ContractsV2Tests(unittest.TestCase):
    def setUp(self):
        fixture = v1.ContractsV1Tests()
        fixture.setUp()
        self.now, self.scope = fixture.now, fixture.scope
        self.receipt = v.OperationReceiptV2("task-1", "model", m.ReceiptStatus.SUCCEEDED,
            "audit-1", m.Revision(3), self.now)
        self.task = v.ModelTaskReceiptV2("task-1", self.receipt, "asset-1", p.TaskUsage(1, 10, 20),
            v.ResultAccess(v.ResultAccessStatus.AVAILABLE, self.now), self.now, self.now + timedelta(minutes=8))

    def roundtrip(self, record):
        wire = json.loads(json.dumps(to_wire(record), ensure_ascii=False, allow_nan=False))
        self.assertEqual(from_wire(type(record), wire), record)
        return wire

    def test_complete_enums_and_default_off(self):
        expected = {
            v.CommandDecision: "undecided commit abort",
            v.CommandSettlement: "pending confirmed unknown",
            v.ResultAccessStatus: "available unavailable unknown",
            v.TaskInspectionStatus: "found absent unknown",
        }
        actual = {value for value in vars(v).values() if inspect.isclass(value)
                  and issubclass(value, Enum) and value.__module__ == v.__name__}
        self.assertEqual(actual, set(expected))
        for enum, members in expected.items():
            self.assertEqual([item.value for item in enum], members.split())
            self.assertEqual(len(enum.__members__), len(enum))
            for item in enum:
                self.assertIs(enum(json.loads(json.dumps(item.value))), item)
        self.assertEqual(v.DEFAULT_CAPABILITIES, ())
        self.assertEqual(v.SUPPORTED_CONTRACT_VERSIONS, (1, 2))
        self.assertNotEqual(v.API_VERSION, "companion.life-host.async.v1")

    def test_exact_receipt_transition_table_and_immutable_terminals(self):
        expected = {
            "pending": {"running", "succeeded", "rejected", "failed", "cancelled", "unknown"},
            "running": {"succeeded", "failed", "cancelled", "unknown"},
            "unknown": {"pending", "running", "succeeded", "rejected", "failed", "cancelled"},
            "succeeded": set(), "rejected": set(), "failed": set(), "cancelled": set(),
        }
        graph = dict(v.RECEIPT_TRANSITIONS)
        self.assertEqual(len(graph), len(v.RECEIPT_TRANSITIONS))
        self.assertEqual(set(graph), set(m.ReceiptStatus))
        for start in m.ReceiptStatus:
            self.assertEqual({end.value for end in graph[start]}, expected[start.value])
            for end in m.ReceiptStatus:
                self.assertEqual(end in graph[start], end.value in expected[start.value])

    def test_all_new_records_roundtrip_with_utc_null_and_enum_values(self):
        command = "activity-model-complete-1"
        records = (
            self.receipt, self.task, v.ResultAccess(v.ResultAccessStatus.UNKNOWN, self.now, m.ReasonCode.RESULT_UNKNOWN),
            v.LifeCommandRequest(self.scope, command, "hash", "life.prepared-command.v1:" + command, "manifest-ref"),
            v.AbortPreparedCommandRequest(self.scope, command, "hash", "life.prepared-command.v1:" + command),
            v.LifeCommandReceipt(command, "hash", "decision-1", v.CommandDecision.COMMIT, v.CommandSettlement.PENDING),
            v.AuthorizedIntentRequest(self.scope, "op-1", "digest", "intent-ref"),
            v.IntentInspection(self.scope, "op-1", "digest"),
            v.IntentStartReceipt("op-1", m.ReceiptStatus.SUCCEEDED, "start-1", "audit-1"),
            v.TaskInspectionResult("task-1", v.TaskInspectionStatus.FOUND, self.task),
            v.TaskInspectionResult("task-1", v.TaskInspectionStatus.ABSENT, None, m.ReasonCode.MISSING_DEPENDENCY),
            v.TaskInspectionResult("task-1", v.TaskInspectionStatus.UNKNOWN, None, m.ReasonCode.RESULT_UNKNOWN),
        )
        for record in records:
            with self.subTest(record=type(record).__name__):
                wire = self.roundtrip(record)
                self.assertNotIn("__class__", wire)
        wire = self.roundtrip(self.task)
        self.assertEqual(wire["receipt"]["observed_at"], "2026-11-16T08:00:00Z")
        self.assertEqual(wire["deadline_at"], "2026-11-16T08:08:00Z")
        self.assertEqual(wire["result_access"]["status"], "available")
        self.assertIsNone(wire["receipt"]["reason_code"])

    def test_result_access_changes_do_not_change_execution_facts(self):
        for status, reason in ((v.ResultAccessStatus.UNAVAILABLE, m.ReasonCode.SOURCE_UNAVAILABLE),
                               (v.ResultAccessStatus.UNKNOWN, m.ReasonCode.RESULT_UNKNOWN),
                               (v.ResultAccessStatus.AVAILABLE, None)):
            changed = replace(self.task, result_ref="asset-1" if reason is None else None,
                result_access=v.ResultAccess(status, self.now + timedelta(minutes=1), reason))
            self.roundtrip(changed)
            self.assertEqual(changed.receipt, self.task.receipt)
            self.assertEqual(changed.receipt.receipt_revision, 3)
            self.assertEqual(changed.receipt.status, m.ReceiptStatus.SUCCEEDED)
            self.assertEqual(changed.deadline_at, self.task.deadline_at)

    def test_unknown_legacy_timing_is_explicit_null(self):
        legacy = replace(self.task, accepted_at=None, deadline_at=None)
        wire = self.roundtrip(legacy)
        self.assertIsNone(wire["accepted_at"])
        self.assertIsNone(wire["deadline_at"])
        self.assertEqual([field.name for field in fields(p.ModelTaskReceipt)],
                         ["task_id", "receipt", "result_ref", "usage"])

    def test_v1_strict_record_cannot_silently_accept_v2_receipt(self):
        self.assertRaises(KeyError, from_wire, m.OperationReceipt, to_wire(self.receipt))
        old = m.OperationReceipt("task-1", "model", m.ReceiptStatus.UNKNOWN, "audit-1", m.ReasonCode.RESULT_UNKNOWN)
        self.assertEqual(from_wire(m.OperationReceipt, to_wire(old)), old)
        self.assertRaises(TypeError, from_wire, v.OperationReceiptV2, to_wire(old))
        self.assertEqual(set(to_wire(old)), {"operation_id", "kind", "status", "evidence_ref", "reason_code", "message"})

    def test_v2_protocols_have_exact_async_signatures(self):
        expected = {
            v.LifeCoordinationPortsV2: {
                "reconcile_life_command": (v.LifeCommandRequest, v.LifeCommandReceipt),
                "abort_own_prepared_command": (v.AbortPreparedCommandRequest, v.LifeCommandReceipt),
                "start_authorized_intent": (v.AuthorizedIntentRequest, v.IntentStartReceipt),
                "inspect_authorized_intent": (v.IntentInspection, v.IntentStartReceipt),
            },
            v.ModelTaskPortsV2: {"run_model_task": (p.ModelTaskRequest, v.ModelTaskReceiptV2),
                                 "inspect_task": (p.TaskInspection, v.TaskInspectionResult)},
        }
        for protocol, signatures in expected.items():
            methods = {name: method for name, method in vars(protocol).items() if not name.startswith("_")}
            self.assertEqual(set(methods), set(signatures))
            for name, (request, response) in signatures.items():
                method = methods[name]
                self.assertTrue(inspect.iscoroutinefunction(method))
                self.assertEqual(list(inspect.signature(method).parameters), ["self", "request"])
                self.assertEqual(get_type_hints(method), {"request": request, "return": response})
        self.assertIsNot(p.CompanionHostPorts, v.CompanionHostPortsV2)

    def test_every_v2_record_is_frozen_and_resolvable(self):
        records = [item for item in vars(v).values() if inspect.isclass(item)
                   and is_dataclass(item) and item.__module__ == v.__name__]
        self.assertEqual(len(records), 10)
        for record in records:
            self.assertTrue(record.__dataclass_params__.frozen)
            self.assertTrue(get_type_hints(record))


if __name__ == "__main__":
    unittest.main()
