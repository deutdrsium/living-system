from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from companion_v2.execution.models import digest, encode, require, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.integration import life_commands, life_events
from companion_v2.role_work.ledger import PersistentRoleLifeLedger
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.storage.activity_scan_floor import FLOORS, next_rowid
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.storage.runtime import StorageRuntime
from good_life.activity_import import stage_activities
from good_life.prepared_commands import PreparedCommands
from good_life.staged_activity_commands import StagedActivityTransitions
from good_life.storage import LifeStorage
from rehearse_activity_rollback import rehearse


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ActivityRollbackTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        role = SubjectRef("installation", SubjectKind.BOT, "role")
        self.context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=role), key="context")
        self.owner = RoleOwnerRef("installation", "bot", "role")
        self.activity = RoleActivity("activity", "role", scope, NOW, RoleActivityState.ACTIVE)
        self.ledger = PersistentRoleLifeLedger(self.runtime)
        self.ledger.add_activity(self.activity)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, ?)",
                       ("memory.activity-ingestion.v1:test", '{"activity":100,"journal":0}'))
        self.version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        self.life_root = self.root / "life"
        stage_activities(self.runtime.database_path, expected_version=self.version, destination=self.life_root)
        self.life = LifeStorage.open(self.life_root)
        self.addCleanup(self.life.close)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.transitions = StagedActivityTransitions(self.life)
        self.operation = OperationRef("life-operation", "role.life", scope, VersionRef("life-operation", 1), "life-operation")

    def request(self, consumers=()):
        return {"command_id": "command", "owner_key": digest(self.owner), "context_key": self.context.key,
            "kind": "activity.transition", "expected_revision": 1,
            "payload": {"activity_id": "activity", "operation_id": self.operation.operation_id,
                "previous_hash": hashlib.sha256(encode(self.activity).encode("utf-8")).hexdigest(),
                "state": "completed", "observed_at": stamp(NOW + timedelta(seconds=1)), "checkpoint": "finished",
                "reading": None, "work": None, "sleep": None, "event_consumers": list(consumers)}}

    def finish_transition(self, consumers=()):
        request = self.request(consumers)
        self.transitions.prepare(request, prepared_at=NOW)
        operations = OperationRepository(self.runtime)
        def reserve(_connection, _request):
            require(operations.reserve(self.operation, parameter_hash="activity-parameters", observed_at=NOW))
        decision = life_commands.decide(self.runtime, context=self.context, request=request, outcome="commit", observed_at=NOW,
            verify_prepared=self.commands.verify_prepared, authorize=lambda *_: Outcome.success(True), apply=reserve)
        self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                              apply=self.transitions.apply)
        def finalize(_connection, _decision):
            require(operations.transition(self.operation.operation_id, expected_state=OperationState.ACCEPTED,
                                         next_state=OperationState.COMPLETED, observed_at=NOW))
        life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.transitions.verify_commit,
                               finalize=finalize)
        self.transitions.acknowledge("command", verify_host_completion=lambda evidence:
            life_commands.verify_completion(self.runtime, evidence) and
            operations.get(self.operation.operation_id).unwrap().state is OperationState.COMPLETED)

    def run_rehearsal(self, destination=None):
        return rehearse(self.runtime.database_path, self.life_root / "good-life.sqlite3",
                        expected_version=self.version, destination=destination)

    def test_new_receipts_versions_and_scan_floors_return_to_an_isolated_copy(self):
        self.finish_transition()
        with self.life.transaction() as db:
            db.execute("UPDATE sqlite_sequence SET seq=500 WHERE name='staged_role_activity_revisions'")
        expected = {"activities": 1, "revisions": 2, "artifacts": 0, "receipts": 1, "events": 0}
        self.assertEqual(self.run_rehearsal(), {"state": "dry_run", **expected})
        output = self.root / "restored.sqlite3"
        self.assertEqual(self.run_rehearsal(output), {"state": "rehearsed", **expected})
        self.assertEqual(self.ledger.activity("activity", owner=self.owner).revision, 1)
        with closing(sqlite3.connect(output, isolation_level=None)) as db:
            self.assertEqual(db.execute("SELECT revision, state FROM role_activities").fetchone(), (2, "completed"))
            self.assertEqual(db.execute("SELECT rowid FROM role_life_operations").fetchone()[0], 101)
            self.assertEqual(db.execute("SELECT value FROM runtime_metadata WHERE key=?", (FLOORS["role_activity_revisions"],)).fetchone()[0], "500")
            db.execute("BEGIN")
            self.assertEqual(next_rowid(db, "role_activity_revisions"), 501)
            self.assertEqual(next_rowid(db, "role_life_operations"), 102)
            db.rollback()

    def test_events_need_both_acknowledgement_and_matching_host_receipt(self):
        self.finish_transition(("journal",))
        event = self.life.pending_events("journal")[0]
        with self.assertRaisesRegex(ValueError, "not yet acknowledged"):
            self.run_rehearsal()
        with self.life.transaction():
            self.life.acknowledge(event_id=event["event_id"], consumer="journal", acknowledged_at=NOW)
        with self.assertRaisesRegex(ValueError, "no matching host receipt"):
            self.run_rehearsal()
        life_events.consume_once(self.runtime, context=self.context, event=event, consumer="journal", observed_at=NOW,
                                 authorize=lambda *_: Outcome.success(True), apply=lambda *_: None)
        self.assertEqual(self.run_rehearsal()["events"], 1)

    def test_unresolved_command_or_removed_original_stops_rehearsal(self):
        self.transitions.prepare(self.request(), prepared_at=NOW)
        with self.assertRaisesRegex(ValueError, "not yet resolved"):
            self.run_rehearsal()
        with self.life.transaction() as db:
            db.execute("UPDATE life_prepared_commands SET state='aborted'")
            db.execute("DELETE FROM staged_role_activity_revisions")
            db.execute("DELETE FROM staged_role_activities")
        with self.assertRaisesRegex(ValueError, "omitted an original"):
            self.run_rehearsal()

    def test_unconfirmed_host_operation_removes_failed_destination(self):
        self.finish_transition()
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET state='accepted' WHERE operation_id=?", (self.operation.operation_id,))
        output = self.root / "restored.sqlite3"
        with self.assertRaisesRegex(ValueError, "not confirmed"):
            self.run_rehearsal(output)
        self.assertFalse(output.exists())

    def test_copy_failure_rolls_back_and_removes_only_the_new_file(self):
        self.finish_transition()
        with self.runtime.transaction() as db:
            db.execute("""CREATE TRIGGER fail_activity_restore BEFORE INSERT ON role_activity_revisions
                WHEN NEW.revision=2 BEGIN SELECT RAISE(ABORT, 'simulated failure'); END""")
        output = self.root / "restored.sqlite3"
        with self.assertRaises(sqlite3.IntegrityError):
            self.run_rehearsal(output)
        self.assertFalse(output.exists())
        self.assertEqual(self.ledger.activity("activity", owner=self.owner).revision, 1)

    def test_existing_deleted_activity_receipt_survives_without_restoring_activity(self):
        removed = RoleActivity("removed", "role", self.context.scope, NOW, RoleActivityState.ACTIVE)
        self.ledger.add_activity(removed)
        self.ledger.transition_activity("removed", owner=self.owner, expected_revision=1,
            state=RoleActivityState.COMPLETED, operation_id="removed-operation", observed_at=NOW + timedelta(seconds=1))
        self.ledger.delete_activity("removed", owner=self.owner)
        other_life = self.root / "life-detached"
        stage_activities(self.runtime.database_path, expected_version=self.version, destination=other_life)
        output = self.root / "restored.sqlite3"
        rehearse(self.runtime.database_path, other_life / "good-life.sqlite3", expected_version=self.version, destination=output)
        with closing(sqlite3.connect(output)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM role_activities WHERE activity_id='removed'").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM role_life_operations WHERE operation_id='removed-operation'").fetchone())


if __name__ == "__main__":
    unittest.main()
