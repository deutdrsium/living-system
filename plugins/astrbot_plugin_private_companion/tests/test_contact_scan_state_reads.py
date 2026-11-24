"""A contact plan reads role state once, and that read does not scale with source ancestry."""

import sqlite3
import unittest
from unittest.mock import patch

import companion_v2.storage.runtime as storage_runtime
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.execution.models import TaskError, digest
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.state.maintenance import state_profile
from companion_v2.role_work.state.management import StateManagement
from companion_v2.role_work.state.service import FIELDS, RoleStateService
from companion_v2.sources.dependencies import link
from companion_v2.sources.governance import bind
from companion_v2.sources.models import SourceRecord
from tests.contact_schedule_support import ContactScheduleFixture


class ContactScanStateReadTests(ContactScheduleFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.statements = 0
        connect = sqlite3.connect

        def counting(*args, **kwargs):
            connection = connect(*args, **kwargs)
            connection.set_trace_callback(lambda _statement: setattr(self, "statements", self.statements + 1))
            return connection

        patcher = patch.object(storage_runtime.sqlite3, "connect", counting)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()
        self.profiles += (state_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="contact-scan-state", fields=configuration_fields(self.profiles))

    async def prepared(self):
        host, _ = await self.scheduled()
        app = host.application
        context = self.contact_context()
        manager = StateManagement(app, context)
        before = manager.status(self.core).unwrap()
        manager.command(self.core, dict(action="configure", operation_key="state-on",
            edit_token=before["edit_token"], role_edit_token=before["role_edit_token"],
            changes={"enabled": True})).unwrap()
        self.addCleanup(bind(app.runtime, "test", lambda record, at: None).close)
        return app, context

    def ancestry(self, app, context, prefix, hubs, leaves):
        now = self.clock.now()

        def publish(name):
            reference = SourceRef(name, "test", context.scope, VersionRef(name, 1), now)
            app.messages.sources.publish(SourceRecord(reference, context.audience, digest(name), name),
                                         observed_at=now).unwrap()
            return reference

        roots = []
        for h in range(hubs):
            hub = publish(f"{prefix}-hub-{h}")
            link(app.runtime, hub, [publish(f"{prefix}-leaf-{h}-{i}") for i in range(leaves)], now)
            roots.append(hub)
        return tuple(roots)

    def write_state(self, app, context, sources, revision):
        data = {name: [] if name == "conditions" else None for name in FIELDS} | {"energy": 60, "sleep": "有点困"}
        app.role_state.write(context, data=data, expected_revision=revision,
                             operation_key="state-" + str(revision), sources=sources)

    def statements_for_read(self, app, context):
        now = app.now()
        app.role_state.read(context, at=now)
        self.statements = 0
        app.role_state.read(context, at=now)
        return self.statements

    async def test_one_plan_reads_role_state_once(self):
        app, context = await self.prepared()
        self.write_state(app, context, self.ancestry(app, context, "plan", 3, 10), 0)
        # 08:10 local: the morning anchor asks for the role's state and its sleep hint.
        self.settings(app, {"enable_daily_greetings": True, "greeting_idle_minutes": 0,
                            "chat.enabled": False, "max_contacts_24h": 5}).unwrap()
        self.advance(app, 600)
        now = app.now()
        _, rows, *_ = app.contact_schedule._begin_tick()
        with patch.object(RoleStateService, "_read", autospec=True, side_effect=RoleStateService._read) as read:
            plan = app.contact_schedule._plan_one(rows[0], now)
        self.assertIn("greeting_event", plan[4])
        self.assertIn("精力=60", plan[2][0].thought.summary)
        self.assertEqual(read.call_count, 1)
        # Outside a scope every read stays independent.
        with patch.object(RoleStateService, "_read", autospec=True, side_effect=RoleStateService._read) as read:
            app.role_state.read(context, at=now)
            app.role_state.read(context, at=now)
        self.assertEqual(read.call_count, 2)

    async def test_read_statements_do_not_grow_with_source_ancestry(self):
        app, context = await self.prepared()
        self.write_state(app, context, self.ancestry(app, context, "small", 2, 5), 0)
        small = self.statements_for_read(app, context)
        self.write_state(app, context, self.ancestry(app, context, "large", 8, 40), 1)
        large = self.statements_for_read(app, context)
        # The control and visibility walks each prefetch the graph in one batch (life-owned
        # state calls between them keep the batch per walk); a guarded source is loaded once
        # per walk for its owner check, never with one query per node.
        reachable = 1 + 8 * 41
        self.assertLess(large - small, 2 * (reachable + 8))
        self.assertLess(large, 2 * reachable + 80, large)

    async def test_scoped_read_matches_unscoped_and_replays_its_failure(self):
        app, context = await self.prepared()
        self.write_state(app, context, self.ancestry(app, context, "same", 2, 3), 0)
        now = app.now()
        with app.role_state.read_scope():
            scoped = app.role_state.read(context, at=now)
        self.assertEqual(scoped, app.role_state.read(context, at=now))
        failure = TaskError("role_state_denied", "denied")
        with patch.object(RoleStateService, "_read", autospec=True, side_effect=failure) as read:
            with app.role_state.read_scope():
                for _ in range(2):
                    with self.assertRaises(TaskError):
                        app.role_state.read(context, at=now)
        self.assertEqual(read.call_count, 1)


if __name__ == "__main__":
    unittest.main()
