import asyncio
from contextlib import contextmanager
from dataclasses import make_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import OperationalError
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_private_companion"))
pytest.importorskip("astrbot")
from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_ledger_binding import refresh_session_binding
from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID
from companion_v2.agenda.calendar import CalendarService, LIFE_CALENDAR_OWNER_KEY
from companion_v2.agenda.models import Schedule
from companion_v2.execution.models import TaskContext, TaskError, digest, encode, stamp
from companion_v2.foundation.outcome import Outcome
from companion_v2.foundation.refs import AudienceRef, ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.identity_policy.models import AuthenticatedHostPrincipal, ResolvedIdentity, RoleBinding
from companion_v2.integration.calendar_adoption_guard import CERTIFICATE_KEY, HASHES, SCHEMA
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY, LifeLedgerRouter
from companion_v2.role_work.ledger import PersistentRoleLifeLedger
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.storage.runtime import StorageRuntime


class LifeProviderBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.runtime = StorageRuntime.open(data_root=Path(temporary.name) / "host", installation_id="installation")
        self.addCleanup(self.runtime.close)
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        self.activity = RoleActivity("activity", "role", scope, datetime(2026, 1, 1, tzinfo=timezone.utc), RoleActivityState.ACTIVE)
        self.plugin = SimpleNamespace()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=self.plugin, activated=True)
        context = SimpleNamespace(get_all_stars=lambda: [star])
        self.controller = SimpleNamespace(context=context, _terminated=False, session=None)
        self.api = UnifiedCompanionExtensionAPI(self.controller)
        self.addCleanup(self.api.close_memory_providers)
        self.addCleanup(self.api.close_life_providers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))

    def application(self):
        return SimpleNamespace(runtime=self.runtime, role_life=LifeLedgerRouter(PersistentRoleLifeLedger(self.runtime)))

    def handlers(self, callback=None):
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["activity_read"] = callback or (lambda *_args, **_kwargs: encode(self.activity))
        return handlers

    def register(self, handlers=None):
        return self.api.register_life_provider(plugin=self.plugin, api_version=API_VERSION, handlers=handlers or self.handlers())

    def read(self, app):
        return app.role_life.activity("activity", owner=self.activity.owner)

    def test_provider_before_session_and_session_rebuild_revoke_old_leases(self):
        token = self.register()
        first = self.application()
        self.controller.session = SimpleNamespace(application=first)
        refresh_session_binding(self.controller)
        first_configuration_lease = self.runtime.configuration_store._life_configuration_lease
        self.assertEqual(self.read(first), self.activity)
        previous = first.role_life.activity
        second = self.application()
        self.controller.session = SimpleNamespace(application=second)
        refresh_session_binding(self.controller)
        self.assertTrue(first_configuration_lease.revoked)
        self.assertIsNot(first_configuration_lease, self.runtime.configuration_store._life_configuration_lease)
        self.assertEqual(self.read(second), self.activity)
        with self.assertRaises(TaskError):
            previous("activity", owner=self.activity.owner)
        self.assertTrue(self.api.unregister_life_provider(token))
        with self.assertRaises(TaskError):
            self.read(second)
        new_token = self.register()
        self.assertEqual(self.read(second), self.activity)
        with self.assertRaises(TaskError):
            previous("activity", owner=self.activity.owner)
        self.assertFalse(self.api.unregister_life_provider(token))
        self.assertTrue(self.api.unregister_life_provider(new_token))

    def test_session_before_provider_and_callback_can_unregister_itself(self):
        app = self.application()
        self.controller.session = SimpleNamespace(application=app)
        refresh_session_binding(self.controller)
        with self.assertRaises(TaskError):
            self.read(app)
        token = []
        def read_and_unregister(*_args, **_kwargs):
            self.assertTrue(self.api.unregister_life_provider(token[0]))
            return encode(self.activity)
        token.append(self.register(self.handlers(read_and_unregister)))
        self.assertEqual(self.read(app), self.activity)
        with self.assertRaises(TaskError):
            self.read(app)
        replacement = self.register()
        self.assertEqual(self.read(app), self.activity)
        self.assertTrue(self.api.unregister_life_provider(replacement))

    def test_temporarily_unready_provider_recovers_without_session_rebuild(self):
        app = self.application()
        self.controller.session = SimpleNamespace(application=app)
        active = [False]
        token = self.api.register_life_provider(plugin=self.plugin, api_version=API_VERSION,
            handlers=self.handlers(), is_active=lambda: active[0])
        with self.assertRaises(TaskError):
            self.read(app)
        active[0] = True
        self.assertEqual(self.read(app), self.activity)
        self.assertTrue(self.api.unregister_life_provider(token))

    def kernel_application(self):
        # Supply stand-ins through the same constructor negotiation as Life.
        constructors = {name: make_dataclass(name, fields.split(), frozen=True) for name, fields in {
            "ExecutionScope": "owner_key context_key authority_ref registration_generation",
            "AudienceBound": "permission_ref context_keys",
            "GateSnapshot": "state free_until busy capabilities",
            "StateSnapshot": "owner_key observed_at revision values",
        }.items()}
        self.plugin._runtime = SimpleNamespace(kernel_access_types=lambda: constructors)
        app = self.application()
        scope = self.activity.scope
        subject = SubjectRef(scope.installation_id, SubjectKind.HUMAN, "user")
        role = SubjectRef(scope.installation_id, SubjectKind.BOT, self.activity.owner.persona_id)
        principal = AuthenticatedHostPrincipal(scope.installation_id, "native", scope.platform,
                                                scope.native_scope_id, scope.bot_id, scope.account_id)
        context = TaskContext(ResolvedIdentity(principal, subject, "binding", 1, 1),
                              RoleBinding(scope, role, 1), AudienceRef(scope, (subject,)))
        routine = SimpleNamespace(context=context, instruction="persona", _workers={}, _closed=False, _retired=False)
        app.life_runtime = SimpleNamespace(routines={context.key: routine, "alias": routine})
        app.require_running = lambda: None
        app.now = lambda: self.activity.started_at
        allowed = lambda *_args, **_kwargs: Outcome.success(True)
        app.tasks = SimpleNamespace(runtime=self.runtime, generation=1, now=app.now,
            identity=SimpleNamespace(authorize=allowed), authorize_context=allowed, check_context_access=allowed)
        app.configuration = SimpleNamespace(store=self.runtime)
        app.role_state = SimpleNamespace(settings=lambda _context: ({"calendar_enabled": True}, "settings"))
        app.calendar = CalendarService(app.tasks, None, app.role_state)
        app.state_maintenance = SimpleNamespace(foreground_busy=lambda _context: False)
        self.controller.session = SimpleNamespace(application=app)
        return app, routine

    def test_kernel_access_rechecks_authority_and_revokes_on_session_and_registration_changes(self):
        app, routine = self.kernel_application()
        token = self.register()
        with self.assertRaises(TaskError):
            self.api.life_kernel_access(self.plugin, token, version=True)
        access = self.api.life_kernel_access(self.plugin, token)
        scope, = access.scopes()
        self.assertEqual(scope.owner_key, digest(self.activity.owner))
        self.assertEqual(access.audience(scope).context_keys, (routine.context.key,))
        self.assertTrue(access.is_current())
        self.assertTrue(access.authorize(scope, "kernel.start"))
        self.assertTrue(access.authorize(scope, "kernel.snapshot"))
        self.assertFalse(access.authorize(scope, "kernel.arbitrary"))
        self.assertFalse(access.authorize(replace(scope, authority_ref="forged"), "kernel.start"))
        self.assertFalse(access.authorize(replace(scope, owner_key="another-role"), "kernel.start"))
        check = app.tasks.authorize_context
        app.tasks.authorize_context = lambda *_args, **_kwargs: Outcome.failed("binding_rebound", "role changed")
        self.assertEqual(access.scopes(), ())
        self.assertFalse(access.authorize(scope, "kernel.start"))
        with self.assertRaises(TaskError):
            access.audience(scope)
        app.tasks.authorize_context = check
        self.controller.session = SimpleNamespace(application=app)
        refresh_session_binding(self.controller)
        self.assertFalse(access.is_current())
        self.assertFalse(access.authorize(scope, "kernel.start"))
        with self.assertRaises(TaskError):
            access.snapshot(scope, app.now())
        current = self.api.life_kernel_access(self.plugin, token)
        next_scope, = current.scopes()
        self.assertNotEqual(scope.registration_generation, next_scope.registration_generation)
        self.assertFalse(current.authorize(scope, "kernel.start"))
        self.assertTrue(self.api.unregister_life_provider(token))
        self.register()
        self.assertFalse(current.authorize(next_scope, "kernel.start"))

    def test_optional_liveness_failures_leave_legacy_registration_running(self):
        from good_life.runtime import LifeRuntime
        from good_life.storage import LifeStorage
        from test_life_runtime import Host

        app, _routine = self.kernel_application()
        registration = self.register()
        real_access = self.api.life_kernel_access(self.plugin, registration)
        host = Host()
        host.life_kernel_access = lambda **_: real_access

        @contextmanager
        def unavailable():
            raise OperationalError("transient host reader failure")
            yield

        async def verify(storage):
            runtime = LifeRuntime(storage, object(), kernel_enabled=True, wall_clock=app.now)
            try:
                await runtime.bind(host)
                token, scheduler, kernel = runtime.token, runtime.scheduler, runtime.kernel
                self.assertIsNotNone(kernel)
                with patch.object(app.runtime, "read_only_snapshot", unavailable):
                    self.assertIsNone(real_access.is_current())
                    await runtime.bind(host)
                self.assertIs(runtime.kernel, kernel)
                self.assertTrue(real_access.is_current())
                self.controller.session = SimpleNamespace(application=app)
                host.life_kernel_access = lambda **_: self.api.life_kernel_access(self.plugin, registration)
                await runtime.bind(host)
                self.assertTrue(kernel.closed)
                self.assertIsNotNone(runtime.kernel)
                self.assertIsNot(runtime.kernel, kernel)
                self.assertIs(runtime.token, token)
                self.assertIs(runtime.scheduler, scheduler)
                self.assertFalse(scheduler._stopped)
                self.assertEqual(host.unregistered, [])
            finally:
                await runtime.close()

        with TemporaryDirectory() as directory:
            storage = LifeStorage.open(directory)
            try:
                asyncio.run(verify(storage))
            finally:
                storage.close()

    def test_open_native_turn_only_blocks_the_matching_role(self):
        app, routine = self.kernel_application()
        token = self.register()
        access = self.api.life_kernel_access(self.plugin, token)
        first, = access.scopes()
        context = routine.context
        role = SubjectRef(context.scope.installation_id, SubjectKind.BOT, "other-role")
        other = replace(context, role_binding=replace(context.role_binding, role=role))
        app.life_runtime.routines[other.key] = SimpleNamespace(**{**vars(routine), "context": other})
        second, = (scope for scope in access.scopes() if scope.context_key == other.key)
        with patch("astrbot_host.life_kernel_access.context_rows", return_value=({"context_json": encode(other)},)):
            self.assertFalse(access.snapshot(first, app.now()).busy)
            self.assertTrue(access.snapshot(second, app.now()).busy)

    def test_kernel_snapshot_is_host_local_and_tracks_calendar_facts(self):
        app, routine = self.kernel_application()
        def unexpected_provider(*_args, **_kwargs):
            self.fail("kernel read invoked a provider or refreshed the old routine")
        routine.refresh = unexpected_provider
        token = self.register(dict.fromkeys(HANDLERS, unexpected_provider))
        access = self.api.life_kernel_access(self.plugin, token)
        scope, = access.scopes()
        now = app.now()
        with self.runtime.coordinated_snapshot(unexpected_provider):
            idle = access.snapshot(scope, now)
            self.assertEqual(access.snapshot(scope, now), idle)
        self.assertEqual(idle.state.observed_at, now)
        self.assertFalse(idle.busy)
        self.assertGreater(idle.free_until, now)
        start = now + timedelta(minutes=20)
        storage_key = app.calendar.records.role_key(routine.context)
        row = {"id": "plan", "revision": 1, "updated_at": stamp(now), "kind": "role_plan",
               "state": "accepted", "schedule": Schedule(start, "UTC"), "duration_minutes": 30,
               "title": "Plan", "detail": "", "presence": "", "exceptions": {}, "sources": []}
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO calendar_records VALUES (?, ?, ?, ?, ?)",
                       (storage_key, row["id"], row["revision"], row["updated_at"], encode(row)))
        planned = access.snapshot(scope, now)
        self.assertEqual(planned.free_until, start)
        self.assertNotEqual(planned.state.revision, idle.state.revision)
        self.assertTrue(access.snapshot(scope, start).busy)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_CALENDAR_OWNER_KEY,))
        delegated = access.snapshot(scope, start)
        self.assertEqual(delegated.capabilities, ("host.calendar.life_owned",))
        self.assertIsNone(delegated.free_until)
        self.assertFalse(delegated.busy)
        self.assertIsNone(access.calendar_keys(scope))
        certificate = {"schema": SCHEMA, "source_version": "1", **dict.fromkeys(HASHES, "0" * 64)}
        records = app.calendar._legacy_records
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, ?)", (CERTIFICATE_KEY, encode(certificate)))
        self.assertEqual(access.calendar_keys(scope), (storage_key, records.conversation_key(routine.context)))
        self.assertEqual(access.calendar_adoption_certificate(scope), certificate)

        def unavailable(_context):
            raise TaskError("foreground_unavailable", "foreground read failed")
        app.state_maintenance.foreground_busy = unavailable
        self.assertIsNone(access.snapshot(scope, now).busy)


if __name__ == "__main__":
    unittest.main()
