from pathlib import Path
from datetime import timedelta
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]

from companion_v2.execution.models import TaskError, digest
from companion_v2.integration.exposure_session_proxy import ExposureSessionReadProxy
from companion_v2.integration.life_activity_control_proxy import LifeActivityCreationProxy, LifeActivityTransitionProxy
from companion_v2.integration.life_event_inbox import receive_pending
from companion_v2.integration.life_event_provider_proxy import LifeEventProviderProxy
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.integration.manual_sleep_handoff import ManualSleepHandoff
from companion_v2.integration.manual_wake_handoff import ManualWakeHandoff
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleSleepReceipt
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from good_life.activity_import import stage_activities
from good_life.activity_records import ActivityTransitionPending, StagedActivityRecords
from good_life.activity_control import ActivityControl
from good_life.event_delivery import LifeEventDelivery
from good_life.exposure_import import stage_exposures
from good_life.exposure_records import StagedExposureRecords
from good_life.state_import import stage_state
from good_life.prepared_commands import PreparedCommands
from good_life.staged_activity_creations import StagedActivityCreations
from good_life.staged_activity_commands import StagedActivityTransitions
from good_life.storage import LifeStorage, StorageReader
from rehearse_activity_rollback import rehearse
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


class LocalLease:
    """Contract-level stand-in; no plugin receives the host database."""
    def __init__(self, storage):
        self.records = StagedActivityRecords(storage)
        self.creations = StagedActivityCreations(storage)
        self.transitions = StagedActivityTransitions(storage)
        self.control = ActivityControl(storage)
        self.events = LifeEventDelivery(storage)
        self.exposures = StagedExposureRecords(storage)
        self.interrupt_after_resolve = False
        self.interrupt_wake_after_resolve = False

    def dispatch(self, handler, operation, *args, **kwargs):
        if handler == "activity_read":
            return self.records.dispatch(operation, *args, **kwargs)
        if handler == "activity_history":
            return self.events.dispatch(operation, *args, **kwargs)
        if handler == "browsing_read":
            return self.exposures.dispatch(operation, *args, **kwargs)
        if handler == "activity_control":
            result = self.control.dispatch(operation, *args, **kwargs)
            if operation == "activity_creation.resolve" and self.interrupt_after_resolve:
                self.interrupt_after_resolve = False
                raise ValueError("simulated interruption after life commit")
            if operation == "activity_transition.resolve" and self.interrupt_wake_after_resolve:
                self.interrupt_wake_after_resolve = False
                raise ValueError("simulated interruption after wake commit")
            return result
        raise ValueError("unsupported test handler")

    def snapshot(self, handler, operation):
        if handler != "activity_history":
            raise ValueError("unsupported test snapshot")
        return self.records.snapshot(operation)


class ManualSleepHandoffTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def prepare_sleep(self, *, existing_night=False, interrupted=False, slept_seconds=3600):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        grant = self.grant(context.actor, context.scope, "role.life.run")
        await self.start(host)
        app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        if existing_night:
            night_id = existing_night if type(existing_night) is str else "existing-night"
            sleep = (RoleSleepReceipt("night", 480, 480, slept_seconds=slept_seconds, interruptions=1,
                                      wake_until=NOW + timedelta(minutes=10)) if interrupted
                     else RoleSleepReceipt("night", 480, 480))
            activity = RoleActivity(night_id, manager.owner.persona_id, context.scope, NOW - timedelta(hours=1),
                RoleActivityState.PAUSED if interrupted else RoleActivityState.ACTIVE,
                work=RoleWorkSpec("sleep", context.key, "night-request", due_at=NOW + timedelta(hours=7)), sleep=sleep,
                updated_at=NOW if interrupted else None)
            app.role_life.add_activity(activity)
        self.version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        self.life_root = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=self.version, destination=self.life_root)
        stage_exposures(self.runtime.database_path, expected_version=self.version, destination=self.life_root,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_state(self.runtime.database_path, expected_version=self.version, destination=self.life_root)
        life = LifeStorage.open(self.life_root)
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        lease = LocalLease(life)
        browsing = ExposureSessionReadProxy(self.runtime, lease, life=app.role_life,
                                            visible=app.role_social._visible_contact,
                                            writable=app.role_social._active_contact)
        app.role_social.exposure.session_reader = app.role_life.browsing_reader = browsing
        reader = LifeLedgerReadProxy(self.runtime, lease)
        creation = LifeActivityCreationProxy(lease)
        app.role_life.bind_reader(reader, creation_provider=creation, transition_provider=LifeActivityTransitionProxy(lease),
                                  events_provider=LifeEventProviderProxy(self.runtime, lease))
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        access = manager.authorize(self.core)
        data = {"action": "sleep", "minutes": 90, "operation_key": "sleep-request",
                "view_token": manager.token(self.core, access)}
        return manager, data, grant, life, lease

    async def test_management_creates_in_life_store_and_replays_without_second_sleep(self):
        manager, data, _, _, _ = await self.prepare_sleep()
        result = await manager.command(self.core, data)
        self.assertTrue(result.is_success, result)
        activity = manager.app.role_life.activity(result.unwrap()["activity_id"], owner=manager.owner)
        self.assertEqual((activity.state, activity.sleep.kind, activity.sleep.target_minutes),
                         (RoleActivityState.ACTIVE, "manual", 90))
        self.assertIsNone(self.runtime.read_one("SELECT 1 FROM role_activities WHERE activity_id=?", (activity.activity_id,)))
        replay = await manager.command(self.core, data)
        self.assertTrue(replay.is_success, replay)
        self.assertTrue(replay.unwrap()["replayed"])
        conflict = await manager.command(self.core, {**data, "minutes": 91})
        self.assertFalse(conflict.is_success)
        result = rehearse(self.runtime.database_path, self.life_root / "good-life.sqlite3", expected_version=self.version)
        self.assertEqual(result["activities"], 1)
        operation = manager.operation_id("life-command-", self.core, data["operation_key"])
        with self.runtime.transaction() as connection:
            connection.execute("UPDATE operations SET parameter_hash='changed' WHERE operation_id=?", (operation,))
        with self.assertRaisesRegex(ValueError, "creation evidence"):
            rehearse(self.runtime.database_path, self.life_root / "good-life.sqlite3", expected_version=self.version)
        self.assertEqual(len(self.sent), 0)

    async def test_interrupted_commit_is_hidden_and_recovered_once(self):
        manager, data, _, life, lease = await self.prepare_sleep()
        lease.interrupt_after_resolve = True
        interrupted = await manager.command(self.core, data)
        self.assertFalse(interrupted.is_success)
        with self.assertRaises(ActivityTransitionPending):
            lease.records.dispatch("activities_for_role", owner_key=digest(manager.owner),
                                   state=None, limit=100, after_id=None)
        operation = manager.operation_id("life-command-", self.core, data["operation_key"])
        self.assertEqual(OperationRepository(self.runtime).get(operation).unwrap().state, OperationState.ACCEPTED)
        retry = await manager.command(self.core, data)
        self.assertTrue(retry.is_success, retry)
        self.assertEqual(OperationRepository(self.runtime).get(operation).unwrap().state, OperationState.COMPLETED)
        self.assertEqual(StorageReader(life).one("SELECT COUNT(*) AS n FROM staged_role_activities", ())["n"], 1)
        self.assertEqual(len(self.sent), 0)

    async def test_revoked_authority_aborts_before_activity_is_created(self):
        manager, data, grant, life, lease = await self.prepare_sleep()
        handoff = ManualSleepHandoff(manager, LifeActivityCreationProxy(lease))
        request = handoff.prepare(self.core, data, observed_at=NOW)
        self.identity.revoke_grant(grant.grant_id, expected_revision=self.identity.revision, revoked_at=NOW)
        with self.assertRaises(TaskError):
            handoff.decide(self.core, data, request, observed_at=NOW)
        self.assertEqual(handoff.abort(request, observed_at=NOW)["state"], "aborted")
        self.assertEqual(StorageReader(life).one("SELECT COUNT(*) AS n FROM staged_role_activities", ())["n"], 0)

    async def test_manual_wake_preserves_elapsed_time_audits_and_event(self):
        manager, data, _, life, lease = await self.prepare_sleep()
        started = await manager.command(self.core, data)
        self.assertTrue(started.is_success, started)
        identifier = started.unwrap()["activity_id"]
        access = manager.authorize(self.core)
        wake = {"action": "wake", "activity_id": identifier, "expected_revision": 1, "operation_key": "wake-request",
                "view_token": manager.token(self.core, access, identifier, 1)}
        self.assertFalse((await manager.command(self.core, wake)).is_success)
        self.clock.advance(timedelta(minutes=15))
        lease.interrupt_wake_after_resolve = True
        self.assertFalse((await manager.command(self.core, wake)).is_success)
        self.assertEqual(len(life.pending_events("dream")), 0)
        manager.app.role_life.unbind_reader(manager.app.role_life._reader)
        life.close()
        life = LifeStorage.open(self.life_root)
        self.addCleanup(life.close)
        lease = LocalLease(life)
        manager.app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, lease),
            creation_provider=LifeActivityCreationProxy(lease), transition_provider=LifeActivityTransitionProxy(lease),
            events_provider=LifeEventProviderProxy(self.runtime, lease))
        result = await manager.command(self.core, wake)
        self.assertTrue(result.is_success, result)
        completed = manager.app.role_life.activity(identifier, owner=manager.owner)
        self.assertEqual((completed.state, completed.revision, completed.sleep.slept_seconds),
                         (RoleActivityState.COMPLETED, 2, 900))
        operation = manager.operation_id("life-command-", self.core, wake["operation_key"])
        for audit_id in (operation, operation + ":wake"):
            self.assertEqual(OperationRepository(self.runtime).get(audit_id).unwrap().state, OperationState.COMPLETED)
        self.assertEqual(len(life.pending_events("dream")), 1)
        self.assertEqual(len(life.pending_events("wardrobe")), 0)
        replay = await manager.command(self.core, wake)
        self.assertTrue(replay.is_success, replay)
        self.assertTrue(replay.unwrap()["replayed"])
        self.assertEqual(len(life.pending_events("dream")), 1)
        ended = {"action": "wake", "activity_id": identifier, "expected_revision": 2, "operation_key": "wake-ended",
                 "view_token": manager.token(self.core, manager.authorize(self.core), identifier, 2)}
        self.assertTrue((await manager.command(self.core, ended)).is_success)
        self.assertTrue((await manager.command(self.core, ended)).unwrap()["replayed"])
        self.assertEqual(len(life.pending_events("dream")), 1)
        for consumer in ("journal", "dream"):
            delivered = receive_pending(manager.app, manager.context, consumer=consumer, observed_at=self.clock.now())
            self.assertEqual((delivered["received"], delivered["acknowledged"]), (1, 1))
            self.assertEqual(len(life.pending_events(consumer)), 0)
        self.assertEqual(len(self.sent), 0)

    async def test_waking_an_imported_night_delivers_wardrobe_event(self):
        manager, _, _, life, _ = await self.prepare_sleep(existing_night=True)
        access = manager.authorize(self.core)
        wake = {"action": "wake", "activity_id": "existing-night", "expected_revision": 1, "operation_key": "wake-night",
                "view_token": manager.token(self.core, access, "existing-night", 1)}
        result = await manager.command(self.core, wake)
        self.assertTrue(result.is_success, result)
        self.assertEqual(manager.app.role_life.activity("existing-night", owner=manager.owner).sleep.slept_seconds, 3600)
        for consumer in ("journal", "dream", "wardrobe"):
            self.assertEqual(len(life.pending_events(consumer)), 1)
            delivered = receive_pending(manager.app, manager.context, consumer=consumer, observed_at=self.clock.now())
            self.assertEqual((delivered["received"], delivered["acknowledged"]), (1, 1))

    async def test_wake_preparation_aborts_if_role_permission_is_revoked(self):
        manager, _, grant, life, lease = await self.prepare_sleep(existing_night=True)
        access = manager.authorize(self.core)
        wake = {"action": "wake", "activity_id": "existing-night", "expected_revision": 1, "operation_key": "denied-wake",
                "view_token": manager.token(self.core, access, "existing-night", 1)}
        handoff = ManualWakeHandoff(manager, LifeActivityTransitionProxy(lease))
        request = handoff.prepare(self.core, wake, observed_at=self.clock.now())
        self.identity.revoke_grant(grant.grant_id, expected_revision=self.identity.revision, revoked_at=self.clock.now())
        with self.assertRaises(TaskError):
            handoff.decide(self.core, wake, request, observed_at=self.clock.now())
        self.assertEqual(handoff.abort(request, observed_at=self.clock.now())["state"], "aborted")
        self.assertEqual(manager.app.role_life.activity("existing-night", owner=manager.owner).state, RoleActivityState.ACTIVE)
        self.assertEqual(len(life.pending_events("dream")), 0)

    async def test_cancel_sleep_keeps_elapsed_time_even_after_execution_grant_is_revoked(self):
        manager, _, grant, life, _ = await self.prepare_sleep(existing_night=True)
        cancel = {"action": "cancel", "activity_id": "existing-night", "expected_revision": 1,
                  "operation_key": "cancel-night",
                  "view_token": manager.token(self.core, manager.authorize(self.core), "existing-night", 1)}
        self.identity.revoke_grant(grant.grant_id, expected_revision=self.identity.revision, revoked_at=self.clock.now())
        result = await manager.command(self.core, cancel)
        self.assertTrue(result.is_success, result)
        activity = manager.app.role_life.activity("existing-night", owner=manager.owner)
        self.assertEqual((activity.state, activity.sleep.slept_seconds), (RoleActivityState.CANCELLED, 3600))
        self.assertEqual(len(life.pending_events("dream")), 0)
        self.assertTrue((await manager.command(self.core, cancel)).unwrap()["replayed"])

    async def test_actual_plugin_facade_routes_creation(self):
        from importlib.util import find_spec
        if find_spec("astrbot") is None:
            self.skipTest("AstrBot is required for the plugin facade")
        from types import SimpleNamespace
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID
        from good_life.sleep_scheduler import DueSleepScheduler
        manager, data, _, life, lease = await self.prepare_sleep()
        manager.app.role_life.unbind_reader(manager.app.role_life._reader)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=manager.app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_memory_providers)
        self.addCleanup(api.close_life_providers)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"] = lease.records.dispatch
        handlers["activity_history"] = lease.events.dispatch
        handlers["browsing_read"] = lease.exposures.dispatch
        handlers["activity_control"] = lambda operation, *args, **kwargs: lease.dispatch("activity_control", operation, *args, **kwargs)
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        host = api.life_host_capabilities(plugin=plugin, registration_id=token)
        scheduler = DueSleepScheduler(life, {name: host[name] for name in
            ("api_version", "contexts", "calendar_contexts", "wake", "recover", "recover_manual_sleep")})
        self.addAsyncCleanup(scheduler.close)
        lease.interrupt_after_resolve = True
        result = await manager.command(self.core, data)
        self.assertFalse(result.is_success)
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1, scheduler.last_failure)
        result = await manager.command(self.core, data)
        self.assertTrue(result.is_success, result)
        identifier = result.unwrap()["activity_id"]
        self.assertEqual(manager.app.role_life.activity(identifier, owner=manager.owner).sleep.target_minutes, 90)
        self.clock.advance(timedelta(minutes=15))
        wake = {"action": "wake", "activity_id": identifier, "expected_revision": 1, "operation_key": "facade-wake",
                "view_token": manager.token(self.core, manager.authorize(self.core), identifier, 1)}
        lease.interrupt_wake_after_resolve = True
        self.assertFalse((await manager.command(self.core, wake)).is_success)
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1, scheduler.last_failure)
        completed = manager.app.role_life.activity(identifier, owner=manager.owner)
        self.assertEqual((completed.state, completed.sleep.slept_seconds), (RoleActivityState.COMPLETED, 900))
        self.assertEqual(len(life.pending_events("dream")), 1)
        pending_data = {**data, "operation_key": "undecided-sleep"}
        handoff = ManualSleepHandoff(manager, manager.app.role_life.creation_provider())
        pending = handoff.prepare(self.core, pending_data, observed_at=self.clock.now())
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1, scheduler.last_failure)
        row = StorageReader(life).one("SELECT state FROM life_prepared_commands WHERE command_id=?",
                                                      (pending["command_id"],))
        self.assertEqual(row["state"], "aborted")


if __name__ == "__main__":
    unittest.main()
