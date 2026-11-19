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
from companion_v2.integration import calendar_reminder_hold as holds, life_commands
from companion_v2.integration.calendar_handoff import CalendarHandoff, PREFIX
from companion_v2.storage.models import OperationState
from companion_v2.storage.repositories import OperationRepository
from good_life.calendar_import import DDL
from good_life.calendar_records import CalendarTransitionPending, StagedCalendarRecords
from good_life.prepared_commands import PreparedCommands
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.storage import LifeStorage
from tests.test_agenda_background import AgendaBackgroundFixture
from tests.test_management_access import NOW


class CalendarHandoffReminderIntegrationTests(AgendaBackgroundFixture, unittest.IsolatedAsyncioTestCase):
    async def test_calendar_commit_cancels_the_real_queued_reminder_before_unfencing(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        with life.transaction() as db:
            for statement in DDL:
                db.execute(statement)
        commands = PreparedCommands(life)
        commands.install()
        changes = StagedCalendarCommands(life)
        host = self.host()
        app = host.application
        reminder_id = "calendar-reminder-handoff"

        def validate(_context, identifier, _at):
            if holds.held(self.runtime, identifier):
                raise TaskError(holds.ERROR, "calendar reminder awaits a decision")

        with patch.object(app.calendar, "validate_reminder", side_effect=validate):
            with patch.object(app.background, "advance", return_value=False):
                await self.start(host)
                occurrence, _target = self.reminder(app, reminder_id)
                app.agenda_background.advance().unwrap()
                self.assertIsNotNone(self.delivery(occurrence))
                reminder = app.agenda.repository.reminder(reminder_id)
                context = app.agenda_background._context(reminder)
                owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id,
                                     context.role_binding.role.subject_id)
                role_key = app.calendar.records.role_key(context)
                conversation_key = app.calendar.records.conversation_key(context)
                data = {"kind": "role_plan", "title": "午后散步", "detail": "慢慢走",
                        "schedule": {"anchor": stamp(NOW + timedelta(hours=1)), "timezone_name": "UTC",
                                     "frequency": "once", "interval": 1, "count": None, "fold": 0, "gap_policy": "reject"},
                        "duration_minutes": 30, "presence": "散步"}
                current = {**data, "id": "event", "revision": 1, "updated_at": stamp(NOW), "state": "accepted",
                           "sources": [], "exceptions": {}, "accepted_by": [], "changed_by": json.loads(encode(context.role_binding.role)),
                           "reminder_id": reminder_id, "reminder_context_key": context.key}
                raw = encode(current)
                with life.transaction() as db:
                    db.execute("INSERT INTO staged_calendar_records VALUES (?, ?, ?, ?, ?)", (role_key, "event", 1, stamp(NOW), raw))
                    db.execute("INSERT INTO staged_calendar_history VALUES (?, ?, ?, ?, ?)", (role_key, "event", 1, "old", raw))
                operation_key = "handoff"
                operation_id = "calendar:" + digest([app.calendar.records.operation_key(context), operation_key])
                request = {"command_id": "handoff", "owner_key": digest(owner), "context_key": context.key,
                           "kind": "calendar.change", "expected_revision": 1,
                           "payload": {"storage_key": role_key, "lookup_owner_keys": [role_key, conversation_key], "record_id": "event",
                                "operation_id": operation_id, "previous_hash": hashlib.sha256(raw.encode()).hexdigest(),
                                "action": "revise", "data": {**data, "title": "傍晚散步"}, "sources": [],
                                "actor": json.loads(encode(context.role_binding.role)), "operator_authenticated": False,
                                "planning": None, "observed_at": stamp(NOW)}}
                handoff = CalendarHandoff(app.calendar, changes)
                handoff.prepare(context, request, operation_key=operation_key, sources=(), operator=None, observed_at=NOW)
                proposal = changes.proposal(request)
                self.assertEqual(proposal["cancel_reminder"]["reminder_id"], reminder_id)
                decision = handoff.decide(context, request, operation_key=operation_key, sources=(), operator=None, observed_at=NOW)
                self.assertEqual(app.outbound.pending(limit=1), ())
                self.assertEqual(handoff.decide(context, request, operation_key=operation_key, sources=(),
                                                operator=None, observed_at=NOW), decision)
                life.close()
                life = LifeStorage.open(Path(temporary.name) / "life")
                self.addCleanup(life.close)
                commands = PreparedCommands(life)
                commands.install()
                changes = StagedCalendarCommands(life)
                handoff = CalendarHandoff(app.calendar, changes)
                handoff.apply(decision)
                with self.assertRaises(CalendarTransitionPending):
                    StagedCalendarRecords(life).dispatch("get", owner_key=role_key, identifier="event")
                life.close()
                life = LifeStorage.open(Path(temporary.name) / "life")
                self.addCleanup(life.close)
                commands = PreparedCommands(life)
                commands.install()
                changes = StagedCalendarCommands(life)
                handoff = CalendarHandoff(app.calendar, changes)
                pending = commands.pending()[0]
                recovered = changes.recover(command_id=pending["command_id"], owner_key=pending["owner_key"],
                    context_key=pending["context_key"], request_hash=pending["request_hash"])
                self.assertEqual(recovered, request)
                self.assertEqual(life_commands.lookup(self.runtime, context_key=context.key, command_id="handoff",
                                                     request_hash=pending["request_hash"]), decision)
                cancel = app.calendar._cancel_reminder_at_storage

                def interrupted_cancel(*args):
                    cancel(*args)
                    raise RuntimeError("simulated interruption before commit")
                with patch.object(app.calendar, "_cancel_reminder_at_storage", side_effect=interrupted_cancel):
                    with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                        handoff.complete(context, recovered, decision, observed_at=NOW)
                self.assertEqual(app.agenda.repository.reminder(reminder_id).state, "active")
                self.assertTrue(holds.held(self.runtime, reminder_id))
                self.assertEqual(OperationRepository(self.runtime).get(operation_id).unwrap().state,
                                 OperationState.ACCEPTED)
                self.assertEqual(app.outbound.pending(limit=1), ())
                evidence_key = PREFIX + decision["decision_id"]
                with self.runtime.transaction() as db:
                    original_evidence = db.execute("SELECT value FROM runtime_metadata WHERE key=?", (evidence_key,)).fetchone()[0]
                    changed_evidence = json.loads(original_evidence)
                    changed_evidence["scope_hash"] = "different-scope"
                    db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (json.dumps(changed_evidence), evidence_key))
                with self.assertRaises(TaskError):
                    handoff.complete(context, recovered, decision, observed_at=NOW)
                with self.runtime.transaction() as db:
                    db.execute("UPDATE runtime_metadata SET value=? WHERE key=?", (original_evidence, evidence_key))
                self.assertTrue(handoff.complete(context, recovered, decision, observed_at=NOW))
                self.assertFalse(handoff.complete(context, recovered, decision, observed_at=NOW))
                self.assertEqual(app.agenda.repository.reminder(reminder_id).state, "cancelled")
                self.assertTrue(handoff.acknowledge(decision))
                self.assertFalse(handoff.acknowledge(decision))
                self.assertEqual(handoff.prepare(context, request, operation_key=operation_key,
                                                 sources=(), operator=None, observed_at=NOW)["state"], "acknowledged")
                self.assertEqual(handoff.decide(context, request, operation_key=operation_key, sources=(),
                                                operator=None, observed_at=NOW), decision)
                self.assertEqual(handoff.apply(decision)["state"], "acknowledged")
                with self.assertRaises(TaskError):
                    handoff.decide(context, request, operation_key="different", sources=(), operator=None, observed_at=NOW)
                saved = StagedCalendarRecords(life).dispatch("get", owner_key=role_key, identifier="event")
                self.assertEqual((saved["title"], saved["reminder_id"]), ("傍晚散步", None))
            app.notify()
            await self.until(lambda: self.delivery(occurrence)["state"] == "cancelled")
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
