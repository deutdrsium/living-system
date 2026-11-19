from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
sys.path.insert(0, str(ROOT / "tools"))
from good_life.calendar_import import stage_calendar
from good_life import calendar_import
from good_life.calendar_records import StagedCalendarRecords
from verify_calendar_shadow import verify


class CalendarMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.sqlite3"
        self.target = self.root / "isolated-destination"
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.executescript("""
                CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO runtime_metadata VALUES ('schema_version', 'test-v1');
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY, owner TEXT, state TEXT, context TEXT, commit_state TEXT);
                CREATE TABLE calendar_records(owner_key TEXT, record_id TEXT, revision INTEGER, updated_at TEXT,
                    record_json TEXT, PRIMARY KEY(owner_key, record_id));
                CREATE TABLE calendar_history(owner_key TEXT, record_id TEXT, revision INTEGER,
                    operation_id TEXT REFERENCES operations(operation_id), record_json TEXT,
                    PRIMARY KEY(owner_key, record_id, revision),
                    FOREIGN KEY(owner_key, record_id) REFERENCES calendar_records(owner_key, record_id));
                CREATE TABLE calendar_receipts(operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    owner_key TEXT, record_id TEXT, result_json TEXT);
            """)
            db.execute("INSERT INTO operations VALUES (?, ?, ?, ?, ?)", ("operation", "calendar", "completed", "private", "committed"))
            db.execute("INSERT INTO calendar_records VALUES (?, ?, ?, ?, ?)",
                       ("owner", "event", 1, "2026-01-01T00:00:00+00:00", '{"private":"calendar content"}'))
            db.execute("INSERT INTO calendar_history VALUES (?, ?, ?, ?, ?)",
                       ("owner", "event", 1, "operation", '{"private":"calendar content"}'))
            db.execute("INSERT INTO calendar_receipts VALUES (?, ?, ?, ?)",
                       ("operation", "owner", "event", '{"revision":1}'))
            db.commit()

    def source_digest(self):
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def stage(self, **kwargs):
        return stage_calendar(self.source, expected_version="test-v1", **kwargs)

    def test_dry_run_does_not_modify_source_or_create_destination(self):
        before = self.source_digest()
        result = self.stage()
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(result["summary"]["calendar_records"]["rows"], 1)
        self.assertNotIn("calendar content", str(result))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.source_digest(), before)

    def test_staging_preserves_rows_and_exact_replay_is_idempotent(self):
        before = self.source_digest()
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")
        self.assertEqual(self.stage(destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            row = db.execute("SELECT owner_key, record_id, revision, record_json FROM staged_calendar_records").fetchone()
            self.assertEqual(row, ("owner", "event", 1, '{"private":"calendar content"}'))
            self.assertEqual(db.execute("SELECT operation_id FROM staged_calendar_history").fetchone()[0], "operation")
        self.assertEqual(self.source_digest(), before)

    def test_staged_calendar_reads_preserve_owner_versions_and_receipts(self):
        with closing(sqlite3.connect(self.source)) as db:
            payload = '{"kind":"role_plan","reminder_id":"reminder","private":"calendar content"}'
            db.execute("UPDATE calendar_records SET record_json=?", (payload,))
            db.execute("UPDATE calendar_history SET record_json=?", (payload,))
            db.commit()
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            records = StagedCalendarRecords(db)
            item = records.get(owner_key="owner", identifier="event")
            self.assertEqual(records.version(owner_key="owner", identifier="event", revision=1), item)
            self.assertEqual(records.receipt(owner_key="owner", operation_id="operation"), {"revision": 1})
            self.assertEqual(records.list(owner_key="owner", kinds=("role_plan",)), (item,))
            self.assertEqual(records.list(owner_key="owner", kinds=("commitment",)), ())
            self.assertEqual(records.by_reminder(owner_keys=("owner",), reminder_id="reminder"), (item,))
            self.assertIsNone(records.get(owner_key="other", identifier="event"))
            self.assertEqual(records.by_reminder(owner_keys=("other",), reminder_id="reminder"), ())
            with self.assertRaises(KeyError):
                records.receipt(owner_key="other", operation_id="operation")

    def test_shadow_queries_match_source_and_detect_changed_data(self):
        self.stage(destination=self.target)
        staged = self.target / "good-life.sqlite3"
        self.assertEqual(verify(self.source, staged, expected_version="test-v1"),
                         {"state": "matched", "owners": 1, "records": 1, "revisions": 1, "receipts": 1})
        with closing(sqlite3.connect(staged)) as db:
            db.execute("UPDATE staged_calendar_records SET record_json='{}'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "differs"):
            verify(self.source, staged, expected_version="test-v1")

    def test_changed_source_and_modified_destination_are_rejected(self):
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("UPDATE staged_calendar_records SET record_json = '{}' ")
            db.commit()
        with self.assertRaisesRegex(ValueError, "staged calendar changed"):
            self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("UPDATE staged_calendar_records SET record_json = ?", ('{"private":"calendar content"}',))
            db.commit()
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE operations SET context = 'changed'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target)

    def test_inconsistent_history_and_wrong_version_fail_before_creating_destination(self):
        with self.assertRaisesRegex(ValueError, "schema version"):
            stage_calendar(self.source, expected_version="wrong", destination=self.target)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE operations SET state = 'unknown'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_unknown_source_columns_fail_instead_of_silently_dropping_data(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("ALTER TABLE calendar_records ADD COLUMN extra_field TEXT")
            db.commit()
        with self.assertRaisesRegex(ValueError, "table shape"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_completed_operation_with_unknown_commit_state_is_rejected(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE operations SET commit_state='unknown'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_interrupted_import_rolls_back_all_staging_tables_and_can_retry(self):
        original = calendar_import._summaries
        def interrupt(connection, *, staged=False):
            if staged:
                raise RuntimeError("simulated interruption")
            return original(connection, staged=staged)
        with patch.object(calendar_import, "_summaries", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "interruption"):
                self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any(name.startswith("staged_calendar_") for name in names))
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")


if __name__ == "__main__":
    unittest.main()
