from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
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
from companion_v2.foundation.refs import OperationRef, RoleOwnerRef, ScopeKind, ScopeRef, SourceRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.integration import life_commands
from companion_v2.role_work.ledger import PersistentRoleLifeLedger
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State, RoleArtifact, RoleArtifactKind, advance_activity
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from companion_v2.storage.runtime import StorageRuntime
from good_life.activity_import import stage_activities
from good_life.activity_records import ActivityTransitionPending, StagedActivityRecords
from good_life.prepared_commands import PreparedCommands, _hash
from good_life.staged_activity_deletions import StagedActivityDeletions
from good_life.storage import LifeStorage
from rehearse_activity_rollback import rehearse


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ActivityDeletionRollbackTests(unittest.TestCase):
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
        source = SourceRef("source", "library", scope, VersionRef("source", 1), NOW)
        work = RoleWorkSpec("reading", self.context.key, "reading-request", source, digest("reading text"), 0, 7)
        self.ledger = PersistentRoleLifeLedger(self.runtime)
        self.ledger.add_activity(RoleActivity("book", "role", scope, NOW, State.ACTIVE, work=work))
        self.activity = self.ledger.transition_activity("book", owner=self.owner, expected_revision=1,
            state=State.CANCELLED, operation_id="reading-cancel", observed_at=NOW + timedelta(seconds=1))
        self.ledger.add_artifact(RoleArtifact("artifact", "book", RoleArtifactKind.TEXT, source, NOW, owner=self.owner),
                                 owner=self.owner)
        self.version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        self.life_root = self.root / "life"
        stage_activities(self.runtime.database_path, expected_version=self.version, destination=self.life_root)
        self.life = LifeStorage.open(self.life_root)
        self.addCleanup(self.life.close)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.deletions = StagedActivityDeletions(self.life)
        self.management = OperationRef("management-operation", "role.life.management", scope,
                                       VersionRef("management-operation", 1), "management-operation")
        self.data_hash = digest({"action": "remove", "activity_id": "book"})
        description = self.deletions.describe(owner_key=digest(self.owner), activity_id="book")
        self.request = {"command_id": "deletion", "owner_key": digest(self.owner), "context_key": self.context.key,
            "kind": "activity.delete", "expected_revision": self.activity.revision,
            "payload": {"activity_id": "book", "management_operation_id": self.management.operation_id,
                "management_parameter_hash": self.data_hash, "rows_hash": description["rows_hash"],
                "source_hash": digest(self.activity.work.source),
                "observed_at": stamp(NOW + timedelta(seconds=1)), "related": [],
                "previous_hash": hashlib.sha256(encode(self.activity).encode("utf-8")).hexdigest()}}

    def apply(self):
        self.deletions.prepare(self.request, prepared_at=NOW)
        operations = OperationRepository(self.runtime)
        def reserve(connection, _request):
            require(operations.reserve(self.management, parameter_hash=self.data_hash, observed_at=NOW))
            for item in self.request["payload"].get("related", []):
                operation = OperationRef(item["operation_id"], "role.life", self.context.scope,
                    VersionRef(item["operation_id"], 1), item["operation_id"])
                parameters = digest([self.owner, item["activity_id"], item["revision"], State.CANCELLED, None])
                require(operations.reserve(operation, parameter_hash=parameters, observed_at=NOW))
            decision_id = "life-decision-" + digest([self.context.key, self.request["command_id"]])
            evidence = {"request_hash": _hash(self.request),
                "operation_id": self.management.operation_id, "parameter_hash": self.data_hash,
                "scope_hash": digest(self.context.scope), "source": json.loads(encode(self.activity.work.source))}
            connection.execute("INSERT INTO runtime_metadata VALUES (?, ?)",
                               ("life.activity.deletion.v1:" + decision_id, encode(evidence)))
        decision = life_commands.decide(self.runtime, context=self.context, request=self.request, outcome="commit",
            observed_at=NOW, verify_prepared=self.commands.verify_prepared,
            authorize=lambda *_: Outcome.success(True), apply=reserve)
        self.commands.resolve(decision, verify_host_decision=lambda value: life_commands.verify_decision(self.runtime, value),
                              apply=self.deletions.apply)
        return decision

    def complete(self, decision):
        operations = OperationRepository(self.runtime)
        def finalize(_connection, _decision):
            for item in self.request["payload"].get("related", []):
                require(operations.transition(item["operation_id"], expected_state=OperationState.ACCEPTED,
                                             next_state=OperationState.COMPLETED, observed_at=NOW))
            require(operations.transition(self.management.operation_id, expected_state=OperationState.ACCEPTED,
                                         next_state=OperationState.COMPLETED, observed_at=NOW))
        life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.deletions.verify_commit,
                               finalize=finalize)
        return self.deletions.acknowledge("deletion", verify_host_completion=lambda value:
            life_commands.verify_completion(self.runtime, value)
            and operations.get(self.management.operation_id).unwrap().state is OperationState.COMPLETED)

    def rehearse(self, output=None):
        return rehearse(self.runtime.database_path, self.life_root / "good-life.sqlite3",
                        expected_version=self.version, destination=output)

    def test_deletion_survives_rollback_and_original_receipt_remains(self):
        decision = self.apply()
        with self.assertRaises(ActivityTransitionPending):
            StagedActivityRecords(self.life).dispatch("activity", owner_key=digest(self.owner), activity_id="book")
        with self.assertRaisesRegex(ValueError, "not yet resolved"):
            self.rehearse()
        self.assertTrue(self.complete(decision))
        self.assertFalse(self.complete(decision))
        output = self.root / "reconstructed.sqlite3"
        result = self.rehearse(output)
        self.assertEqual((result["activities"], result["revisions"], result["receipts"]), (0, 0, 1))
        with closing(sqlite3.connect(output)) as db:
            self.assertIsNone(db.execute("SELECT 1 FROM role_activities WHERE activity_id='book'").fetchone())
            self.assertIsNone(db.execute("SELECT 1 FROM role_artifacts WHERE activity_id='book'").fetchone())
            self.assertIsNotNone(db.execute("SELECT 1 FROM role_life_operations WHERE operation_id='reading-cancel'").fetchone())
        self.assertEqual(self.ledger.activity("book", owner=self.owner).revision, 2)

    def test_changed_deletion_evidence_or_management_receipt_blocks_rollback(self):
        decision = self.apply()
        self.complete(decision)
        with self.life.transaction() as db:
            raw = db.execute("SELECT result_json FROM life_prepared_commands WHERE command_id='deletion'").fetchone()[0]
            result = json.loads(raw)
            result["row_hashes"]["role_activity_revisions"].pop()
            db.execute("UPDATE life_prepared_commands SET result_json=? WHERE command_id='deletion'", (json.dumps(result),))
        with self.assertRaisesRegex(ValueError, "deletion evidence"):
            self.rehearse()
        with self.life.transaction() as db:
            db.execute("UPDATE life_prepared_commands SET result_json=? WHERE command_id='deletion'", (raw,))
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET parameter_hash='different' WHERE operation_id=?", (self.management.operation_id,))
        with self.assertRaisesRegex(ValueError, "deletion evidence"):
            self.rehearse()

    def test_prepared_deletion_rejects_changed_history(self):
        with self.life.transaction() as db:
            db.execute("UPDATE staged_role_activity_revisions SET provenance='altered' WHERE revision=1")
        with self.assertRaisesRegex(ValueError, "records changed"):
            self.deletions.prepare(self.request, prepared_at=NOW)

    def test_previous_single_deletion_format_still_recovers(self):
        del self.request["payload"]["related"]
        del self.request["payload"]["observed_at"]
        decision = self.apply()
        self.assertTrue(self.complete(decision))
        self.assertEqual(self.rehearse()["activities"], 0)

    def test_related_reading_added_after_proposal_blocks_deletion(self):
        related = RoleActivity("related", "role", self.context.scope, NOW, State.PLANNED, work=self.activity.work)
        raw, owner = encode(related), encode(self.owner)
        with self.life.transaction() as db:
            db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (digest(self.owner), "related", owner, 1, "planned", related.updated_at.isoformat(timespec="microseconds"), raw))
        with self.assertRaisesRegex(ValueError, "related reading set changed"):
            self.deletions.prepare(self.request, prepared_at=NOW)

    def test_planned_related_reading_and_deletion_share_one_commit(self):
        related = RoleActivity("related", "role", self.context.scope, NOW, State.PLANNED, work=self.activity.work)
        raw = encode(related)
        with self.life.transaction() as db:
            db.execute("INSERT INTO staged_role_activities VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (digest(self.owner), "related", encode(self.owner), 1, "planned", stamp(NOW), raw))
            db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES (?, ?, 1, ?, 'recorded', NULL, ?)""", (digest(self.owner), "related", stamp(NOW), raw))
        changed = advance_activity(related, expected_revision=1, state=State.CANCELLED, observed_at=NOW + timedelta(seconds=1))
        self.request["payload"]["related"] = [{"activity_id": "related", "revision": 1,
            "previous_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "operation_id": self.management.operation_id + ":" + digest("related"),
            "result_hash": hashlib.sha256(encode(changed).encode("utf-8")).hexdigest()}]
        decision = self.apply()
        with self.assertRaises(ActivityTransitionPending):
            StagedActivityRecords(self.life).dispatch("scan_activity_receipts", after=0, limit=100)
        self.assertTrue(self.complete(decision))
        record = StagedActivityRecords(self.life).dispatch("activity", owner_key=digest(self.owner), activity_id="related")
        self.assertEqual(json.loads(record)["state"], "cancelled")
        result = self.rehearse()
        self.assertEqual((result["activities"], result["receipts"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
