from datetime import timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_provider_registry import PROVIDER_ID
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.models import RoleActivityState
from good_life.activity_import import stage_activities
from good_life.runtime import LifeRuntime
from good_life.storage import LifeStorage
from tests.multi_life_support import MultiLifeFixture


class NativeSleepInterruptionTests(MultiLifeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_native_session_admits_message_then_interrupts_life_owned_sleep(self):
        app = await self.initialize()
        await self.enable(sleep_enabled=True, sleep_timezone="Asia/Shanghai", rest_reply_active_windows="00:00-23:59")
        await self.start()
        app.life_runtime.bind(self.context(), self.native.persona["prompt"])
        result = await self.command("sleep", minutes=90)
        activity_id = result["activity_id"]
        self.clock.advance(timedelta(minutes=1))
        self.controller.session.application.life_runtime.events.advance = lambda _routines: None
        version = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        life_root = self.root / "isolated-life"
        stage_activities(app.runtime.database_path, expected_version=version, destination=life_root)
        storage = LifeStorage.open(life_root)
        self.addCleanup(storage.close)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        self.native.get_all_stars = lambda: [star]
        api = UnifiedCompanionExtensionAPI(self.controller)
        self.addCleanup(api.close_memory_providers)
        self.addCleanup(api.close_life_providers)
        runtime = LifeRuntime(storage, plugin)
        await runtime.bind(api)
        self.addAsyncCleanup(runtime.close)
        with app.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        accepted = await self.controller.receive_claimed(self.event(identifier="wake-message"))
        self.assertTrue(accepted.is_success, accepted)
        await runtime.scheduler.tick(now=self.clock.now())
        context = self.context()
        owner = app.life_runtime.for_context(context).owner
        activity = app.role_life.activity(activity_id, owner=owner)
        self.assertEqual((activity.state, activity.sleep.interruptions, activity.sleep.slept_seconds),
                         (RoleActivityState.PAUSED, 1, 60))


if __name__ == "__main__":
    unittest.main()
