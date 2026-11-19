from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY, READ_METHODS
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.storage import LifeStorage
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


class LifeLedgerRouterTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def set_up_router(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        await self.start(host)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        activity = RoleActivity("activity", owner.persona_id, context.scope, NOW, RoleActivityState.ACTIVE)
        app.role_life.add_activity(activity)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        path = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=path)
        life = LifeStorage.open(path)
        self.addCleanup(life.close)
        records = StagedActivityRecords(life)
        registry = SimpleNamespace(dispatch=lambda handler, method, **params: records.dispatch(method, **params))
        return app, owner, activity, LifeLedgerReadProxy(self.runtime, registry)

    def select_life(self):
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata(key, value) VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))

    async def test_shared_application_and_journal_read_same_owned_activity(self):
        app, owner, activity, reader = await self.set_up_router()
        self.assertIs(app.role_journal.life, app.role_life)
        legacy_read = app.role_life.activity
        self.assertEqual(legacy_read("activity", owner=owner), activity)
        app.role_life.bind_reader(reader)
        self.select_life()
        with self.assertRaises(TaskError) as stale:
            legacy_read("activity", owner=owner)
        self.assertEqual(stale.exception.code, "life_activity_owner_changed")
        self.assertEqual(app.role_life.activity("activity", owner=owner), activity)
        self.assertEqual(app.role_journal.life.activity("activity", owner=owner), activity)
        with self.assertRaises(TaskError) as write:
            app.role_life.add_activity(activity)
        self.assertEqual(write.exception.code, "life_activity_write_unavailable")
        with self.runtime.transaction():
            with self.assertRaises(TaskError) as transaction:
                app.role_life.activity("activity", owner=owner)
            self.assertEqual(transaction.exception.code, "life_activity_transaction_active")
            with self.assertRaises(TaskError):
                app.role_life.unbind_reader(reader)
        with self.runtime.read_snapshot():
            with self.assertRaises(TaskError) as snapshot:
                app.role_life.activity("activity", owner=owner)
            self.assertEqual(snapshot.exception.code, "life_activity_transaction_active")
        old_read = app.role_life.activity
        self.assertTrue(app.role_life.unbind_reader(reader))
        with self.assertRaises(TaskError):
            old_read("activity", owner=owner)
        with self.assertRaises(TaskError):
            app.role_life.activity("activity", owner=owner)
        app.role_life.bind_reader(reader)
        with self.assertRaises(TaskError):
            old_read("activity", owner=owner)
        self.assertEqual(app.role_life.activity("activity", owner=owner), activity)

    async def test_unbind_waits_for_an_inflight_read(self):
        app, owner, activity, _ = await self.set_up_router()
        entered, release, unbind_started = Event(), Event(), Event()
        def read(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("read did not finish")
            return activity
        handlers = {name: (read if name == "activity" else lambda *_a, **_kw: None) for name in READ_METHODS}
        reader = SimpleNamespace(**handlers)
        app.role_life.bind_reader(reader)
        self.select_life()
        with ThreadPoolExecutor(max_workers=2) as executor:
            self._test_executor = executor  # Keep thread-owned SQLite readers reachable until runtime cleanup.
            reading = executor.submit(app.role_life.activity, "activity", owner=owner)
            self.assertTrue(entered.wait(5))
            def unbind():
                unbind_started.set()
                return app.role_life.unbind_reader(reader)
            unbinding = executor.submit(unbind)
            self.assertTrue(unbind_started.wait(5))
            self.assertFalse(unbinding.done())
            release.set()
            self.assertEqual(reading.result(timeout=5), activity)
            self.assertTrue(unbinding.result(timeout=5))


if __name__ == "__main__":
    unittest.main()
