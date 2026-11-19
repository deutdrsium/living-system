from datetime import timedelta
import unittest

from test_manual_sleep_handoff import ManualSleepHandoffTests, LocalLease
from companion_v2.execution.models import TaskError, digest
from companion_v2.integration.life_activity_control_proxy import LifeActivityCreationProxy, LifeActivityTransitionProxy
from companion_v2.integration.life_event_provider_proxy import LifeEventProviderProxy
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.natural_wake_handoff import NaturalWakeHandoff
from companion_v2.role_work.models import RoleActivityState
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from good_life.storage import LifeStorage
from good_life.sleep_scheduler import DueSleepScheduler
from tests.test_role_state import StateFixture


class NaturalWakeHandoffTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    prepare_sleep = ManualSleepHandoffTests.prepare_sleep

    async def configured(self, *, identifier, interrupted=False, slept_seconds=3600, advance=timedelta(hours=7)):
        manager, _, _, life, lease = await self.prepare_sleep(existing_night=identifier, interrupted=interrupted,
                                                               slept_seconds=slept_seconds)
        status = manager.status(self.core)
        config = {"action": "configure", "operation_key": "enable-routine", "view_token": status["view_token"],
                  "edit_token": status["edit_token"], "changes": {"enabled": True, "diary_enabled": False}}
        result = await manager.command(self.core, config)
        self.assertTrue(result.is_success, result)
        manager.life.refresh()
        manager.app.life_runtime.events.advance = lambda _routines: None
        self.clock.advance(advance)
        return manager, life, lease

    async def test_due_wake_is_audited_and_delivers_the_sleep_event(self):
        manager, life, lease = await self.configured(identifier="existing-night")
        handoff = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        result = await handoff.execute("existing-night", expected_revision=1)
        self.assertEqual(result["state"], "committed")
        activity = manager.app.role_life.activity("existing-night", owner=manager.owner)
        self.assertEqual((activity.state, activity.revision, activity.sleep.slept_seconds),
                         (RoleActivityState.COMPLETED, 2, 8 * 3600))
        operation = "body-wake-" + digest(["existing-night", 1])
        self.assertEqual(OperationRepository(self.runtime).get(operation).unwrap().state, OperationState.COMPLETED)
        self.assertEqual(len(life.pending_events("dream")), 1)
        self.assertEqual(len(life.pending_events("wardrobe")), 1)
        self.assertEqual(len(self.sent), 0)

    async def test_due_oversleep_extends_once_and_then_finishes(self):
        manager, life, lease = await self.configured(identifier="night-4")
        handoff = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        await handoff.execute("night-4", expected_revision=1)
        activity = manager.app.role_life.activity("night-4", owner=manager.owner)
        self.assertEqual((activity.state, activity.sleep.oversleep_minutes), (RoleActivityState.ACTIVE, 15))
        self.assertEqual(len(life.pending_events("dream")), 0)
        self.clock.advance(timedelta(minutes=15))
        await handoff.execute("night-4", expected_revision=2)
        activity = manager.app.role_life.activity("night-4", owner=manager.owner)
        self.assertEqual((activity.state, activity.revision, activity.sleep.slept_seconds),
                         (RoleActivityState.COMPLETED, 3, 8 * 3600 + 15 * 60))
        self.assertEqual(len(life.pending_events("dream")), 1)

    async def test_due_wake_recovers_after_life_store_reopens(self):
        manager, life, lease = await self.configured(identifier="existing-night")
        handoff = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        request = handoff.prepare("existing-night", expected_revision=1, observed_at=self.clock.now())
        decision = handoff.decide(request, observed_at=self.clock.now())
        handoff.apply(request, decision)
        manager.app.role_life.unbind_reader(manager.app.role_life._reader)
        life.close()
        life = LifeStorage.open(self.life_root)
        self.addCleanup(life.close)
        lease = LocalLease(life)
        manager.app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, lease),
            creation_provider=LifeActivityCreationProxy(lease), transition_provider=LifeActivityTransitionProxy(lease),
            events_provider=LifeEventProviderProxy(self.runtime, lease))
        handoff = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        self.assertEqual(handoff.decide(request, observed_at=self.clock.now()), decision)
        self.assertEqual(handoff.apply(request, decision)["state"], "committed")
        self.assertTrue(handoff.complete(request, decision, observed_at=self.clock.now()))
        self.assertTrue(handoff.acknowledge(request, decision))
        self.assertEqual(len(life.pending_events("dream")), 1)

    async def test_due_wake_rejects_disabled_routine(self):
        manager, _, _, _, lease = await self.prepare_sleep(existing_night=True)
        self.clock.advance(timedelta(hours=7))
        handoff = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        with self.assertRaises(TaskError):
            await handoff.execute("existing-night", expected_revision=1)

    def bind_facade(self, manager, lease):
        from importlib.util import find_spec
        if find_spec("astrbot") is None:
            self.skipTest("AstrBot is required for the plugin facade")
        from types import SimpleNamespace
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID
        manager.app.role_life.unbind_reader(manager.app.role_life._reader)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=manager.app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_memory_providers)
        self.addCleanup(api.close_life_providers)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"], handlers["activity_history"] = lease.records.dispatch, lease.events.dispatch
        handlers["activity_control"] = lease.control.dispatch
        handlers["browsing_read"] = lease.exposures.dispatch
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        return api, plugin, token

    async def test_plugin_scheduler_wakes_via_scoped_host_capabilities(self):
        manager, life, lease = await self.configured(identifier="existing-night")
        api, plugin, token = self.bind_facade(manager, lease)
        with self.assertRaises(TaskError):
            api.life_host_capabilities(plugin=object(), registration_id=token)
        scheduler = DueSleepScheduler(life, api.life_host_capabilities(plugin=plugin, registration_id=token))
        self.addAsyncCleanup(scheduler.close)
        self.assertEqual(await scheduler.tick(now=self.clock.now()), {"recovered": 0, "woken": 1})
        self.assertEqual(manager.app.role_life.activity("existing-night", owner=manager.owner).state, RoleActivityState.COMPLETED)
        self.assertEqual(len(life.pending_events("dream")), 1)
        api.unregister_life_provider(token)
        with self.assertRaises(TaskError):
            await scheduler.tick(now=self.clock.now())

    async def test_plugin_scheduler_recovers_a_confirmed_interrupted_wake(self):
        manager, life, lease = await self.configured(identifier="existing-night")
        handoff = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        request = handoff.prepare("existing-night", expected_revision=1, observed_at=self.clock.now())
        decision = handoff.decide(request, observed_at=self.clock.now())
        handoff.apply(request, decision)
        api, plugin, token = self.bind_facade(manager, lease)
        scheduler = DueSleepScheduler(life, api.life_host_capabilities(plugin=plugin, registration_id=token))
        self.addAsyncCleanup(scheduler.close)
        self.assertEqual(await scheduler.tick(now=self.clock.now()), {"recovered": 1, "woken": 0})
        self.assertEqual(manager.app.role_life.activity("existing-night", owner=manager.owner).state, RoleActivityState.COMPLETED)
        self.assertEqual(len(life.pending_events("dream")), 1)

    async def test_interrupted_sleep_resumes_after_grace_without_counting_awake_time(self):
        manager, life, lease = await self.configured(identifier="existing-night", interrupted=True, advance=timedelta())
        api, plugin, token = self.bind_facade(manager, lease)
        scheduler = DueSleepScheduler(life, api.life_host_capabilities(plugin=plugin, registration_id=token))
        self.addAsyncCleanup(scheduler.close)
        self.assertEqual(await scheduler.tick(now=self.clock.now()), {"recovered": 0, "woken": 0})
        self.clock.advance(timedelta(minutes=10))
        self.assertEqual(await scheduler.tick(now=self.clock.now()), {"recovered": 0, "woken": 1})
        resumed = manager.app.role_life.activity("existing-night", owner=manager.owner)
        self.assertEqual((resumed.state, resumed.sleep.slept_seconds, resumed.sleep.wake_until),
                         (RoleActivityState.ACTIVE, 3600, None))
        self.assertEqual(resumed.work.due_at, self.clock.now() + timedelta(hours=7))
        self.assertEqual(len(life.pending_events("dream")), 0)

    async def test_interrupted_sleep_already_at_target_completes_after_grace(self):
        manager, life, lease = await self.configured(identifier="existing-night", interrupted=True,
                                                     slept_seconds=8 * 3600, advance=timedelta(minutes=10))
        handoff = NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease))
        await handoff.execute("existing-night", expected_revision=1)
        completed = manager.app.role_life.activity("existing-night", owner=manager.owner)
        self.assertEqual((completed.state, completed.sleep.slept_seconds, completed.sleep.wake_until),
                         (RoleActivityState.COMPLETED, 8 * 3600, None))
        self.assertEqual(len(life.pending_events("dream")), 1)


if __name__ == "__main__":
    unittest.main()
