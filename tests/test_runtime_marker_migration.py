from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.metadata_import import stage_metadata


class RuntimeMarkerMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source.sqlite3"
        self.target = self.root / "isolated-destination"
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.executemany("INSERT INTO runtime_metadata VALUES (?, ?)", [
                ("schema_version", "test-v1"), ("role.reading.lazy:book", "1"),
                ("role.reading.choice:slot", '{"private":"choice"}'),
                ("role.life.retry_interrupted:task", '{"activity_id":"paused","revision":3}'),
                ("role.sleep.pressure-correction.v1:role", '{"private":"sleep"}'),
                ("role.life.rest_gate.v1:role", '{"private":"host"}'),
            ])
            db.commit()

    def stage(self, **kwargs):
        return stage_metadata(self.source, expected_version="test-v1", **kwargs)

    def digest(self):
        return hashlib.sha256(self.source.read_bytes()).hexdigest()

    def test_dry_run_is_read_only_and_reports_no_private_values(self):
        before = self.digest()
        result = self.stage()
        self.assertEqual(result["summary"]["life_markers"]["rows"], 4)
        self.assertEqual(result["summary"]["all_related_markers"]["rows"], 5)
        self.assertNotIn("paused", str(result))
        self.assertFalse(self.target.exists())
        self.assertEqual(self.digest(), before)

    def test_retry_and_reading_markers_are_preserved_without_host_chat_settings(self):
        self.assertEqual(self.stage(destination=self.target)["state"], "staged")
        self.assertEqual(self.stage(destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            rows = dict(db.execute("SELECT key, value FROM staged_life_runtime_metadata"))
        self.assertEqual(rows["role.life.retry_interrupted:task"], '{"activity_id":"paused","revision":3}')
        self.assertNotIn("role.life.rest_gate.v1:role", rows)

    def test_unclassified_or_invalid_marker_fails_before_import(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("INSERT INTO runtime_metadata VALUES ('role.reading.future:key', '1')")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unclassified"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("DELETE FROM runtime_metadata WHERE key='role.reading.future:key'")
            db.execute("UPDATE runtime_metadata SET value='invalid' WHERE key='role.reading.lazy:book'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.stage(destination=self.target)
        self.assertFalse(self.target.exists())

    def test_source_or_destination_drift_stops_repeat(self):
        self.stage(destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("UPDATE staged_life_runtime_metadata SET value='2' WHERE key='role.reading.lazy:book'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "staged runtime markers changed"):
            self.stage(destination=self.target)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE runtime_metadata SET value='2' WHERE key='role.reading.lazy:book'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "runtime markers changed"):
            self.stage(destination=self.target)


if __name__ == "__main__":
    unittest.main()
