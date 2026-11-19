import asyncio
from contextlib import contextmanager
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
from companion_v2.storage.runtime import StorageRuntime


class CoordinatedStorageSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = StorageRuntime.open(data_root=Path(temporary.name) / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)

    async def test_factory_is_task_local_and_wraps_only_synchronous_database_scopes(self):
        events, ready, release = [], asyncio.Event(), asyncio.Event()
        @contextmanager
        def snapshot():
            events.append("enter")
            try:
                yield
            finally:
                events.append("exit")

        async def journal_task():
            with self.runtime.coordinated_snapshot(snapshot):
                ready.set()
                await release.wait()
                self.assertEqual(events, [])
                with self.runtime.transaction() as db:
                    db.execute("INSERT INTO runtime_metadata VALUES ('coordinated-test', 'ok')")
                    with self.runtime.read_snapshot():
                        self.assertEqual(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                                               ("coordinated-test",))["value"], "ok")
                self.assertEqual(events, ["enter", "enter", "exit", "exit"])

        task = asyncio.create_task(journal_task())
        await ready.wait()
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES ('unrelated-test', 'ok')")
        self.assertEqual(events, [])
        release.set()
        await task
        with self.runtime.read_snapshot():
            self.assertEqual(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?",
                                                  ("coordinated-test",))["value"], "ok")
        self.assertEqual(events, ["enter", "enter", "exit", "exit"])

    async def test_factory_failure_starts_no_host_transaction_and_resets_context(self):
        @contextmanager
        def unavailable():
            raise ValueError("provider unavailable")
            yield
        with self.assertRaisesRegex(ValueError, "provider unavailable"):
            with self.runtime.coordinated_snapshot(unavailable):
                with self.runtime.transaction():
                    self.fail("host transaction must not start")
        self.assertIsNone(self.runtime._write_owner_thread_id)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES ('after-failure', 'ok')")

    async def test_host_only_accounting_can_finish_after_provider_disappears(self):
        @contextmanager
        def unavailable():
            raise ValueError("provider unavailable")
            yield
        with self.runtime.coordinated_snapshot(unavailable):
            with self.runtime.without_coordinated_snapshot(), self.runtime.transaction() as db:
                db.execute("INSERT INTO runtime_metadata VALUES ('accounted', 'ok')")
            with self.assertRaisesRegex(ValueError, "provider unavailable"):
                with self.runtime.read_snapshot():
                    self.fail("normal validation still needs the provider")
        self.assertEqual(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='accounted'", ())["value"], "ok")


if __name__ == "__main__":
    unittest.main()
