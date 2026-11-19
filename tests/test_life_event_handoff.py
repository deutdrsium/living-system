from datetime import datetime, timezone
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

from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import RoleOwnerRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.integration.life_events import consume_once
from companion_v2.storage.runtime import StorageRuntime
from good_life.storage import LifeStorage


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class LifeEventHandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.life = LifeStorage.open(root / "life")
        self.addCleanup(self.life.close)
        self.host_root = root / "host"
        self.runtime = StorageRuntime.open(data_root=self.host_root, installation_id="installation")
        self.addCleanup(self.runtime.close)
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot",
                         "account", "conversation", "session")
        role = SubjectRef("installation", SubjectKind.BOT, "role")
        self.context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=role), key="original-context")
        self.owner = digest(RoleOwnerRef("installation", "bot", "role"))
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES ('test-effect', '0')")

    def publish(self, identifier):
        with self.life.transaction():
            self.life.record_event(event_id=identifier, owner_key=self.owner, context_key=self.context.key,
                kind="activity.completed", payload={"activity": identifier}, occurred_at=NOW,
                consumers=["journal", "interest"])
        return next(event for event in self.life.pending_events("journal") if event["event_id"] == identifier)

    @staticmethod
    def authorize(_context, _at):
        return Outcome.success(True)

    @staticmethod
    def effect(connection, _event):
        connection.execute("UPDATE runtime_metadata SET value = CAST(value AS INTEGER) + 1 WHERE key = 'test-effect'")

    def receive(self, event, **changes):
        return consume_once(self.runtime, **{"context": self.context, "event": event, "consumer": "journal",
            "observed_at": NOW, "authorize": self.authorize, "apply": self.effect, **changes})

    def count(self):
        return int(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key = ?", ("test-effect",))["value"])

    def test_replay_after_host_commit_has_one_effect_and_each_consumer_acknowledges_independently(self):
        event = self.publish("event-1")
        self.assertTrue(self.receive(event))
        self.runtime.close()
        self.runtime = StorageRuntime.open(data_root=self.host_root, installation_id="installation")
        self.addCleanup(self.runtime.close)
        self.assertFalse(self.receive(self.life.pending_events("journal")[0]))
        self.assertEqual(self.count(), 1)
        changed = {**event, "payload": {"activity": "different"}}
        payload = json.dumps(changed["payload"], ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False)
        identity = json.dumps([changed["event_id"], changed["owner_key"], changed["context_key"],
            changed["kind"], payload, changed["occurred_at"], changed["consumers"]],
            ensure_ascii=False, separators=(",", ":"))
        changed["content_hash"] = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        with self.assertRaises(TaskError):
            self.receive(changed)
        self.assertEqual(self.count(), 1)
        with self.life.transaction():
            self.life.acknowledge(event_id="event-1", consumer="journal", acknowledged_at=NOW)
        self.assertEqual(self.life.pending_events("journal"), ())
        self.assertEqual(len(self.life.pending_events("interest")), 1)

    def test_invalid_context_tampered_content_and_denied_authorization_do_not_run_effect(self):
        event = self.publish("event-2")
        with self.assertRaises(TaskError):
            self.receive({**event, "context_key": "different"})
        with self.assertRaises(TaskError):
            self.receive({**event, "payload": {"activity": "different"}})
        with self.assertRaises(TaskError):
            self.receive(event, authorize=lambda *_: Outcome.failed("denied", "denied"))
        with self.assertRaises(TaskError):
            self.receive(event, authorize=lambda *_: Outcome.success(False))
        self.assertEqual(self.count(), 0)

    def test_failed_local_effect_rolls_back_receipt_and_can_be_retried(self):
        event = self.publish("event-3")
        def fail(connection, value):
            self.effect(connection, value)
            raise RuntimeError("failed")
        with self.assertRaises(RuntimeError):
            self.receive(event, apply=fail)
        self.assertEqual(self.count(), 0)
        def failed_outcome(connection, value):
            self.effect(connection, value)
            return Outcome.failed("denied", "denied")
        with self.assertRaisesRegex(ValueError, "must return None"):
            self.receive(event, apply=failed_outcome)
        self.assertEqual(self.count(), 0)
        self.assertTrue(self.receive(event))
        self.assertEqual(self.count(), 1)


if __name__ == "__main__":
    unittest.main()
