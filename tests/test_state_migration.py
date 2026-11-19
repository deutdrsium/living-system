from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.state_import import stage_state, stage_state_configuration
from good_life.configuration_records import StagedConfigurationRecords
from good_life.storage import LifeStorage


class StateMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "runtime.sqlite3"
        self.config = self.root / "config.sqlite3"
        self.target = self.root / "life"
        with closing(sqlite3.connect(self.source)) as db:
            db.executescript("""
                CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT);
                INSERT INTO runtime_metadata VALUES('schema_version','test-v1');
                INSERT INTO runtime_metadata VALUES('role.state.dialogue-cursor.v1:test','{"through":"t"}');
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY, owner TEXT, state TEXT, commit_state TEXT);
                INSERT INTO operations VALUES('op','role_state','completed','committed');
                CREATE TABLE role_state_records(owner_key TEXT,record_id TEXT,revision INTEGER,updated_at TEXT,record_json TEXT);
                CREATE TABLE role_state_history(owner_key TEXT,record_id TEXT,revision INTEGER,operation_id TEXT,record_json TEXT);
                CREATE TABLE role_state_receipts(operation_id TEXT,owner_key TEXT,record_id TEXT,result_json TEXT);
                CREATE TABLE role_state_schedules(context_key TEXT,context_json TEXT,settings_json TEXT,next_at TEXT,
                    revision INTEGER,enabled INTEGER,result_json TEXT);
                INSERT INTO role_state_records VALUES('role','current',1,'time','{"values":{"hunger":"fine"}}');
                INSERT INTO role_state_history VALUES('role','current',1,'op','{"values":{"hunger":"fine"}}');
                INSERT INTO role_state_receipts VALUES('op','role','current','{"revision":1}');
                INSERT INTO role_state_schedules VALUES('ctx','{}','{}','time',1,1,NULL);
            """)
            db.commit()
        with closing(sqlite3.connect(self.config)) as db:
            db.executescript("""
                CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY,value TEXT);
                INSERT INTO runtime_metadata VALUES('schema_version','config-v1');
                CREATE TABLE configuration_targets(target_key TEXT,target_json TEXT,revision INTEGER,updated_at TEXT);
                CREATE TABLE configuration_values(target_key TEXT,field_id TEXT,value_json TEXT,revision INTEGER);
                CREATE TABLE configuration_changes(sequence INTEGER,operation_id TEXT,group_id TEXT);
                CREATE TABLE configuration_applications(target_key TEXT,consumer_id TEXT,group_id TEXT);
                CREATE TABLE configuration_consumers(consumer_id TEXT);
                CREATE TABLE operations(operation_id TEXT PRIMARY KEY,owner TEXT,state TEXT,commit_state TEXT);
                INSERT INTO configuration_targets VALUES('target','{}',1,'time');
                INSERT INTO configuration_values VALUES('target','role.state.hunger_enabled','true',1);
                INSERT INTO configuration_values VALUES('target','other.secret','"not copied"',1);
                INSERT INTO operations VALUES('cfg','role.state.settings','completed','committed');
                INSERT INTO configuration_changes VALUES(1,'cfg','role.state');
            """)
            db.commit()

    def test_rehearsal_preserves_state_and_configuration_without_copying_audit(self):
        dry = stage_state(self.source, expected_version="test-v1")
        self.assertEqual(dry["summary"]["role_state_history"]["rows"], 1)
        self.assertNotIn("fine", str(dry))
        self.assertFalse(self.target.exists())
        self.assertEqual(stage_state(self.source, expected_version="test-v1", destination=self.target)["state"], "staged")
        self.assertEqual(stage_state_configuration(self.config, expected_version="config-v1", destination=self.target)["state"], "staged")
        self.assertEqual(stage_state(self.source, expected_version="test-v1", destination=self.target)["state"], "unchanged")
        self.assertEqual(stage_state_configuration(self.config, expected_version="config-v1", destination=self.target)["state"], "unchanged")
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT record_json FROM staged_role_state_records").fetchone()[0],
                             '{"values":{"hunger":"fine"}}')
            self.assertEqual(db.execute("SELECT context_key,next_at FROM staged_role_state_schedules").fetchone(), ("ctx", "time"))
            self.assertEqual(db.execute("SELECT field_id FROM staged_state_configuration_values").fetchall(),
                             [("role.state.hunger_enabled",)])
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='operations'").fetchone())
        storage = LifeStorage.open(self.target)
        self.addCleanup(storage.close)
        result = StagedConfigurationRecords(storage).dispatch("target", domain="state", target_key="target",
                                                               fields=["role.state.hunger_enabled"])
        self.assertEqual((result["revision"], result["values"][0]["value_json"]), (1, "true"))

    def test_changed_data_incomplete_audit_and_unknown_metadata_stop_migration(self):
        stage_state(self.source, expected_version="test-v1", destination=self.target)
        with closing(sqlite3.connect(self.target / "good-life.sqlite3")) as db:
            db.execute("UPDATE staged_role_state_records SET record_json='{}'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "staged state changed"):
            stage_state(self.source, expected_version="test-v1", destination=self.target)
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE operations SET state='unknown'")
            db.commit()
        with self.assertRaisesRegex(ValueError, "audit is inconsistent"):
            stage_state(self.source, expected_version="test-v1")
        with closing(sqlite3.connect(self.source)) as db:
            db.execute("UPDATE operations SET state='completed'")
            db.execute("INSERT INTO runtime_metadata VALUES('role.state.new:unknown','{}')")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unclassified"):
            stage_state(self.source, expected_version="test-v1")


if __name__ == "__main__":
    unittest.main()
