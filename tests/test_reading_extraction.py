"""A3 exercises real host supervision + life ledger, with a local model double."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
               str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tests")]
from tests.test_role_state import StateFixture
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.reader import reading_profile, book_choice_profile
from tests.test_management_access import NOW
from tests.test_role_tool_loop import answered
from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
from companion_v2.execution.models import digest, encode
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.integration.reading_handoff import ReadingStartHandoff
from companion_v2.integration import life_event_inbox
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.life.reading_queue import queue_key
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from good_life.activity_import import stage_activities
from good_life.exposure_import import stage_exposures
from good_life.metadata_import import stage_metadata
from good_life.storage import LifeStorage
from good_life.runtime import LifeRuntime
from good_life.reading_execution import ReadingExecutor, ReadingExecutionStore, response_types
from good_life.contracts import ports as p, models as m


class ReadingExtractionFixture(StateFixture):
    def setUp(self):
        super().setUp()
        self.profiles += (reading_profile("primary", max_output_tokens=100), book_choice_profile("primary", max_output_tokens=100))
        self.registry = FieldRegistry(version="a3-reading-tests", fields=configuration_fields(self.profiles))

    async def fixture(self, *, life_executor=True, start=True):
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        self.grant(context.actor, context.scope, "role.reading.record")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        status = manager.status(self.core)
        configured = await manager.command(self.core, {"action": "configure", "operation_key": "a3-enable",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False, "reading_enabled": True, "reading_chunk_chars": 200}})
        self.assertTrue(configured.is_success, configured)
        routine.refresh()
        text = "原文" * 305
        source = SourceRef("a3-book", "library", context.scope, VersionRef("a3-book", 1), NOW)
        app.role_journal.sources.publish(SourceRecord(source, context.audience, digest(text), text), observed_at=NOW).unwrap()
        activity = RoleActivity("a3-reading", routine.owner.persona_id, context.scope, NOW, RoleActivityState.PLANNED,
            description="book · 1–610 / 610 字符", work=RoleWorkSpec("reading", context.key, "read", source, digest(text), 0, len(text)))
        app.role_life.add_activity(activity)
        with app.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, '1')", (queue_key(context.key, source),))
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name) / "life"
        version = app.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(app.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(app.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_metadata(app.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        provider = LifeRuntime(life, plugin)
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        with app.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        ports = (api.life_async_capabilities(plugin=plugin, registration_id=token,
            contract_version=1, response_types=response_types())["ports"] if life_executor else None)
        executor = ReadingExecutor(life, ports, enabled=True) if life_executor else None
        started = (await ReadingStartHandoff(app, routine, app.role_life.transition_provider()).execute(
            activity.activity_id, expected_revision=1, request_id="a3-run") if start else activity)
        if life_executor and start:
            executor.claim(context.key, activity.activity_id)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"reflection":"片段的读后感"}'))
        return SimpleNamespace(**locals())

class ReadingExtractionTests(ReadingExtractionFixture, unittest.IsolatedAsyncioTestCase):
    async def test_new_path_matches_legacy_proposal_progress_notes_events_and_task_id(self):
        f = await self.fixture()
        expected_task_id = f.app.role_reader.task_id(f.context, f.started)
        # A life execution must not call the legacy activity executor.
        async def forbidden(*args, **kwargs):
            raise AssertionError("legacy consume called")
        f.app.role_reader.consume = forbidden
        result = await f.executor.execute(f.context.key, f.activity.activity_id, f.started.revision)
        self.assertEqual(result, {"state": "completed"})
        completed = f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner)
        self.assertEqual((completed.state.value, completed.reading.start_offset, completed.reading.end_offset,
                          completed.reading.reflection), ("completed", 0, 200, "片段的读后感"))
        self.assertEqual(f.executor.store.get(f.context.key, f.activity.activity_id)["task_id"], expected_task_id)
        following = next(a for a in f.app.role_life.activities_for_role(f.routine.owner, limit=100) if a.state.value == "planned")
        self.assertEqual((following.work.start_offset, following.work.end_offset), (200, 610))
        event = f.life.pending_events("interest")[0]
        self.assertEqual(event["payload"]["operation_id"], "reading-complete-" + digest(expected_task_id))
        self.assertEqual(len(self.called), 1)
        self.assertTrue(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer="interest"))
        self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)

    async def test_model_return_then_commit_interruption_recovers_without_regeneration(self):
        f = await self.fixture()
        original = f.ports.execute_platform_action
        async def interrupted(request):
            raise ValueError("injected before host decision")
        f.ports.execute_platform_action = interrupted
        with self.assertRaisesRegex(ValueError, "injected"):
            await f.executor.execute(f.context.key, f.activity.activity_id)
        self.assertEqual(len(self.called), 1)
        f.ports.execute_platform_action = original
        f.api.unregister_life_provider(f.token)
        token = f.api.register_life_provider(plugin=f.plugin, api_version=API_VERSION, handlers=f.provider.handlers)
        ports = f.api.life_async_capabilities(plugin=f.plugin, registration_id=token, contract_version=1, response_types=response_types())["ports"]
        recovered = ReadingExecutor(f.life, ports, enabled=False)
        self.assertEqual(await recovered.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)
        self.assertTrue(recovered.owns(f.context.key, f.activity.activity_id))

    async def test_committed_model_asset_recovers_interrupted_supervisor_settlement(self):
        from unittest.mock import patch
        f = await self.fixture()
        tasks = f.app.role_reader.tasks
        port_tasks = f.app.role_reader.port_tasks
        with patch.object(tasks, "finish", side_effect=ValueError("injected after model asset commit")), patch.object(
                port_tasks, "recover_result", side_effect=lambda _context, task: task):
            self.assertEqual(await f.executor.execute(f.context.key, f.activity.activity_id), {"state": "unknown"})
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual(tasks.read(f.context, state["task_id"]).unwrap().state, "uncertain")
        stopped = tasks.observe(f.context, state["task_id"]).unwrap()
        self.assertTrue(stopped["worker_stopped"])
        self.assertFalse(stopped["slot_held"])
        self.assertIsNone(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).reading)
        recovered = ReadingExecutor(f.life, f.ports, enabled=False)
        self.assertEqual(await recovered.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(tasks.read(f.context, state["task_id"]).unwrap().state, "succeeded")
        self.assertEqual(len(self.called), 1)

    async def test_persisted_model_result_retains_original_completion_time_on_delayed_recovery(self):
        from unittest.mock import patch
        f = await self.fixture()
        with patch.object(f.executor.transitions, "propose_reading_completion", side_effect=ValueError("injected before proposal")):
            with self.assertRaisesRegex(ValueError, "injected"):
                await f.executor.execute(f.context.key, f.activity.activity_id)
        self.assertEqual(len(self.called), 1)
        self.clock.advance(timedelta(minutes=20))
        recovered = ReadingExecutor(f.life, f.ports, enabled=False)
        self.assertEqual(await recovered.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        activity = f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner)
        self.assertEqual(activity.reading.completed_at, NOW)
        self.assertEqual(len(self.called), 1)

    async def test_source_revoked_during_model_never_commits_and_asset_cannot_be_read(self):
        f = await self.fixture()
        async def withdraw(_env):
            row = f.app.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id=?", (f.source.source_id,))
            f.app.role_journal.sources.revoke(f.source, expected_governance_revision=row["governance_revision"],
                                             observed_at=self.clock.now()).unwrap()
            return answered('{"reflection":"private"}')
        self.model_behavior = withdraw
        result = await f.executor.execute(f.context.key, f.activity.activity_id)
        self.assertIn(result["state"], ("paused", "unknown"))
        current = f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner)
        self.assertIsNone(current.reading)
        self.assertEqual(f.life.pending_events("interest"), ())
        self.assertEqual(len(self.called), 1)

    async def test_unknown_task_is_only_inspected_after_executor_switch(self):
        f = await self.fixture()
        task_id = f.app.role_reader.task_id(f.context, f.started)
        # Simulate an already attempted bridge task with an unknown model call.
        async def unknown(_env):
            raise TimeoutError("unknown provider outcome")
        self.model_behavior = unknown
        f.app.calendar.prepare_life_snapshot_read()
        with f.app.runtime.coordinated_snapshot(f.app.role_life.snapshot_factory()):
            await f.app.role_reader.consume(f.context, f.started, role_instruction=f.routine.instruction, life=True)
        count = len(self.called)
        self.assertGreaterEqual(count, 1)
        result = await f.executor.execute(f.context.key, f.activity.activity_id)
        self.assertIn(result["state"], ("paused", "unknown"))
        await f.executor.execute(f.context.key, f.activity.activity_id)
        self.assertEqual(len(self.called), count)
        self.assertIsNone(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).reading)

    async def _recover_stage(self, stage):
        from unittest.mock import patch
        from companion_v2.integration.model_completion_handoff import ModelCompletionHandoff
        f = await self.fixture()
        f.executor.clock = self.clock.now
        original = getattr(ModelCompletionHandoff, stage)
        def interrupt(handoff, *args, **kwargs):
            original(handoff, *args, **kwargs)
            # Cancellation models a worker crash without being translated by
            # the host into a permanent invalid_config contract error.
            raise asyncio.CancelledError("injected after " + stage)
        with patch.object(ModelCompletionHandoff, stage, interrupt):
            with self.assertRaises(asyncio.CancelledError):
                await f.executor.execute(f.context.key, f.activity.activity_id)
        self.clock.advance(timedelta(seconds=30))
        recovered = ReadingExecutor(f.life, f.ports, enabled=False, clock=self.clock.now)
        self.assertEqual(await recovered.execute(f.context.key, f.activity.activity_id), {"state": "completed"})
        self.assertEqual(len(self.called), 1)
        self.assertEqual(len(f.life.pending_events("interest")), 1)

    async def test_recover_after_host_decision(self):
        await self._recover_stage("decide")

    async def test_recover_after_life_commit(self):
        await self._recover_stage("apply")

    async def test_recover_after_host_confirmation(self):
        await self._recover_stage("complete")

    async def test_recover_after_command_acknowledgement(self):
        await self._recover_stage("acknowledge")

    async def test_book_choice_uses_life_prompt_and_preserves_choice_task_id(self):
        from good_life.reading_selection import choose
        f = await self.fixture()
        await f.executor.execute(f.context.key, f.activity.activity_id)
        following = next(a for a in f.app.role_life.activities_for_role(f.routine.owner, limit=100) if a.state.value == "planned")
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"想继续读"}'))
        activities = [json.loads(encode(following))]
        result = await choose(f.executor, f.context.key, activities, "a3-slot", "日历中没有其他角色安排的空档")
        self.assertEqual(result, {"state": "committed", "value": {"source_id": f.source.source_id,
            "reason": "想继续读", "starts": 0, "next_at": None}})
        self.assertEqual(len(self.called), 2)
        self.assertEqual(result, await choose(f.executor, f.context.key, activities, "a3-slot", "日历中没有其他角色安排的空档"))
        self.assertEqual(len(self.called), 2)
        task_id = f.app.role_reader.choice_id(f.context, "a3-slot")
        self.assertIsNotNone(f.app.runtime.read_one("SELECT 1 FROM execution_tasks WHERE task_id=?", (task_id,)))

    async def test_invalid_book_choice_preserves_original_backoff_and_advances_slot(self):
        from zoneinfo import ZoneInfo
        from good_life.reading_choices import choice_slot, reading_window
        from good_life.reading_scheduler import ReadingScheduler
        from good_life.reading_selection import choose
        f = await self.fixture()
        await f.executor.execute(f.context.key, f.activity.activity_id)
        self.clock.advance(timedelta(hours=1))
        capabilities = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        snapshot = capabilities["reading_snapshot"](f.context.key)
        snapshot["sleep_enabled"] = False
        capabilities["reading_snapshot"] = lambda _key: {**snapshot, "observed_at": self.clock.now().isoformat()}
        local = self.clock.now().astimezone(ZoneInfo(snapshot["timezone"]))
        slot = choice_slot(snapshot["records"], local.date().isoformat()) + ":0"
        activities = [item for item in snapshot["records"] if item["state"] == "planned"]
        schedule = reading_window(snapshot["calendar"], self.clock.now(), local, None)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{}'))
        self.assertEqual(await choose(f.executor, f.context.key, activities, slot, schedule), {"state": "failed"})
        self.assertEqual(await choose(f.executor, f.context.key, activities, slot, schedule), {"state": "failed"})
        self.assertEqual(len(self.called), 2)
        scheduler = ReadingScheduler(f.life, capabilities, executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        planner = SimpleNamespace(_windows={})
        scheduler.advance({f.context.key: f.routine.owner.persona_id}, self.clock.now(), planner)
        self.assertNotIn(f.context.key, scheduler.workers)
        self.assertEqual(scheduler.slots[f.context.key]["retry"], 0)
        self.clock.advance(timedelta(seconds=31))
        scheduler.advance({f.context.key: f.routine.owner.persona_id}, self.clock.now(), planner)
        self.assertEqual(scheduler.slots[f.context.key]["retry"], 1)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"choice":"1","reason":"继续"}'))
        scheduler.advance({f.context.key: f.routine.owner.persona_id}, self.clock.now(), planner)
        self.assertEqual((await scheduler.workers[f.context.key])["state"], "committed")
        self.assertEqual(len(self.called), 3)

    async def test_source_withdrawn_after_commit_finishes_and_allows_another_reading(self):
        from good_life.reading_scheduler import ReadingScheduler
        f = await self.fixture()
        f.executor.clock = self.clock.now
        original = f.ports.execute_platform_action
        commits = []
        async def withdraw_after_commit(request):
            receipt = await original(request)
            if request.action == "life.command.commit":
                activity_id = request.payload["command"]["payload"]["activity_id"]
                commits.append(activity_id)
                if activity_id == f.activity.activity_id:
                    row = f.app.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id=?", (f.source.source_id,))
                    f.app.role_journal.sources.revoke(f.source, expected_governance_revision=row["governance_revision"],
                                                     observed_at=self.clock.now()).unwrap()
            return receipt
        f.ports.execute_platform_action = withdraw_after_commit
        capabilities = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        scheduler = ReadingScheduler(f.life, capabilities, executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        contexts, planner = {f.context.key: digest(f.routine.owner)}, SimpleNamespace(_windows={})
        scheduler.advance(contexts, self.clock.now(), planner)
        self.assertEqual(await scheduler.workers[f.context.key], {"state": "completed"})
        scheduler.collect(self.clock.now())
        self.assertEqual(f.app.role_life.activity(f.activity.activity_id, owner=f.routine.owner).state.value, "completed")
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        self.assertEqual((state["phase"], state["reason_code"]), ("completed", "events_withheld_source_unavailable"))
        self.assertEqual(state["events_withheld_consumers"], ["journal", "interest"])
        self.assertNotIn("command", state)
        self.assertNotIn("proof", state)
        self.assertEqual(f.executor.store.pending(f.context.key), [])
        self.assertFalse(f.executor.store.has_life_runs())
        for consumer in ("journal", "interest"):
            event = f.life.pending_events(consumer)[0]
            self.assertFalse(life_event_inbox.verify_received(f.app.runtime, f.context, event, consumer=consumer))
        for _ in range(10):
            self.clock.advance(timedelta(seconds=30))
            scheduler.advance(contexts, self.clock.now(), planner)
            self.assertNotIn(f.context.key, scheduler.workers)
        self.assertEqual(commits, [f.activity.activity_id])
        # A different, authorized book can actually start and finish through
        # the same context after the old completion's events were withheld.
        self.clock.advance(timedelta(hours=1))
        status = f.manager.status(self.core)
        added = await f.manager.command(self.core, {"action": "add_text", "operation_key": "after-withheld-import",
            "view_token": status["view_token"], "title": "另一本书", "author": "作者", "text": "新原文" * 50})
        self.assertTrue(added.is_success, added)
        other_id = added.unwrap()["activity_ids"][0]
        card = next(item for item in f.manager.status(self.core)["activities"] if item["activity_id"] == other_id)
        requested = await f.manager.command(self.core, {"action": "read", "operation_key": "after-withheld-read",
            "activity_id": other_id, "expected_revision": card["revision"], "view_token": card["view_token"]})
        self.assertTrue(requested.is_success, requested)
        scheduler.advance(contexts, self.clock.now(), planner)
        self.assertEqual(scheduler.activity_ids[f.context.key], other_id)
        self.assertEqual(await scheduler.workers[f.context.key], {"state": "completed"})
        self.assertEqual(commits, [f.activity.activity_id, other_id])
        self.assertEqual(len(self.called), 2)

    async def test_manual_reading_uses_same_life_executor_and_original_intent_receipt(self):
        from good_life.sleep_scheduler import DueSleepScheduler
        f = await self.fixture()
        await f.executor.execute(f.context.key, f.activity.activity_id)
        self.clock.advance(timedelta(hours=1))
        manager = RoleLifeManagement(f.app, f.context)
        status = manager.status(self.core)
        card = next(item for item in status["activities"] if item["state"] == "planned")
        request = {"action": "read", "activity_id": card["activity_id"], "expected_revision": card["revision"],
            "operation_key": "a3-manual", "view_token": card["view_token"]}
        scheduled = await manager.command(self.core, request)
        self.assertTrue(scheduled.is_success, scheduled)
        capabilities = f.api.life_host_capabilities(plugin=f.plugin, registration_id=f.token)
        async def forbidden(*args):
            raise AssertionError("legacy manual execution called")
        capabilities["execute_manual_reading"] = forbidden
        scheduler = DueSleepScheduler(f.life, capabilities, reading_executor=f.executor)
        self.addAsyncCleanup(scheduler.close)
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.reading_scheduler.workers[f.context.key]
        self.assertEqual(await worker, {"state": "completed"})
        final = f.app.role_life.activity(card["activity_id"], owner=f.routine.owner)
        self.assertEqual((final.state.value, final.reading.start_offset, final.reading.end_offset), ("completed", 200, 400))
        self.assertEqual(len(self.called), 2)
        self.assertTrue((await manager.command(self.core, request)).unwrap()["replayed"])
        self.assertEqual(len(self.called), 2)

    async def test_owner_is_pinned_and_request_digest_ignores_lease_but_checks_content(self):
        from good_life.reading_execution import signed
        from companion_v2.integration.life_async_codec import params_digest
        f = await self.fixture()
        disabled = ReadingExecutor(f.life, f.ports, enabled=False)
        self.assertEqual(disabled.claim(f.context.key, f.activity.activity_id), "life")
        self.assertEqual(disabled.claim(f.context.key, "next-bridge"), "host_bridge")
        self.assertIsNone(disabled.store.get(f.context.key, "next-bridge"))
        self.assertEqual(f.executor.claim(f.context.key, "next-bridge"), "life")
        scope = await f.executor._scope(f.context.key)
        original = await f.executor._original(scope, json.loads(encode(f.started)))
        persona = await f.ports.get_persona_context(p.PersonaRequest(scope, "reading", (original.source_ref,)))
        with self.assertRaises(TypeError):
            persona.content["instruction"] = "changed"
        request = signed("model_task", p.ModelTaskRequest(scope, "task-one", "", "role.reading.consume", m.TemplateRef("example", 1),
            {"text": "中文"}, (original.source_ref,), original.audience_bound, p.ModelBudget(8, 8000000, 524288)))
        self.assertEqual(request.params_digest, params_digest("model_task", request))
        rebound = replace(request, task_id="other-id", scope=replace(scope, authority_ref="new-lease", registration_generation=2))
        self.assertEqual(request.params_digest, params_digest("model_task", rebound))
        self.assertNotEqual(request.params_digest, params_digest("model_task", replace(request, inputs={"text": "改变"})))

    async def test_duplicate_event_delivery_and_memory_retry_do_not_execute_reading(self):
        f = await self.fixture()
        await f.executor.execute(f.context.key, f.activity.activity_id)
        event = f.life.pending_events("interest")[0]
        self.assertFalse(life_event_inbox.accept(f.app, f.context, event, consumer="interest", observed_at=self.clock.now()))
        self.assertFalse(life_event_inbox.accept(f.app, f.context, event, consumer="interest", observed_at=self.clock.now()))
        self.assertEqual(len(self.called), 1)
        self.assertEqual(len(f.life.pending_events("interest")), 1)
        self.assertEqual(f.life._connection.execute("SELECT COUNT(*) FROM staged_role_activities WHERE state='completed'").fetchone()[0], 1)

    async def test_revoked_completed_result_and_stale_lease_cannot_be_read(self):
        f = await self.fixture()
        await f.executor.execute(f.context.key, f.activity.activity_id)
        scope = await f.executor._scope(f.context.key)
        state = f.executor.store.get(f.context.key, f.activity.activity_id)
        result_ref = "life.model.result.v1:" + state["task_id"]
        ref = m.SourceRef(result_ref, "1", None, None, None, NOW, None, result_ref)
        row = f.app.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id=?", (f.source.source_id,))
        f.app.role_journal.sources.revoke(f.source, expected_governance_revision=row["governance_revision"], observed_at=self.clock.now()).unwrap()
        with self.assertRaises(ValueError):
            await f.ports.read_material(p.MaterialRequest(scope, ref, "inspect"))
        f.api.unregister_life_provider(f.token)
        with self.assertRaises(ValueError):
            await f.ports.inspect_task(p.TaskInspection(scope, state["task_id"], state["params_digest"]))
