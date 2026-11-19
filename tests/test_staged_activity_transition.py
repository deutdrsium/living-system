from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError, digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, ScopeKind, ScopeRef, SourceRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.integration import life_commands
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleReadingReceipt, RoleSleepReceipt, advance_activity
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.storage.runtime import StorageRuntime
from good_life.activity_import import DDL
from good_life.activity_records import StagedActivityRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_activity_commands import StagedActivityTransitions, _build, _encode, _reading, _sleep, _work
from good_life.storage import LifeStorage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class StagedActivityTransitionTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        with self.life.transaction() as db:
            for statement in DDL:
                db.execute(statement)
            db.execute("INSERT INTO sqlite_sequence(name, seq) VALUES ('staged_role_life_operations', 597)")
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.transitions = StagedActivityTransitions(self.life)
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        role = SubjectRef("installation", SubjectKind.BOT, "role")
        self.context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=role), key="context-1")
        self.owner = RoleOwnerRef("installation", "bot", "role")
        work = RoleWorkSpec("social", self.context.key, "request")
        self.activity = RoleActivity("activity", "role", scope, NOW, RoleActivityState.ACTIVE, work=work)
        raw = encode(self.activity)
        with self.life.transaction() as db:
            db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest(self.owner), self.activity.activity_id, encode(self.owner), 1, "active", stamp(NOW), raw))
            db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES (?, ?, 1, ?, 'recorded', NULL, ?)""", (digest(self.owner), self.activity.activity_id, stamp(NOW), raw))
        self.updated = advance_activity(self.activity, expected_revision=1, state=RoleActivityState.COMPLETED,
                                        observed_at=NOW + timedelta(seconds=1), checkpoint="完成已记录的活动")
        self.request = {"command_id": "command-1", "owner_key": digest(self.owner), "context_key": self.context.key,
            "kind": "activity.transition", "expected_revision": 1,
            "payload": {"activity_id": "activity", "operation_id": "original-life-operation",
                        "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                        "state": "completed", "observed_at": stamp(NOW + timedelta(seconds=1)),
                        "checkpoint": "完成已记录的活动", "reading": None, "work": None, "sleep": None,
                        "event_consumers": ["journal", "interest"]}}
        self.original_operation = OperationRef("original-life-operation", "role.life", scope,
                                               VersionRef("original-life-operation", 1), "original-life-operation")

    def host_reserve(self, _connection, _request):
        receipt = require(OperationRepository(self.runtime).reserve(self.original_operation,
                          parameter_hash="original-parameters", observed_at=NOW))
        if receipt.replayed:
            raise ValueError("original operation was already reserved")

    def host_finalize(self, _connection, _decision):
        require(OperationRepository(self.runtime).transition(self.original_operation.operation_id,
            expected_state=OperationState.ACCEPTED, next_state=OperationState.COMPLETED, observed_at=NOW))

    def decision(self):
        return life_commands.decide(self.runtime, context=self.context, request=self.request, outcome="commit",
            observed_at=NOW, verify_prepared=self.commands.verify_prepared,
            authorize=lambda *_: Outcome.success(True), apply=self.host_reserve)

    def reopen(self):
        self.life.close()
        self.runtime.close()
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.transitions = StagedActivityTransitions(self.life)
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)

    def host_completion(self, evidence):
        receipt = OperationRepository(self.runtime).get(self.original_operation.operation_id)
        return (life_commands.verify_completion(self.runtime, evidence) and receipt.is_success
                and receipt.value.state is OperationState.COMPLETED and receipt.value.operation == self.original_operation
                and receipt.value.parameter_hash == "original-parameters")

    def test_real_activity_version_and_host_receipt_recover_before_event_delivery(self):
        other = replace(self.activity, activity_id="other-activity", role_id="other-role")
        with self.life.transaction() as db:
            db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest(other.owner), other.activity_id, encode(other.owner), 1, "active", stamp(NOW), encode(other)))
            db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES (?, ?, 1, ?, 'recorded', NULL, ?)""", (digest(other.owner), other.activity_id, stamp(NOW), encode(other)))
        self.transitions.prepare(self.request, prepared_at=NOW)
        decision = self.decision()
        self.assertEqual(OperationRepository(self.runtime).get(self.original_operation.operation_id).unwrap().state,
                         OperationState.ACCEPTED)
        self.reopen()
        pending = self.commands.pending()[0]
        recovered_request = self.commands.recovery_request(command_id=pending["command_id"], owner_key=pending["owner_key"],
            context_key=pending["context_key"], request_hash=pending["request_hash"])
        self.assertEqual(recovered_request, self.request)
        self.assertIsNone(self.commands.recovery_request(command_id=pending["command_id"], owner_key=pending["owner_key"],
            context_key="other-context", request_hash=pending["request_hash"]))
        decision = life_commands.lookup(self.runtime, context_key=pending["context_key"],
            command_id=pending["command_id"], request_hash=pending["request_hash"])
        result = self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                                       apply=self.transitions.apply)
        self.assertEqual(result["result"]["record_json"], encode(self.updated))
        self.assertEqual(self.life.pending_events("journal"), ())
        records = StagedActivityRecords(self.life)
        proxy = LifeLedgerReadProxy(self.runtime, SimpleNamespace(
            dispatch=lambda handler, operation, **request: records.dispatch(operation, **request)))
        with self.assertRaises(TaskError) as blocked:
            proxy.activity("activity", owner=self.owner)
        self.assertEqual(blocked.exception.code, "life_activity_pending")
        with self.assertRaises(TaskError):
            proxy.scan_activity_receipts(after=0, limit=10)
        self.assertEqual(proxy.activity("other-activity", owner=other.owner), other)
        self.assertEqual(records.activity(owner_key=digest(self.owner), activity_id="activity"), encode(self.updated))
        self.assertEqual(records.activity_revision(owner_key=digest(self.owner), activity_id="activity", revision=2), encode(self.updated))
        self.assertEqual(records.activity_receipt(owner_key=digest(self.owner), operation_id="original-life-operation"), encode(self.updated))
        self.assertGreater(records.scan_activity_receipts(after=597, limit=1)[0]["scan_id"], 597)
        self.assertEqual(OperationRepository(self.runtime).get(self.original_operation.operation_id).unwrap().state,
                         OperationState.ACCEPTED)
        self.reopen()
        records = StagedActivityRecords(self.life)
        proxy = LifeLedgerReadProxy(self.runtime, SimpleNamespace(
            dispatch=lambda handler, operation, **request: records.dispatch(operation, **request)))
        self.assertTrue(life_commands.complete(self.runtime, decision, observed_at=NOW,
                        verify_life_commit=self.transitions.verify_commit, finalize=self.host_finalize))
        self.assertEqual(OperationRepository(self.runtime).get(self.original_operation.operation_id).unwrap().state,
                         OperationState.COMPLETED)
        self.assertEqual(self.life.pending_events("journal"), ())
        with self.assertRaises(TaskError):
            proxy.activity("activity", owner=self.owner)
        self.assertTrue(self.transitions.acknowledge("command-1",
                        verify_host_completion=self.host_completion))
        self.assertFalse(self.transitions.acknowledge("command-1",
                         verify_host_completion=self.host_completion))
        self.assertEqual(len(self.life.pending_events("journal")), 1)
        self.assertEqual(len(self.life.pending_events("interest")), 1)
        self.assertEqual(proxy.activity("activity", owner=self.owner), self.updated)

    def test_changed_identity_and_stale_revision_are_rejected_before_host_decision(self):
        altered = json.loads(encode(self.activity.work))
        foreign_scope = json.loads(encode(self.context.scope))
        foreign_scope["session_id"] = "other-session"
        altered["source"] = {"scope": foreign_scope}
        wrong = {**self.request, "payload": {**self.request["payload"],
            "work": altered}}
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.transitions.prepare(wrong, prepared_at=NOW)
        with self.assertRaises(ValueError):
            self.transitions.prepare({**self.request, "expected_revision": 9}, prepared_at=NOW)
        self.assertEqual(self.commands.pending(), ())
        self.assertFalse(OperationRepository(self.runtime).get(self.original_operation.operation_id).is_success)

    def test_out_of_band_change_blocks_commit_without_receipt_or_event(self):
        self.transitions.prepare(self.request, prepared_at=NOW)
        decision = self.decision()
        with self.life.transaction() as db:
            db.execute("UPDATE staged_role_activities SET record_json='{}' WHERE activity_id='activity'")
        with self.assertRaises(ValueError):
            self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                                  apply=self.transitions.apply)
        self.assertIsNone(StagedActivityRecords(self.life).activity_receipt(
            owner_key=digest(self.owner), operation_id="original-life-operation"))
        self.assertEqual(self.life.pending_events("journal"), ())
        with self.assertRaises(TaskError):
            life_commands.complete(self.runtime, decision, observed_at=NOW,
                                   verify_life_commit=self.transitions.verify_commit, finalize=self.host_finalize)

    def test_changed_committed_activity_cannot_confirm_or_publish(self):
        self.transitions.prepare(self.request, prepared_at=NOW)
        decision = self.decision()
        self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                              apply=self.transitions.apply)
        with self.life.transaction() as db:
            db.execute("UPDATE staged_role_activities SET record_json='{}' WHERE activity_id='activity'")
        with self.assertRaises(TaskError):
            life_commands.complete(self.runtime, decision, observed_at=NOW,
                                   verify_life_commit=self.transitions.verify_commit, finalize=self.host_finalize)
        self.assertEqual(self.life.pending_events("journal"), ())

    def test_acknowledged_historical_receipt_survives_later_activity_revision(self):
        paused = advance_activity(self.activity, expected_revision=1, state=RoleActivityState.PAUSED,
                                  observed_at=NOW + timedelta(seconds=1))
        self.request["payload"]["state"] = "paused"
        self.request["payload"]["checkpoint"] = None
        self.request["payload"]["event_consumers"] = []
        self.transitions.prepare(self.request, prepared_at=NOW)
        decision = self.decision()
        self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                              apply=self.transitions.apply)
        life_commands.complete(self.runtime, decision, observed_at=NOW,
                               verify_life_commit=self.transitions.verify_commit, finalize=self.host_finalize)
        self.transitions.acknowledge("command-1", verify_host_completion=self.host_completion)
        resumed = advance_activity(paused, expected_revision=2, state=RoleActivityState.ACTIVE,
                                   observed_at=NOW + timedelta(seconds=2))
        with self.life.transaction() as db:
            db.execute("UPDATE staged_role_activities SET revision=3, state='active', updated_at=?, record_json=? WHERE activity_id='activity'",
                       (stamp(resumed.updated_at), encode(resumed)))
            db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES (?, 'activity', 3, ?, 'recorded', 'resume', ?)""",
                (digest(self.owner), stamp(resumed.updated_at), encode(resumed)))
            db.execute("INSERT INTO staged_role_life_operations(operation_id, owner_key, result_json) VALUES ('resume', ?, ?)",
                       (digest(self.owner), encode(resumed)))
        self.assertTrue(self.transitions.verify_commit(decision))
        self.assertFalse(life_commands.complete(self.runtime, decision, observed_at=NOW,
                                                verify_life_commit=self.transitions.verify_commit, finalize=self.host_finalize))

    def test_reading_and_sleep_transitions_match_original_domain_logic(self):
        scope = self.context.scope
        source = SourceRef("book", "role.reading", scope, VersionRef("book", 1), NOW)
        reading_work = RoleWorkSpec("reading", self.context.key, "read", source, "hash", 0, 5)
        reading_activity = replace(self.activity, work=reading_work)
        reading = RoleReadingReceipt(source, "hash", 10, 0, 5, NOW + timedelta(seconds=1), "一点感想")
        sleep_work = RoleWorkSpec("sleep", self.context.key, "sleep", due_at=NOW + timedelta(minutes=30))
        sleep_activity = replace(self.activity, work=sleep_work)
        sleep = RoleSleepReceipt("manual", base_need_minutes=30, target_minutes=30, slept_seconds=60)
        for activity, receipt_name, receipt in ((reading_activity, "reading", reading), (sleep_activity, "sleep", sleep)):
            kwargs = {receipt_name: receipt}
            expected = advance_activity(activity, expected_revision=1, state=RoleActivityState.COMPLETED,
                                        observed_at=NOW + timedelta(seconds=1), **kwargs)
            payload = {"state": "completed", "observed_at": "2026-01-01T08:00:01+08:00", "checkpoint": None,
                       "reading": None, "work": None, "sleep": None}
            payload[receipt_name] = json.loads(encode(receipt))
            self.assertEqual(_encode(_build(json.loads(encode(activity)), payload)), encode(expected))

    def test_wire_validation_matches_domain_work_and_receipt_rules(self):
        scope = self.context.scope
        scope_wire = json.loads(encode(scope))
        source = SourceRef("book", "role.reading", scope, VersionRef("book", 1), NOW)
        work = RoleWorkSpec("reading", " context ", " request ", source, " hash ", 3, 8)
        wire_work = json.loads(encode(work))
        _work(wire_work, scope_wire)
        reading = RoleReadingReceipt(source, "hash", 12, 3, 8, NOW + timedelta(seconds=1), "读到这里")
        wire_reading = json.loads(encode(reading))
        _reading(wire_reading, scope_wire, "completed", stamp(reading.completed_at))
        for name, value in (("end_offset", 13), ("content_hash", " hash ")):
            with self.subTest(reading_field=name):
                invalid = {**wire_reading, name: value}
                with self.assertRaises(ValueError):
                    _reading(invalid, scope_wire, "completed", stamp(reading.completed_at))
        foreign = json.loads(json.dumps(wire_work))
        foreign["source"]["scope"]["session_id"] = "other-session"
        with self.assertRaises(ValueError):
            _work(foreign, scope_wire)
        sleep_work = RoleWorkSpec("sleep", self.context.key, "sleep", due_at=NOW + timedelta(minutes=30))
        wire_sleep_work = json.loads(encode(sleep_work))
        _work(wire_sleep_work, scope_wire)
        sleep = json.loads(encode(RoleSleepReceipt("nap", base_need_minutes=30, target_minutes=30)))
        _sleep(sleep, wire_sleep_work)
        with self.assertRaises(ValueError):
            _sleep({**sleep, "quality_min": 95, "quality_max": 90}, wire_sleep_work)

    def test_activity_state_rules_match_original_domain_logic(self):
        for original_state in RoleActivityState:
            activity = replace(self.activity, state=original_state)
            for requested_state in RoleActivityState:
                payload = {"state": requested_state.value, "observed_at": stamp(NOW + timedelta(seconds=1)),
                           "checkpoint": None, "reading": None, "work": None, "sleep": None}
                with self.subTest(original=original_state.value, requested=requested_state.value):
                    try:
                        expected = advance_activity(activity, expected_revision=1, state=requested_state,
                                                    observed_at=NOW + timedelta(seconds=1))
                    except ValueError:
                        with self.assertRaises(ValueError):
                            _build(json.loads(encode(activity)), payload)
                    else:
                        self.assertEqual(_encode(_build(json.loads(encode(activity)), payload)), encode(expected))

    def test_reading_completion_preserves_the_exact_consumed_span(self):
        source = SourceRef("book", "role.reading", self.context.scope, VersionRef("book", 1), NOW)
        work = RoleWorkSpec("reading", self.context.key, "read", source, "hash", 3, 8)
        activity = replace(self.activity, work=work)
        raw = encode(activity)
        with self.life.transaction() as db:
            db.execute("UPDATE staged_role_activities SET record_json=? WHERE activity_id='activity'", (raw,))
            db.execute("UPDATE staged_role_activity_revisions SET record_json=? WHERE activity_id='activity'", (raw,))
        receipt = RoleReadingReceipt(source, "hash", 12, 3, 8, NOW + timedelta(seconds=1), "读到这里")
        self.request["payload"]["previous_hash"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        self.request["payload"]["reading"] = json.loads(encode(receipt))
        invalid = json.loads(json.dumps(self.request))
        invalid["payload"]["reading"]["end_offset"] = 13
        with self.assertRaises(ValueError):
            self.transitions.prepare(invalid, prepared_at=NOW)
        self.transitions.prepare(self.request, prepared_at=NOW)
        decision = self.decision()
        result = self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                                       apply=self.transitions.apply)
        saved = json.loads(result["result"]["record_json"])
        self.assertEqual((saved["reading"]["start_offset"], saved["reading"]["end_offset"],
                          saved["reading"]["text_length"]), (3, 8, 12))
        self.assertEqual(self.life.pending_events("journal"), ())
        life_commands.complete(self.runtime, decision, observed_at=NOW,
                               verify_life_commit=self.transitions.verify_commit, finalize=self.host_finalize)
        self.transitions.acknowledge("command-1", verify_host_completion=self.host_completion)
        self.assertEqual(len(self.life.pending_events("journal")), 1)


if __name__ == "__main__":
    unittest.main()
