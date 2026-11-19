from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.storage import LifeStorage
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


class LifeJointSnapshotTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_snapshot(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _manager = self.setup_state()
        await self.start(host)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        activity = RoleActivity("activity", owner.persona_id, context.scope, NOW, RoleActivityState.ACTIVE)
        app.role_life.add_activity(activity)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        directory = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        records = StagedActivityRecords(life)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        registry = LifeProviderRegistry(SimpleNamespace(get_all_stars=lambda: [star]), owner=object(), host_active=lambda: True)
        self.addCleanup(registry.close)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"], handlers["activity_history"] = records.dispatch, records.snapshot
        token = registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, registry.lease()))
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        return app, owner, activity, life, registry, plugin, handlers, token

    async def test_joint_snapshot_pins_life_and_keeps_host_transaction_boundaries(self):
        app, owner, activity, life, *_ = await self.setup_snapshot()
        with self.runtime.read_snapshot():
            with self.assertRaises(TaskError) as outside:
                app.role_life.activity("activity", owner=owner)
            self.assertEqual(outside.exception.code, "life_activity_transaction_active")
        started, finished = Event(), Event()
        errors = []
        def write():
            try:
                started.set()
                with life.transaction() as db:
                    db.execute("INSERT INTO life_metadata VALUES ('test-write', 'done')")
                finished.set()
            except Exception as error:
                errors.append(error)
        worker = Thread(target=write)
        try:
            with app.role_life.consistent_snapshot():
                with self.runtime.read_snapshot():
                    with app.role_life.consistent_snapshot():
                        self.assertEqual(app.role_life.activity("activity", owner=owner), activity)
                    with self.assertRaisesRegex(ValueError, "active transaction or snapshot"):
                        life.close()
                    worker.start()
                    self.assertTrue(started.wait(5))
                    self.assertFalse(finished.wait(0.1))
                    self.assertEqual(app.role_life.activity("activity", owner=owner), activity)
        finally:
            if worker.ident is not None:
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(finished.is_set())
        self.assertEqual(errors, [])
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            with app.role_life.consistent_snapshot():
                raise RuntimeError("interrupted")
        with life.transaction() as db:
            self.assertEqual(db.execute("SELECT value FROM life_metadata WHERE key='test-write'").fetchone()[0], "done")

    async def test_revocation_blocks_reads_and_replacement_until_snapshot_finishes(self):
        app, owner, _activity, _life, registry, plugin, handlers, token = await self.setup_snapshot()
        with app.role_life.consistent_snapshot():
            self.assertTrue(registry.unregister(token))
            with self.assertRaises(TaskError):
                app.role_life.activity("activity", owner=owner)
            with self.assertRaisesRegex(ValueError, "draining"):
                registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        replacement = registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        self.assertTrue(registry.unregister(replacement))


if __name__ == "__main__":
    unittest.main()
