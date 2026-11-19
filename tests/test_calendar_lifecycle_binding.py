import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
pytest.importorskip("astrbot")

from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_ledger_binding import refresh_session_binding
from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID
from audit_calendar_ownership import adoption_plan
from companion_v2.agenda.calendar import LIFE_CALENDAR_OWNER_KEY
from companion_v2.adapters.astrbot.application import optional_life_snapshot
from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.integration.calendar_adoption_guard import CERTIFICATE_KEY
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.integration import state_intervention_queue
from companion_v2.identity_policy.interventions import STATE_INTERVENTION
from companion_v2.role_work.state.service import LIFE_STATE_OWNER_KEY
from companion_v2.role_work.state.ownership import role_owner
from companion_v2.role_work.state.management import StateManagement
from companion_v2.identity_policy.private_domain import source_ref
from companion_v2.storage.versioned_records import VersionedRecords
from good_life.calendar_adoption import adopt_staged_calendar
from good_life.activity_control import ActivityControl
from good_life.activity_import import stage_activities
from good_life.activity_records import StagedActivityRecords
from good_life.calendar_import import stage_calendar
from good_life.calendar_records import StagedCalendarRecords
from good_life.event_delivery import LifeEventDelivery
from good_life.prepared_commands import PreparedCommands
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.state_import import stage_state
from good_life.state_records import StagedStateRecords
from good_life.state_commands import StagedStateCommands
from good_life.state_scheduler import StateSchedules
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.storage import LifeStorage
from rehearse_state_rollback import rehearse as rehearse_state_rollback
from verify_calendar_adoption import verify
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, calendar_data, state_data
from tests.test_role_tool_loop import answered


class CalendarLifecycleBindingTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_lifecycle(self, *, with_reminder=False):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        await self.start(host)
        data = calendar_data()
        app.calendar.command(context, identifier="event", action="create", expected_revision=0,
                             operation_key="original", data=data)
        if with_reminder:
            self.grant(context.actor, context.scope, "reminder.deliver")
            self.grant(context.actor, context.scope, "reminder.create")
            app.calendar.remind(context, identifier="event", expected_revision=1, operation_key="remind")
        old_records = VersionedRecords(self.runtime, "calendar")
        for identifier, reminder in (("adopted", None), ("pinned", "old-reminder")):
            old_records.change(context, identifier, operation_key=identifier, expected_revision=0,
                parameters=[identifier], at=NOW, transform=lambda _current, reminder=reminder: {
                    **data, "state": "accepted", "sources": [], "exceptions": {}, "details": {},
                    "accepted_by": [], "changed_by": context.role_binding.role, "reminder_id": reminder})
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        plan = adoption_plan(self.runtime.database_path, expected_version=version)
        directory = Path(temporary.name) / "life"
        stage_calendar(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        adopt_staged_calendar(life, plan)
        PreparedCommands(life).install()
        records, commands = StagedCalendarRecords(life), StagedCalendarCommands(life)
        certificate = verify(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)["certificate"]
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]),
                                     _terminated=False, session=None)
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_memory_providers)
        self.addCleanup(api.close_life_providers)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["calendar_read"], handlers["calendar_command"] = records.dispatch, commands.dispatch
        handlers["activity_control"] = ActivityControl(life).dispatch
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_CALENDAR_OWNER_KEY,))
        return app, context, data, controller, api, plugin, handlers, certificate

    def certificate(self, value):
        with self.runtime.transaction() as db:
            db.execute("INSERT OR REPLACE INTO runtime_metadata VALUES (?, ?)", (CERTIFICATE_KEY, json.dumps(value)))

    def register(self, api, plugin, handlers, **kwargs):
        return api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=handlers, **kwargs)

    async def test_intake_reads_calendar_from_the_same_pinned_life_snapshot(self):
        app, context, data, controller, api, plugin, handlers, certificate = await self.setup_lifecycle()
        life = handlers["calendar_command"].__self__.storage
        handlers["activity_history"] = LifeEventDelivery(life).dispatch
        self.certificate(certificate)
        controller.session = SimpleNamespace(application=app)
        token = self.register(api, plugin, handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        with self.runtime.transaction():
            with self.assertRaises(TaskError):
                app.calendar.read(context, "event")
        with optional_life_snapshot(app), self.runtime.transaction():
            self.assertEqual(app.calendar.read(context, "event")["revision"], 1)
            with self.assertRaises(TaskError):
                app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                     operation_key="inside-host-transaction", data=data)
            self.assertTrue(api.unregister_life_provider(token))
            with self.assertRaises(TaskError):
                app.calendar.read(context, "event")

    async def test_imported_state_is_read_and_sealed_with_the_life_snapshot(self):
        app, context, _, controller, api, plugin, handlers, certificate = await self.setup_lifecycle()
        app.role_state.write(context, data=state_data(energy=48, hunger="有点饿", health="状态平稳"),
                             expected_revision=0, operation_key="original-state")
        before = app.role_state.read(context)
        original = app.role_state.records.get(context, "current")
        life = handlers["calendar_command"].__self__.storage
        directory = Path(life._connection.execute("PRAGMA database_list").fetchone()[2]).parent
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_state(self.runtime.database_path, expected_version=version, destination=directory)
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        handlers["state_read"] = StagedStateRecords(life).dispatch
        schedules = StateSchedules(life)
        schedules.install()
        force_interrupted = [False]
        def state_schedule(operation, **kwargs):
            result = schedules.dispatch(operation, **kwargs)
            if operation == "force_due" and force_interrupted[0]:
                force_interrupted[0] = False
                raise TaskError("test_manual_interrupted", "simulated interruption after scheduling")
            return result
        handlers["state_schedule"] = state_schedule
        commands = StagedStateCommands(life)
        interrupted = [True]
        def state_command(operation, *args, **kwargs):
            result = commands.dispatch(operation, *args, **kwargs)
            if operation == "resolve" and interrupted[0]:
                interrupted[0] = False
                raise TaskError("test_interrupted", "simulated interruption after life commit")
            return result
        handlers["state_command"] = state_command
        handlers["activity_read"] = StagedActivityRecords(life).dispatch
        handlers["activity_history"] = LifeEventDelivery(life).dispatch
        self.certificate(certificate)
        controller.session = SimpleNamespace(application=app)
        token = self.register(api, plugin, handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_STATE_OWNER_KEY,))
        with self.runtime.transaction():
            with self.assertRaises(TaskError):
                app.role_state.records.get(context, "current")
        with optional_life_snapshot(app), self.runtime.transaction():
            self.assertEqual(app.role_state.read(context), before)
            captured = app.messages.role_state_context.capture(context, self.clock.now())
            self.assertEqual((captured["revision"], captured["storage"]), (1, "role"))
            self.assertEqual(app.role_state.records.version(context, "current", 1), original)
            consumer = app.messages.role_state_context
            materials = consumer.contributions(captured, context, self.clock.now())
            self.assertEqual(consumer.instruction(materials), consumer.instruction(
                materials, life_records=app.role_state.records, context=context))
        original_replay = app.role_state.write(context, data=state_data(energy=48, hunger="有点饿", health="状态平稳"),
                                               expected_revision=0, operation_key="original-state")
        self.assertTrue(original_replay["replayed"])
        manager = StateManagement(app, context)
        self.configure(manager, mood_enabled=True, cycle_enabled=True, cycle_initialization="first_day")
        mood = {"baseline": 6, "joy": 6, "anger": 1, "sorrow": 1, "fear": 1,
                "intensity": "mild", "description": "平常"}
        payload = state_data(energy=52, hunger="吃饱了", health="状态平稳", mood=mood)
        with self.assertRaises(TaskError) as interrupted_write:
            app.role_state.write(context, data=payload, expected_revision=1, operation_key="state-update",
                                 projected_inputs=True, dialogue_through=self.clock.now())
        self.assertEqual(interrupted_write.exception.code, "test_interrupted")
        with self.assertRaises(TaskError):
            app.role_state.records.get(context, "current")
        with self.assertRaisesRegex(ValueError, "not yet resolved"):
            rehearse_state_rollback(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)
        self.assertTrue(api.unregister_life_provider(token))
        token = self.register(api, plugin, handlers)
        app.life_runtime.bind(context, "test persona")
        host = api.life_host_capabilities(plugin=plugin, registration_id=token)
        async def unused(*_args):
            self.fail("sleep execution is outside this test")
        scheduler = DueSleepScheduler(life, {"api_version": "companion.life-host.v1",
            "contexts": lambda: [], "calendar_contexts": host["calendar_contexts"],
            "wake": unused, "recover": unused, "recover_state": host["recover_state"]})
        self.addAsyncCleanup(scheduler.close)
        with patch.object(scheduler.records, "dispatch", return_value=()):
            self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        updated = app.role_state.write(context, data=payload, expected_revision=1, operation_key="state-update",
                                       projected_inputs=True, dialogue_through=self.clock.now())
        self.assertTrue(updated["replayed"])
        self.assertEqual(updated["revision"], 2)
        with self.assertRaises(TaskError) as conflict:
            app.role_state.write(context, data=state_data(energy=10), expected_revision=1, operation_key="state-update")
        self.assertEqual(conflict.exception.code, "idempotency_key_conflict")
        self.assertEqual(app.role_state.read(context)["values"]["energy"], 52)
        self.assertEqual(app.role_state.records.dialogue_cursor(context)["through"], stamp(self.clock.now()))
        self.assertEqual(app.role_state.records.history_at(context, "current", stamp(self.clock.now()))["revision"], 2)
        status = manager.status(self.core).unwrap()
        body = {"action": "state.write", "operation_key": "state-from-page", "expected_revision": 2,
                "view_token": status["state"]["view_token"], "data": state_data(energy=55, hunger="不饿", mood=mood)}
        stale = manager.command(self.core, {**body, "operation_key": "bad-token", "view_token": "bad"})
        self.assertEqual(stale.failure.code, "state_view_stale")
        self.assertEqual(manager.command(self.core, body).unwrap()["revision"], 3)
        self.assertTrue(manager.command(self.core, body).unwrap()["replayed"])
        status = manager.status(self.core).unwrap()
        alignment = {"action": "cycle.align", "operation_key": "align-from-page", "expected_revision": 3,
                     "view_token": status["state"]["view_token"], "settings_token": status["cycle_settings_token"],
                     "data": {"applicability": "applicable", "day": 5}}
        self.assertEqual(manager.command(self.core, alignment).unwrap()["revision"], 4)
        self.assertTrue(manager.command(self.core, alignment).unwrap()["replayed"])
        current = app.role_state.records.get(context, "current")
        self.assertEqual(current["cycle_clock"]["applicability"], "applicable")
        self.assertEqual(current["mood_dynamics"]["axes"]["joy"], 6)
        app.state_maintenance.bind(context, "test persona")
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(encode(state_data(energy=62, hunger="正常"))))
        calls_before = len(self.called)
        maintained = await app.state_maintenance.run_life(context, "test persona", "life-maintenance")
        self.assertEqual(maintained["state"], "succeeded", maintained)
        self.assertEqual(app.role_state.read(context)["values"]["energy"], 62)
        self.assertTrue((await app.state_maintenance.run_life(context, "test persona", "life-maintenance"))["replayed"])
        self.assertEqual(len(self.called), calls_before + 1)
        async def ownership_changed(_env):
            with self.runtime.transaction() as db:
                db.execute("UPDATE runtime_metadata SET value='legacy' WHERE key=?", (LIFE_STATE_OWNER_KEY,))
            return answered(encode(state_data(energy=99)))
        self.model_behavior = ownership_changed
        rejected = await app.state_maintenance.run_life(context, "test persona", "owner-change")
        self.assertNotEqual(rejected["state"], "succeeded")
        with self.runtime.transaction() as db:
            db.execute("UPDATE runtime_metadata SET value='good_life' WHERE key=?", (LIFE_STATE_OWNER_KEY,))
        self.assertEqual(app.role_state.read(context)["values"]["energy"], 62)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(encode(state_data(energy=63, hunger="正常"))))
        calls_before = len(self.called)
        host = api.life_host_capabilities(plugin=plugin, registration_id=token)
        patrol_snapshot = host["state_patrol_snapshot"](context.key, digest(role_owner(context)))
        self.assertEqual(patrol_snapshot["revision"], app.role_state.read(context)["revision"])
        timer = DueSleepScheduler(life, {"api_version": "companion.life-host.v1", "contexts": lambda: (),
            "wake": unused, "recover": unused, "state_bindings": host["state_bindings"],
            "state_preflight": host["state_preflight"], "state_run": host["state_run"],
            "state_patrol_snapshot": host["state_patrol_snapshot"],
            "state_recover_manual": host["state_recover_manual"],
            "state_intervention_pending": host["state_intervention_pending"],
            "state_intervention_run": host["state_intervention_run"]})
        self.addAsyncCleanup(timer.close)
        await timer.tick(now=self.clock.now())
        self.assertIsNotNone(timer.state_scheduler.worker, timer.last_failure)
        await timer.state_scheduler.worker
        await timer.tick(now=self.clock.now())
        self.assertEqual(app.role_state.read(context)["values"]["energy"], 63)
        self.assertEqual(manager.status(self.core).unwrap()["maintenance"]["result"]["state"], "succeeded")
        self.assertEqual(len(self.called), calls_before + 1)
        status = manager.status(self.core).unwrap()
        manual = {"action": "maintain", "operation_key": "manual-life-state", "expected_revision": 6,
                  "view_token": status["state"]["view_token"]}
        force_interrupted[0] = True
        self.assertEqual(manager.command(self.core, manual).failure.code, "test_manual_interrupted")
        with self.assertRaisesRegex(ValueError, "manual state maintenance requests"):
            rehearse_state_rollback(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(encode(state_data(energy=64, hunger="正常"))))
        await timer.tick(now=self.clock.now())
        self.assertIsNotNone(timer.state_scheduler.worker, timer.last_failure)
        await timer.state_scheduler.worker
        await timer.tick(now=self.clock.now())
        self.assertEqual(app.role_state.read(context)["values"]["energy"], 64)
        self.assertTrue(manager.command(self.core, manual).unwrap()["replayed"])
        self.assertEqual(len(self.called), calls_before + 2)
        self.grant(context.actor, context.scope, STATE_INTERVENTION)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        state_intervention_queue.enqueue(app, context, "stale-state-intervention", epoch)
        with self.assertRaisesRegex(ValueError, "state intervention requests"):
            rehearse_state_rollback(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)
        app.state_maintenance.note_input(context, "new-message")
        await timer.tick(now=self.clock.now())
        self.assertEqual((await timer.state_scheduler.worker)["code"], "state_inputs_changed")
        await timer.tick(now=self.clock.now())
        self.assertEqual(len(self.called), calls_before + 2)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        state_intervention_queue.enqueue(app, context, "restart-state-intervention", epoch)
        app.state_maintenance._intervention_generation = "restarted-state-maintenance"
        await timer.tick(now=self.clock.now())
        self.assertEqual((await timer.state_scheduler.worker)["code"], "state_inputs_changed")
        await timer.tick(now=self.clock.now())
        self.assertEqual(len(self.called), calls_before + 2)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        state_intervention_queue.enqueue(app, context, "current-state-intervention", epoch)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(encode(state_data(energy=65, hunger="正常"))))
        await timer.tick(now=self.clock.now())
        self.assertEqual((await timer.state_scheduler.worker)["state"], "succeeded")
        await timer.tick(now=self.clock.now())
        self.assertEqual(app.role_state.read(context)["values"]["energy"], 65)
        self.assertEqual(len(self.called), calls_before + 3)
        await timer.close()
        timer = DueSleepScheduler(life, {"api_version": "companion.life-host.v1", "contexts": lambda: (),
            "wake": unused, "recover": unused, "state_bindings": host["state_bindings"],
            "state_preflight": host["state_preflight"], "state_run": host["state_run"],
            "state_recover_manual": host["state_recover_manual"]})
        self.addAsyncCleanup(timer.close)
        await timer.tick(now=self.clock.now())
        self.assertIsNone(timer.state_scheduler.worker)
        self.assertEqual(len(self.called), calls_before + 3)
        copy = directory.parent / "state-rollback.sqlite3"
        rollback = rehearse_state_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                          expected_version=version, destination=copy)
        self.assertEqual((rollback["state"], rollback["revisions"]), ("rehearsed", 8))
        db = sqlite3.connect(copy)
        try:
            raw = db.execute("SELECT record_json FROM role_state_records WHERE owner_key=? AND record_id='current'",
                             (app.role_state.records.owner_key(context),)).fetchone()[0]
        finally:
            db.close()
        self.assertEqual(json.loads(raw)["values"]["energy"], 65)
        with patch.object(app.state_maintenance, "patrol") as old_patrol:
            app.state_maintenance.advance()
            old_patrol.assert_not_called()
        with self.assertRaises(TaskError):
            await app.state_maintenance.run(context, "test persona", "must-not-run")
        latest = app.role_state.records.get(context, "current")
        self.revoke_source(app, source_ref(latest["sources"][0]))
        with optional_life_snapshot(app):
            with self.assertRaises(TaskError):
                app.role_state.read(context)
        self.assertTrue(api.unregister_life_provider(token))
        with self.assertRaises(TaskError):
            app.role_state.records.get(context, "current")

    async def test_existing_calendar_reminder_is_delivered_from_life_snapshot(self):
        app, context, _, controller, api, plugin, handlers, certificate = await self.setup_lifecycle(with_reminder=True)
        life = handlers["calendar_command"].__self__.storage
        handlers["activity_history"] = LifeEventDelivery(life).dispatch
        self.certificate(certificate)
        controller.session = SimpleNamespace(application=app)
        token = self.register(api, plugin, handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        self.clock.advance(timedelta(hours=1))
        self.assertTrue(api.unregister_life_provider(token))
        app.agenda_background.advance().unwrap()
        self.assertEqual(self.runtime.read_one("SELECT COUNT(*) AS n FROM agenda_dispatches", ())["n"], 0)
        token = self.register(api, plugin, handlers)
        app.agenda_background.advance().unwrap()
        self.assertTrue(api.unregister_life_provider(token))
        app.background.advance(planners=False)
        self.assertEqual(len(self.sent), 0)
        token = self.register(api, plugin, handlers)
        app.background.advance(planners=False)
        self.assertTrue(app.background._workers)
        self.assertTrue(api.unregister_life_provider(token))
        await asyncio.gather(*tuple(app.background._workers.values()))
        app.background.advance(planners=False)
        self.assertEqual(len(self.sent), 0)
        token = self.register(api, plugin, handlers)
        app.background.advance(planners=False)
        for _ in range(100):
            if self.sent:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("私人阅读安排", self.sent[0].text)
        self.assertTrue(api.unregister_life_provider(token))

    async def test_state_write_adopts_legacy_window_without_losing_its_history(self):
        app, context, _, controller, api, plugin, handlers, certificate = await self.setup_lifecycle()
        app.role_state.write(context, data=state_data(energy=35), expected_revision=0, operation_key="legacy-state")
        old = app.role_state._legacy_records
        role_key, window_key = old.owner_key(context), old.legacy.owner_key(context)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO role_state_records SELECT ?,record_id,revision,updated_at,record_json FROM role_state_records WHERE owner_key=?",
                       (window_key, role_key))
            db.execute("INSERT INTO role_state_history SELECT ?,record_id,revision,operation_id,record_json FROM role_state_history WHERE owner_key=?",
                       (window_key, role_key))
            db.execute("UPDATE role_state_receipts SET owner_key=? WHERE owner_key=?", (window_key, role_key))
            db.execute("DELETE FROM role_state_history WHERE owner_key=?", (role_key,))
            db.execute("DELETE FROM role_state_records WHERE owner_key=?", (role_key,))
        self.assertEqual(old.storage(context), "window")
        life = handlers["calendar_command"].__self__.storage
        directory = Path(life._connection.execute("PRAGMA database_list").fetchone()[2]).parent
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_state(self.runtime.database_path, expected_version=version, destination=directory)
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        handlers["state_read"] = StagedStateRecords(life).dispatch
        handlers["state_command"] = StagedStateCommands(life).dispatch
        handlers["activity_read"] = StagedActivityRecords(life).dispatch
        handlers["activity_history"] = LifeEventDelivery(life).dispatch
        self.certificate(certificate)
        controller.session = SimpleNamespace(application=app)
        token = self.register(api, plugin, handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_STATE_OWNER_KEY,))
        self.assertEqual(app.role_state.records.storage(context), "window")
        result = app.role_state.write(context, data=state_data(energy=40), expected_revision=1, operation_key="adopt")
        self.assertEqual((result["revision"], app.role_state.records.storage(context)), (2, "role"))
        self.assertEqual(app.role_state.records.version(context, "current", 1)["values"]["energy"], 35)
        self.assertEqual(rehearse_state_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                                expected_version=version)["revisions"], 3)
        self.assertTrue(api.unregister_life_provider(token))

    async def test_management_page_reads_and_changes_calendar_after_transfer(self):
        app, context, data, controller, api, plugin, handlers, certificate = await self.setup_lifecycle()
        life = handlers["calendar_command"].__self__.storage
        directory = Path(life._connection.execute("PRAGMA database_list").fetchone()[2]).parent
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        handlers["activity_read"] = StagedActivityRecords(life).dispatch
        handlers["activity_history"] = LifeEventDelivery(life).dispatch
        self.certificate(certificate)
        controller.session = SimpleNamespace(application=app)
        token = self.register(api, plugin, handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        self.grant(context.actor, context.scope, "reminder.create")
        self.grant(context.actor, context.scope, "reminder.deliver")
        manager = StateManagement(app, context)
        status = manager.status(self.core).unwrap()
        body = {"action": "calendar.create", "operation_key": "new-from-page", "id": "page-event",
                "expected_revision": 0, "view_token": status["create_token"], "data": data}
        stale = manager.command(self.core, {**body, "operation_key": "bad-token", "view_token": "bad"})
        self.assertEqual(stale.failure.code, "state_view_stale")
        handoff = app.calendar._life_command_bridge.handoff
        complete = handoff.complete
        def interrupted(*args, **kwargs):
            complete(*args, **kwargs)
            raise TaskError("test_interrupted", "simulated interruption")
        with patch.object(handoff, "complete", side_effect=interrupted):
            self.assertEqual(manager.command(self.core, body).failure.code, "test_interrupted")
        result = manager.command(self.core, body).unwrap()
        self.assertEqual((result["id"], result["revision"]), ("page-event", 1))
        self.assertTrue(manager.command(self.core, body).unwrap()["replayed"])
        item = next(item for item in manager.status(self.core).unwrap()["calendar"]["items"] if item["id"] == "page-event")
        reminder = {"action": "calendar.remind", "operation_key": "page-remind", "id": "page-event",
                    "expected_revision": 1, "view_token": item["view_token"], "sequence": 0}
        self.assertEqual(manager.command(self.core, reminder).unwrap()["revision"], 2)
        self.assertTrue(manager.command(self.core, reminder).unwrap()["replayed"])
        self.assertTrue(api.unregister_life_provider(token))

    async def test_replacing_calendar_reminder_recovers_after_host_transaction_interruption(self):
        app, context, _, controller, api, plugin, handlers, certificate = await self.setup_lifecycle(with_reminder=True)
        life = handlers["calendar_command"].__self__.storage
        handlers["activity_history"] = LifeEventDelivery(life).dispatch
        self.grant(context.actor, context.scope, "reminder.cancel")
        self.certificate(certificate)
        controller.session = SimpleNamespace(application=app)
        token = self.register(api, plugin, handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        original = app.calendar.read(context, "event")
        previous_id = original["reminder_id"]
        create = app.agenda.create_reminder

        def interrupt(*args, **kwargs):
            create(*args, **kwargs)
            raise RuntimeError("simulated interruption")

        with patch.object(app.agenda, "create_reminder", side_effect=interrupt):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                app.calendar.remind(context, identifier="event", expected_revision=2, operation_key="new-reminder")
        self.assertEqual(app.agenda.repository.reminder(previous_id).state, "active")
        with self.assertRaises(TaskError):
            app.calendar.read(context, "event")

        self.assertTrue(api.unregister_life_provider(token))
        token = self.register(api, plugin, handlers)
        app.life_runtime.bind(context, "test persona")
        host = api.life_host_capabilities(plugin=plugin, registration_id=token)
        async def unused(*_args):
            self.fail("sleep execution is outside this test")
        scheduler = DueSleepScheduler(life, {"api_version": "companion.life-host.v1",
            "contexts": lambda: [], "calendar_contexts": host["calendar_contexts"],
            "wake": unused, "recover": unused, "recover_calendar": host["recover_calendar"]})
        self.addAsyncCleanup(scheduler.close)
        with patch.object(scheduler.records, "dispatch", return_value=()):
            self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        current = app.calendar.read(context, "event")
        self.assertEqual(current["revision"], 3)
        self.assertNotEqual(current["reminder_id"], previous_id)
        self.assertEqual(app.agenda.repository.reminder(previous_id).state, "cancelled")
        self.assertEqual(app.agenda.repository.reminder(current["reminder_id"]).state, "active")
        repeated = app.calendar.remind(context, identifier="event", expected_revision=2, operation_key="new-reminder")
        self.assertTrue(repeated["replayed"])
        handoff = app.calendar._life_command_bridge.handoff
        with patch.object(handoff, "decide", side_effect=SystemExit("simulated restart")):
            with self.assertRaises(SystemExit):
                app.calendar.command(context, identifier="event", action="presence", expected_revision=3,
                                     operation_key="abandoned", data={"presence": "阅读"})
        with patch.object(scheduler.records, "dispatch", return_value=()):
            self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        self.assertEqual(app.calendar.read(context, "event")["revision"], 3)
        self.clock.advance(timedelta(hours=1))
        app.agenda_background.advance().unwrap()
        app.background.advance(planners=False)
        for _ in range(100):
            if self.sent:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(self.sent), 1)
        self.assertIn("私人阅读安排", self.sent[0].text)
        self.assertTrue(api.unregister_life_provider(token))

    async def test_provider_first_verified_binding_command_and_session_rebuild(self):
        app, context, data, controller, api, plugin, handlers, certificate = await self.setup_lifecycle()
        token = self.register(api, plugin, handlers)
        controller.session = SimpleNamespace(application=app)
        refresh_session_binding(controller)
        with self.assertRaises(TaskError) as unverified:
            app.calendar.read(context, "event")
        self.assertEqual(unverified.exception.code, "life_calendar_adoption_unverified")
        self.certificate(certificate)
        self.assertEqual(app.calendar.read(context, "event")["revision"], 1)
        self.assertEqual(app.calendar.read(context, "adopted")["revision"], 1)
        self.assertEqual(app.calendar.read(context, "pinned")["revision"], 1)
        previous = app.calendar.records.get
        changed = app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                       operation_key="life-update", data={**data, "title": "生活安排"})
        self.assertEqual(changed["revision"], 2)
        self.assertEqual(app.calendar._legacy_records.get(context, "event")["revision"], 1)
        controller.session = SimpleNamespace(application=app)
        refresh_session_binding(controller)
        self.assertEqual(app.calendar.read(context, "event")["revision"], 2)
        with self.assertRaises(TaskError):
            previous(context, "event")
        current = app.calendar.records.get
        self.assertTrue(api.unregister_life_provider(token))
        with self.assertRaises(TaskError):
            current(context, "event")
        with self.assertRaises(TaskError):
            app.calendar.read(context, "event")
        token = self.register(api, plugin, handlers)
        self.assertEqual(app.calendar.read(context, "event")["revision"], 2)
        with self.assertRaises(TaskError):
            current(context, "event")
        self.assertTrue(api.unregister_life_provider(token))

    async def test_session_first_unready_and_certificate_mismatch_fail_closed(self):
        app, context, _, controller, api, plugin, handlers, certificate = await self.setup_lifecycle()
        controller.session = SimpleNamespace(application=app)
        refresh_session_binding(controller)
        with self.assertRaises(TaskError):
            app.calendar.read(context, "event")
        ready = [False]
        self.certificate({**certificate, "plan_sha256": "0" * 64})
        token = self.register(api, plugin, handlers, is_active=lambda: ready[0])
        with self.assertRaises(TaskError):
            app.calendar.read(context, "event")
        ready[0] = True
        with self.assertRaises(TaskError) as mismatch:
            app.calendar.read(context, "event")
        self.assertEqual(mismatch.exception.code, "life_calendar_adoption_unverified")
        self.certificate(certificate)
        self.assertEqual(app.calendar.read(context, "event")["revision"], 1)
        stale = app.calendar.records.get
        ready[0] = False
        with self.assertRaises(TaskError):
            stale(context, "event")
        ready[0] = True
        self.assertEqual(app.calendar.read(context, "event")["revision"], 1)
        with self.assertRaises(TaskError):
            stale(context, "event")
        self.assertTrue(api.unregister_life_provider(token))

    async def test_session_refresh_does_not_wait_for_provider_callback(self):
        app, context, _, controller, api, plugin, handlers, certificate = await self.setup_lifecycle()
        self.certificate(certificate)
        controller.session = SimpleNamespace(application=app)
        started, proceed, refreshed = Event(), Event(), Event()
        results, errors, token = [], [], []
        original_read = handlers["calendar_read"]

        def read_and_unregister(*args, **kwargs):
            started.set()
            if not proceed.wait(5):
                raise TimeoutError("callback did not resume")
            api.unregister_life_provider(token[0])
            return original_read(*args, **kwargs)

        handlers["calendar_read"] = read_and_unregister
        token.append(self.register(api, plugin, handlers))

        def read():
            try:
                results.append(app.calendar.read(context, "event"))
            except Exception as error:
                errors.append(error)

        worker = Thread(target=read)
        def refresh():
            controller.session = SimpleNamespace(application=app)
            refresh_session_binding(controller)
            refreshed.set()
        reloader = Thread(target=refresh)
        # StorageRuntime tracks readers by weak Thread keys. Keep the workers
        # alive through fixture cleanup so runtime.close can close their SQLite
        # connections before Windows removes the temporary database.
        self._test_threads = (worker, reloader)
        try:
            worker.start()
            self.assertTrue(started.wait(5))
            reloader.start()
            self.assertTrue(refreshed.wait(2), "session refresh waited on the active calendar call")
        finally:
            proceed.set()
            worker.join(5)
            if reloader.ident is not None:
                reloader.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(reloader.is_alive())
        self.assertTrue(not errors or all(isinstance(error, TaskError) for error in errors))
        self.assertLessEqual(len(results), 1)
        with self.assertRaises(TaskError):
            app.calendar.read(context, "event")
        handlers["calendar_read"] = original_read
        replacement = self.register(api, plugin, handlers)
        self.assertEqual(app.calendar.read(context, "event")["revision"], 1)
        self.assertTrue(api.unregister_life_provider(replacement))


if __name__ == "__main__":
    unittest.main()
