import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tests"), str(ROOT / "tools")]

from companion_v2.configuration.registry import FieldRegistry
from companion_v2.execution.models import digest
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.integration.life_activity_control_proxy import LifeActivityCreationProxy, LifeActivityTransitionProxy
from companion_v2.integration.life_event_provider_proxy import LifeEventProviderProxy
from companion_v2.integration.life_ledger_proxy import LifeLedgerReadProxy
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.integration.reading_handoff import ReadingStartHandoff
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.model_gateway.models import ProviderResponse
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.life.reading_queue import queue_key
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.reader import book_choice_profile, reading_profile
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from good_life.activity_import import stage_activities
from good_life.exposure_import import stage_exposures
from good_life.activity_records import ActivityTransitionPending
from good_life.metadata_import import stage_metadata
from good_life.prepared_commands import PreparedCommands
from good_life.runtime import LifeRuntime
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage
from rehearse_activity_rollback import rehearse as rehearse_activity_rollback
from rehearse_reading_markers_rollback import rehearse as rehearse_marker_rollback
from test_manual_sleep_handoff import LocalLease
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture
from tests.test_role_tool_loop import answered


async def bound_default_scheduler(provider, api, token):
    """Exercise production construction; keep test clocks in manual control."""
    from unittest.mock import patch
    api.unregister_life_provider(token)
    with patch.object(DueSleepScheduler, "start"):
        await provider.bind(api)
    if provider.scheduler.reading_scheduler.executor is not None:
        raise AssertionError("default LifeRuntime must use the untouched legacy scheduler")
    return provider.scheduler


class ReadingLifeExecutionTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (reading_profile("primary", max_output_tokens=100), book_choice_profile("primary", max_output_tokens=100))
        self.registry = FieldRegistry(version="life-reading-tests", fields=configuration_fields(self.profiles))

    async def test_cancel_reading_keeps_following_span_and_stops_active_worker(self):
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        self.grant(context.actor, context.scope, "role.reading.record")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        status = manager.status(self.core)
        result = await manager.command(self.core, {"action": "configure", "operation_key": "enable-cancel-test",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False, "reading_enabled": True, "reading_chunk_chars": 200}})
        self.assertTrue(result.is_success, result)
        routine.refresh()
        text = "原文" * 305
        source = SourceRef("book-cancel", "library", context.scope, VersionRef("book-cancel", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
        activity = RoleActivity("reading-cancel", routine.owner.persona_id, context.scope, NOW, RoleActivityState.PLANNED,
            description="book · 1–610 / 610 字符",
            work=RoleWorkSpec("reading", context.key, "read", source, digest(text), 0, len(text)))
        app.role_life.add_activity(activity)
        previous = replace(activity, activity_id="reading-prior-session",
                           work=replace(activity.work, context_key="previous-session"))
        app.role_life.add_activity(previous)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, '1')", (queue_key(context.key, source),))
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        directory = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        provider = LifeRuntime(life, plugin)
        original_control = provider.handlers["activity_control"]
        interrupted = [False]
        def control(operation, *args, **kwargs):
            value = original_control(operation, *args, **kwargs)
            if (operation == "activity_transition.resolve" and args[0]["command_id"].startswith("activity-cancel-reading-")
                    and interrupted[0]):
                interrupted[0] = False
                raise ValueError("simulated interruption after reading cancellation commit")
            return value
        provider.handlers["activity_control"] = control
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        scheduler = await bound_default_scheduler(provider, api, token)
        provider.scheduler = scheduler
        self.addAsyncCleanup(scheduler.close)
        worker = asyncio.create_task(asyncio.Event().wait())
        scheduler.reading_scheduler.workers["previous-session"] = worker
        scheduler.reading_scheduler.activity_ids["previous-session"] = previous.activity_id
        previous_cancel = {"action": "cancel", "activity_id": previous.activity_id, "expected_revision": 1,
            "operation_key": "cancel-prior-session", "view_token": manager.token(self.core, manager.authorize(self.core),
                                                                                   previous.activity_id, 1)}
        self.assertTrue((await manager.command(self.core, previous_cancel)).is_success)
        await asyncio.gather(worker, return_exceptions=True)
        self.assertTrue(worker.cancelled())
        self.assertEqual(app.role_life.activity(previous.activity_id, owner=routine.owner).state, RoleActivityState.CANCELLED)
        cancel = {"action": "cancel", "activity_id": activity.activity_id, "expected_revision": 1,
                  "operation_key": "cancel-planned", "view_token": manager.token(self.core, manager.authorize(self.core), activity.activity_id, 1)}
        result = await manager.command(self.core, cancel)
        self.assertTrue(result.is_success, result)
        current = app.role_life.activity(activity.activity_id, owner=routine.owner)
        self.assertEqual((current.state, current.work.end_offset), (RoleActivityState.CANCELLED, 200))
        following = next(item for item in app.role_life.activities_for_role(routine.owner, limit=100)
                         if item.state is RoleActivityState.PLANNED)
        self.assertEqual(following.work.start_offset, 200)
        manual = {"action": "read", "activity_id": following.activity_id, "expected_revision": 1,
                  "operation_key": "read-after-cancel",
                  "view_token": manager.token(self.core, manager.authorize(self.core), following.activity_id, 1)}
        self.assertTrue((await manager.command(self.core, manual)).is_success)
        started, stopped = asyncio.Event(), asyncio.Event()
        async def waiting_model(_env):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
        self.model_behavior = waiting_model
        await scheduler.tick(now=self.clock.now())
        await asyncio.wait_for(started.wait(), timeout=5)
        current = app.role_life.activity(following.activity_id, owner=routine.owner)
        self.assertEqual((current.state, current.work.start_offset, current.work.end_offset), (RoleActivityState.ACTIVE, 200, 400))
        cancel = {"action": "cancel", "activity_id": current.activity_id, "expected_revision": current.revision,
                  "operation_key": "cancel-active",
                  "view_token": manager.token(self.core, manager.authorize(self.core), current.activity_id, current.revision)}
        interrupted[0] = True
        result = await manager.command(self.core, cancel)
        self.assertFalse(result.is_success)
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1, scheduler.last_failure)
        await asyncio.wait_for(stopped.wait(), timeout=5)
        await asyncio.gather(scheduler.reading_scheduler.workers[context.key], return_exceptions=True)
        current = app.role_life.activity(following.activity_id, owner=routine.owner)
        self.assertEqual(current.state, RoleActivityState.CANCELLED)
        pending = next(item for item in app.role_life.activities_for_role(routine.owner, limit=100)
                       if item.state is RoleActivityState.PLANNED)
        self.assertEqual(pending.work.start_offset, 400)
        source_row = self.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id=?", (source.source_id,))
        app.role_journal.sources.revoke(source, expected_governance_revision=source_row["governance_revision"],
                                        observed_at=self.clock.now()).unwrap()
        cancel = {"action": "cancel", "activity_id": pending.activity_id, "expected_revision": pending.revision,
                  "operation_key": "cancel-withdrawn",
                  "view_token": manager.token(self.core, manager.authorize(self.core), pending.activity_id, pending.revision)}
        result = await manager.command(self.core, cancel)
        self.assertTrue(result.is_success, result)
        self.assertFalse(any(item.state is RoleActivityState.PLANNED
                             for item in app.role_life.activities_for_role(routine.owner, limit=100)))
        self.assertEqual(len(life.pending_events("interest")), 0)
        self.assertGreaterEqual(rehearse_activity_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                                          expected_version=version)["activities"], 3)
        self.assertGreaterEqual(rehearse_marker_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                                         expected_version=version)["markers"], 1)
        self.assertEqual(len(self.sent), 0)

    async def test_supervised_reading_commits_original_span_and_next_queue_in_life_store(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        self.grant(context.actor, context.scope, "role.reading.record")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        status = manager.status(self.core)
        configured = await manager.command(self.core, {"action": "configure", "operation_key": "enable-reading",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False, "reading_enabled": True, "reading_chunk_chars": 200}})
        self.assertTrue(configured.is_success, configured)
        routine.refresh()
        text = "原文" * 305
        source = SourceRef("book", "library", context.scope, VersionRef("book", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
        activity = RoleActivity("reading", routine.owner.persona_id, context.scope, NOW, RoleActivityState.PLANNED,
            description="book · 1–610 / 610 字符",
            work=RoleWorkSpec("reading", context.key, "read", source, digest(text), 0, len(text)))
        app.role_life.add_activity(activity)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, '1')", (queue_key(context.key, source),))
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        directory = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        lease = LocalLease(life)
        app.role_life.bind_reader(LifeLedgerReadProxy(self.runtime, lease),
            creation_provider=LifeActivityCreationProxy(lease), transition_provider=LifeActivityTransitionProxy(lease),
            events_provider=LifeEventProviderProxy(self.runtime, lease))
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        started = await ReadingStartHandoff(app, routine, LifeActivityTransitionProxy(lease)).execute(
            activity.activity_id, expected_revision=1, request_id="reading-run")
        self.assertEqual((started.state, started.work.start_offset, started.work.end_offset), (RoleActivityState.ACTIVE, 0, 200))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"reflection":"片段的读后感"}'))
        app.calendar.prepare_life_snapshot_read()
        with self.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            result = await app.role_reader.consume(context, started, role_instruction=routine.instruction, life=True)
        self.assertTrue(result.is_success, result)
        self.assertEqual(result.unwrap().task.state, "succeeded", result)
        completed = app.role_life.activity(activity.activity_id, owner=routine.owner)
        self.assertEqual((completed.state, completed.reading.start_offset, completed.reading.end_offset,
                          completed.reading.reflection), (RoleActivityState.COMPLETED, 0, 200, "片段的读后感"))
        activities = app.role_life.activities_for_role(routine.owner, limit=100)
        following = next(item for item in activities if item.activity_id != activity.activity_id)
        self.assertEqual((following.state, following.work.start_offset, following.work.end_offset),
                         (RoleActivityState.PLANNED, 200, len(text)))
        consumers = {row[0] for row in life._connection.execute("SELECT consumer FROM life_event_deliveries")}
        self.assertEqual(consumers, {"journal", "interest"})
        self.assertEqual(len(self.called), 1)
        self.clock.advance(timedelta(hours=1))
        started_again = await ReadingStartHandoff(app, routine, LifeActivityTransitionProxy(lease)).execute(
            following.activity_id, expected_revision=1, request_id="reading-run-2")
        lease.interrupt_wake_after_resolve = True
        with self.runtime.coordinated_snapshot(app.role_life.snapshot_factory()):
            interrupted = await app.role_reader.consume(context, started_again, role_instruction=routine.instruction, life=True)
        self.assertNotEqual(interrupted.unwrap().task.state, "succeeded")
        pending = next(row for row in PreparedCommands(life).pending() if row["command_id"].startswith("activity-reading-complete-"))
        self.assertEqual(pending["state"], "committed")
        with self.assertRaises(ActivityTransitionPending):
            LifeActivityTransitionProxy(lease).reading_metadata(context_key=context.key)
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
        app.role_life.unbind_reader(app.role_life._reader)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        provider = LifeRuntime(life, plugin, wall_clock=self.clock.now,
                               monotonic_clock=lambda: (self.clock.now() - NOW).total_seconds())
        original_control = provider.handlers["activity_control"]
        interrupt_manual_ack = [False]
        def activity_control(operation, *args, **kwargs):
            result = original_control(operation, *args, **kwargs)
            if operation == "activity_transition.acknowledge" and interrupt_manual_ack[0]:
                interrupt_manual_ack[0] = False
                raise ValueError("simulated interruption after the manual start was acknowledged")
            return result
        provider.handlers["activity_control"] = activity_control
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        scheduler = await bound_default_scheduler(provider, api, token)
        self.addAsyncCleanup(scheduler.close)
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        recovered = app.role_life.activity(following.activity_id, owner=routine.owner)
        self.assertEqual((recovered.state, recovered.reading.start_offset, recovered.reading.end_offset),
                         (RoleActivityState.COMPLETED, 200, 400))
        last = next(item for item in app.role_life.activities_for_role(routine.owner, limit=100)
                    if item.state is RoleActivityState.PLANNED)
        self.assertEqual((last.work.start_offset, last.work.end_offset), (400, len(text)))
        self.assertEqual(len(self.called), 2)
        self.clock.advance(timedelta(hours=1))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(
            '{"choice":"1","reason":"想继续读"}' if len(self.called) == 3 else '{"reflection":"最后一段的感想"}'))
        await scheduler.tick(now=self.clock.now())
        choosing = scheduler.reading_scheduler.workers.get(context.key)
        self.assertIsNotNone(choosing, scheduler.last_failure)
        await choosing
        await scheduler.tick(now=self.clock.now())
        reading = scheduler.reading_scheduler.workers.get(context.key)
        self.assertIsNotNone(reading, scheduler.last_failure)
        await reading
        await scheduler.tick(now=self.clock.now())
        final = app.role_life.activity(last.activity_id, owner=routine.owner)
        self.assertEqual((final.state, final.reading.start_offset, final.reading.end_offset, final.reading.reflection),
                         (RoleActivityState.COMPLETED, 400, 600, "最后一段的感想"))
        self.assertEqual(len(self.called), 4)
        self.clock.advance(timedelta(hours=1))
        last = next(item for item in app.role_life.activities_for_role(routine.owner, limit=100)
                    if item.state is RoleActivityState.PLANNED)
        self.assertEqual((last.work.start_offset, last.work.end_offset), (600, len(text)))
        status = manager.status(self.core)
        card = next(item for item in status["activities"] if item["activity_id"] == last.activity_id)
        self.assertEqual(card["state"], "planned")
        manual = {"action": "read", "activity_id": last.activity_id, "expected_revision": last.revision,
                  "operation_key": "manual-reading", "view_token": card["view_token"]}
        queued = await manager.command(self.core, manual)
        self.assertTrue(queued.is_success, queued)
        self.assertEqual(queued.unwrap()["state"], "scheduled")
        with self.assertRaisesRegex(ValueError, "manual reading requests"):
            rehearse_activity_rollback(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('invalid json'))
        interrupt_manual_ack[0] = True
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.reading_scheduler.workers.get(context.key)
        self.assertIsNotNone(worker, scheduler.last_failure)
        interrupted_start = await asyncio.gather(worker, return_exceptions=True)
        self.assertIsInstance(interrupted_start[0], ValueError)
        self.assertTrue((await manager.command(self.core, manual)).unwrap()["replayed"])
        self.assertEqual(len(self.called), 4)
        await scheduler.tick(now=self.clock.now())
        self.clock.advance(timedelta(seconds=31))
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.reading_scheduler.workers.get(context.key)
        self.assertIsNotNone(worker, scheduler.last_failure)
        await worker
        await scheduler.tick(now=self.clock.now())
        paused = app.role_life.activity(last.activity_id, owner=routine.owner)
        self.assertEqual((paused.state, paused.work.start_offset, paused.work.end_offset),
                         (RoleActivityState.PAUSED, 600, len(text)))
        self.assertTrue((await manager.command(self.core, manual)).unwrap()["replayed"])
        self.assertEqual(len(self.called), 5)
        status = manager.status(self.core)
        card = next(item for item in status["activities"] if item["activity_id"] == paused.activity_id)
        retry = {"action": "read", "activity_id": paused.activity_id, "expected_revision": paused.revision,
                 "operation_key": "manual-retry", "view_token": card["view_token"]}
        previous_task = app.role_reader.task_id(context, paused)
        call = self.runtime.read_one("SELECT call_id,state FROM model_calls WHERE task_id=?", (previous_task,))
        with self.runtime.transaction() as db:
            db.execute("UPDATE model_calls SET state='unknown' WHERE call_id=?", (call["call_id"],))
        self.assertEqual((await manager.command(self.core, retry)).failure.code, "life_reading_unresolved")
        with self.runtime.transaction() as db:
            db.execute("UPDATE model_calls SET state=? WHERE call_id=?", (call["state"], call["call_id"]))
        self.assertTrue((await manager.command(self.core, retry)).is_success)
        self.clock.advance(timedelta(seconds=31))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"reflection":"续读同一片段的感想"}'))
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.reading_scheduler.workers.get(context.key)
        self.assertIsNotNone(worker, scheduler.last_failure)
        await worker
        await scheduler.tick(now=self.clock.now())
        final = app.role_life.activity(paused.activity_id, owner=routine.owner)
        self.assertEqual((final.state, final.reading.start_offset, final.reading.end_offset, final.reading.reflection),
                         (RoleActivityState.COMPLETED, 600, len(text), "续读同一片段的感想"))
        self.assertEqual(len(self.called), 6)
        rollback = rehearse_marker_rollback(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)
        self.assertGreaterEqual(rollback["markers"], 3)
        self.assertEqual(len(self.sent), 0)

    async def test_approved_interrupted_reading_resumes_same_span_once(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        self.grant(context.actor, context.scope, "role.reading.record")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        status = manager.status(self.core)
        configured = await manager.command(self.core, {"action": "configure", "operation_key": "enable-reading",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False, "reading_enabled": True, "reading_chunk_chars": 200}})
        self.assertTrue(configured.is_success, configured)
        routine.refresh()
        text = "原文" * 120
        source = SourceRef("book", "library", context.scope, VersionRef("book", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
        activity = RoleActivity("reading", routine.owner.persona_id, context.scope, NOW, RoleActivityState.PLANNED,
            description="book", work=RoleWorkSpec("reading", context.key, "read", source, digest(text), 0, len(text)))
        app.role_life.add_activity(activity)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, '1')", (queue_key(context.key, source),))
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        directory = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        provider = LifeRuntime(life, plugin)
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        capabilities = api.life_host_capabilities(plugin=plugin, registration_id=token)
        started = await ReadingStartHandoff(app, routine, app.role_life.transition_provider()).execute(
            activity.activity_id, expected_revision=1, request_id="uncertain-reading")
        self.model_behavior = lambda _env: asyncio.sleep(0, result=ProviderResponse("unknown"))
        self.assertEqual((await capabilities["execute_reading"](context.key, started.activity_id, started.revision))["state"], "paused")
        paused = app.role_life.activity(activity.activity_id, owner=routine.owner)
        task_id = app.role_reader.task_id(context, paused)
        task = self.runtime.read_one("SELECT record_json,attempt_id FROM execution_tasks WHERE task_id=?", (task_id,))
        attempt = self.runtime.read_one("SELECT record_json FROM execution_attempts WHERE attempt_id=?", (task["attempt_id"],))
        task_record, attempt_record = json.loads(task["record_json"]), json.loads(attempt["record_json"])
        task_record.update(state="cancelled", effect="none")
        attempt_record["stopped_proof"] = "isolated-test-confirmed-stopped"
        with self.runtime.transaction() as db:
            db.execute("UPDATE execution_tasks SET state='cancelled',record_json=? WHERE task_id=?", (json.dumps(task_record), task_id))
            db.execute("UPDATE execution_attempts SET slot_held=0,record_json=? WHERE attempt_id=?",
                       (json.dumps(attempt_record), task["attempt_id"]))
        marker_key = "role.life.retry_interrupted:" + task_id
        with life.transaction() as db:
            db.execute("INSERT INTO staged_life_runtime_metadata VALUES (?,?)",
                       (marker_key, json.dumps({"activity_id": paused.activity_id, "revision": paused.revision})))
        scheduler = await bound_default_scheduler(provider, api, token)
        self.addAsyncCleanup(scheduler.close)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"reflection":"续读原片段"}'))
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.reading_scheduler.workers.get(context.key)
        self.assertIsNotNone(worker, scheduler.last_failure)
        await worker
        await scheduler.tick(now=self.clock.now())
        final = app.role_life.activity(activity.activity_id, owner=routine.owner)
        self.assertEqual((final.state, final.reading.start_offset, final.reading.end_offset),
                         (RoleActivityState.COMPLETED, 0, 200))
        self.assertIsNone(life._connection.execute("SELECT value FROM staged_life_runtime_metadata WHERE key=?", (marker_key,)).fetchone())
        self.assertEqual(len(self.called), 2)
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()
