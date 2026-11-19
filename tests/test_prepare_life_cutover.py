from contextlib import closing
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
import prepare_life_cutover
import prepare_life_rollback
from tests.test_role_state import StateFixture, state_data


class PrepareLifeCutoverTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def test_interrupted_attempts_remain_for_host_recovery(self):
        with TemporaryDirectory() as temporary:
            host = Path(temporary) / "runtime.sqlite3"
            with closing(sqlite3.connect(host)) as db:
                db.execute("CREATE TABLE execution_tasks(task_id TEXT,kind TEXT,state TEXT,attempt_id TEXT,generation INTEGER)")
                db.execute("CREATE TABLE execution_attempts(attempt_id TEXT,task_id TEXT,generation INTEGER,record_json TEXT)")
                db.execute("CREATE TABLE runtime_metadata(key TEXT PRIMARY KEY, value TEXT)")
                db.execute("INSERT INTO execution_tasks VALUES('p','proactive.compose','running',NULL,1)")
                db.commit()
            self.assertEqual(prepare_life_cutover._check_quiesced(host), {"proactive.compose": 1})
            with closing(sqlite3.connect(host)) as db:
                db.execute("INSERT INTO execution_tasks VALUES('r','role.reading.consume','running','a',1)")
                db.commit()
            with self.assertRaisesRegex(ValueError, "life tasks need recovery evidence"):
                prepare_life_cutover._check_quiesced(host)
            with closing(sqlite3.connect(host)) as db:
                db.execute("INSERT INTO execution_attempts VALUES('a','r',1,'{\"started_at\":\"2026-01-01\"}')")
                db.commit()
            self.assertEqual(prepare_life_cutover._check_quiesced(host)["role.reading.consume"], 1)

    async def test_paired_candidate_keeps_backups_and_switches_only_copies(self):
        _, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True)
        app.role_state.write(context, data=state_data(energy=42), expected_revision=0, operation_key="synthetic-state")
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            host, config = root / "host.sqlite3", root / "config.sqlite3"
            for target in (host, config):
                with closing(sqlite3.connect(self.runtime.database_path)) as source, closing(sqlite3.connect(target)) as backup:
                    source.backup(backup)
            result = prepare_life_cutover.prepare(host, config, runtime_version=version, configuration_version=version,
                destination=root / "candidate", source_commit="a" * 40)
            self.assertEqual(result["state"], "candidate")
            with closing(sqlite3.connect(host)) as original, closing(sqlite3.connect(root / "candidate" / "companion-v2.sqlite3")) as candidate:
                for key in prepare_life_cutover.OWNER_KEYS:
                    self.assertIsNone(original.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone())
                    self.assertEqual(candidate.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()[0], "good_life")
                self.assertIsNotNone(candidate.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                                       (prepare_life_cutover.CERTIFICATE_KEY,)).fetchone())
            with closing(sqlite3.connect(root / "candidate" / "life" / "good-life.sqlite3")) as life:
                self.assertEqual(life.execute("SELECT value FROM life_metadata WHERE key=?",
                                              (prepare_life_cutover.READY_KEY,)).fetchone()[0], "ready")
                self.assertEqual(life.execute("SELECT COUNT(*) FROM staged_role_state_records").fetchone()[0], 1)
            with closing(sqlite3.connect(config)) as original, closing(sqlite3.connect(
                    root / "candidate" / "configuration" / "companion-v2.sqlite3")) as candidate:
                self.assertIsNone(original.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                                   (prepare_life_cutover.CONFIG_OWNER_KEY,)).fetchone())
                self.assertEqual(candidate.execute("SELECT value FROM runtime_metadata WHERE key=?",
                    (prepare_life_cutover.CONFIG_OWNER_KEY,)).fetchone()[0], "good_life")
            self.assertEqual(len(result["configuration_sha256"]), 64)
            rollback = prepare_life_rollback.prepare(root / "candidate" / "companion-v2.sqlite3",
                root / "candidate" / "configuration" / "companion-v2.sqlite3",
                root / "candidate" / "life" / "good-life.sqlite3", runtime_version=version,
                configuration_version=version, destination=root / "rollback", legacy_commit="b" * 40)
            self.assertEqual(rollback["state"], "candidate")
            with closing(sqlite3.connect(root / "rollback" / "companion-v2.sqlite3")) as restored:
                for key in prepare_life_cutover.OWNER_KEYS:
                    self.assertEqual(restored.execute("SELECT value FROM runtime_metadata WHERE key=?", (key,)).fetchone()[0], "legacy")
            with closing(sqlite3.connect(root / "rollback" / "configuration" / "companion-v2.sqlite3")) as restored:
                self.assertEqual(restored.execute("SELECT value FROM runtime_metadata WHERE key=?",
                    (prepare_life_cutover.CONFIG_OWNER_KEY,)).fetchone()[0], "legacy")


if __name__ == "__main__":
    unittest.main()
