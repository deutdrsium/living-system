import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins" / "astrbot_plugin_good_life"))
from good_life.runtime import LifeRuntime, OptionalGroupBinding
from good_life.activity_import import DDL
from good_life.activities.local_order import TEMPLATE, register_local_order
from good_life.contracts.models import ActivityStatus, StateQuality, TemplateRef
from good_life.kernel.codec import KernelError
from good_life.reading_execution import ReadingExecutionStore
from good_life.staged_activity_creations import StagedActivityCreations
from test_kernel_support import Access, NOW
from datetime import timedelta
from good_life.storage import LifeStorage


class Host:
    def __init__(self):
        self.token = None
        self.handlers = None
        self.unregistered = []

    def register_life_provider(self, *, plugin, api_version, handlers, is_active):
        assert api_version == "companion.life.v1"
        self.handlers, self.active = handlers, is_active
        self.token = object()
        return self.token

    def life_host_capabilities(self, *, plugin, registration_id):
        assert registration_id is self.token and self.active()
        async def action(*_args):
            return {"state": "committed"}
        return {"api_version": "companion.life-host.v1", "contexts": lambda: (), "wake": action, "recover": action}

    def unregister_life_provider(self, token):
        self.unregistered.append(token)


class LifeRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rebinding_stops_previous_scheduler_and_revokes_registration(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = LifeStorage.open(temporary.name)
        self.addCleanup(storage.close)
        runtime = LifeRuntime(storage, object())
        first, second = Host(), Host()
        await runtime.bind(first)
        self.assertEqual(set(first.handlers), {"calendar_read", "calendar_command", "configuration_read", "configuration_command",
                                               "activity_read", "activity_control",
                                               "activity_history", "browsing_read", "browsing_command", "state_read", "state_command", "state_schedule"})
        original = runtime.scheduler
        await runtime.bind(first)
        self.assertIs(runtime.scheduler, original)
        await runtime.bind(second)
        self.assertFalse(first.active())
        self.assertEqual(first.unregistered, [first.token])
        self.assertTrue(original._stopped)
        await runtime.close()
        self.assertEqual(second.unregistered, [second.token])
        self.assertFalse(runtime.ready)

    def make_runtime(self, **settings):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        storage = LifeStorage.open(temporary.name)
        self.addCleanup(storage.close)
        runtime = LifeRuntime(storage, object(), **settings)
        self.addAsyncCleanup(runtime.close)
        return runtime

    async def test_optional_negotiation_retries_keep_provider_ready_and_receipts(self):
        clock = [0]
        runtime = self.make_runtime(kernel_enabled=True, reading_executor_enabled=True,
            reading_abort_own_enabled=True, monotonic_clock=lambda: clock[0])
        host = Host()
        async def legacy(*_args):
            return {"state": "legacy"}
        base_capabilities = host.life_host_capabilities
        host.life_host_capabilities = lambda **kwargs: {**base_capabilities(**kwargs), **dict.fromkeys(
            ("reading_snapshot", "reading_choice_result", "choose_reading", "start_reading", "execute_reading",
             "recover_reading", "execute_manual_reading"), legacy)}
        store = ReadingExecutionStore(runtime.storage)
        original = {"execution_owner": "life", "activity_id": "original", "context_key": "context",
                    "phase": "unknown", "task_id": "original-task", "command": {"command_id": "original-receipt"}}
        with runtime.storage.transaction() as db:
            for statement in DDL:
                db.execute(statement)
            db.execute("CREATE TABLE staged_life_runtime_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        store.save("context", "original", original)
        calls = []
        def ports(**kwargs):
            self.assertTrue(runtime.ready)
            calls.append(kwargs["contract_version"])
            if clock[0] == 0 or kwargs["contract_version"] != 1:
                raise RuntimeError("unavailable")
            keys = ("resolve_authority", "get_persona_context", "read_material", "run_model_task",
                    "inspect_task", "execute_platform_action", "accept_life_event")
            return {"api_version": "companion.life-host.async.v1", "contract_version": 1,
                    "capabilities": keys, "ports": SimpleNamespace(**dict.fromkeys(keys, legacy))}
        host.life_async_capabilities = ports
        await runtime.bind(host)
        scheduler = runtime.scheduler.reading_scheduler
        self.assertEqual(set(runtime.optional_diagnostics), {"reading_execute", "reading_recovery", "reading_abort_own", "kernel"})
        self.assertEqual(await scheduler._manual("context", {"activity_id": "original", "operation_id": "original-task"}), {"state": "paused"})
        self.assertEqual(await scheduler._manual("context", {"activity_id": "new", "operation_id": "new-task"}), {"state": "legacy"})
        self.assertEqual(store.get("context", "original"), original)
        await runtime.bind(host)
        self.assertEqual(calls, [1, 1, 2])
        clock[0] = 1
        await runtime.bind(host)
        self.assertTrue(runtime.ready)
        self.assertIs(scheduler, runtime.scheduler.reading_scheduler)
        self.assertIsNotNone(scheduler.executor)
        self.assertIsNotNone(scheduler.recovery_executor)
        self.assertIsNone(scheduler.coordination_ports)
        self.assertEqual(store.get("context", "original"), original)
        self.assertEqual(host.unregistered, [])

    async def test_optional_binding_recovers_after_the_initial_retry_burst(self):
        clock = [0]
        runtime = self.make_runtime(kernel_enabled=True, monotonic_clock=lambda: clock[0])
        host, access = Host(), Access()
        attempted = []
        def negotiate(**_):
            attempted.append(clock[0])
            if clock[0] < 61:
                raise ValueError("host cannot negotiate")
            return access
        host.life_kernel_access = negotiate
        for instant in (0, 1, 3, 7, 15, 31, 60, 61):
            clock[0] = instant
            await runtime.bind(host)
            self.assertTrue(runtime.ready)
        self.assertEqual(attempted, [0, 1, 3, 7, 15, 31, 61])
        self.assertIsNotNone(runtime.kernel)
        self.assertEqual(runtime.optional_diagnostics, {})
        self.assertEqual(host.unregistered, [])

    async def test_kernel_groups_project_facts_and_stop_before_unregister(self):
        runtime = self.make_runtime(kernel_enabled=True, wall_clock=lambda: NOW)
        host, access = Host(), Access()
        host.life_kernel_access = lambda **_: access
        order, projection = [], [{"revision": 1}]
        delivered = asyncio.Event()
        def pre(context):
            self.assertTrue(context.runtime.ready)
            self.assertIsNone(context.kernel)
            register_local_order(context.registry)
            return OptionalGroupBinding(value="travel", settle=lambda **_: order.append("settle"),
                projections={"geo.shared_position": lambda snapshot: projection[0]})
        def post(context):
            self.assertEqual(context.groups["travel"], "travel")
            self.assertIs(context.kernel, runtime.kernel)
            async def deliver(**_):
                order.append("deliver")
                delivered.set()
                try:
                    await asyncio.Future()
                finally:
                    order.append("cancel")
            return OptionalGroupBinding(commitments=lambda **_: order.append("commitments"),
                propose=lambda **_: order.append("propose"), deliver=deliver, close=lambda: order.append("close"),
                projections={"study.status": lambda _: 1 / 0})
        runtime.register_optional_group("travel", pre, phase="pre_kernel")
        runtime.register_optional_group("study", post, requires=("travel",))
        runtime.register_optional_group("duplicate", lambda _: OptionalGroupBinding(projections={"geo.shared_position": lambda _: 2}))
        original_tick = runtime.advance_kernel
        runtime.advance_kernel = lambda **options: (order.append("tick"), original_tick(**options))[1]
        await runtime.bind(host)
        await asyncio.wait_for(delivered.wait(), timeout=2)
        worker = runtime._optional.worker
        await runtime.bind(host)
        self.assertIs(runtime._optional.worker, worker)
        self.assertEqual(order, ["settle", "commitments", "propose", "tick", "deliver"])
        first = runtime.kernel._snapshot(access.scope, NOW)
        self.assertEqual(first.state.values["study.status"].quality, StateQuality.UNKNOWN)
        self.assertIn("duplicate", runtime.optional_diagnostics)
        self.assertIn("study.projections", runtime.optional_diagnostics)
        projection[0] = {"revision": 2}
        self.assertNotEqual(first.state.revision, runtime.kernel._snapshot(access.scope, NOW).state.revision)
        kernel = runtime.kernel
        await runtime.unbind()
        self.assertEqual(order[-2:], ["cancel", "close"])
        self.assertTrue(worker.done())
        self.assertTrue(kernel.closed)
        self.assertFalse(kernel.access.authorize(access.scope, "kernel.start"))
        self.assertEqual(host.unregistered, [host.token])
        await runtime.bind(host)
        self.assertIsNot(runtime.kernel, kernel)
        self.assertIsNot(runtime._optional.worker, worker)

    async def test_prepare_rolls_back_if_lease_is_revoked_during_executor_callback(self):
        runtime = self.make_runtime(kernel_enabled=True, wall_clock=lambda: NOW)
        host, access = Host(), Access()
        host.life_kernel_access = lambda **_: access
        entered, revoked, finished = Event(), Event(), Event()
        loop = asyncio.get_running_loop()

        async def close():
            revoked.set()
            await loop.run_in_executor(None, finished.wait, 3)

        def bind(context):
            register_local_order(context.registry)
            return OptionalGroupBinding(close=close)

        runtime.register_optional_group("fixture", bind, phase="pre_kernel")
        await runtime.bind(host)
        await runtime._optional.stop_worker()
        kernel, scope = runtime.kernel, access.scope
        candidate = kernel.propose(scope, TemplateRef(TEMPLATE.id, 1), opportunity_key="racing",
            reason="tidy_requested", now=NOW, expires_at=NOW + timedelta(hours=1))
        executor = kernel.registry.executor(TEMPLATE)
        original = executor.prepare

        def prepare(*args):
            entered.set()
            self.assertTrue(revoked.wait(3))
            return original(*args)

        def run():
            try:
                return kernel.prepare_candidate(scope, candidate.candidate_id, command_id="late-prepare", now=NOW)
            finally:
                finished.set()

        with patch.object(executor, "prepare", prepare):
            work = loop.run_in_executor(None, run)
            self.assertTrue(await loop.run_in_executor(None, entered.wait, 2))
            await runtime.unbind()
            with self.assertRaises(KernelError) as error:
                await work
        self.assertEqual(error.exception.code, "unauthorized")
        self.assertFalse(kernel.repo.foreground_busy(scope.owner_key))
        self.assertEqual(kernel.repo.candidate(scope.owner_key, candidate.candidate_id)[1], candidate)

    async def test_cancelled_callback_is_isolated_and_finished_worker_restarts(self):
        runtime = self.make_runtime(kernel_enabled=True, wall_clock=lambda: NOW)
        host, access = Host(), Access()
        host.life_kernel_access = lambda **_: access
        delivered = asyncio.Event()

        async def settle(**_):
            asyncio.current_task().cancel()
            await asyncio.Future()

        runtime.register_optional_group("cancelled", lambda _: OptionalGroupBinding(settle=settle))
        runtime.register_optional_group("later", lambda _: OptionalGroupBinding(deliver=lambda **_: delivered.set()))
        await runtime.bind(host)
        worker = runtime._optional.worker
        await asyncio.wait_for(delivered.wait(), 2)
        self.assertFalse(worker.done())
        self.assertEqual(runtime.optional_diagnostics["cancelled.settle"]["reason"], "optional_work_failed")
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        delivered.clear()
        await runtime.bind(host)
        self.assertIsNot(runtime._optional.worker, worker)
        await asyncio.wait_for(delivered.wait(), 2)
        self.assertEqual(host.unregistered, [])

    async def test_settlement_binds_disabled_dependencies_without_history(self):
        runtime = self.make_runtime(wall_clock=lambda: NOW)
        host, access = Host(), Access()
        host.life_kernel_access = lambda **_: access
        settled, bound = asyncio.Event(), []

        def trip(context):
            self.assertEqual(context.mode, "settle_only")
            self.assertEqual(context.groups["lookup"], "lookup-value")
            return OptionalGroupBinding(settle=lambda **_: settled.set())

        def lookup(context):
            bound.append(context.mode)
            return OptionalGroupBinding(value="lookup-value", propose=lambda **_: self.fail("lookup proposed new work"))

        runtime.register_optional_group("trip", trip, phase="pre_kernel", enabled=False,
            requires=("lookup",), has_history=lambda _: True)
        runtime.register_optional_group("lookup", lookup, phase="pre_kernel", enabled=False)
        await runtime.bind(host)
        await asyncio.wait_for(settled.wait(), 2)
        self.assertEqual(bound, ["settle_only"])
        self.assertEqual(runtime.optional_diagnostics["lookup"]["reason"], "capability_disabled")

    async def test_cancelled_calendar_rows_do_not_consume_the_commitment_limit(self):
        from good_life.kernel.binding import _agenda
        from good_life.storage import StorageReader

        runtime = self.make_runtime()
        with runtime.storage.transaction() as db:
            db.execute("CREATE TABLE staged_calendar_records (owner_key TEXT, record_id TEXT, record_json TEXT)")
            db.executemany("INSERT INTO staged_calendar_records VALUES (?,?,?)", [
                ("role", str(index), json.dumps({"kind": "role_plan", "state": "cancelled"})) for index in range(201)])
        reader, end = StorageReader(runtime.storage), NOW + timedelta(hours=1)
        self.assertEqual(_agenda(reader, ("role",), NOW, end), (end, ()))
        start = NOW + timedelta(minutes=20)
        accepted = json.dumps({"kind": "commitment", "state": "accepted", "duration_minutes": 30,
                               "schedule": {"anchor": start.isoformat(), "timezone_name": "UTC"}, "exceptions": {}})
        with runtime.storage.transaction() as db:
            db.execute("INSERT INTO staged_calendar_records VALUES (?,?,?)", ("role", "z", accepted))
        self.assertEqual(_agenda(reader, ("role",), NOW, end), (start, (accepted,)))

    async def test_default_off_keeps_existing_schema_and_never_negotiates(self):
        runtime = self.make_runtime()
        host = Host()
        def unexpected(**kwargs):
            self.fail(f"disabled group was negotiated: {kwargs.keys()}")
        host.life_kernel_access = host.life_async_capabilities = unexpected
        runtime.register_optional_group("network", unexpected, phase="pre_kernel")
        with runtime.storage._lock:
            schema = tuple(runtime.storage._connection.iterdump())
        await runtime.bind(host)
        self.assertTrue(runtime.ready)
        self.assertIsNone(runtime.kernel)
        self.assertIsNone(runtime._optional.worker)
        with runtime.storage._lock:
            self.assertEqual(tuple(runtime.storage._connection.iterdump()), schema)

    async def test_concurrent_legacy_and_kernel_admission_across_two_sessions(self):
        runtime = self.make_runtime(kernel_enabled=True)
        with runtime.storage.transaction() as db:
            for statement in DDL:
                db.execute(statement)
        host, access = Host(), Access()
        host.life_kernel_access = lambda **_: access
        runtime.register_optional_group("test", lambda ctx: OptionalGroupBinding(register_local_order(ctx.registry)),
                                        phase="pre_kernel")
        await runtime.bind(host)
        creations = StagedActivityCreations(runtime.storage)
        owner = {"installation_id": "install", "bot_id": "bot", "persona_id": "role"}
        from good_life.staged_activity_commands import _digest_json
        scope = replace(access.scope, owner_key=_digest_json(owner))
        other = replace(scope, context_key="second-session")
        access.inventory = (scope, other)
        legacy_scope = {"installation_id": "install", "bot_id": "bot", "account_id": "acct", "platform": "local",
                        "kind": "private", "native_scope_id": "native", "session_id": "session", "conversation_id": "conv"}
        legacy = creations.propose_social(owner=owner, scope=legacy_scope, context_key=other.context_key,
                                          request_id="original-intent", observed_at=NOW.isoformat())
        request = {"command_id": "original-legacy-command", "owner_key": scope.owner_key, "context_key": other.context_key,
            "kind": "activity.create", "expected_revision": 0, "payload": {"owner": owner, "activity": legacy}}
        candidate = runtime.kernel.propose(scope, TemplateRef(TEMPLATE.id, 1), opportunity_key="tidy", reason="tidy_requested",
                                            now=NOW, expires_at=NOW + timedelta(hours=1))
        barrier = Barrier(2)
        def start_legacy():
            barrier.wait()
            try:
                return creations.prepare(request, prepared_at=NOW)["state"] == "prepared"
            except ValueError as error:
                self.assertIn("kernel foreground", str(error))
                return False
        def start_kernel():
            barrier.wait()
            return runtime.kernel.prepare_candidate(scope, candidate.candidate_id, command_id="kernel-prepare", now=NOW).gate.allowed
        with ThreadPoolExecutor(max_workers=2) as threads:
            legacy_result = threads.submit(start_legacy)
            kernel_result = threads.submit(start_kernel)
            self.assertEqual(int(legacy_result.result()) + int(kernel_result.result()), 1)
        if legacy_result.result():
            self.assertIsNotNone(creations.request_for_replay(command_id=request["command_id"], owner_key=scope.owner_key,
                context_key=other.context_key))

    async def test_settle_only_does_not_resume_persisted_dynamic_template(self):
        from good_life.activities.local_order import LocalOrder, LocalTimedUnits, SPEC

        runtime = self.make_runtime(kernel_enabled=True, wall_clock=lambda: NOW)
        host, access = Host(), Access()
        host.life_kernel_access = lambda **_: access
        reference = TemplateRef(TEMPLATE.id, TEMPLATE.version)

        def bind(context):
            context.registry.register_module(SPEC, LocalOrder())
            context.registry.register_executor(LocalTimedUnits())
            return OptionalGroupBinding()

        runtime.register_optional_group("fixture", bind, phase="pre_kernel")
        await runtime.bind(host)
        await runtime._optional.stop_worker()
        kernel, scope = runtime.kernel, access.scope
        kernel.registry.register_template(TEMPLATE)
        candidate = kernel.propose(scope, reference, opportunity_key="dynamic", reason="tidy_requested",
            now=NOW, expires_at=NOW + timedelta(hours=1))
        prepared = kernel.prepare_candidate(scope, candidate.candidate_id, command_id="original-prepare", now=NOW).instance
        instance = kernel.start(scope, prepared.instance_id, expected_revision=prepared.revision,
                                command_id="original-start", now=NOW)
        await runtime.unbind()

        restarted = LifeRuntime(runtime.storage, object(), wall_clock=lambda: NOW)
        self.addAsyncCleanup(restarted.close)
        restarted.register_optional_group("fixture", bind, phase="pre_kernel", enabled=False,
            has_history=lambda reader: reader.one("SELECT 1 FROM life_kernel_instances") is not None)
        await restarted.bind(host)
        await restarted._optional.stop_worker()
        kernel = restarted.kernel
        executor = kernel.registry.executor(TEMPLATE)
        with patch.object(executor, "resume", wraps=executor.resume) as resume:
            report = restarted.advance_kernel(now=NOW + timedelta(seconds=1))
        resume.assert_not_called()
        self.assertEqual(report["failures"], ())
        self.assertEqual(kernel.repo.instance(scope.owner_key, instance.instance_id), instance)
        with self.assertRaisesRegex(KernelError, "capability_disabled"):
            kernel.advance(scope, instance.instance_id, expected_revision=instance.revision,
                           command_id="disabled-advance", now=NOW + timedelta(seconds=1))

    async def test_disabled_group_settles_running_instance_once_on_restart(self):
        runtime = self.make_runtime(kernel_enabled=True, wall_clock=lambda: NOW)
        host, access = Host(), Access()
        host.life_kernel_access = lambda **_: access
        reference = TemplateRef(TEMPLATE.id, 1)
        bound_modes, calls = [], []
        finished = asyncio.Event()
        def bind(context):
            bound_modes.append(context.mode)
            register_local_order(context.registry)
            def settle(*, now):
                if context.mode != "settle_only":
                    return
                kernel = context.runtime.kernel
                for instance in kernel.repo.active(access.scope.owner_key):
                    if instance.template_id == reference.id:
                        kernel.cancel(access.scope, instance.instance_id, expected_revision=instance.revision,
                                      command_id="settle-original", now=now)
                finished.set()
            return OptionalGroupBinding(settle=settle, propose=lambda **_: calls.append("propose"),
                commitments=lambda **_: calls.append("commitments"), deliver=lambda **_: calls.append("deliver"),
                projections={"local_order.status": lambda _: "recorded"})
        def has_history(reader):
            return reader.one("SELECT 1 FROM sqlite_master WHERE name='life_local_order_units'") is not None
        runtime.register_optional_group("movement_fixture", bind, phase="pre_kernel", has_history=has_history)
        await runtime.bind(host)
        candidate = runtime.kernel.propose(access.scope, reference, opportunity_key="original-walk", reason="tidy_requested",
                                            now=NOW, expires_at=NOW + timedelta(hours=1))
        runtime.advance_kernel(now=NOW)
        instance, = runtime.kernel.repo.active(access.scope.owner_key)
        self.assertEqual(instance.status, ActivityStatus.RUNNING)
        await runtime.close()
        calls.clear()
        restarted = LifeRuntime(runtime.storage, object(), kernel_enabled=False, wall_clock=lambda: NOW)
        self.addAsyncCleanup(restarted.close)
        restarted.register_optional_group("movement_fixture", bind, phase="pre_kernel", enabled=False, has_history=has_history)
        restarted.register_optional_group("no_history", lambda _: calls.append("unexpected_bind"),
                                          phase="pre_kernel", enabled=False, has_history=lambda _: False)
        await restarted.bind(host)
        await asyncio.wait_for(finished.wait(), 2)
        self.assertEqual(bound_modes, ["active", "settle_only"])
        self.assertEqual(calls, [])
        settled = restarted.kernel.repo.instance(access.scope.owner_key, instance.instance_id)
        self.assertEqual(settled.status, ActivityStatus.CANCELLED)
        snapshot = restarted.kernel._snapshot(access.scope, NOW)
        self.assertEqual(snapshot.state.values["local_order.status"].value, "recorded")
        with self.assertRaisesRegex(KernelError, "capability_disabled") as denied:
            restarted.kernel.propose(access.scope, reference, opportunity_key="new-walk", reason="tidy_requested",
                                     now=NOW, expires_at=NOW + timedelta(hours=1))
        self.assertEqual(denied.exception.code, "missing_dependency")
        restarted.advance_kernel(now=NOW)
        self.assertEqual(restarted.kernel.repo.instance(access.scope.owner_key, instance.instance_id), settled)
        with runtime.storage._lock:
            receipt, = runtime.storage._connection.execute("SELECT result_id FROM life_kernel_results").fetchall()
        self.assertEqual(receipt[0], "kernel-result-" + instance.instance_id)
        self.assertIsNotNone(candidate.candidate_id)


if __name__ == "__main__":
    unittest.main()
