from copy import deepcopy
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, RLock, Thread
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError, digest
from companion_v2.agenda.calendar import CalendarService, LIFE_CALENDAR_OWNER_KEY
from companion_v2.integration.calendar_command_bridge import CalendarCommandBridge
from companion_v2.integration.calendar_records_proxy import LifeCalendarRecords
from companion_v2.integration.life_calendar_provider_proxy import LifeCalendarCommandsProxy, LifeCalendarStorageProxy
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from good_life.calendar_import import stage_calendar
from good_life.calendar_records import CalendarTransitionPending, StagedCalendarRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.storage import LifeStorage
from tests.test_role_state import StateFixture, calendar_data
from tests.test_management_access import SCOPE
from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry


class CalendarCommandBridgeTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_bridge(self, *, sources=()):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        await self.start(host)
        data = calendar_data()
        if sources == "make":
            sources = (self.source(app, context, "bridge-source"),)
        self.event_command(app, context, identifier="event", action="create", revision=0, data=data, sources=sources)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_calendar(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        records, commands = StagedCalendarRecords(life), StagedCalendarCommands(life)
        bridge = CalendarCommandBridge(app.calendar, records, commands, legacy_adopted=True)
        return app, context, data, tuple(sources), life, records, commands, bridge

    async def test_real_entry_changes_replays_and_preserves_raw_previous_record(self):
        app, context, data, _, life, records, commands, bridge = await self.setup_bridge()
        key = app.calendar.records.role_key(context)
        legacy = bridge.command(context, identifier="event", action="create", expected_revision=0,
                                operation_key="create0", data=data)
        self.assertTrue(legacy["replayed"])
        self.assertEqual((legacy["revision"], legacy["state"]), (1, "accepted"))
        current = records.get(owner_key=key, identifier="event")
        pretty = json.dumps(current, ensure_ascii=False, indent=2)
        with life.transaction() as db:
            db.execute("UPDATE staged_calendar_records SET record_json=? WHERE owner_key=? AND record_id='event'", (pretty, key))
            db.execute("UPDATE staged_calendar_history SET record_json=? WHERE owner_key=? AND record_id='event' AND revision=1", (pretty, key))
        updated = {**data, "title": "新的安排"}
        result = bridge.command(context, identifier="event", action="revise", expected_revision=1,
                                operation_key="revise", data=updated)
        self.assertEqual((result["revision"], result["replayed"]), (2, False))
        self.assertEqual(records.get(owner_key=key, identifier="event")["title"], "新的安排")
        self.assertEqual(app.calendar.records.get(context, "event")["revision"], 1)
        self.assertTrue(bridge.command(context, identifier="event", action="revise", expected_revision=1,
                                       operation_key="revise", data=updated)["replayed"])
        with self.assertRaises(TaskError) as conflict:
            bridge.command(context, identifier="event", action="revise", expected_revision=1,
                           operation_key="revise", data={**updated, "title": "另一项"})
        self.assertEqual(conflict.exception.code, "idempotency_key_conflict")
        created = bridge.command(context, identifier="new-event", action="create", expected_revision=0,
                                 operation_key="new-event", data=data)
        self.assertEqual((created["revision"], created["state"]), (1, "accepted"))
        self.assertEqual(commands.commands.pending(), ())

    async def test_versioned_provider_dispatch_drives_real_host_calendar_entry(self):
        app, context, data, _, _life, records, commands, _ = await self.setup_bridge()
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        registry = LifeProviderRegistry(SimpleNamespace(get_all_stars=lambda: [star]), owner=object(), host_active=lambda: True)
        self.addCleanup(registry.close)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["calendar_read"], handlers["calendar_command"] = records.dispatch, commands.dispatch
        with self.assertRaises(ValueError):
            commands.dispatch("apply", None, None)
        token = registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        legacy = app.calendar.records
        def bind(lease):
            storage = LifeCalendarStorageProxy(lease)
            command = LifeCalendarCommandsProxy(lease)
            reader = LifeCalendarRecords(storage, legacy_owners=lambda value: tuple(
                {"owner_key": item["owner_key"], "record_id": item["record"]["id"]}
                for item in legacy._legacy_role_plans(value)))
            bridge = CalendarCommandBridge(app.calendar, storage, command, legacy_adopted=True)
            app.calendar.bind_life_command_bridge(bridge, records=reader)
            return bridge
        bridge = bind(registry.lease())
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_CALENDAR_OWNER_KEY,))
        changed = {**data, "title": "新生活日程"}
        result = app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                      operation_key="provider-edit", data=changed)
        self.assertEqual(result["revision"], 2)
        self.assertEqual(app.calendar.read(context, "event")["title"], "新生活日程")
        self.assertEqual(legacy.get(context, "event")["revision"], 1)
        self.assertTrue(app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                             operation_key="provider-edit", data=changed)["replayed"])
        stale_read = app.calendar.records.get
        self.assertTrue(registry.unregister(token))
        with self.assertRaises(TaskError):
            stale_read(context, "event")
        with self.assertRaises(TaskError):
            app.calendar.read(context, "event")
        self.assertTrue(app.calendar.unbind_life_command_bridge(bridge))
        token = registry.register(plugin=plugin, api_version=API_VERSION, handlers=handlers)
        bind(registry.lease())
        self.assertEqual(app.calendar.read(context, "event")["revision"], 2)
        self.assertTrue(app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                             operation_key="provider-edit", data=changed)["replayed"])
        with self.assertRaises(TaskError):
            stale_read(context, "event")
        self.assertTrue(registry.unregister(token))
        self.assertEqual(self.sent, [])

    async def test_interruption_after_host_decision_recovers_original_command(self):
        app, context, data, _, _life, records, commands, bridge = await self.setup_bridge()
        updated = {**data, "title": "恢复后的安排"}
        with patch.object(bridge.handoff, "apply", side_effect=RuntimeError("simulated interruption")):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                bridge.command(context, identifier="event", action="revise", expected_revision=1,
                               operation_key="resume", data=updated)
        pending = commands.commands.pending()
        self.assertEqual((len(pending), pending[0]["state"]), (1, "prepared"))
        operation_id = "calendar:" + digest([app.calendar.records.operation_key(context), "resume"])
        self.assertEqual(OperationRepository(self.runtime).get(operation_id).unwrap().state, OperationState.ACCEPTED)
        result = bridge.command(context, identifier="event", action="revise", expected_revision=1,
                                operation_key="resume", data=updated)
        self.assertTrue(result["replayed"])
        self.assertEqual(records.get(owner_key=app.calendar.records.role_key(context), identifier="event")["title"], "恢复后的安排")
        self.assertEqual(commands.commands.pending(), ())

    async def test_source_revoked_before_host_decision_aborts_prepared_command(self):
        app, context, data, sources, _life, _records, commands, bridge = await self.setup_bridge(sources="make")
        decide = bridge.handoff.decide

        def revoke_then_decide(*args, **kwargs):
            self.revoke_source(app, sources[0])
            return decide(*args, **kwargs)

        with patch.object(bridge.handoff, "decide", side_effect=revoke_then_decide):
            with self.assertRaises(TaskError):
                bridge.command(context, identifier="event", action="revise", expected_revision=1,
                               operation_key="revoke", data=deepcopy(data), sources=sources)
        self.assertEqual(commands.commands.pending(), ())
        operation_id = "calendar:" + digest([app.calendar.records.operation_key(context), "revoke"])
        self.assertFalse(OperationRepository(self.runtime).get(operation_id).is_success)

    async def test_entry_cancels_original_reminder_with_calendar_change(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        for action in ("reminder.create", "reminder.deliver", "reminder.cancel"):
            self.grant(context.actor, SCOPE, action)
        await self.start(host)
        data = calendar_data()
        self.event_command(app, context, identifier="event", action="create", revision=0, data=data)
        app.calendar.remind(context, identifier="event", expected_revision=1, operation_key="remind")
        original = app.calendar.read(context, "event")
        reminder_id = original["reminder_id"]
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_calendar(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        records, commands = StagedCalendarRecords(life), StagedCalendarCommands(life)
        bridge = CalendarCommandBridge(app.calendar, records, commands, legacy_adopted=True)
        revised = {**data, "title": "改期后的安排"}
        with patch.object(bridge.handoff, "complete", side_effect=RuntimeError("interrupted after life commit")):
            with self.assertRaisesRegex(RuntimeError, "interrupted after life commit"):
                bridge.command(context, identifier="event", action="revise", expected_revision=2,
                               operation_key="edit-reminder", data=revised)
        self.assertEqual(commands.commands.pending()[0]["state"], "committed")
        self.assertEqual(app.agenda.repository.reminder(reminder_id).state, "active")
        with self.assertRaises(CalendarTransitionPending):
            records.dispatch("get", owner_key=app.calendar.records.role_key(context), identifier="event")
        result = bridge.command(context, identifier="event", action="revise", expected_revision=2,
                                operation_key="edit-reminder", data=revised)
        self.assertEqual((result["revision"], result["replayed"]), (3, True))
        self.assertEqual(app.agenda.repository.reminder(reminder_id).state, "cancelled")
        saved = records.get(owner_key=app.calendar.records.role_key(context), identifier="event")
        self.assertIsNone(saved["reminder_id"])
        self.assertEqual(saved["title"], "改期后的安排")
        self.assertTrue(bridge.command(context, identifier="event", action="revise", expected_revision=2,
                                       operation_key="edit-reminder", data=revised)["replayed"])
        self.assertEqual(self.sent, [])

    async def test_host_entry_delegates_only_when_owner_marker_selects_life(self):
        app, context, data, _, _life, records, _commands, bridge = await self.setup_bridge()
        original = app.calendar.records
        proxy = LifeCalendarRecords(records, legacy_owners=lambda value: tuple(
            {"owner_key": item["owner_key"], "record_id": item["record"]["id"]}
            for item in original._legacy_role_plans(value)))
        app.calendar.bind_life_command_bridge(bridge, records=proxy)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_CALENDAR_OWNER_KEY,))
        revised = {**data, "title": "新插件负责"}
        with self.runtime.transaction():
            with self.assertRaises(TaskError) as nested:
                app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                     operation_key="nested", data=revised)
            self.assertEqual(nested.exception.code, "life_calendar_transaction_active")
            with self.assertRaises(TaskError) as direct_nested:
                bridge.command(context, identifier="event", action="revise", expected_revision=1,
                               operation_key="direct-nested", data=revised)
            self.assertEqual(direct_nested.exception.code, "life_calendar_transaction_active")
        with self.runtime.read_snapshot():
            with self.assertRaises(TaskError) as snapshot:
                app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                     operation_key="snapshot", data=revised)
            self.assertEqual(snapshot.exception.code, "life_calendar_transaction_active")
            with self.assertRaises(TaskError) as snapshot_read:
                app.calendar.read(context, "event")
            self.assertEqual(snapshot_read.exception.code, "life_calendar_transaction_active")
        self.assertEqual(bridge.commands.commands.pending(), ())
        result = app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                      operation_key="delegated", data=revised)
        self.assertEqual((result["revision"], result["replayed"]), (2, False))
        self.assertEqual(records.get(owner_key=app.calendar.records.role_key(context), identifier="event")["title"], "新插件负责")
        self.assertEqual(app.calendar.read(context, "event")["title"], "新插件负责")
        self.assertEqual(original.get(context, "event")["revision"], 1)
        reminder = app.calendar.remind(context, identifier="event", expected_revision=2, operation_key="remind")
        self.assertEqual((reminder["revision"], reminder["replayed"]), (3, False))
        reminder_id = app.calendar.read(context, "event")["reminder_id"]
        self.assertEqual(app.agenda.repository.reminder(reminder_id).state, "active")
        self.assertTrue(app.calendar.unbind_life_command_bridge(bridge))
        self.assertFalse(app.calendar.unbind_life_command_bridge(bridge))
        with self.assertRaises(TaskError) as unavailable:
            app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                 operation_key="delegated", data=revised)
        self.assertEqual(unavailable.exception.code, "life_provider_unavailable")
        with self.assertRaises(TaskError) as read_unavailable:
            app.calendar.read(context, "event")
        self.assertEqual(read_unavailable.exception.code, "life_provider_unavailable")
        app.calendar.bind_life_command_bridge(bridge, records=proxy)
        self.assertTrue(app.calendar.command(context, identifier="event", action="revise", expected_revision=1,
                                             operation_key="delegated", data=revised)["replayed"])
        with self.runtime.transaction() as db:
            db.execute("UPDATE runtime_metadata SET value='unknown' WHERE key=?", (LIFE_CALENDAR_OWNER_KEY,))
        with self.assertRaises(TaskError) as invalid:
            app.calendar.command(context, identifier="event", action="cancel", expected_revision=1,
                                 operation_key="should-not-run")
        self.assertEqual(invalid.exception.code, "life_calendar_owner_invalid")
        self.assertEqual(original.get(context, "event")["revision"], 1)

    async def test_unbinding_does_not_block_an_inflight_calendar_call(self):
        started, release, unbound = Event(), Event(), Event()
        calls, errors = [], []
        calendar = CalendarService.__new__(CalendarService)
        calendar.runtime = SimpleNamespace(read_one=lambda *_args: {"value": "good_life"})
        calendar._life_command_bridge = None
        calendar._life_read_records = None
        calendar._life_read_lease = None
        calendar._life_bridge_lock = RLock()

        class SlowBridge:
            pass

        bridge = SlowBridge()
        bridge.calendar = calendar
        def command(*_args, **_kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test bridge did not release")
            return {"state": "done"}
        bridge.command = command
        required = ("get", "list", "version", "receipt", "by_reminder", "shared_role_plans",
                    "role_key", "conversation_key", "operation_key")
        fake_records = SimpleNamespace(**{name: lambda *_args, **_kwargs: None for name in required})
        calendar.bind_life_command_bridge(bridge, records=fake_records)

        def call():
            try:
                calls.append(calendar.command(object(), identifier="event", action="revise", expected_revision=1,
                                              operation_key="running", data=None))
            except Exception as error:
                errors.append(error)

        worker = Thread(target=call)
        detached = Thread(target=lambda: (calendar.unbind_life_command_bridge(bridge), unbound.set()))
        try:
            worker.start()
            self.assertTrue(started.wait(5))
            detached.start()
            self.assertTrue(unbound.wait(5))
        finally:
            release.set()
            worker.join(5)
            if detached.ident is not None:
                detached.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(detached.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(calls, [{"state": "done"}])
        self.assertTrue(unbound.is_set())

    async def test_unbinding_does_not_block_reads_and_revokes_old_read_lease(self):
        started, release, unbound = Event(), Event(), Event()
        results, errors = [], []
        calendar = CalendarService.__new__(CalendarService)
        calendar.runtime = SimpleNamespace(read_one=lambda *_args: {"value": "good_life"})
        calendar._life_command_bridge = calendar._life_read_records = calendar._life_read_lease = None
        calendar._life_bridge_lock = RLock()
        bridge = SimpleNamespace(calendar=calendar, command=lambda *_args, **_kwargs: None)
        required = ("get", "list", "version", "receipt", "by_reminder", "shared_role_plans",
                    "role_key", "conversation_key", "operation_key")
        records = SimpleNamespace(**{name: lambda *_args, **_kwargs: None for name in required})
        def slow_read(*_args, **_kwargs):
            started.set()
            if not release.wait(5):
                raise TimeoutError("test reader did not release")
            return {"id": "event"}
        records.get = slow_read
        calendar.bind_life_command_bridge(bridge, records=records)
        lease = calendar.records

        def call():
            try:
                results.append(lease.get(object(), "event"))
            except Exception as error:
                errors.append(error)

        worker = Thread(target=call)
        detached = Thread(target=lambda: (calendar.unbind_life_command_bridge(bridge), unbound.set()))
        try:
            worker.start()
            self.assertTrue(started.wait(5))
            detached.start()
            self.assertTrue(unbound.wait(5))
        finally:
            release.set()
            worker.join(5)
            if detached.ident is not None:
                detached.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(detached.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(results, [{"id": "event"}])
        self.assertTrue(unbound.is_set())
        calendar.bind_life_command_bridge(bridge, records=records)
        with self.assertRaises(TaskError) as revoked:
            lease.get(object(), "event")
        self.assertEqual(revoked.exception.code, "life_provider_unavailable")
        self.assertEqual(calendar.records.get(object(), "event"), {"id": "event"})

    async def test_calendar_read_does_not_hold_bridge_lock_while_waiting_for_life_snapshot(self):
        calendar = CalendarService.__new__(CalendarService)
        calendar.runtime = SimpleNamespace(read_one=lambda *_args: {"value": "good_life"})
        calendar._life_command_bridge = calendar._life_read_records = calendar._life_read_lease = None
        calendar._life_bridge_lock = RLock()
        bridge = SimpleNamespace(calendar=calendar, command=lambda *_args, **_kwargs: None)
        life_lock, snapshot_started, reader_entered = RLock(), Event(), Event()
        errors = []
        required = ("get", "list", "version", "receipt", "by_reminder", "shared_role_plans",
                    "role_key", "conversation_key", "operation_key")
        records = SimpleNamespace(**{name: lambda *_args, **_kwargs: None for name in required})

        def read(*_args, **_kwargs):
            reader_entered.set()
            with life_lock:
                return {"id": "event"}

        records.get = read
        calendar.bind_life_command_bridge(bridge, records=records)
        lease = calendar.records

        def snapshot():
            try:
                with life_lock:
                    snapshot_started.set()
                    if not reader_entered.wait(3):
                        raise TimeoutError("reader did not start")
                    calendar.records.get(object(), "event")
            except Exception as error:
                errors.append(error)

        def reader():
            try:
                if not snapshot_started.wait(3):
                    raise TimeoutError("snapshot did not start")
                lease.get(object(), "event")
            except Exception as error:
                errors.append(error)

        threads = [Thread(target=snapshot, daemon=True), Thread(target=reader, daemon=True)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads), "calendar/life snapshot deadlocked")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
