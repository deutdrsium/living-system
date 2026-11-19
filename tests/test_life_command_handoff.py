from datetime import datetime, timezone
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.integration import life_commands
from companion_v2.storage.runtime import StorageRuntime
from good_life.prepared_commands import PreparedCommands
from good_life.storage import LifeStorage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class LifeCommandHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot",
                         "account", "conversation", "session")
        role = SubjectRef("installation", SubjectKind.BOT, "role")
        self.context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=role), key="context-1")
        self.request = {"command_id": "command-1", "owner_key": digest(RoleOwnerRef("installation", "bot", "role")),
                        "context_key": self.context.key, "kind": "activity.transition", "expected_revision": 1,
                        "payload": {"state": "completed"}}
        with self.life.transaction() as connection:
            connection.execute("CREATE TABLE activity (revision INTEGER NOT NULL)")
            connection.execute("INSERT INTO activity VALUES (1)")
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES ('test-life-command-effect', '0')")

    def prepare(self):
        return self.commands.prepare(**self.request, prepared_at=NOW,
            validate=lambda view, request: view.one("SELECT revision FROM activity")[0] == request["expected_revision"])

    def verify_prepared(self, request):
        self.assertFalse(self.runtime._connection.in_transaction)
        return self.commands.verify_prepared(request)

    def verify_decision(self, decision):
        self.assertFalse(self.life._connection.in_transaction)
        return life_commands.verify_decision(self.runtime, decision)

    def verify_life_commit(self, decision):
        self.assertFalse(self.runtime._connection.in_transaction)
        return self.commands.verify_commit(decision)

    def verify_host_completion(self, evidence):
        self.assertFalse(self.life._connection.in_transaction)
        return life_commands.verify_completion(self.runtime, evidence)

    @staticmethod
    def authorize(_context, _at):
        return Outcome.success(True)

    @staticmethod
    def host_effect(connection, _request):
        connection.execute("UPDATE runtime_metadata SET value = CAST(value AS INTEGER) + 1 WHERE key='test-life-command-effect'")

    def life_effect(self, connection, request):
        changed = connection.execute("UPDATE activity SET revision=revision+1 WHERE revision=?", (request["expected_revision"],))
        if changed.rowcount != 1:
            raise ValueError("activity revision changed")
        self.life.record_event(event_id="event-1", owner_key=request["owner_key"], context_key=request["context_key"],
            kind="activity.completed", payload={"activity_id": "example"}, occurred_at=NOW, consumers=("journal",))
        return {"revision": 2}

    def decide(self, **changes):
        return life_commands.decide(self.runtime, **{"context": self.context, "request": self.request,
            "outcome": "commit", "observed_at": NOW, "verify_prepared": self.verify_prepared,
            "authorize": self.authorize, "apply": self.host_effect, **changes})

    def host_count(self):
        return int(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                         ("test-life-command-effect",))["value"])

    def reopen(self):
        self.life.close()
        self.runtime.close()
        self.life = LifeStorage.open(self.root / "life")
        self.addCleanup(self.life.close)
        self.commands = PreparedCommands(self.life)
        self.commands.install()
        self.runtime = StorageRuntime.open(data_root=self.root / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)

    def test_two_database_handoff_recovers_each_commit_boundary(self):
        self.prepare()
        decision = self.decide()
        self.assertEqual(self.host_count(), 1)
        with self.assertRaises(TaskError):
            life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.verify_life_commit)
        self.reopen()
        pending = self.commands.pending()[0]
        recovered = life_commands.lookup(self.runtime, context_key=pending["context_key"],
                                         command_id=pending["command_id"], request_hash=pending["request_hash"])
        self.assertEqual(recovered, decision)
        self.assertIsNone(life_commands.lookup(self.runtime, context_key="other-context",
                                              command_id=pending["command_id"], request_hash=pending["request_hash"]))
        result = self.commands.resolve(recovered, verify_host_decision=self.verify_decision, apply=self.life_effect)
        self.assertEqual(result, {"state": "committed", "result": {"revision": 2}})
        self.reopen()
        self.assertEqual(self.decide(), decision)
        self.assertEqual(self.host_count(), 1)
        self.assertEqual(self.commands.resolve(decision, verify_host_decision=self.verify_decision, apply=self.life_effect), result)
        self.assertTrue(life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.verify_life_commit))
        self.assertFalse(life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.verify_life_commit))
        self.reopen()
        self.assertTrue(self.commands.acknowledge("command-1", verify_host_completion=self.verify_host_completion))
        self.assertEqual(self.commands.pending(), ())
        self.assertEqual(len(self.life.pending_events("journal")), 1)
        self.assertEqual(self.life._connection.execute("SELECT revision FROM activity").fetchone()[0], 2)

    def test_revoked_authority_can_abort_without_a_partial_effect(self):
        self.prepare()
        with self.assertRaises(TaskError):
            self.decide(authorize=lambda *_: Outcome.failed("denied", "denied"))
        with self.assertRaises(TaskError):
            self.decide(authorize=lambda *_: Outcome.success(False))
        self.assertEqual(self.host_count(), 0)
        self.assertEqual(self.commands.pending()[0]["state"], "prepared")
        decision = self.decide(outcome="abort")
        result = self.commands.resolve(decision, verify_host_decision=self.verify_decision, apply=self.life_effect)
        self.assertEqual(result, {"state": "aborted", "result": None})
        self.assertEqual(self.host_count(), 0)
        self.assertEqual(self.commands.pending(), ())
        self.assertEqual(self.life.pending_events("journal"), ())

    def test_host_effect_and_decision_rollback_together(self):
        self.prepare()
        def interrupted(connection, request):
            self.host_effect(connection, request)
            raise RuntimeError("interrupted")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.decide(apply=interrupted)
        self.assertEqual(self.host_count(), 0)
        self.assertEqual(self.commands.pending()[0]["state"], "prepared")
        def failed_outcome(connection, request):
            self.host_effect(connection, request)
            return Outcome.failed("denied", "denied")
        with self.assertRaisesRegex(ValueError, "must return None"):
            self.decide(apply=failed_outcome)
        self.assertEqual(self.host_count(), 0)
        decision = self.decide()
        self.assertTrue(life_commands.verify_decision(self.runtime, decision))
        self.assertEqual(self.host_count(), 1)

    def test_foreign_context_and_unprepared_commands_cannot_decide(self):
        with self.assertRaises(TaskError):
            self.decide()
        self.prepare()
        other = SimpleNamespace(scope=self.context.scope, role_binding=self.context.role_binding, key="other-context")
        with self.assertRaises(TaskError):
            self.decide(context=other)
        self.assertEqual(self.host_count(), 0)

    def test_decision_fails_closed_when_host_audit_is_missing_or_changed(self):
        self.prepare()
        decision = self.decide()
        self.assertTrue(self.verify_decision(decision))
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE operations SET parameter_hash='changed' WHERE owner='life.command'")
        self.assertFalse(self.verify_decision(decision))
        with self.assertRaisesRegex(ValueError, "unverified"):
            self.commands.resolve(decision, verify_host_decision=self.verify_decision, apply=self.life_effect)
        self.assertEqual(self.life._connection.execute("SELECT revision FROM activity").fetchone()[0], 1)

    def test_changed_life_request_cannot_be_decided_or_confirmed(self):
        self.prepare()
        with self.life.transaction() as connection:
            connection.execute("UPDATE life_prepared_commands SET payload_json='{}'")
        with self.assertRaises(TaskError):
            self.decide()
        self.assertEqual(self.host_count(), 0)

    def test_changed_committed_request_cannot_complete_host_receipt(self):
        self.prepare()
        decision = self.decide()
        self.commands.resolve(decision, verify_host_decision=self.verify_decision, apply=self.life_effect)
        with self.life.transaction() as connection:
            connection.execute("UPDATE life_prepared_commands SET payload_json='{}'")
        with self.assertRaises(TaskError):
            life_commands.complete(self.runtime, decision, observed_at=NOW, verify_life_commit=self.verify_life_commit)
        self.assertEqual(self.commands.pending()[0]["state"], "committed")

    def test_finalize_and_after_ack_effects_roll_back_and_replay_once(self):
        self.prepare()
        decision = self.decide()
        self.commands.resolve(decision, verify_host_decision=self.verify_decision, apply=self.life_effect)
        def fail_finalize(connection, value):
            self.host_effect(connection, value)
            raise RuntimeError("host interrupted")
        with self.assertRaisesRegex(RuntimeError, "host interrupted"):
            life_commands.complete(self.runtime, decision, observed_at=NOW,
                                   verify_life_commit=self.verify_life_commit, finalize=fail_finalize)
        self.assertEqual(self.host_count(), 1)
        self.assertTrue(life_commands.complete(self.runtime, decision, observed_at=NOW,
                                               verify_life_commit=self.verify_life_commit, finalize=self.host_effect))
        self.assertEqual(self.host_count(), 2)

        def publish(_connection, request, _result):
            self.life.record_event(event_id="event-2", owner_key=request["owner_key"], context_key=request["context_key"],
                kind="activity.completed", payload={"activity_id": "another"}, occurred_at=NOW, consumers=("journal",))
        def fail_ack(connection, request, result):
            publish(connection, request, result)
            raise RuntimeError("life interrupted")
        with self.assertRaisesRegex(RuntimeError, "life interrupted"):
            self.commands.acknowledge("command-1", verify_host_completion=self.verify_host_completion, after_ack=fail_ack)
        self.assertEqual(len(self.life.pending_events("journal")), 1)
        self.assertEqual(self.commands.pending()[0]["state"], "committed")
        def false_ack(connection, request, result):
            publish(connection, request, result)
            return False
        with self.assertRaisesRegex(ValueError, "must return None"):
            self.commands.acknowledge("command-1", verify_host_completion=self.verify_host_completion, after_ack=false_ack)
        self.assertEqual(len(self.life.pending_events("journal")), 1)
        self.assertTrue(self.commands.acknowledge("command-1", verify_host_completion=self.verify_host_completion, after_ack=publish))
        self.assertFalse(self.commands.acknowledge("command-1", verify_host_completion=self.verify_host_completion, after_ack=publish))
        self.assertEqual(len(self.life.pending_events("journal")), 2)


if __name__ == "__main__":
    unittest.main()
