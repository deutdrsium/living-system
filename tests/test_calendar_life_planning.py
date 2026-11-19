import asyncio
from contextlib import closing
from datetime import timedelta
import json
import sqlite3
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
from audit_calendar_ownership import adoption_plan
from companion_v2.agenda.calendar import LIFE_CALENDAR_OWNER_KEY
from companion_v2.agenda.planning import planning_profile
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.integration.calendar_adoption_guard import CERTIFICATE_KEY
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.integration import manual_calendar_queue
from companion_v2.identity_policy.interventions import CALENDAR_INTERVENTION
from companion_v2.role_work.state.service import LIFE_STATE_OWNER_KEY
from companion_v2.model_gateway.routing import configuration_fields
from good_life.activity_import import stage_activities
from good_life.calendar_adoption import adopt_staged_calendar
from good_life.calendar_import import stage_calendar
from good_life.exposure_import import stage_exposures
from good_life.metadata_import import stage_metadata
from good_life.prepared_commands import PreparedCommands
from good_life.runtime import LifeRuntime
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.state_import import stage_state
from good_life.storage import LifeStorage
from rehearse_calendar_rollback import rehearse as rehearse_calendar_rollback
from rehearse_life_rollback import rehearse as rehearse_life_rollback
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, state_data
from tests.test_role_tool_loop import answered
from verify_calendar_adoption import verify
from verify_exposure_authority import verify_contexts


class CalendarLifePlanningTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (planning_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="life-planning-tests", fields=configuration_fields(self.profiles))

    async def test_supervised_planning_recovers_one_atomic_life_batch(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True, planning_enabled=True)
        app.calendar_planning.next_checks[context.key] = NOW + timedelta(days=1)
        await self.start(host)
        instruction = "test persona"
        app.state_maintenance.bind(context, instruction)
        app.life_runtime.bind(context, instruction)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        plan = adoption_plan(self.runtime.database_path, expected_version=version)
        directory = Path(temporary.name) / "life"
        stage_calendar(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        adopt_staged_calendar(life, plan)
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_state(self.runtime.database_path, expected_version=version, destination=directory)
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory, validate_source=verify_contexts)
        certificate = verify(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)["certificate"]
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        provider = LifeRuntime(life, plugin)
        original = provider.handlers["calendar_command"]
        interrupted = [True]
        def calendar_command(operation, *args, **kwargs):
            result = original(operation, *args, **kwargs)
            if operation == "batch.resolve" and interrupted[0]:
                interrupted[0] = False
                raise ValueError("simulated interruption after batch commit")
            return result
        provider.handlers["calendar_command"] = calendar_command
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_CALENDAR_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_STATE_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, ?)", (CERTIFICATE_KEY, json.dumps(certificate)))
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"items": [
            {"title": "First plan", "start": (NOW + timedelta(hours=2)).isoformat(), "minutes": 30, "detail": "First"},
            {"title": "Second plan", "start": (NOW + timedelta(hours=4)).isoformat(), "minutes": 30, "detail": "Second"}]})))
        capabilities = api.life_host_capabilities(plugin=plugin, registration_id=token)
        capabilities.pop("state_run")  # State writes below are explicit in this combined rollback test.
        scheduler = DueSleepScheduler(life, capabilities)
        self.addAsyncCleanup(scheduler.close)
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.calendar_scheduler.worker
        self.assertIsNotNone(worker, scheduler.last_failure)
        report = await worker
        self.assertNotEqual(report["state"], "succeeded")
        pending = next(item for item in PreparedCommands(life).pending() if item["kind"] == "calendar.batch")
        self.assertEqual(pending["state"], "committed")
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        saved = app.calendar.inventory(context, limit=100)["items"]
        self.assertEqual({item["title"] for item in saved}, {"First plan", "Second plan"})
        operation = self.runtime.read_one("SELECT state FROM operations WHERE operation_id=?", (report["task_id"],))
        self.assertEqual(operation["state"], "completed")
        self.assertEqual(len(self.called), 1)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"items": [
            {"title": "Manual plan", "start": (NOW + timedelta(hours=6)).isoformat(), "minutes": 30, "detail": "Manual"}]})))
        operation_id = manual_calendar_queue.enqueue(app, context, mode="daily", slot="manual:daily:synthetic", refresh=False)
        await scheduler.tick(now=self.clock.now())
        manual_worker = scheduler.calendar_scheduler.worker
        self.assertIsNotNone(manual_worker)
        self.assertEqual((await manual_worker)["state"], "succeeded")
        self.assertEqual((await manual_calendar_queue.wait(app, operation_id, timeout=1))["state"], "succeeded")
        self.assertEqual(manual_calendar_queue.enqueue(app, context, mode="daily", slot="manual:daily:synthetic", refresh=False), operation_id)
        self.assertEqual(len(self.called), 2)
        self.grant(context.actor, context.scope, CALENDAR_INTERVENTION)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        stale = manual_calendar_queue.enqueue(app, context, mode="intervention", slot="intervention:stale",
                                              refresh=False, expected_input_epoch=epoch)
        app.state_maintenance.note_input(context, "new-message")
        await scheduler.tick(now=self.clock.now())
        self.assertEqual((await scheduler.calendar_scheduler.worker)["code"], "planning_inputs_changed")
        self.assertEqual((await manual_calendar_queue.wait(app, stale, timeout=1))["state"], "failed")
        self.assertEqual(len(self.called), 2)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        restarted = manual_calendar_queue.enqueue(app, context, mode="intervention", slot="intervention:restart",
                                                  refresh=False, expected_input_epoch=epoch)
        app.state_maintenance._intervention_generation = "restarted-state-maintenance"
        await scheduler.tick(now=self.clock.now())
        self.assertEqual((await scheduler.calendar_scheduler.worker)["code"], "planning_inputs_changed")
        self.assertEqual((await manual_calendar_queue.wait(app, restarted, timeout=1))["state"], "failed")
        self.assertEqual(len(self.called), 2)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"changes":[]}'))
        valid = manual_calendar_queue.enqueue(app, context, mode="intervention", slot="intervention:current",
                                              refresh=False, expected_input_epoch=epoch)
        await scheduler.tick(now=self.clock.now())
        self.assertEqual((await scheduler.calendar_scheduler.worker)["state"], "succeeded")
        self.assertEqual((await manual_calendar_queue.wait(app, valid, timeout=1))["state"], "succeeded")
        self.assertEqual(len(self.called), 3)
        target = Path(temporary.name) / "calendar-rollback.sqlite3"
        restored = rehearse_calendar_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                              expected_version=version, destination=target)
        self.assertEqual((restored["state"], restored["records"]), ("rehearsed", 3))
        with closing(sqlite3.connect(target)) as db:
            titles = {json.loads(row[0])["title"] for row in db.execute("SELECT record_json FROM calendar_records")}
        self.assertEqual(titles, {"First plan", "Second plan", "Manual plan"})
        await scheduler.close()
        app.role_state.write(context, data=state_data(energy=57, hunger="正常"), expected_revision=0,
                             operation_key="combined-rollback-state")
        host_snapshot = Path(temporary.name) / "host-snapshot.sqlite3"
        life_snapshot = Path(temporary.name) / "life-snapshot.sqlite3"
        with closing(sqlite3.connect(self.runtime.database_path)) as source, closing(sqlite3.connect(host_snapshot)) as backup:
            source.backup(backup)
        with life._lock, closing(sqlite3.connect(life_snapshot)) as backup:
            life._connection.backup(backup)
        combined = Path(temporary.name) / "combined-rollback.sqlite3"
        restored = rehearse_life_rollback(host_snapshot, life_snapshot, expected_version=version, destination=combined)
        self.assertEqual((restored["state"], restored["ownership_changed"]), ("rehearsed", False))
        with closing(sqlite3.connect(combined)) as db:
            titles = {json.loads(row[0])["title"] for row in db.execute("SELECT record_json FROM calendar_records")}
            state = json.loads(db.execute("SELECT record_json FROM role_state_records WHERE record_id='current'").fetchone()[0])
        self.assertEqual(titles, {"First plan", "Second plan", "Manual plan"})
        self.assertEqual(state["values"]["energy"], 57)
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()
