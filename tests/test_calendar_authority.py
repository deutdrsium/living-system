from copy import deepcopy
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
from companion_v2.agenda.models import AgendaError
from companion_v2.foundation.refs import RoleOwnerRef
from companion_v2.integration import calendar_authority, life_commands
from companion_v2.integration.calendar_handoff import CalendarHandoff
from companion_v2.storage.repositories import OperationRepository
from good_life.calendar_import import stage_calendar
from good_life.prepared_commands import PreparedCommands
from good_life.staged_calendar_commands import StagedCalendarCommands
from good_life.calendar_records import StagedCalendarRecords
from good_life.storage import LifeStorage
from tests.test_management_access import NOW
from tests.test_management_access import SCOPE
from tests.test_role_state import StateFixture, calendar_data


class CalendarAuthorityTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def test_real_calendar_permission_and_source_are_rechecked_before_decision(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        await self.start(host)
        original = self.source(app, context, "calendar-source")
        data = calendar_data()
        self.event_command(app, context, identifier="event", action="create", revision=0, data=data, sources=(original,))
        current = app.calendar.read(context, "event")
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_calendar(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        commands = PreparedCommands(life)
        commands.install()
        changes = StagedCalendarCommands(life)
        role_key = app.calendar.records.role_key(context)
        conversation_key = app.calendar.records.conversation_key(context)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        operation_key = "revised-event"
        operation_id = "calendar:" + digest([app.calendar.records.operation_key(context), operation_key])
        request = {"command_id": "calendar-authority", "owner_key": digest(owner), "context_key": context.key,
            "kind": "calendar.change", "expected_revision": current["revision"],
            "payload": {"storage_key": role_key, "lookup_owner_keys": [role_key, conversation_key],
                        "record_id": "event", "operation_id": operation_id,
                        "previous_hash": hashlib.sha256(encode(current).encode("utf-8")).hexdigest(),
                        "action": "revise", "data": {**data, "title": "修改后的安排"}, "sources": json.loads(encode((original,))),
                        "actor": json.loads(encode(context.role_binding.role)), "operator_authenticated": False,
                        "planning": None, "observed_at": stamp(NOW)}}
        changes.prepare(request, prepared_at=NOW)
        proposal = changes.proposal(request)

        def authorize(value, at):
            return calendar_authority.authorize(app.calendar, value, request, proposal,
                operation_key=operation_key, sources=(original,), operator=None, observed_at=at)

        self.assertTrue(authorize(context, NOW).is_success)
        altered = deepcopy(request)
        altered["payload"]["actor"]["subject_id"] = "other-role"
        with self.assertRaises(TaskError) as mismatch:
            calendar_authority.authorize(app.calendar, context, altered, proposal, operation_key=operation_key,
                                         sources=(original,), operator=None, observed_at=NOW)
        self.assertEqual(mismatch.exception.code, "life_calendar_command_invalid")
        foreign = deepcopy(request)
        foreign["payload"]["lookup_owner_keys"] = [role_key, "other-conversation"]
        with self.assertRaises(TaskError):
            calendar_authority.authorize(app.calendar, context, foreign, proposal, operation_key=operation_key,
                                         sources=(original,), operator=None, observed_at=NOW)
        future = deepcopy(request)
        future["payload"]["observed_at"] = stamp(NOW + timedelta(minutes=1))
        with self.assertRaises(TaskError):
            calendar_authority.authorize(app.calendar, context, future, proposal, operation_key=operation_key,
                                         sources=(original,), operator=None, observed_at=NOW)
        forged = deepcopy(proposal)
        forged["receipt"]["operation_id"] = "other-operation"
        with self.assertRaises(TaskError):
            calendar_authority.authorize(app.calendar, context, request, forged, operation_key=operation_key,
                                         sources=(original,), operator=None, observed_at=NOW)
        self.revoke_source(app, original)
        with self.assertRaises(TaskError):
            life_commands.decide(self.runtime, context=context, request=request, outcome="commit", observed_at=NOW,
                verify_prepared=commands.verify_prepared, authorize=authorize, apply=lambda *_: None)
        self.assertEqual(commands.pending()[0]["state"], "prepared")
        self.assertFalse(OperationRepository(self.runtime).get(operation_id).is_success)

    async def test_reminder_cancellation_permission_is_checked_before_decision(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        self.grant(context.actor, SCOPE, "reminder.create")
        self.grant(context.actor, SCOPE, "reminder.deliver")
        self.grant(context.actor, SCOPE, "reminder.cancel")
        await self.start(host)
        data = calendar_data()
        self.event_command(app, context, identifier="event", action="create", revision=0, data=data)
        app.calendar.remind(context, identifier="event", expected_revision=1, operation_key="set-reminder")
        current = app.calendar.read(context, "event")
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_calendar(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        changes = StagedCalendarCommands(life)
        role_key, conversation_key = app.calendar.records.role_key(context), app.calendar.records.conversation_key(context)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        operation_key = "revise-with-reminder"
        operation_id = "calendar:" + digest([app.calendar.records.operation_key(context), operation_key])
        request = {"command_id": "calendar-reminder-authority", "owner_key": digest(owner), "context_key": context.key,
            "kind": "calendar.change", "expected_revision": current["revision"],
            "payload": {"storage_key": role_key, "lookup_owner_keys": [role_key, conversation_key],
                        "record_id": "event", "operation_id": operation_id,
                        "previous_hash": hashlib.sha256(encode(current).encode("utf-8")).hexdigest(),
                        "action": "revise", "data": data, "sources": [],
                        "actor": json.loads(encode(context.role_binding.role)), "operator_authenticated": False,
                        "planning": None, "observed_at": stamp(NOW)}}
        changes.prepare(request, prepared_at=NOW)
        proposal = changes.proposal(request)
        self.assertEqual(proposal["cancel_reminder"], current)
        self.assertTrue(calendar_authority.authorize(app.calendar, context, request, proposal,
                        operation_key=operation_key, sources=(), operator=None, observed_at=NOW).is_success)
        real_authorize = app.agenda._authorize
        def deny_cancel(actor, target, action, *args):
            if action == "reminder.cancel":
                raise AgendaError("reminder_cancel_denied", "simulated reminder permission denial")
            return real_authorize(actor, target, action, *args)
        with patch.object(app.agenda, "_authorize", side_effect=deny_cancel):
            with self.assertRaises(TaskError) as original_error:
                app.calendar.command(context, identifier="event", action="revise", expected_revision=current["revision"],
                                     operation_key=operation_key, data=data)
            with self.assertRaises(TaskError) as moved_error:
                calendar_authority.authorize(app.calendar, context, request, proposal, operation_key=operation_key,
                                             sources=(), operator=None, observed_at=NOW)
        self.assertEqual(moved_error.exception.code, original_error.exception.code)
        self.assertEqual(app.agenda.repository.reminder(current["reminder_id"]).state, "active")

    async def test_unreminded_change_preserves_original_calendar_audit_and_replay(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        await self.start(host)
        data = calendar_data()
        self.event_command(app, context, identifier="event", action="create", revision=0, data=data)
        current = app.calendar.read(context, "event")
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_calendar(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        PreparedCommands(life).install()
        changes = StagedCalendarCommands(life)
        role_key, conversation_key = app.calendar.records.role_key(context), app.calendar.records.conversation_key(context)
        owner = RoleOwnerRef(context.scope.installation_id, context.scope.bot_id, context.role_binding.role.subject_id)
        operation_key = "revise-without-reminder"
        operation_id = "calendar:" + digest([app.calendar.records.operation_key(context), operation_key])
        revised = {**data, "title": "新的安排"}
        request = {"command_id": "calendar-unreminded", "owner_key": digest(owner), "context_key": context.key,
            "kind": "calendar.change", "expected_revision": current["revision"],
            "payload": {"storage_key": role_key, "lookup_owner_keys": [role_key, conversation_key],
                        "record_id": "event", "operation_id": operation_id,
                        "previous_hash": hashlib.sha256(encode(current).encode("utf-8")).hexdigest(),
                        "action": "revise", "data": revised, "sources": [],
                        "actor": json.loads(encode(context.role_binding.role)), "operator_authenticated": False,
                        "planning": None, "observed_at": stamp(NOW)}}
        handoff = CalendarHandoff(app.calendar, changes)
        unauthorized = deepcopy(request)
        unauthorized["payload"]["actor"]["subject_id"] = "other-role"
        with self.assertRaises(TaskError):
            handoff.prepare(context, unauthorized, operation_key=operation_key, sources=(), operator=None, observed_at=NOW)
        self.assertEqual(PreparedCommands(life).pending(), ())
        handoff.prepare(context, request, operation_key=operation_key, sources=(), operator=None, observed_at=NOW)
        decision = handoff.decide(context, request, operation_key=operation_key, sources=(), operator=None, observed_at=NOW)
        handoff.apply(decision)
        self.assertTrue(handoff.complete(context, request, decision, observed_at=NOW))
        self.assertTrue(handoff.acknowledge(decision))
        result = StagedCalendarRecords(life).dispatch("get", owner_key=role_key, identifier="event")
        self.assertEqual((result["revision"], result["title"]), (2, "新的安排"))
        parameters = ["revise", revised, (), context.role_binding.role]
        expected_hash = digest([context.key, "event", current["revision"], parameters])
        receipt = OperationRepository(self.runtime).get(operation_id).unwrap()
        self.assertEqual(receipt.parameter_hash, expected_hash)
        self.assertEqual(handoff.prepare(context, request, operation_key=operation_key,
                                         sources=(), operator=None, observed_at=NOW)["state"], "acknowledged")
        self.assertEqual(handoff.decide(context, request, operation_key=operation_key,
                                        sources=(), operator=None, observed_at=NOW), decision)
        self.assertEqual(app.calendar.records.get(context, "event")["revision"], 1)


if __name__ == "__main__":
    unittest.main()
