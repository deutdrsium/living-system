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
from companion_v2.execution.models import digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, ScopeKind, ScopeRef, SourceRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.integration import life_commands
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleSleepReceipt
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.storage.runtime import StorageRuntime
from good_life.activity_import import DDL
from good_life.activity_records import ActivityTransitionPending, StagedActivityRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_activity_creations import StagedActivityCreations
from good_life.staged_activity_commands import StagedActivityTransitions
from good_life.storage import LifeStorage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def wire(value):
    return json.loads(encode(value))


class StagedActivityCreationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        with self.life.transaction() as db:
            for statement in DDL:
                db.execute(statement)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.creations = StagedActivityCreations(self.life)
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)
        self.scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        self.role = SubjectRef("installation", SubjectKind.BOT, "role")
        self.context = SimpleNamespace(scope=self.scope, role_binding=SimpleNamespace(role=self.role), key="context")
        self.owner = RoleOwnerRef("installation", "bot", "role")

    def request(self, *, context=None, kind="sleep", request_id="request", command_id="command"):
        context = context or self.context
        identifier = ("sleep-" if kind == "sleep" else "social-activity-") + digest([context.key, request_id])
        work = RoleWorkSpec(kind, context.key, request_id, due_at=NOW + timedelta(hours=8) if kind == "sleep" else None)
        sleep = RoleSleepReceipt("night", 480, 480) if kind == "sleep" else None
        activity = RoleActivity(identifier, "role", context.scope, NOW, RoleActivityState.ACTIVE, work=work, sleep=sleep)
        return {"command_id": command_id, "owner_key": digest(self.owner), "context_key": context.key,
                "kind": "activity.create", "expected_revision": 0,
                "payload": {"owner": wire(self.owner), "activity": wire(activity)}}

    def decide(self, request, context=None):
        return life_commands.decide(self.runtime, context=context or self.context, request=request, outcome="commit", observed_at=NOW,
            verify_prepared=self.commands.verify_prepared, authorize=lambda *_: Outcome.success(True), apply=lambda *_: None)

    def complete(self, decision):
        life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.creations.verify_commit)
        self.creations.acknowledge(decision["command_id"], verify_host_completion=lambda evidence:
                                   life_commands.verify_completion(self.runtime, evidence))

    def reopen_life(self):
        self.life.close()
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.creations = StagedActivityCreations(self.life)

    def test_creation_recovers_and_stays_hidden_until_host_confirms(self):
        request = self.request()
        self.creations.prepare(request, prepared_at=NOW)
        decision = self.decide(request)
        self.reopen_life()
        first = self.creations.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        self.assertEqual(first["state"], "committed")
        records = StagedActivityRecords(self.life)
        with self.assertRaises(ActivityTransitionPending):
            records.dispatch("activity", owner_key=digest(self.owner), activity_id=request["payload"]["activity"]["activity_id"])
        self.assertTrue(self.creations.verify_commit(decision))
        self.reopen_life()
        self.complete(decision)
        records = StagedActivityRecords(self.life)
        self.assertEqual(json.loads(records.dispatch("activity", owner_key=digest(self.owner),
            activity_id=request["payload"]["activity"]["activity_id"])), request["payload"]["activity"])
        self.assertEqual(self.life.pending_events("journal"), ())
        replay = self.creations.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        self.assertEqual(replay["state"], "acknowledged")

    def test_role_serialization_and_existing_sleep_prevent_double_sleep(self):
        request = self.request()
        self.creations.prepare(request, prepared_at=NOW)
        other_scope = replace(self.scope, session_id="other-session")
        other = SimpleNamespace(scope=other_scope, role_binding=SimpleNamespace(role=self.role), key="other-context")
        second = self.request(context=other, request_id="other", command_id="other-command")
        with self.assertRaisesRegex(ValueError, "another command"):
            self.creations.prepare(second, prepared_at=NOW)
        decision = self.decide(request)
        self.creations.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        self.complete(decision)
        with self.assertRaisesRegex(ValueError, "pending sleep"):
            self.creations.prepare(second, prepared_at=NOW)
        browsing = self.request(context=other, kind="social", request_id="browse", command_id="browse-command")
        with self.assertRaisesRegex(ValueError, "pending sleep"):
            self.creations.prepare(browsing, prepared_at=NOW)

    def test_cross_scope_identity_and_occupied_context_are_rejected(self):
        request = self.request(kind="social")
        bad = json.loads(json.dumps(request))
        bad["payload"]["activity"]["scope"]["bot_id"] = "another-bot"
        with self.assertRaisesRegex(ValueError, "crosses"):
            self.creations.prepare(bad, prepared_at=NOW)
        bad = json.loads(json.dumps(request))
        bad["payload"]["activity"]["work"]["context_key"] = "other-context"
        with self.assertRaisesRegex(ValueError, "crosses"):
            self.creations.prepare(bad, prepared_at=NOW)
        self.creations.prepare(request, prepared_at=NOW)
        decision = self.decide(request)
        self.creations.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        self.complete(decision)
        next_request = self.request(kind="social", request_id="second", command_id="second-command")
        with self.assertRaisesRegex(ValueError, "active activity"):
            self.creations.prepare(next_request, prepared_at=NOW)

    def test_active_reading_blocks_sleep_across_the_roles_windows(self):
        source = SourceRef("source", "library", self.scope, VersionRef("source", 1), NOW)
        work = RoleWorkSpec("reading", "another-context", "reading", source, digest("text"), 0, 4)
        reading = RoleActivity("reading", "role", self.scope, NOW, RoleActivityState.ACTIVE, work=work)
        with self.life.transaction() as db:
            db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest(self.owner), reading.activity_id, encode(self.owner), 1, "active", reading.updated_at.isoformat(), encode(reading)))
            db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES (?, ?, 1, ?, 'recorded', NULL, ?)""",
                (digest(self.owner), reading.activity_id, reading.updated_at.isoformat(), encode(reading)))
        with self.assertRaisesRegex(ValueError, "active reading"):
            self.creations.prepare(self.request(), prepared_at=NOW)

    def test_tampered_created_record_cannot_be_acknowledged(self):
        request = self.request(kind="social")
        self.creations.prepare(request, prepared_at=NOW)
        decision = self.decide(request)
        self.creations.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.creations.verify_commit)
        with self.life.transaction() as db:
            db.execute("UPDATE staged_role_activities SET state='paused'")
        with self.assertRaisesRegex(ValueError, "changed before host acknowledgement"):
            self.creations.acknowledge(decision["command_id"], verify_host_completion=lambda evidence:
                                       life_commands.verify_completion(self.runtime, evidence))
        with self.assertRaises(ActivityTransitionPending):
            StagedActivityRecords(self.life).dispatch("activity", owner_key=digest(self.owner),
                activity_id=request["payload"]["activity"]["activity_id"])

    def test_created_activity_can_transition_without_losing_creation_evidence(self):
        request = self.request(kind="social")
        self.creations.prepare(request, prepared_at=NOW)
        first = self.decide(request)
        self.creations.resolve(first, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value))
        self.complete(first)
        activity = request["payload"]["activity"]
        operation = OperationRef("pause-operation", "role.life", self.scope, VersionRef("pause-operation", 1), "pause-operation")
        transition = {"command_id": "pause-command", "owner_key": digest(self.owner), "context_key": self.context.key,
            "kind": "activity.transition", "expected_revision": 1,
            "payload": {"activity_id": activity["activity_id"], "operation_id": operation.operation_id,
                "previous_hash": hashlib.sha256(encode(activity).encode("utf-8")).hexdigest(), "state": "paused",
                "observed_at": stamp(NOW + timedelta(seconds=1)), "checkpoint": "interrupted",
                "reading": None, "work": None, "sleep": None, "event_consumers": []}}
        transitions = StagedActivityTransitions(self.life)
        transitions.prepare(transition, prepared_at=NOW)
        operations = OperationRepository(self.runtime)
        def reserve(_connection, _request):
            require(operations.reserve(operation, parameter_hash="pause", observed_at=NOW))
        second = life_commands.decide(self.runtime, context=self.context, request=transition, outcome="commit", observed_at=NOW,
            verify_prepared=self.commands.verify_prepared, authorize=lambda *_: Outcome.success(True), apply=reserve)
        self.commands.resolve(second, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                              apply=transitions.apply)
        def finalize(_connection, _decision):
            require(operations.transition(operation.operation_id, expected_state=OperationState.ACCEPTED,
                                         next_state=OperationState.COMPLETED, observed_at=NOW))
        life_commands.complete(self.runtime, second, observed_at=NOW, verify_life_commit=transitions.verify_commit, finalize=finalize)
        transitions.acknowledge(second["command_id"], verify_host_completion=lambda value:
                                life_commands.verify_completion(self.runtime, value))
        self.assertTrue(self.creations.verify_commit(first))
        current = StagedActivityRecords(self.life).dispatch("activity", owner_key=digest(self.owner), activity_id=activity["activity_id"])
        self.assertEqual((json.loads(current)["revision"], json.loads(current)["state"]), (2, "paused"))


if __name__ == "__main__":
    unittest.main()
