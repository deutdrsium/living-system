from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.exposure_import import stage_exposures


class ExposureMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.sqlite3"
        self.target = self.root / "isolated-destination"
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript("""
                CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO runtime_metadata VALUES ('schema_version', 'test-v1');
                CREATE TABLE role_activities(owner_key TEXT, activity_id TEXT, record_json TEXT,
                    PRIMARY KEY(owner_key, activity_id));
                CREATE TABLE role_exposure_sessions(scope_key TEXT, activity_id TEXT, platform TEXT,
                    selected_id TEXT, selection_made INTEGER, PRIMARY KEY(scope_key, activity_id));
                CREATE TABLE role_exposures(scope_key TEXT, activity_id TEXT, platform TEXT, content_id TEXT,
                    title TEXT, summary TEXT, source_url TEXT, fetched_at TEXT, exposed_at TEXT,
                    attempted_at TEXT, coverage TEXT, random_attempt INTEGER,
                    PRIMARY KEY(scope_key, activity_id, platform, content_id));
                CREATE TABLE role_interests(scope_key TEXT, interest_id TEXT);
                CREATE TABLE role_interest_coefficients(scope_key TEXT, interest_id TEXT);
                CREATE TABLE role_exposure_topics(scope_key TEXT, activity_id TEXT, platform TEXT, content_id TEXT, interest_id TEXT);
                CREATE TABLE role_exposure_feedback(scope_key TEXT, activity_id TEXT, platform TEXT, content_id TEXT);
            """)
            db.execute("INSERT INTO role_activities VALUES ('owner', 'social', ?)",
                       (json.dumps({"work": {"kind": "social"}}),))
            db.execute("INSERT INTO role_exposure_sessions(rowid, scope_key, activity_id, platform, selected_id, selection_made) VALUES (9, 'scope', 'social', 'zhihu', '', 0)")
            for platform in ("zhihu", "bilibili"):
                db.execute("INSERT INTO role_exposures VALUES ('scope', 'social', ?, 'item', 'private title', 'private summary', '', 'time', NULL, NULL, '', 0)", (platform,))
            db.execute("INSERT INTO role_interests VALUES ('scope', 'interest')")
            db.execute("INSERT INTO role_exposure_topics VALUES ('scope', 'social', 'zhihu', 'item', 'interest')")
            db.execute("INSERT INTO role_exposure_feedback VALUES ('scope', 'social', 'zhihu', 'item')")
            db.commit()

    def stage(self, **kwargs):
        kwargs.setdefault("validate_source", lambda db: {"state": "matched", "sessions": db.execute(
            "SELECT COUNT(*) FROM role_exposure_sessions").fetchone()[0], "sha256": "fixture"})
        return stage_exposures(self.source, expected_version="test-v1", **kwargs)

    def test_two_platforms_share_session_and_contents_are_not_reported(self):
        result = self.stage()
        self.assertEqual(result["summary"]["role_exposures"]["rows"], 2)
        self.assertNotIn("private title", str(result))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")
        self.assertEqual(self.stage(destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT scan_id FROM staged_role_exposure_sessions").fetchone()[0], 9)
            db.execute("BEGIN")
            inserted = db.execute("INSERT INTO staged_role_exposure_sessions(scope_key, activity_id, platform, selected_id, selection_made) VALUES ('scope', 'next', 'zhihu', '', 0)")
            self.assertGreater(inserted.lastrowid, 9)
            db.rollback()

    def test_changed_host_interest_link_blocks_replay(self):
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE role_exposure_topics SET interest_id='different'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target)

    def test_provenance_preflight_shares_snapshot_and_detects_drift(self):
        def validate(connection):
            self.assertTrue(connection.in_transaction)
            count = connection.execute("SELECT COUNT(*) FROM role_exposure_sessions").fetchone()[0]
            return {"state": "matched", "sessions": count, "sha256": "first"}
        result = self.stage(destination=self.target, validate_source=validate)
        self.assertEqual(result["summary"]["context_provenance"]["sessions"], 1)
        self.assertEqual(self.stage(destination=self.target, validate_source=validate)["state"], "unchanged")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target, validate_source=lambda _: {"state": "matched", "sha256": "changed"})

    def test_failed_provenance_never_creates_destination(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.stage(destination=self.target, validate_source=lambda _: {"state": "unmatched"})
        self.assertFalse(self.target.exists())

    def test_old_staging_manifest_is_not_silently_overwritten(self):
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("UPDATE staged_exposure_manifest SET migration_id='exposures.v1'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.stage(destination=self.target)

    def test_ambiguous_activity_or_missing_session_fails_before_import(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("INSERT INTO role_activities VALUES ('other-owner', 'social', ?)",
                       (json.dumps({"work": {"kind": "social"}}),))
            db.commit()
        with self.assertRaisesRegex(ValueError, "matched"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("DELETE FROM role_activities WHERE owner_key='other-owner'")
            db.execute("DELETE FROM role_exposure_sessions")
            db.commit()
        with self.assertRaisesRegex(ValueError, "matched"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()
