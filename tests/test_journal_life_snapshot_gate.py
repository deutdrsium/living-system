from datetime import timedelta
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.storage import LifeStorage
from tests.test_role_life_runtime import LifeFixture


class JournalSnapshotGateTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_missing_joint_snapshot_preserves_dream_and_diary_attempts(self):
        await self.setup_life()
        routine = self.manager.life
        sleep = routine.start_sleep("test-sleep", 1)
        self.clock.advance(timedelta(minutes=2))
        completed = routine.wake(sleep.activity_id, expected_revision=sleep.revision, operation_id="test-wake")
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "life"
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        records = StagedActivityRecords(life)
        provider = SimpleNamespace(dispatch=lambda _handler, method, **params: records.dispatch(method, **params))
        self.app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, provider))
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))

        dream = await routine.dream(completed)
        diary = await routine.diary()
        for result in (dream, diary):
            self.assertFalse(result.is_success)
            self.assertEqual(result.failure.code, "life_journal_snapshot_unavailable")
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM operations WHERE owner='role.journal.trigger'", ())["n"], 0)
        self.assertEqual(self.called, [])

        # A verified rollback can still run the original first attempt.
        with self.runtime.transaction() as db:
            db.execute("UPDATE runtime_metadata SET value='legacy' WHERE key=?", (LIFE_ACTIVITY_OWNER_KEY,))
        restored = await routine.dream(completed)
        self.assertTrue(restored.is_success, restored)
        self.assertEqual(restored.unwrap().state, "written")
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM role_journals WHERE kind='dream'", ())["n"], 1)
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
