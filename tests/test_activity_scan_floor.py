from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from companion_v2.foundation.refs import ScopeKind, ScopeRef
from companion_v2.role_work.ledger import PersistentRoleLifeLedger
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.storage.activity_scan_floor import FLOORS, next_rowid
from companion_v2.storage.runtime import StorageRuntime
from good_life.activity_import import SCAN_FLOORS


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ActivityScanFloorTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = StorageRuntime.open(data_root=Path(temporary.name), installation_id="installation")
        self.addCleanup(self.runtime.close)
        self.ledger = PersistentRoleLifeLedger(self.runtime, now=lambda: NOW)
        self.scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")

    def activity(self, identifier):
        return RoleActivity(identifier, "role", self.scope, NOW, RoleActivityState.PLANNED)

    def floor(self, table, value):
        with self.runtime.transaction() as db:
            db.execute("INSERT OR REPLACE INTO runtime_metadata(key, value) VALUES (?, ?)", (FLOORS[table], str(value)))

    def scan(self, table):
        return tuple(row[0] for row in self.runtime.read_all(f"SELECT rowid FROM {table} ORDER BY rowid", ()))

    def test_default_behavior_and_persisted_floors_survive_deleted_maximum(self):
        self.assertEqual(FLOORS, SCAN_FLOORS)
        first = self.ledger.add_activity(self.activity("first"))
        self.assertEqual(self.scan("role_activity_revisions"), (1,))
        self.floor("role_activity_revisions", 50)
        self.floor("role_life_operations", 100)
        self.ledger.transition_activity("first", owner=first.owner, expected_revision=1,
            state=RoleActivityState.ACTIVE, operation_id="first-operation", observed_at=NOW + timedelta(seconds=1))
        self.assertEqual(self.scan("role_activity_revisions"), (1, 51))
        self.assertEqual(self.scan("role_life_operations"), (101,))
        self.ledger.delete_activity("first", owner=first.owner)
        with self.runtime.transaction() as db:
            db.execute("DELETE FROM role_life_operations WHERE operation_id='first-operation'")
        second = self.ledger.add_activity(self.activity("second"))
        self.ledger.transition_activity("second", owner=second.owner, expected_revision=1,
            state=RoleActivityState.ACTIVE, operation_id="second-operation", observed_at=NOW + timedelta(seconds=1))
        self.assertEqual(self.scan("role_activity_revisions"), (52, 53))
        self.assertEqual(self.scan("role_life_operations"), (102,))

    def test_failed_transaction_keeps_floor_and_invalid_floor_fails_closed(self):
        self.floor("role_activity_revisions", 50)
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.runtime.transaction() as db:
                self.assertEqual(next_rowid(db, "role_activity_revisions"), 51)
                raise RuntimeError("rollback")
        self.assertEqual(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                               (FLOORS["role_activity_revisions"],))["value"], "50")
        self.floor("role_activity_revisions", "not-a-number")
        with self.assertRaises(sqlite3.IntegrityError):
            self.ledger.add_activity(self.activity("blocked"))
        self.assertEqual(self.runtime.read_all("SELECT * FROM role_activities", ()), ())


if __name__ == "__main__":
    unittest.main()
