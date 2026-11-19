from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskError, digest, encode, stamp
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.integration import calendar_reminder_hold as holds
from companion_v2.integration.calendar_batch_handoff import CalendarBatchHandoff
from good_life.calendar_import import DDL
from good_life.calendar_records import CalendarTransitionPending, StagedCalendarRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.storage import LifeStorage
from tests.test_agenda_background import AgendaBackgroundFixture
from tests.test_management_access import NOW


class BatchProvider:
    def __init__(self, commands):
        self.commands = commands

    def batch(self, operation, *args, **kwargs):
        return self.commands.dispatch("batch." + operation, *args, **kwargs)


class CalendarBatchHandoffTests(AgendaBackgroundFixture, unittest.IsolatedAsyncioTestCase):
    async def test_batch_reminder_hold_and_interrupted_finalization_recover_together(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        with life.transaction() as db:
            for statement in DDL:
                db.execute(statement)
        PreparedCommands(life).install()
        changes = StagedCalendarCommands(life)
        host = self.host()
        app = host.application
        reminder_id = "batch-reminder"
        def validate(_context, identifier, _at):
            if holds.held(self.runtime, identifier):
                raise TaskError(holds.ERROR, "calendar reminder awaits a decision")
        with patch.object(app.calendar, "validate_reminder", side_effect=validate):
            background = patch.object(app.background, "advance", return_value=False)
            background.start()
            self.addCleanup(background.stop)
            await self.start(host)
            occurrence, _target = self.reminder(app, reminder_id)
            app.agenda_background.advance().unwrap()
            reminder = app.agenda.repository.reminder(reminder_id)
            context = app.agenda_background._context(reminder)
            owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
            role_key = app.calendar.records.role_key(context)
            conversation_key = app.calendar.records.conversation_key(context)
            data = {"kind": "role_plan", "title": "散步", "detail": "慢慢走",
                "schedule": {"anchor": stamp(NOW + timedelta(hours=1)), "timezone_name": "UTC", "frequency": "once",
                             "interval": 1, "count": None, "fold": 0, "gap_policy": "reject"},
                "duration_minutes": 30, "presence": "散步"}
            current = {**data, "id": "event", "revision": 1, "updated_at": stamp(NOW), "state": "accepted",
                "sources": [], "exceptions": {}, "accepted_by": [], "changed_by": json.loads(encode(context.role_binding.role)),
                "reminder_id": reminder_id, "reminder_context_key": context.key}
            raw = encode(current)
            with life.transaction() as db:
                db.execute("INSERT INTO staged_calendar_records VALUES (?,?,?,?,?)", (role_key, "event", 1, stamp(NOW), raw))
                db.execute("INSERT INTO staged_calendar_history VALUES (?,?,?,?,?)", (role_key, "event", 1, "old", raw))

            def command(identifier, action, revision, key, previous, body):
                operation_id = "calendar:" + digest([app.calendar.records.operation_key(context), key])
                return {"command_id": "life-calendar-" + digest([context.key, key]), "owner_key": digest(owner),
                    "context_key": context.key, "kind": "calendar.change", "expected_revision": revision,
                    "payload": {"storage_key": role_key, "lookup_owner_keys": [role_key, conversation_key],
                        "record_id": identifier, "operation_id": operation_id, "previous_hash": previous,
                        "action": action, "data": body, "sources": [], "actor": json.loads(encode(context.role_binding.role)),
                        "operator_authenticated": False, "planning": None, "observed_at": stamp(NOW)}}
            first = command("event", "cancel", 1, "cancel-old", hashlib.sha256(raw.encode()).hexdigest(), None)
            second = command("new-event", "create", 0, "create-new", None, {**data, "title": "新计划"})
            request = {"command_id": "life-calendar-batch-test", "owner_key": digest(owner), "context_key": context.key,
                "kind": "calendar.batch", "expected_revision": 0,
                "payload": {"commands": [first, second], "storage_keys": [role_key]}}
            handoff = CalendarBatchHandoff(app.calendar, BatchProvider(changes))
            handoff.prepare(context, request, operation_keys=["cancel-old", "create-new"], observed_at=NOW)
            decision = handoff.decide(context, request, operation_keys=["cancel-old", "create-new"], observed_at=NOW)
            self.assertTrue(holds.held(self.runtime, reminder_id))
            self.assertEqual(app.outbound.pending(limit=1), ())
            handoff.apply(decision)
            with self.assertRaises(CalendarTransitionPending):
                StagedCalendarRecords(life).dispatch("get", owner_key=role_key, identifier="new-event")
            cancel = app.calendar._cancel_reminder_at_storage
            def interrupted(*args):
                cancel(*args)
                raise RuntimeError("simulated host interruption")
            with patch.object(app.calendar, "_cancel_reminder_at_storage", side_effect=interrupted):
                with self.assertRaises(RuntimeError):
                    handoff.complete(context, request, decision, observed_at=NOW)
            self.assertEqual(app.agenda.repository.reminder(reminder_id).state, "active")
            self.assertTrue(holds.held(self.runtime, reminder_id))
            self.assertEqual(handoff.recover(context, request), {"state": "completed"})
            records = StagedCalendarRecords(life)
            self.assertEqual(records.dispatch("get", owner_key=role_key, identifier="event")["state"], "cancelled")
            self.assertEqual(records.dispatch("get", owner_key=role_key, identifier="new-event")["title"], "新计划")
            self.assertEqual(app.agenda.repository.reminder(reminder_id).state, "cancelled")
            self.assertFalse(holds.held(self.runtime, reminder_id))
            background.stop()
            app.notify()
            await self.until(lambda: self.delivery(occurrence)["state"] == "cancelled")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
