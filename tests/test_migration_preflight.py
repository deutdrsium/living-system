import hashlib
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from life_migration_preflight import LIFE_TABLES, inspect_database


class MigrationPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT)")
            connection.execute("INSERT INTO runtime_metadata VALUES ('schema_version', '72')")
            for name in LIFE_TABLES:
                connection.execute(f'CREATE TABLE "{name}" (id TEXT PRIMARY KEY, content TEXT)')
            connection.execute("INSERT INTO role_activities VALUES (?, ?)", ("private-id", "private-content"))
            connection.commit()

    def digest(self):
        return hashlib.sha256(self.path.read_bytes()).hexdigest()

    def test_inventory_is_read_only_and_contains_no_record_content(self):
        before = self.digest()
        result = inspect_database(self.path, expected_version="72")
        self.assertEqual(result["tables"]["role_activities"]["rows"], 1)
        self.assertFalse(result["migration_ready"])
        self.assertNotIn("private-content", str(result))
        self.assertNotIn("private-id", str(result))
        self.assertEqual(self.digest(), before)

    def test_mismatched_version_fails_without_changing_database(self):
        before = self.digest()
        with self.assertRaisesRegex(ValueError, "schema version"):
            inspect_database(self.path, expected_version="71")
        self.assertEqual(self.digest(), before)

    def test_missing_table_fails_closed(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("DROP TABLE calendar_history")
            connection.commit()
        before = self.digest()
        with self.assertRaisesRegex(ValueError, "calendar_history"):
            inspect_database(self.path)
        self.assertEqual(self.digest(), before)

    def test_foreign_key_violations_fail_without_exposing_row_values(self):
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("CREATE TABLE dependent (id TEXT REFERENCES calendar_records(id))")
            connection.execute("INSERT INTO dependent VALUES ('private-orphan')")
            connection.commit()
        before = self.digest()
        with self.assertRaisesRegex(ValueError, "foreign key violations") as caught:
            inspect_database(self.path)
        self.assertNotIn("private-orphan", str(caught.exception))
        self.assertEqual(self.digest(), before)


if __name__ == "__main__":
    unittest.main()
