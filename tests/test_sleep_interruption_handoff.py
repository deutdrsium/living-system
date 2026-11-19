from datetime import timedelta
from importlib.util import find_spec
from types import SimpleNamespace
import unittest

import test_manual_sleep_handoff as manual
from companion_v2.adapters.astrbot.application import optional_life_snapshot
from companion_v2.execution.models import TaskError, require
from companion_v2.integration import sleep_interruption_queue
from companion_v2.role_work.models import RoleActivityState
from good_life.runtime import LifeRuntime
from rehearse_activity_rollback import rehearse
from tests.test_role_state import StateFixture


class SleepInterruptionTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    prepare_sleep = manual.ManualSleepHandoffTests.prepare_sleep

    async def configured(self, *, interrupt_once=False):
        if find_spec("astrbot") is None:
            self.skipTest("AstrBot is required for the provider facade")
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import PROVIDER_ID
        original = self.setup_state
        def setup(*args, **kwargs):
            result = original(*args, **kwargs)
            self.actual_host = result[0]
            return result
        self.setup_state = setup
        manager, _, _, life, lease = await self.prepare_sleep(existing_night=True)
        status = manager.status(self.core)
        configured = await manager.command(self.core, {"action": "configure", "operation_key": "enable-sleep-test",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False, "sleep_enabled": True,
                        "sleep_timezone": "Asia/Shanghai", "rest_reply_active_windows": "00:00-23:59"}})
        self.assertTrue(configured.is_success, configured)
        manager.life.refresh()
        manager.app.life_runtime.events.advance = lambda _routines: None
        manager.app.role_life.unbind_reader(manager.app.role_life._reader)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=manager.app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_memory_providers)
        self.addCleanup(api.close_life_providers)
        runtime = LifeRuntime(life, plugin)
        if interrupt_once:
            original_control = runtime.handlers["activity_control"]
            failed = False
            def control(operation, *args, **kwargs):
                nonlocal failed
                result = original_control(operation, *args, **kwargs)
                if operation == "activity_transition.resolve" and not failed:
                    failed = True
                    raise ValueError("simulated interruption after the life commit")
                return result
            runtime.handlers["activity_control"] = control
        await runtime.bind(api)
        self.addAsyncCleanup(runtime.close)
        return manager, life, runtime

    async def test_message_admission_interrupts_and_resumes_sleep_once(self):
        manager, _, runtime = await self.configured()
        with optional_life_snapshot(manager.app), self.runtime.transaction():
            result = self.actual_host.receive(self.event("wakes-role"))
            require(result)
        self.assertTrue(result.is_success, result)
        pending = sleep_interruption_queue.pending(self.runtime)
        self.assertEqual(len(pending), 1)
        self.assertEqual(manager.app.role_life.activity("existing-night", owner=manager.owner).state, RoleActivityState.ACTIVE)
        await runtime.scheduler.tick(now=self.clock.now())
        self.assertIsNone(runtime.scheduler.last_failure)
        sleep = manager.app.role_life.activity("existing-night", owner=manager.owner)
        self.assertEqual((sleep.state, sleep.sleep.slept_seconds, sleep.sleep.interruptions),
                         (RoleActivityState.PAUSED, 3600, 1))
        self.assertEqual(sleep.sleep.wake_until, self.clock.now() + timedelta(minutes=30))
        self.assertEqual(sleep_interruption_queue.pending(self.runtime), ())
        self.clock.advance(timedelta(minutes=30))
        await runtime.scheduler.tick(now=self.clock.now())
        sleep = manager.app.role_life.activity("existing-night", owner=manager.owner)
        self.assertEqual((sleep.state, sleep.sleep.slept_seconds, sleep.sleep.interruptions),
                         (RoleActivityState.ACTIVE, 3600, 1))
        self.assertEqual(len(self.sent), 0)

    async def test_interrupted_commit_recovers_after_provider_rebind(self):
        manager, _, runtime = await self.configured(interrupt_once=True)
        with optional_life_snapshot(manager.app), self.runtime.transaction():
            require(self.actual_host.receive(self.event("wakes-role")))
        await runtime.scheduler.tick(now=self.clock.now())
        self.assertEqual(len(sleep_interruption_queue.pending(self.runtime)), 1)
        with self.assertRaisesRegex(ValueError, "interruptions are still pending"):
            rehearse(self.runtime.database_path, self.life_root / "good-life.sqlite3", expected_version=self.version)
        api = runtime.api
        await runtime.unbind()
        await runtime.bind(api)
        await runtime.scheduler.tick(now=self.clock.now())
        sleep = manager.app.role_life.activity("existing-night", owner=manager.owner)
        self.assertEqual((sleep.state, sleep.revision, sleep.sleep.interruptions), (RoleActivityState.PAUSED, 2, 1))
        self.assertEqual(sleep_interruption_queue.pending(self.runtime), ())
        self.assertEqual(rehearse(self.runtime.database_path, self.life_root / "good-life.sqlite3",
                                  expected_version=self.version)["activities"], 1)

    async def test_rejected_message_does_not_interrupt_sleep(self):
        manager, _, _ = await self.configured()
        def reject(*_args, **_kwargs):
            raise TaskError("test_rejected", "input was not admitted")
        with self.assertRaises(TaskError):
            with optional_life_snapshot(manager.app), self.runtime.transaction():
                require(self.actual_host.receive(self.event("not-admitted"), on_admitted=reject))
        self.assertEqual(sleep_interruption_queue.pending(self.runtime), ())
        self.assertEqual(manager.app.role_life.activity("existing-night", owner=manager.owner).state, RoleActivityState.ACTIVE)


if __name__ == "__main__":
    unittest.main()
