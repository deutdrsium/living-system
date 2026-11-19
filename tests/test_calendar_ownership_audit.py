from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
sys.path.insert(0, str(ROOT / "tools"))
from audit_calendar_ownership import inspect
from companion_v2.execution.models import TaskContext
from companion_v2.foundation.refs import AudienceRef
from companion_v2.identity_policy.personas import ConversationRoute
from companion_v2.integration.calendar_ownership_audit import audit_calendar_ownership
from companion_v2.storage.versioned_records import VersionedRecords
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, calendar_data


class CalendarOwnershipAuditTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def old_record(self, context, identifier, key, *, storage_key=None, reminder=None):
        records = VersionedRecords(self.runtime, "calendar")
        return records.change(context, identifier, operation_key=key, expected_revision=0, parameters=[key], at=NOW,
            storage_key=storage_key, transform=lambda _current: {
                **calendar_data(), "state": "accepted", "sources": [], "exceptions": {}, "details": {},
                "accepted_by": [], "changed_by": context.role_binding.role, "reminder_id": reminder})

    async def test_read_only_inventory_covers_shared_cross_window_and_old_reminders(self):
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        await self.start(host)
        app.calendar.command(context, identifier="shared", action="create", expected_revision=0,
                             operation_key="shared", data=calendar_data())
        app.calendar.command(context, identifier="report", action="create", expected_revision=0,
                             operation_key="report", data=calendar_data(kind="user_report"))
        other_scope = replace(context.scope, native_scope_id="second-private", conversation_id="conversation-second",
                              session_id="session-second")
        self.personas.register_route(ConversationRoute(other_scope, user=context.actor.subject), observed_at=NOW)
        other = TaskContext(context.actor, self.identity.resolve_role(other_scope, observed_at=NOW).unwrap(),
                            AudienceRef(other_scope, (context.actor.subject,)))
        self.old_record(other, "legacy", "legacy")
        self.old_record(context, "pinned", "pinned", reminder="old-reminder")
        before = tuple(tuple(row) for row in self.runtime.read_all(
            "SELECT * FROM calendar_records ORDER BY owner_key, record_id", ()))
        report = audit_calendar_ownership(self.runtime, self.identity)
        self.assertEqual(report["summary"], {"records": 4, "other": 1, "shared": 1, "adoptable": 1, "pinned": 1})
        self.assertEqual(report["moves"][0]["from_owner"], app.calendar.records.conversation_key(other))
        self.assertEqual(report["moves"][0]["to_owner"], app.calendar.records.role_key(context))
        self.assertEqual(report["moves"][0]["record_id"], "legacy")
        self.assertEqual(before, tuple(tuple(row) for row in self.runtime.read_all(
            "SELECT * FROM calendar_records ORDER BY owner_key, record_id", ())))
        self.assertEqual(report, audit_calendar_ownership(self.runtime, self.identity))
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        self.assertEqual(inspect(self.runtime.database_path, expected_version=version),
                         {"state": "inspected", "source_version": version, **report["summary"]})

    async def test_collision_is_reported_instead_of_silently_overwriting(self):
        _host, app, context, _manager = self.setup_state()
        self.old_record(context, "same", "legacy")
        self.old_record(context, "same", "shared", storage_key=app.calendar.records.role_key(context))
        with self.assertRaisesRegex(ValueError, "ownership collision"):
            audit_calendar_ownership(self.runtime, self.identity)

    async def test_unverifiable_owner_and_missing_operation_stop_the_inspection(self):
        _host, _app, context, _manager = self.setup_state()
        self.old_record(context, "legacy", "legacy")
        other_role = replace(context.role_binding.role, subject_id="another-role")
        result = SimpleNamespace(is_success=True, unwrap=lambda: replace(context.role_binding, role=other_role))
        with patch.object(self.identity, "resolve_role", return_value=result):
            with self.assertRaisesRegex(ValueError, "owner cannot be verified"):
                audit_calendar_ownership(self.runtime, self.identity)
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET commit_state='unknown' WHERE owner='calendar'")
        with self.assertRaisesRegex(ValueError, "operation is not confirmed"):
            audit_calendar_ownership(self.runtime, self.identity)
        with self.runtime.transaction() as db:
            db.execute("UPDATE operations SET commit_state='committed' WHERE owner='calendar'")
            db.execute("DELETE FROM calendar_history")
        with self.assertRaisesRegex(ValueError, "operation is not confirmed"):
            audit_calendar_ownership(self.runtime, self.identity)


if __name__ == "__main__":
    unittest.main()
