"""Declaration conformance only; no claim of runtime contract enforcement."""
import ast
from collections.abc import Mapping
from dataclasses import FrozenInstanceError, fields, is_dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import inspect
import json
from pathlib import Path
import sys
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.contracts import CONTRACT_VERSION
from good_life.contracts import models as m, ports as p


# Test-only reference codec. Runtime codecs belong to future boundary adapters.
def to_wire(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive time")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value):
        return {field.name: to_wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {key: to_wire(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [to_wire(item) for item in value]
    return value


def json_value(value):
    if isinstance(value, list):
        return tuple(json_value(item) for item in value)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value


def from_wire(record_type, value):
    if value is None:
        return None
    if hasattr(record_type, "__supertype__"):
        return record_type(from_wire(record_type.__supertype__, value))
    if record_type is datetime:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("naive time")
        return parsed.astimezone(timezone.utc)
    if inspect.isclass(record_type) and issubclass(record_type, Enum):
        return record_type(value)
    if is_dataclass(record_type):
        hints = get_type_hints(record_type)
        return record_type(**{key: from_wire(hints[key], item) for key, item in value.items()})
    origin, args = get_origin(record_type), get_args(record_type)
    if origin in (Union, UnionType):
        # Optional structured record, or recursive untyped JSON value.
        nonnull = [arg for arg in args if arg is not type(None)]
        if len(nonnull) == 1:
            return from_wire(nonnull[0], value)
        structured = [arg for arg in args if is_dataclass(arg)]
        return from_wire(structured[0], value) if structured else json_value(value)
    if origin is tuple:
        return tuple(from_wire(args[0], item) for item in value)
    if origin is Mapping:
        return {key: from_wire(args[1], item) for key, item in value.items()}
    return json_value(value)


class ContractsV1Tests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
        self.owner, self.context = m.OwnerKey("owner"), m.ContextKey("context")
        self.audience = m.AudienceBound("permission-1", (self.context,))
        self.source = m.SourceRef("source-1", "v1", None, 0, 12,
                                  self.now, None, "permission-1")
        self.scope = m.ExecutionScope(self.owner, self.context, "authority-1",
                                      m.RegistrationGeneration(1))
        self.outcome = m.Outcome(m.OutcomeStatus.SUCCEEDED, True, "read one section")
        self.receipt = m.OperationReceipt("operation-1", "model", m.ReceiptStatus.SUCCEEDED,
                                          "audit-1")
        self.artifact = m.Artifact("artifact-1", "impression", "asset-1", (self.source,),
                                   m.Provenance.GENERATED, self.audience,
                                   (m.FieldProvenance("/quote", m.Provenance.SOURCE_BACKED),))
        self.event = m.EventEnvelope("event-1", "activity.completed", m.SchemaVersion(1),
                                     self.owner, self.context, "instance-1", m.Revision(2),
                                     self.now, self.now, None, {"partial": True},
                                     (self.source,), m.Provenance.SOURCE_BACKED, self.audience)

    def assert_roundtrip(self, record):
        payload = json.dumps(to_wire(record), ensure_ascii=False, allow_nan=False)
        self.assertEqual(from_wire(type(record), json.loads(payload)), record)
        self.assertNotIn("__class__", payload)
        return json.loads(payload)

    def test_versions_are_independent_and_contract_is_frozen(self):
        # The architecture advances; the v1 shapes tested here stay frozen.
        self.assertEqual(CONTRACT_VERSION, 2)
        for alias in (m.SchemaVersion, m.TemplateVersion, m.Revision, m.RegistrationGeneration):
            self.assertIs(alias.__supertype__, int)
        self.assertEqual(len({id(m.SchemaVersion), id(m.TemplateVersion), id(m.Revision),
                              id(m.RegistrationGeneration)}), 4)
        self.assertIn("contract_version: 2", (ROOT / "docs/architecture-next/03-shared-contracts.md").read_text(encoding="utf-8"))

    def test_enums_are_complete_without_legacy_state_leakage(self):
        expected = {
            m.CandidateStatus: "pending consumed rejected expired",
            m.ActivityStatus: "preparing running paused completed failed cancelled unknown",
            m.ExecutionOwner: "life host_bridge",
            m.OutcomeStatus: "succeeded failed cancelled",
            m.ReceiptStatus: "pending running succeeded rejected failed cancelled unknown",
            m.StateQuality: "valid estimated stale unknown unavailable",
            m.ValueType: "boolean integer number string object array",
            m.Provenance: "source_backed simulated authored generated",
            m.ReasonCode: "invalid_config missing_dependency unauthorized stale_version insufficient_resource no_time_window source_unavailable provider_unavailable result_unknown cancelled",
        }
        actual = {value for value in vars(m).values()
                  if inspect.isclass(value) and issubclass(value, Enum) and value is not Enum}
        self.assertEqual(actual, set(expected))
        for enum, values in expected.items():
            with self.subTest(enum=enum.__name__):
                self.assertEqual({item.value for item in enum}, set(values.split()))
                self.assertEqual(len(enum.__members__), len(enum))
                for item in enum:
                    self.assertIs(enum(json.loads(json.dumps(item.value))), item)
        for legacy in ("planned", "active", "uncertain", "recorded"):
            self.assertRaises(ValueError, m.ActivityStatus, legacy)

    def test_exact_activity_transition_graph(self):
        expected = {
            "preparing": {"running", "failed", "cancelled", "unknown"},
            "running": {"paused", "completed", "failed", "cancelled", "unknown"},
            "paused": {"running", "failed", "cancelled", "unknown"},
            "unknown": {"preparing", "running", "paused", "completed", "failed", "cancelled"},
            "completed": set(), "failed": set(), "cancelled": set(),
        }
        graph = dict(m.ACTIVITY_TRANSITIONS)
        self.assertEqual(len(graph), len(m.ACTIVITY_TRANSITIONS))
        self.assertEqual(set(graph), set(m.ActivityStatus))
        self.assertEqual({state.value: {end.value for end in ends} for state, ends in graph.items()}, expected)
        for start in m.ActivityStatus:
            for end in m.ActivityStatus:
                self.assertEqual(end in graph[start], end.value in expected[start.value])
        # UNKNOWN recovery still needs evidence; a table alone does not authorize it.
        self.assertNotIn(m.ActivityStatus.COMPLETED, graph[m.ActivityStatus.PREPARING])
        self.assertNotIn(m.ActivityStatus.PAUSED, graph[m.ActivityStatus.PREPARING])

    def test_exact_candidate_transition_graph(self):
        graph = dict(m.CANDIDATE_TRANSITIONS)
        self.assertEqual(len(graph), len(m.CANDIDATE_TRANSITIONS))
        self.assertEqual(set(graph), set(m.CandidateStatus))
        self.assertEqual(set(graph[m.CandidateStatus.PENDING]), set(m.CandidateStatus) - {m.CandidateStatus.PENDING})
        for state in (m.CandidateStatus.CONSUMED, m.CandidateStatus.REJECTED, m.CandidateStatus.EXPIRED):
            self.assertEqual(graph[state], ())

    def test_template_candidate_instance_roundtrip(self):
        ref = m.RecordRef("book", "book-1", m.Revision(3))
        template = m.ActivityTemplate("read", m.TemplateVersion(1), "reader@1", ("material",),
                                      ({"id": "read-window", "type": "time_window"},),
                                      {"free_s": 600}, {"material": {"required": True}},
                                      {"duration_s": (600, 900)})
        candidate = m.Candidate("candidate-1", self.owner, m.TemplateRef("read", m.TemplateVersion(1)),
                                ref, ("read-window",), self.now,
                                self.now.replace(hour=9), "read:book-1:window-1",
                                m.Revision(3), m.CandidateStatus.PENDING)
        instance = m.ActivityInstance("instance-1", self.owner, self.context, "read", m.TemplateVersion(1),
                                      "1", m.ExecutionOwner.HOST_BRIDGE, ref, "candidate-1", None,
                                      m.ActivityStatus.PREPARING, m.Revision(1), "material", None,
                                      self.now, 600, 0, {"offset": 0}, (), (self.source,),
                                      ("operation-1",), None, None)
        for record in (template, candidate, instance, m.Money(1000, "CNY")):
            with self.subTest(record=type(record).__name__):
                self.assert_roundtrip(record)

    def test_result_nested_receipts_effects_artifacts_and_event_roundtrip(self):
        effect = m.Effect("effect-1", "instance-1", "affect", "reading.impression",
                          {"delta": 0.1}, self.now, (self.source,))
        proposal = m.EventProposal("activity.completed", m.SchemaVersion(1), {"partial": True},
                                   (self.source,), m.Provenance.SOURCE_BACKED, self.audience)
        result = m.ActivityResult("instance-1", m.Revision(2), self.outcome, self.now,
                                  m.Progress(600, {"sections": 1}, True), (self.receipt,), (effect,),
                                  (proposal,), (self.artifact,),
                                  (m.ShareCandidate(("artifact-1",), "question", self.audience),))
        self.assert_roundtrip(result)
        wire = self.assert_roundtrip(self.event)
        self.assertEqual(wire["occurred_at"], "2026-11-16T08:00:00Z")
        self.assertEqual(wire["schema_version"], 1)
        self.assertEqual(wire["source_refs"][0]["collected_at"], None)
        self.assertEqual(wire["provenance"], "source_backed")
        self.assertEqual(wire["audience_bound"]["context_keys"], ["context"])

    def test_local_terminal_failure_carries_reason_without_external_receipt(self):
        for status, reason in ((m.OutcomeStatus.FAILED, m.ReasonCode.NO_TIME_WINDOW),
                               (m.OutcomeStatus.CANCELLED, m.ReasonCode.CANCELLED)):
            result = m.ActivityResult("instance-1", m.Revision(2),
                                      m.Outcome(status, False, "cannot continue", reason),
                                      self.now, m.Progress(0, {}, False), (), (), (), (), ())
            with self.subTest(status=status):
                wire = self.assert_roundtrip(result)
                self.assertEqual(wire["receipts"], [])
                self.assertEqual(wire["outcome"]["reason_code"], reason.value)

    def test_event_failure_receipt_preserves_identity_and_typed_error(self):
        for status, reason in ((m.ReceiptStatus.REJECTED, m.ReasonCode.INVALID_CONFIG),
                               (m.ReceiptStatus.FAILED, m.ReasonCode.PROVIDER_UNAVAILABLE),
                               (m.ReceiptStatus.UNKNOWN, m.ReasonCode.RESULT_UNKNOWN),
                               (m.ReceiptStatus.CANCELLED, m.ReasonCode.CANCELLED)):
            receipt = p.LifeEventReceipt("event-1", "host-inbox", self.owner, status,
                                         None, None, reason, "inspect original event")
            with self.subTest(status=status):
                wire = self.assert_roundtrip(receipt)
                self.assertEqual(wire["event_id"], "event-1")
                self.assertEqual(wire["reason_code"], reason.value)
                self.assertEqual(wire["message"], "inspect original event")

    def test_state_metadata_and_each_quality_roundtrip(self):
        var = m.StateVar("energy", "energy", m.ValueType.NUMBER, "1", m.StateRange(0, 1),
                         0.8, 0.8, 3600, True, (m.StateBucket("low", 0, 0.3),), m.SchemaVersion(1))
        self.assert_roundtrip(var)
        for quality in m.StateQuality:
            unknown = quality in (m.StateQuality.UNKNOWN, m.StateQuality.UNAVAILABLE)
            assumptions = m.EstimateAssumptions("energy@1", ("known sleep only",)) if quality is m.StateQuality.ESTIMATED else None
            state = m.StateValue(None if unknown else 0.5, self.now, m.Revision(2), (), quality, assumptions)
            with self.subTest(quality=quality):
                self.assert_roundtrip(m.StateSnapshot(self.owner, self.now, m.Revision(5), {"energy": state}))

    def test_port_records_roundtrip_and_stable_identifiers(self):
        requests = (
            p.AuthorityRequest(self.context, "read", ("source-1",)),
            p.PersonaRequest(self.scope, "impression", (self.source,)),
            p.PersonaContext("persona-1", "v1", {"interest": ("ethics",)}, (self.source,), self.audience),
            p.MaterialRequest(self.scope, self.source, "read"),
            p.AuthorizedMaterial(self.source, "一段材料", self.audience),
            p.ModelTaskRequest(self.scope, "task-1", "digest-1", "impression", m.TemplateRef("read", m.TemplateVersion(1)), {},
                               (self.source,), self.audience, p.ModelBudget(1, 100, 100)),
            p.ModelTaskReceipt("task-1", self.receipt, "result-1", p.TaskUsage(1, None, 20)),
            p.TaskInspection(self.scope, "task-1", "digest-1"),
            p.PlatformActionRequest(self.scope, "operation-1", "digest-1", "send", {}, (), self.audience),
            p.ActionInspection(self.scope, "operation-1", "digest-1"),
            p.PublishAssetRequest(self.scope, "operation-1", "digest-1", self.artifact),
            p.AssetReceipt(self.receipt, "asset-1"),
            p.AcceptLifeEventRequest(self.scope, self.event, "digest-1"),
            p.LifeEventReceipt("event-1", "memory", self.owner, m.ReceiptStatus.SUCCEEDED, "inbox-1", self.now),
            m.ContractError(m.ReasonCode.RESULT_UNKNOWN, "inspect original operation", "instance-1", "operation-1"),
        )
        for request in requests:
            with self.subTest(record=type(request).__name__):
                self.assert_roundtrip(request)

    def test_request_digest_preserves_business_identity_across_rebinding(self):
        def digest(namespace, request):
            wire = to_wire(request)
            wire["scope"] = {key: wire["scope"][key] for key in ("owner_key", "context_key")}
            for key in ("params_digest", "task_id", "operation_id"):
                wire.pop(key, None)
            canonical = json.dumps({"namespace": namespace, "request": wire},
                                   ensure_ascii=False, sort_keys=True,
                                   separators=(",", ":"), allow_nan=False)
            return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        request = p.PlatformActionRequest(self.scope, "op-1", "", "send",
                                           {"target_id": "target-1", "text": "你好"}, (), self.audience)
        original = digest("platform_action", request)
        rebound = replace(self.scope, authority_ref="new-authority",
                          registration_generation=m.RegistrationGeneration(2))
        self.assertEqual(original, digest("platform_action", replace(request, scope=rebound)))
        self.assertEqual(original, digest("platform_action", replace(request, operation_id="other", params_digest="cached")))
        changes = (
            replace(request, scope=replace(self.scope, owner_key=m.OwnerKey("other-owner"))),
            replace(request, scope=replace(self.scope, context_key=m.ContextKey("other-context"))),
            replace(request, payload={"target_id": "other-target", "text": "你好"}),
            replace(request, action="delete"),
        )
        for changed in changes:
            self.assertNotEqual(original, digest("platform_action", changed))
        self.assertNotEqual(original, digest("publish_asset", request))

    def test_all_declared_records_are_frozen_and_types_resolve(self):
        for module in (m, p):
            for value in vars(module).values():
                if inspect.isclass(value) and is_dataclass(value):
                    with self.subTest(record=value.__name__):
                        self.assertTrue(value.__dataclass_params__.frozen)
                        self.assertTrue(get_type_hints(value))
        self.assertRaises(FrozenInstanceError, setattr, self.event, "event_id", "other")
        self.assertRaises(FrozenInstanceError, setattr, self.receipt, "status", m.ReceiptStatus.UNKNOWN)

    def test_host_protocol_has_exact_async_signatures(self):
        expected = {
            "resolve_authority": (p.AuthorityRequest, m.ExecutionScope),
            "get_persona_context": (p.PersonaRequest, p.PersonaContext),
            "read_material": (p.MaterialRequest, p.AuthorizedMaterial),
            "run_model_task": (p.ModelTaskRequest, p.ModelTaskReceipt),
            "inspect_task": (p.TaskInspection, p.ModelTaskReceipt),
            "execute_platform_action": (p.PlatformActionRequest, m.OperationReceipt),
            "inspect_action": (p.ActionInspection, m.OperationReceipt),
            "publish_asset": (p.PublishAssetRequest, p.AssetReceipt),
            "accept_life_event": (p.AcceptLifeEventRequest, p.LifeEventReceipt),
        }
        methods = {name: method for name, method in vars(p.CompanionHostPorts).items() if not name.startswith("_")}
        self.assertEqual(set(methods), set(expected))
        for name, (request, response) in expected.items():
            method = methods[name]
            with self.subTest(method=name):
                self.assertTrue(inspect.iscoroutinefunction(method))
                self.assertEqual(list(inspect.signature(method).parameters), ["self", "request"])
                self.assertEqual(get_type_hints(method), {"request": request, "return": response})

    def test_no_existing_runtime_imports_contracts(self):
        # A3 introduces explicit target adapters; their imports do not change
        # synchronous companion.life.v1 or unconverted host business code.
        adapters = {"good_life/reading_execution.py", "good_life/reading_selection.py", "good_life/reading_scheduler.py"}
        for plugin in ("astrbot_plugin_good_life", "astrbot_plugin_private_companion"):
            for path in (ROOT / "plugins" / plugin).rglob("*.py"):
                relative = path.relative_to(ROOT / "plugins" / plugin)
                if "contracts" in relative.parts or (plugin == "astrbot_plugin_good_life" and relative.as_posix() in adapters):
                    continue
                text = path.read_text(encoding="utf-8-sig")
                # Existing runtime must remain unaware, including dynamic imports.
                self.assertNotIn("good_life.contracts", text, str(path))
                for node in ast.walk(ast.parse(text)):
                    if isinstance(node, ast.ImportFrom):
                        self.assertFalse((node.module or "").endswith("contracts"), str(path))
                        self.assertFalse(any(alias.name == "contracts" for alias in node.names), str(path))

    def test_contract_package_has_declarations_only(self):
        directory = ROOT / "plugins/astrbot_plugin_good_life/good_life/contracts"
        allowed_top = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign, ast.ClassDef)
        for path in directory.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in tree.body:
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                    continue
                self.assertIsInstance(node, allowed_top, str(path))
                if isinstance(node, ast.ClassDef):
                    for member in node.body:
                        if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            self.assertEqual(len(member.body), 1)
                            self.assertIsInstance(member.body[0], ast.Expr)
                            self.assertIs(member.body[0].value.value, Ellipsis)

    def test_reference_codec_rejects_naive_time_and_nonfinite_json(self):
        self.assertRaises(ValueError, to_wire, datetime(2026, 11, 16))
        self.assertRaises(ValueError, json.dumps, {"value": float("nan")}, allow_nan=False)
        self.assertRaises(ValueError, from_wire, datetime, "2026-11-16T08:00:00")


if __name__ == "__main__":
    unittest.main()
