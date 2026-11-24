from datetime import timedelta
import unittest
from unittest.mock import patch

import test_manual_sleep_handoff as manual
from companion_v2.execution.models import TaskError
from companion_v2.integration.life_activity_control_proxy import LifeActivityCreationProxy, LifeActivityTransitionProxy
from companion_v2.integration.natural_wake_handoff import NaturalWakeHandoff
from companion_v2.integration.scheduled_sleep_handoff import ScheduledSleepHandoff
from companion_v2.role_work.models import RoleActivityState
from good_life.sleep_planner import SleepPlanner
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.runtime import LifeRuntime
from rehearse_activity_rollback import rehearse
from tests.test_role_state import StateFixture


class ScheduledSleepHandoffTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    prepare_sleep = manual.ManualSleepHandoffTests.prepare_sleep

    async def configured(self):
        manager, _, grant, life, lease = await self.prepare_sleep()
        status = manager.status(self.core)
        result = await manager.command(self.core, {"action": "configure", "operation_key": "schedule-sleep",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False, "sleep_enabled": True, "sleep_timezone": "Asia/Shanghai",
                        "sleep_time": "08:01", "sleep_variation_minutes": 0, "sleep_window_minutes": 30}})
        self.assertTrue(result.is_success, result)
        manager.life.refresh()
        manager.app.life_runtime.events.advance = lambda _routines: None
        return manager, grant, life, lease

    def plan_at_next_minute(self, handoff):
        planner = SleepPlanner()
        snapshot = handoff.snapshot()
        now = self.clock.now()
        snapshot.pop("observed_at")
        self.assertIsNone(planner.plan(now=now, **snapshot))
        self.clock.advance(timedelta(minutes=1))
        snapshot = handoff.snapshot()
        snapshot.pop("observed_at")
        plan = planner.plan(now=self.clock.now(), **snapshot)
        self.assertIsNotNone(plan, (now, self.clock.now(), planner._windows, snapshot))
        return plan

    async def test_scheduler_starts_then_finishes_sleep_via_registered_host(self):
        from importlib.util import find_spec
        if find_spec("astrbot") is None:
            self.skipTest("AstrBot is required for the plugin facade")
        from types import SimpleNamespace
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import PROVIDER_ID
        manager, _, life, lease = await self.configured()
        manager.app.role_life.unbind_reader(manager.app.role_life._reader)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=manager.app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_memory_providers)
        self.addCleanup(api.close_life_providers)
        runtime = LifeRuntime(life, plugin)
        await runtime.bind(api)
        self.addAsyncCleanup(runtime.close)
        scheduler = runtime.scheduler
        await scheduler.tick(now=self.clock.now())
        self.assertIsNone(scheduler.last_failure)
        self.assertEqual(scheduler.planner._windows[manager.context.key]["next_sleep"], self.clock.now() + timedelta(minutes=1))
        self.clock.advance(timedelta(minutes=1))
        await scheduler.tick(now=self.clock.now())
        self.assertIsNone(scheduler.last_failure)
        active = manager.app.role_state.body_clock.active(manager.context, "sleep")
        self.assertEqual(len(active), 1)
        self.assertEqual((active[0].sleep.kind, active[0].sleep.target_minutes), ("night", 480))
        self.assertIsNone(self.runtime.read_one("SELECT 1 FROM role_activities WHERE activity_id=?", (active[0].activity_id,)))
        self.clock.advance(timedelta(hours=9))
        await scheduler.tick(now=self.clock.now())
        await scheduler.tick(now=self.clock.now())
        finished = manager.app.role_life.activity(active[0].activity_id, owner=manager.owner)
        self.assertEqual(finished.state, RoleActivityState.COMPLETED)
        self.assertEqual(len(life.pending_events("dream")), 1)
        self.assertEqual(len(self.sent), 0)

    async def test_night_alarm_caps_committed_wake_and_preserves_sleep_debt(self):
        manager, _, _, lease = await self.configured()
        handoff = ScheduledSleepHandoff(manager.app, manager.life, LifeActivityCreationProxy(lease))
        plan = self.plan_at_next_minute(handoff)
        duration = timedelta(hours=6, seconds=45)
        alarm = self.clock.now() + duration
        lease.control.creations.night_sleep_alarm = lambda *_: alarm
        self.assertEqual((await handoff.execute(plan))["state"], "committed")
        activity, = manager.app.role_state.body_clock.active(manager.context, "sleep")
        self.assertEqual(activity.work.due_at, alarm)
        self.assertEqual((activity.sleep.base_need_minutes, activity.sleep.target_minutes), (480, 360))

        self.clock.advance(duration)
        wake = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        with (patch("good_life.staged_activity_commands.oversleep_extension", return_value=15),
              patch("companion_v2.integration.natural_wake_handoff.oversleep_extension", return_value=15)):
            await wake.execute(activity.activity_id, expected_revision=activity.revision)
        extended = manager.app.role_life.activity(activity.activity_id, owner=manager.owner)
        self.assertEqual((extended.state, extended.work.due_at), (RoleActivityState.ACTIVE, alarm + timedelta(minutes=15)))
        self.clock.advance(timedelta(minutes=15))
        await wake.execute(extended.activity_id, expected_revision=extended.revision)
        body = manager.app.role_state.body_clock.read(manager.context, now=self.clock.now())
        self.assertEqual(body["state"], "awake")
        self.assertEqual(body["sleep_debt_minutes"], 104)

    async def test_disabling_sleep_aborts_prepared_sleep(self):
        manager, _, life, lease = await self.configured()
        handoff = ScheduledSleepHandoff(manager.app, manager.life, LifeActivityCreationProxy(lease))
        plan = self.plan_at_next_minute(handoff)
        request = handoff.prepare(plan, observed_at=self.clock.now())
        status = manager.status(self.core)
        disabled = await manager.command(self.core, {"action": "configure", "operation_key": "disable-sleep",
            "view_token": status["view_token"], "edit_token": status["edit_token"], "changes": {"sleep_enabled": False}})
        self.assertTrue(disabled.is_success, disabled)
        with self.assertRaises(TaskError):
            handoff.decide(request, observed_at=self.clock.now())
        self.assertEqual(handoff.abort(request, observed_at=self.clock.now())["state"], "aborted")
        self.assertFalse(life.pending_events("dream"))

    async def test_recovery_completes_decided_sleep_only_once(self):
        manager, _, life, lease = await self.configured()
        handoff = ScheduledSleepHandoff(manager.app, manager.life, LifeActivityCreationProxy(lease))
        plan = self.plan_at_next_minute(handoff)
        request = handoff.prepare(plan, observed_at=self.clock.now())
        decision = handoff.decide(request, observed_at=self.clock.now())
        lease.interrupt_after_resolve = True
        with self.assertRaises(ValueError):
            handoff.finish(request, decision, observed_at=self.clock.now())
        self.assertEqual((await handoff.recover(request))["state"], "committed")
        self.assertEqual(len(manager.app.role_state.body_clock.active(manager.context, "sleep")), 1)
        self.assertFalse(life.pending_events("dream"))
        self.assertEqual(rehearse(self.runtime.database_path, self.life_root / "good-life.sqlite3",
                                  expected_version=self.version)["activities"], 1)


if __name__ == "__main__":
    unittest.main()
