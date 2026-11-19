from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import TaskContext, TaskError
from companion_v2.foundation.refs import AudienceRef
from companion_v2.identity_policy.personas import ConversationRoute
from companion_v2.integration.calendar_records_proxy import LifeCalendarRecords
from companion_v2.storage.versioned_records import VersionedRecords
from good_life.calendar_import import stage_calendar
from good_life.calendar_records import StagedCalendarRecords
from good_life.storage import LifeStorage
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, calendar_data


class CalendarRecordsProxyTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def test_calendar_reads_preserve_windows_legacy_owners_and_source_revocation(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True)
        await self.start(host)
        source = self.source(app, context, "calendar-proxy-source")
        receipt = app.calendar.command(context, identifier="plan", action="create", expected_revision=0,
                                       operation_key="plan-create", data=calendar_data(), sources=(source,))
        app.calendar.command(context, identifier="report", action="create", expected_revision=0,
                             operation_key="report-create", data=calendar_data(kind="user_report"))
        legacy = VersionedRecords(self.runtime, "calendar")
        legacy.change(context, "legacy", operation_key="legacy", expected_revision=0, parameters=["legacy"], at=NOW,
            transform=lambda _current: {**calendar_data(), "state": "accepted", "sources": [], "exceptions": {},
                "details": {}, "accepted_by": [], "changed_by": context.role_binding.role, "reminder_id": None})
        app.calendar.records._legacy_cache.clear()
        other_scope = replace(context.scope, native_scope_id="second-private", conversation_id="conversation-second",
                              session_id="session-second")
        self.personas.register_route(ConversationRoute(other_scope, user=context.actor.subject), observed_at=NOW)
        other = TaskContext(context.actor, self.identity.resolve_role(other_scope, observed_at=NOW).unwrap(),
                            AudienceRef(other_scope, (context.actor.subject,)))
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_calendar(self.runtime.database_path, expected_version=version, destination=Path(temporary.name) / "life")
        life = LifeStorage.open(Path(temporary.name) / "life")
        self.addCleanup(life.close)
        original = app.calendar.records
        proxy = LifeCalendarRecords(StagedCalendarRecords(life), legacy_owners=lambda value: tuple(
            {"owner_key": item["owner_key"], "record_id": item["record"]["id"]}
            for item in original._legacy_role_plans(value)))
        for value in (context, other):
            for identifier in ("plan", "report", "legacy", "missing"):
                self.assertEqual(proxy.get(value, identifier), original.get(value, identifier))
            self.assertEqual(proxy.list(value), original.list(value))
            self.assertEqual(proxy.shared_role_plans(value), original.shared_role_plans(value))
            self.assertEqual(proxy.version(value, "plan", 1), original.version(value, "plan", 1))
        self.assertEqual(proxy.receipt(context, receipt["operation_id"]), original.receipt(context, receipt["operation_id"]))
        self.assertEqual(proxy.get(other, "legacy")["id"], "legacy")
        self.assertIsNone(proxy.get(other, "report"))
        app.calendar.records = proxy
        self.assertEqual(app.calendar.read(context, "plan")["id"], "plan")
        self.revoke_source(app, source)
        with self.assertRaises(TaskError) as unavailable:
            app.calendar.read(context, "plan")
        self.assertEqual(unavailable.exception.code, "calendar_unavailable")

    async def test_unavailable_provider_does_not_read_old_calendar(self):
        _host, app, context, _manager = self.setup_state()
        class Unavailable:
            def dispatch(self, *_args, **_kwargs):
                raise RuntimeError("provider stopped")
        proxy = LifeCalendarRecords(Unavailable(), legacy_owners=lambda _context: ())
        with self.assertRaises(TaskError) as error:
            proxy.get(context, "event")
        self.assertEqual(error.exception.code, "life_provider_unavailable")


if __name__ == "__main__":
    unittest.main()
