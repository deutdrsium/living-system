from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
sys.path.insert(0, str(ROOT / "tools"))
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life import activity_import
from verify_activity_shadow import verify


class ActivityMigrationTests(unittest.TestCase):
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
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY, owner TEXT, state TEXT, commit_state TEXT);
                CREATE TABLE role_activities(owner_key TEXT, activity_id TEXT, owner_json TEXT, revision INTEGER,
                    state TEXT, updated_at TEXT, record_json TEXT, PRIMARY KEY(owner_key, activity_id));
                CREATE TABLE role_activity_revisions(owner_key TEXT, activity_id TEXT, revision INTEGER,
                    updated_at TEXT, provenance TEXT, operation_id TEXT REFERENCES operations(operation_id),
                    record_json TEXT, PRIMARY KEY(owner_key, activity_id, revision),
                    FOREIGN KEY(owner_key, activity_id) REFERENCES role_activities(owner_key, activity_id));
                CREATE TABLE role_artifacts(owner_key TEXT, artifact_id TEXT, activity_id TEXT, scope_key TEXT,
                    record_json TEXT, PRIMARY KEY(owner_key, artifact_id),
                    FOREIGN KEY(owner_key, activity_id) REFERENCES role_activities(owner_key, activity_id));
                CREATE TABLE role_life_operations(operation_id TEXT PRIMARY KEY REFERENCES operations(operation_id),
                    owner_key TEXT, result_json TEXT);
            """)
            for activity, kind, scan_id in (("book", "reading", 10), ("other", "creative", 15)):
                payload = json.dumps({"activity_id": activity, "revision": 1,
                                      "work": {"kind": kind}, "private": "not-output"})
                operation = "operation-" + activity
                db.execute("INSERT INTO operations VALUES (?, 'role.life', 'completed', 'committed')", (operation,))
                db.execute("INSERT INTO role_activities VALUES ('owner', ?, '{}', 1, 'completed', 'time', ?)",
                           (activity, payload))
                db.execute("INSERT INTO role_activity_revisions VALUES ('owner', ?, 1, 'time', 'recorded', ?, ?)",
                           (activity, operation, payload))
                db.execute("INSERT INTO role_artifacts VALUES ('owner', ?, ?, 'scope', '{}')", ("artifact-" + activity, activity))
                db.execute("INSERT INTO role_life_operations(rowid, operation_id, owner_key, result_json) VALUES (?, ?, 'owner', ?)",
                           (scan_id, operation, payload))
            db.execute("INSERT INTO runtime_metadata VALUES (?, ?)",
                       ("memory.activity-ingestion.v1:context", '{"activity":20,"journal":5}'))
            db.commit()

    def stage(self, **kwargs):
        return stage_activities(self.source, expected_version="test-v1", **kwargs)

    def digest(self):
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def test_dry_run_is_read_only_and_includes_generic_activity_records(self):
        before = self.digest()
        result = self.stage()
        self.assertEqual(result["state"], "dry_run")
        self.assertEqual(result["summary"]["role_activities"]["rows"], 2)
        self.assertEqual(result["summary"]["role_artifacts"]["rows"], 2)
        self.assertEqual(result["summary"]["host_cursors"]["scan_ceiling"], 20)
        self.assertNotIn("not-output", str(result))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.digest(), before)

    def test_staging_preserves_rowids_and_seeds_above_all_host_cursors(self):
        before = self.digest()
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")
        self.assertEqual(self.stage(destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT scan_id, operation_id FROM staged_role_life_operations").fetchall(),
                             [(10, "operation-book"), (15, "operation-other")])
            self.assertEqual(db.execute("SELECT activity_id FROM staged_role_activities ORDER BY activity_id").fetchall(),
                             [("book",), ("other",)])
            db.execute("BEGIN")
            row = db.execute("INSERT INTO staged_role_life_operations(operation_id, owner_key, result_json) VALUES ('new', 'owner', '{}')")
            self.assertGreater(row.lastrowid, 20)
            db.rollback()
        self.assertEqual(self.digest(), before)

    def test_staged_read_queries_keep_owner_revision_and_cursor_boundaries(self):
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            records = StagedActivityRecords(db)
            original = records.activity(owner_key="owner", activity_id="book")
            self.assertEqual(records.activity_revision(owner_key="owner", activity_id="book", revision=1), original)
            self.assertEqual(records.activity_revision_evidence(owner_key="owner", activity_id="book", revision=1),
                             {"provenance": "recorded", "operation_id": "operation-book"})
            self.assertEqual(records.activity_receipt(owner_key="owner", operation_id="operation-book"), original)
            self.assertIsNone(records.activity_receipt(owner_key="other", operation_id="operation-book"))
            with self.assertRaises(KeyError):
                records.activity(owner_key="other", activity_id="book")
            self.assertEqual(records.scan_activity_receipts(after=9, limit=2)[0]["scan_id"], 10)
            self.assertEqual(records.scan_activity_receipts(after=10, limit=2)[0]["scan_id"], 15)
            self.assertEqual(records.scan_activity_receipts(after=15, limit=2), ())
            self.assertEqual(records.recent_activity_receipts(owner_key="owner", limit=2),
                             ({"operation_id": "operation-other"}, {"operation_id": "operation-book"}))
            page = records.activity_revisions(owner_key="owner", after="a", at="z", limit=2)
            self.assertEqual([(row["activity_id"], row["revision"]) for row in page], [("other", 1), ("book", 1)])

    def test_revision_scan_ids_are_preserved_and_not_reused_after_deletion(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE role_activity_revisions SET rowid=12 WHERE activity_id='book'")
            db.execute("UPDATE role_activity_revisions SET rowid=90 WHERE activity_id='other'")
            db.commit()
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT scan_id, activity_id FROM staged_role_activity_revisions ORDER BY scan_id").fetchall(),
                             [(12, "book"), (90, "other")])
            db.execute("BEGIN")
            db.execute("DELETE FROM staged_role_activity_revisions WHERE scan_id=90")
            inserted = db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES ('owner', 'other', 1, 'time', 'recorded', NULL, '{}')""")
            self.assertGreater(inserted.lastrowid, 90)
            db.rollback()
            db.execute("UPDATE sqlite_sequence SET seq=900 WHERE name='staged_role_activity_revisions'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "revision sequence changed"):
            self.stage(destination=self.target)

    def test_restored_scan_floors_survive_a_later_migration(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.executemany("INSERT INTO runtime_metadata VALUES (?, ?)", [
                (activity_import.SCAN_FLOORS["role_life_operations"], "500"),
                (activity_import.SCAN_FLOORS["role_activity_revisions"], "700")])
            db.commit()
        result = self.stage(destination=self.target)
        self.assertEqual(result["summary"]["host_cursors"]["scan_ceiling"], 500)
        self.assertEqual(result["summary"]["host_cursors"]["revision_ceiling"], 700)
        self.assertEqual(self.stage(destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("BEGIN")
            receipt = db.execute("INSERT INTO staged_role_life_operations(operation_id, owner_key, result_json) VALUES ('new', 'owner', '{}')")
            revision = db.execute("""INSERT INTO staged_role_activity_revisions
                (owner_key, activity_id, revision, updated_at, provenance, operation_id, record_json)
                VALUES ('owner', 'book', 2, 'time', 'recorded', NULL, '{}')""")
            self.assertGreater(receipt.lastrowid, 500)
            self.assertGreater(revision.lastrowid, 700)
            db.rollback()
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE runtime_metadata SET value='501' WHERE key=?",
                       (activity_import.SCAN_FLOORS["role_life_operations"],))
            db.commit()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target)

    def test_invalid_or_exhausted_restored_floor_stops_before_import(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'invalid')",
                       (activity_import.SCAN_FLOORS["role_activity_revisions"],))
            db.commit()
        with self.assertRaisesRegex(ValueError, "scan floor is invalid"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE runtime_metadata SET value='9223372036854775807' WHERE key=?",
                       (activity_import.SCAN_FLOORS["role_activity_revisions"],))
            db.commit()
        with self.assertRaisesRegex(ValueError, "revision sequence has no remaining"):
            self.stage(destination=self.target)

    def test_full_revision_sequence_and_old_staging_version_are_rejected(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE role_activity_revisions SET rowid=9223372036854775807 WHERE activity_id='book'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "revision sequence has no remaining"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE role_activity_revisions SET rowid=10 WHERE activity_id='book'")
            db.commit()
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("UPDATE staged_activity_manifest SET migration_id='activities.v1'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target)

    def test_shadow_queries_match_source_and_detect_changed_data(self):
        self.stage(destination=self.target)
        staged = self.target / "good-life.sqlite3"
        self.assertEqual(verify(self.source, staged, expected_version="test-v1"),
                         {"state": "matched", "roles": 1, "activities": 2, "revisions": 2, "receipts": 2})
        with closing(sqlite3.connect(staged)) as db:
            db.execute("UPDATE staged_role_activities SET record_json='{}'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "differs"):
            verify(self.source, staged, expected_version="test-v1")

    def test_cursor_change_and_destination_edits_fail_closed(self):
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("UPDATE staged_role_activities SET state='paused'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "staged activity data changed"):
            self.stage(destination=self.target)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE runtime_metadata SET value=? WHERE key LIKE 'memory.activity-ingestion.v1:%'",
                       ('{"activity":21,"journal":5}',))
            db.commit()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target)

    def test_invalid_cursor_or_unconfirmed_operation_never_creates_destination(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE runtime_metadata SET value='{}' WHERE key LIKE 'memory.activity-ingestion.v1:%'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "cursor"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE runtime_metadata SET value=? WHERE key LIKE 'memory.activity-ingestion.v1:%'",
                       ('{"activity":20,"journal":5}',))
            db.execute("UPDATE operations SET state='unknown' WHERE operation_id='operation-book'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_unclassified_activity_and_its_history_are_preserved(self):
        with closing(sqlite3.connect(self.source)) as db:
            raw = json.dumps({"activity_id": "unclassified", "revision": 1, "work": None})
            db.execute("INSERT INTO role_activities VALUES ('owner', 'unclassified', '{}', 1, 'planned', 'time', ?)", (raw,))
            db.execute("INSERT INTO role_activity_revisions VALUES ('owner', 'unclassified', 1, 'time', 'legacy_head', NULL, ?)", (raw,))
            db.commit()
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM staged_role_activities WHERE activity_id='unclassified'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM staged_role_activity_revisions WHERE activity_id='unclassified'").fetchone()[0], 1)

    def test_deleted_activities_keep_receipts_without_restoring_activity(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("DELETE FROM role_artifacts WHERE activity_id='book'")
            db.execute("DELETE FROM role_activity_revisions WHERE activity_id='book'")
            db.execute("DELETE FROM role_activities WHERE activity_id='book'")
            db.commit()
        result = self.stage(destination=self.target)
        self.assertEqual(result["summary"]["detached_receipts"]["rows"], 1)
        self.assertEqual(result["summary"]["role_life_operations"]["rows"], 2)
        self.assertEqual(self.stage(destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            records = StagedActivityRecords(db)
            with self.assertRaises(KeyError):
                records.activity(owner_key="owner", activity_id="book")
            self.assertIn('"activity_id": "book"', records.activity_receipt(owner_key="owner", operation_id="operation-book"))
            self.assertEqual([item["scan_id"] for item in records.scan_activity_receipts(after=0, limit=10)], [10, 15])
        self.assertEqual(verify(self.source, self.target / "good-life.sqlite3", expected_version="test-v1")["receipts"], 2)

    def test_recreated_activity_does_not_discard_old_receipt(self):
        with closing(sqlite3.connect(self.source)) as db:
            replacement = json.dumps({"activity_id": "book", "revision": 1, "work": {"kind": "reading"}, "new": True})
            db.execute("INSERT INTO operations VALUES ('replacement', 'role.life', 'completed', 'committed')")
            db.execute("UPDATE role_activities SET record_json=? WHERE activity_id='book'", (replacement,))
            db.execute("UPDATE role_activity_revisions SET operation_id='replacement', record_json=? WHERE activity_id='book'", (replacement,))
            db.commit()
        result = self.stage(destination=self.target)
        self.assertEqual(result["summary"]["detached_receipts"]["rows"], 1)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            records = StagedActivityRecords(db)
            self.assertTrue(json.loads(records.activity(owner_key="owner", activity_id="book"))["new"])
            self.assertNotIn("new", json.loads(records.activity_receipt(owner_key="owner", operation_id="operation-book")))

    def test_malformed_detached_receipt_is_rejected(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("DELETE FROM role_artifacts WHERE activity_id='book'")
            db.execute("DELETE FROM role_activity_revisions WHERE activity_id='book'")
            db.execute("DELETE FROM role_activities WHERE activity_id='book'")
            db.execute("UPDATE role_life_operations SET result_json='{}' WHERE operation_id='operation-book'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_completed_operation_with_unknown_commit_state_is_rejected(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE operations SET commit_state='unknown' WHERE operation_id='operation-book'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_interrupted_import_rolls_back_and_can_retry(self):
        with patch.object(activity_import, "_staged_rows", side_effect=RuntimeError("interrupted")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertFalse(any(name.startswith("staged_role_") for name in names))
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")


if __name__ == "__main__":
    unittest.main()
