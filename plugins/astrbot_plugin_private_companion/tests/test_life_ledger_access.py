from datetime import datetime, timedelta, timezone
import sqlite3
import unittest

from companion_v2.execution.models import digest, stamp
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.role_work.ledger import PersistentRoleLifeLedger


class Runtime:
    installation_id = "installation"

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE role_life_operations (operation_id TEXT, owner_key TEXT, result_json TEXT)")
        self.db.execute("""CREATE TABLE role_activity_revisions
            (owner_key TEXT, activity_id TEXT, revision INTEGER, updated_at TEXT,
             provenance TEXT, operation_id TEXT, record_json TEXT)""")

    def read_one(self, query, params):
        return self.db.execute(query, params).fetchone()

    def read_all(self, query, params):
        return self.db.execute(query, params).fetchall()


class LifeLedgerAccessTests(unittest.TestCase):
    def setUp(self):
        self.runtime = Runtime()
        self.addCleanup(self.runtime.db.close)
        self.ledger = PersistentRoleLifeLedger(self.runtime)
        self.owner = RoleOwnerRef("installation", "bot", "one")
        self.other = RoleOwnerRef("installation", "bot", "two")
        self.at = datetime(2026, 1, 2, tzinfo=timezone.utc)
        self.key = digest(self.owner)
        self.runtime.db.execute("INSERT INTO role_life_operations VALUES (?, ?, ?)",
                                ("operation", self.key, "original"))
        for owner, activity, revision, offset in [
            (self.owner, "a", 1, 1), (self.owner, "a", 2, 2),
            (self.owner, "b", 1, 2), (self.other, "c", 1, 2),
        ]:
            self.runtime.db.execute("INSERT INTO role_activity_revisions VALUES (?, ?, ?, ?, ?, ?, ?)",
                (digest(owner), activity, revision, stamp(self.at - timedelta(minutes=offset)),
                 "recorded", "operation", "{}"))

    def test_receipt_and_revision_evidence_are_owner_scoped(self):
        self.assertEqual(self.ledger.activity_receipt("operation", owner=self.owner), "original")
        self.assertIsNone(self.ledger.activity_receipt("operation", owner=self.other))
        self.assertEqual(self.ledger.activity_revision_evidence("a", 1, owner=self.owner),
                         {"provenance": "recorded", "operation_id": "operation"})
        self.assertIsNone(self.ledger.activity_revision_evidence("a", 1, owner=self.other))

    def test_revision_pagination_preserves_order_and_role_scope(self):
        params = {"owner": self.owner, "after": self.at - timedelta(hours=1), "at": self.at, "limit": 2}
        first = self.ledger.activity_revisions(**params)
        self.assertEqual([(r["activity_id"], r["revision"]) for r in first], [("a", 1), ("b", 1)])
        last = first[-1]
        cursor = (last["updated_at"], last["activity_id"], last["revision"])
        second = self.ledger.activity_revisions(**params, before=cursor)
        self.assertEqual([(r["activity_id"], r["revision"]) for r in second], [("a", 2)])

    def test_receipt_scan_preserves_global_cursor_and_recent_owner_filter(self):
        self.runtime.db.execute("INSERT INTO role_life_operations VALUES (?, ?, ?)",
                                ("other-operation", digest(self.other), "other"))
        self.runtime.db.execute("INSERT INTO role_life_operations VALUES (?, ?, ?)",
                                ("new-operation", self.key, "new"))
        page = self.ledger.scan_activity_receipts(after=1, limit=2)
        self.assertEqual([(r["scan_id"], r["operation_id"]) for r in page],
                         [(2, "other-operation"), (3, "new-operation")])
        recent = self.ledger.recent_activity_receipts(owner=self.owner, limit=2)
        self.assertEqual([r["operation_id"] for r in recent], ["new-operation", "operation"])
        with self.assertRaises(ValueError):
            self.ledger.scan_activity_receipts(after=-1, limit=2)


if __name__ == "__main__":
    unittest.main()
