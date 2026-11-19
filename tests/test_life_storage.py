from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_good_life"))
from good_life.storage import LifeStorage
from good_life.calendar_records import StagedCalendarRecords


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class LifeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)

    def record(self, **changes):
        return self.storage.record_event(**{
            "event_id": "event-1", "owner_key": "role-1", "context_key": "context-1", "kind": "activity.completed",
            "payload": {"activity_id": "activity-1"}, "occurred_at": NOW,
            "consumers": ("journal", "interest"), **changes,
        })

    def test_event_and_activity_rollback_together(self):
        with self.storage.transaction() as connection:
            connection.execute("CREATE TABLE activity_test (id TEXT)")
        with self.assertRaisesRegex(RuntimeError, "interrupt"):
            with self.storage.transaction() as connection:
                connection.execute("INSERT INTO activity_test VALUES ('activity-1')")
                self.record()
                raise RuntimeError("interrupt")
        self.assertEqual(self.storage.pending_events("journal"), ())
        self.assertEqual(self.storage._connection.execute("SELECT COUNT(*) FROM activity_test").fetchone()[0], 0)
        with self.assertRaisesRegex(ValueError, "transaction"):
            self.record()

    def test_duplicate_identity_is_idempotent_but_changed_content_is_rejected(self):
        with self.storage.transaction():
            first = self.record()
            self.assertEqual(self.record(consumers=("interest", "journal")), first)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            with self.storage.transaction():
                self.record(payload={"activity_id": "different"})
        self.assertEqual(len(self.storage.pending_events("journal")), 1)

    def test_acknowledgements_are_independent_and_survive_reopen(self):
        with self.storage.transaction():
            self.record()
            self.assertTrue(self.storage.acknowledge(event_id="event-1", consumer="journal", acknowledged_at=NOW))
            self.assertTrue(self.storage.acknowledge(event_id="event-1", consumer="journal", acknowledged_at=NOW))
            self.assertFalse(self.storage.acknowledge(event_id="event-1", consumer="other", acknowledged_at=NOW))
        self.storage.close()
        self.storage = LifeStorage.open(self.temp.name)
        self.addCleanup(self.storage.close)
        self.assertEqual(self.storage.pending_events("journal"), ())
        self.assertEqual(self.storage.pending_events("interest")[0]["event_id"], "event-1")

    def test_worker_threads_serialize_transactions_and_reads(self):
        with self.storage.transaction() as connection:
            connection.execute("CREATE TABLE staged_calendar_records (owner_key TEXT, record_id TEXT, record_json TEXT)")
            connection.execute("INSERT INTO staged_calendar_records VALUES (?, ?, ?)",
                               ("role-1", "calendar-1", '{"id":"calendar-1"}'))
        records = StagedCalendarRecords(self.storage)

        def worker(index):
            with self.storage.transaction():
                self.record(event_id=f"event-{index}")
            return records.get(owner_key="role-1", identifier="calendar-1")

        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(worker, range(40)))
        self.assertTrue(all(result == {"id": "calendar-1"} for result in results))
        events = self.storage.pending_events("journal")
        self.assertEqual({event["event_id"] for event in events}, {f"event-{index}" for index in range(40)})

    def test_other_thread_cannot_write_in_an_active_transaction(self):
        with ThreadPoolExecutor(max_workers=1) as executor:
            with self.storage.transaction():
                self.record()
                record = executor.submit(self.record, event_id="foreign")
                acknowledge = executor.submit(self.storage.acknowledge, event_id="event-1",
                                              consumer="journal", acknowledged_at=NOW)
                with self.assertRaisesRegex(ValueError, "transaction"):
                    record.result(timeout=5)
                with self.assertRaisesRegex(ValueError, "transaction"):
                    acknowledge.result(timeout=5)
        self.assertEqual([event["event_id"] for event in self.storage.pending_events("journal")], ["event-1"])

    def test_existing_foreign_database_is_not_modified(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "good-life.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE unrelated (x TEXT)")
            connection.commit()
            connection.close()
            before = hashlib.sha256(path.read_bytes()).digest()
            with self.assertRaisesRegex(ValueError, "cannot open"):
                LifeStorage.open(directory)
            self.assertEqual(hashlib.sha256(path.read_bytes()).digest(), before)


if __name__ == "__main__":
    unittest.main()
