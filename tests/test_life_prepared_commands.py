from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_good_life"))
from good_life.prepared_commands import PreparedCommands
from good_life.storage import LifeStorage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class PreparedCommandTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = temporary.name
        self.storage = LifeStorage.open(self.directory)
        self.addCleanup(self.storage.close)
        self.commands = PreparedCommands(self.storage)
        self.commands.install()
        with self.storage.transaction() as db:
            db.execute("CREATE TABLE sample_activity (owner_key TEXT PRIMARY KEY, revision INTEGER)")
            db.execute("INSERT INTO sample_activity VALUES ('role-a', 1)")
        self.host_decisions = Path(self.directory) / "synthetic-host.sqlite3"
        with closing(sqlite3.connect(self.host_decisions)) as db:
            db.execute("CREATE TABLE decisions (decision_id TEXT PRIMARY KEY, payload TEXT NOT NULL, completed INTEGER NOT NULL DEFAULT 0)")
            db.commit()

    def prepare(self, command_id="cmd-1", owner="role-a", context="context-1", payload=None):
        return self.commands.prepare(command_id=command_id, owner_key=owner, context_key=context,
            kind="activity.transition", expected_revision=1, payload=payload or {"state": "completed"},
            prepared_at=NOW, validate=lambda view, request: view.one(
                "SELECT revision FROM sample_activity WHERE owner_key=?", (request["owner_key"],))[0] == 1)

    def decision(self, prepared, *, command_id="cmd-1", outcome="commit", decision_id="decision-1"):
        return {"command_id": command_id, "request_hash": prepared["request_hash"],
                "decision": outcome, "decision_id": decision_id}

    def verify(self, decision):
        with closing(sqlite3.connect(self.host_decisions)) as db:
            row = db.execute("SELECT payload FROM decisions WHERE decision_id=?", (decision["decision_id"],)).fetchone()
        return row is not None and json.loads(row[0]) == decision

    def save_decision(self, decision):
        with closing(sqlite3.connect(self.host_decisions)) as db:
            db.execute("INSERT INTO decisions(decision_id, payload) VALUES (?, ?)",
                       (decision["decision_id"], json.dumps(decision, sort_keys=True)))
            db.commit()

    def host_complete(self, evidence):
        with closing(sqlite3.connect(self.host_decisions)) as db:
            row = db.execute("SELECT payload, completed FROM decisions WHERE decision_id=?", (evidence["decision_id"],)).fetchone()
        if row is None or row[1] != 1:
            return False
        decision = json.loads(row[0])
        return all(decision[key] == value for key, value in evidence.items())

    def apply(self, db, request):
        changed = db.execute("UPDATE sample_activity SET revision=revision+1 WHERE owner_key=? AND revision=?",
                             (request["owner_key"], request["expected_revision"]))
        if changed.rowcount != 1:
            raise ValueError("activity revision changed")
        self.storage.record_event(event_id="event-1", owner_key=request["owner_key"],
            context_key=request["context_key"], kind="activity.completed", payload={"activity_id": "example"},
            occurred_at=NOW, consumers=("journal",))
        return {"revision": 2}

    def revision(self):
        return self.storage._connection.execute("SELECT revision FROM sample_activity WHERE owner_key='role-a'").fetchone()[0]

    def test_crash_after_decision_replays_local_effect_and_event_once(self):
        prepared = self.prepare()
        decision = self.decision(prepared)
        with self.assertRaisesRegex(ValueError, "unverified"):
            self.commands.resolve(decision, verify_host_decision=self.verify, apply=self.apply)
        self.assertEqual(self.revision(), 1)
        self.save_decision(decision)

        def interrupted(db, request):
            self.apply(db, request)
            raise RuntimeError("interrupted before commit")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.commands.resolve(decision, verify_host_decision=self.verify, apply=interrupted)
        self.assertEqual(self.revision(), 1)
        self.assertEqual(self.storage.pending_events("journal"), ())
        self.assertEqual(len(self.commands.pending()), 1)
        self.storage.close()
        self.storage = LifeStorage.open(self.directory)
        self.addCleanup(self.storage.close)
        self.commands = PreparedCommands(self.storage)
        self.commands.install()
        self.assertEqual(self.commands.resolve(decision, verify_host_decision=self.verify, apply=self.apply),
                         {"state": "committed", "result": {"revision": 2}})
        self.storage.close()
        self.storage = LifeStorage.open(self.directory)
        self.addCleanup(self.storage.close)
        self.commands = PreparedCommands(self.storage)
        self.commands.install()
        self.assertEqual(self.commands.resolve(decision, verify_host_decision=self.verify, apply=self.apply),
                         {"state": "committed", "result": {"revision": 2}})
        self.assertEqual(self.revision(), 2)
        self.assertEqual(len(self.storage.pending_events("journal")), 1)
        self.assertEqual(self.commands.pending()[0]["state"], "committed")
        with self.assertRaisesRegex(ValueError, "awaits a decision"):
            self.prepare(command_id="other")
        with self.assertRaisesRegex(ValueError, "completion is unavailable"):
            self.commands.acknowledge("cmd-1", verify_host_completion=self.host_complete)
        with closing(sqlite3.connect(self.host_decisions)) as db:
            db.execute("UPDATE decisions SET completed=1 WHERE decision_id='decision-1'")
            db.commit()
        self.assertTrue(self.commands.acknowledge("cmd-1", verify_host_completion=self.host_complete))
        self.assertFalse(self.commands.acknowledge("cmd-1", verify_host_completion=self.host_complete))
        self.assertEqual(self.commands.resolve(decision, verify_host_decision=self.verify, apply=self.apply),
                         {"state": "acknowledged", "result": {"revision": 2}})
        self.assertEqual(self.commands.pending(), ())

    def test_role_lock_conflicts_and_abort_survive_restart(self):
        prepared = self.prepare()
        self.assertEqual(self.prepare(), prepared)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.prepare(payload={"state": "paused"})
        with self.assertRaisesRegex(ValueError, "awaits a decision"):
            self.prepare(command_id="other", context="other-session")
        self.storage.close()
        self.storage = LifeStorage.open(self.directory)
        self.addCleanup(self.storage.close)
        self.commands = PreparedCommands(self.storage)
        self.commands.install()
        self.assertEqual(self.commands.pending()[0]["command_id"], "cmd-1")
        decision = self.decision(prepared, outcome="abort")
        self.save_decision(decision)
        self.assertEqual(self.commands.resolve(decision, verify_host_decision=self.verify, apply=self.apply),
                         {"state": "aborted", "result": None})
        self.assertEqual(self.revision(), 1)
        self.assertEqual(self.prepare(command_id="other")["state"], "prepared")
        contrary = self.decision(prepared, outcome="commit", decision_id="another-decision")
        self.save_decision(contrary)
        with self.assertRaisesRegex(ValueError, "already resolved"):
            self.commands.resolve(contrary, verify_host_decision=self.verify, apply=self.apply)

    def test_concurrent_commands_for_one_role_cannot_both_prepare(self):
        barrier = threading.Barrier(2)
        def worker(identifier):
            barrier.wait(timeout=5)
            try:
                return self.prepare(command_id=identifier)["state"]
            except ValueError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(worker, ("one", "two")))
        self.assertCountEqual(outcomes, ["prepared", "blocked"])
        self.assertEqual(len(self.commands.pending()), 1)

    def test_modified_request_and_missing_lock_index_are_rejected(self):
        prepared = self.prepare()
        decision = self.decision(prepared)
        self.save_decision(decision)
        with self.storage.transaction() as db:
            db.execute("UPDATE life_prepared_commands SET payload_json='{}' WHERE command_id='cmd-1'")
        with self.assertRaisesRegex(ValueError, "content changed"):
            self.commands.resolve(decision, verify_host_decision=self.verify, apply=self.apply)
        with self.storage.transaction() as db:
            db.execute("DROP INDEX life_one_prepared_command_per_role")
        with self.assertRaisesRegex(ValueError, "schema is incomplete"):
            self.commands.install()

    def test_prepare_validator_cannot_publish_a_write(self):
        def invalid(view, request):
            view.one("UPDATE sample_activity SET revision=2 RETURNING revision")
            return True
        with self.assertRaisesRegex(ValueError, "only read"):
            self.commands.prepare(command_id="cmd", owner_key="role-a", context_key="context-1",
                kind="activity.transition", expected_revision=1, payload={}, prepared_at=NOW, validate=invalid)
        self.assertEqual(self.revision(), 1)
        self.assertEqual(self.commands.pending(), ())

    def test_pending_recovery_pages_do_not_starve_other_roles(self):
        with self.storage.transaction() as db:
            db.executemany("INSERT INTO sample_activity VALUES (?, 1)", [("role-b",), ("role-c",)])
        self.prepare(command_id="command-a", owner="role-a")
        self.prepare(command_id="command-b", owner="role-b")
        self.prepare(command_id="command-c", owner="role-c")
        first = self.commands.pending(limit=2)
        cursor = (first[-1]["prepared_at"], first[-1]["command_id"])
        second = self.commands.pending(limit=2, after=cursor)
        self.assertEqual([item["command_id"] for item in (*first, *second)],
                         ["command-a", "command-b", "command-c"])


if __name__ == "__main__":
    unittest.main()
