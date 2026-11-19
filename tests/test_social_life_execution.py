import asyncio
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]

from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
from audit_calendar_ownership import adoption_plan
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.models import RoleActivityState
from companion_v2.role_work.state.service import LIFE_STATE_OWNER_KEY
from companion_v2.role_work.social import social_profile
from good_life.activity_import import stage_activities
from good_life.calendar_adoption import adopt_staged_calendar
from good_life.calendar_import import stage_calendar
from good_life.exposure_import import stage_exposures
from good_life.metadata_import import stage_metadata
from good_life.prepared_commands import PreparedCommands
from good_life.runtime import LifeRuntime
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.state_import import stage_state
from good_life.storage import LifeStorage
from rehearse_activity_rollback import _creation_evidence
from rehearse_exposure_rollback import rehearse as rehearse_exposure_rollback
from rehearse_life_rollback import rehearse as rehearse_life_rollback
from rehearse_calendar_rollback import _open
from verify_exposure_authority import verify_contexts
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture
from tests.test_role_tool_loop import answered, call, requested
from tests.test_social_integration import FakeSocialPort


class SocialLifeExecutionTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (social_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="life-social-tests", fields=configuration_fields(self.profiles))

    async def test_scheduler_browses_and_recovers_ledger_without_repeating_unknown_interaction(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        status = manager.status(self.core)
        result = await manager.command(self.core, {"action": "configure", "operation_key": "enable-life",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False, "social_random_read_percent": 0}})
        self.assertTrue(result.is_success, result)
        routine.refresh()
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        directory = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=verify_contexts)
        plan = adoption_plan(self.runtime.database_path, expected_version=version)
        stage_calendar(self.runtime.database_path, expected_version=version, destination=directory)
        stage_state(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        adopt_staged_calendar(life, plan)
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        provider = LifeRuntime(life, plugin)
        original = provider.handlers["activity_control"]
        interrupted = [True]
        def activity_control(operation, *args, **kwargs):
            reply = original(operation, *args, **kwargs)
            if operation == "activity_transition.resolve" and interrupted[0]:
                interrupted[0] = False
                raise ValueError("simulated interruption after life completion")
            return reply
        provider.handlers["activity_control"] = activity_control
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_STATE_OWNER_KEY,))

        class UnknownPort(FakeSocialPort):
            async def call(self, operation, args, *, include_images=False):
                if operation == "bili_recommend":
                    self.calls.append((operation, args.copy()))
                    return {"ok": True, "text": "synthetic feed", "_feed_items": [
                        {"platform": "bilibili", "id": "BV1234567890", "title": "Synthetic stars", "summary": "Example",
                         "url": "https://example.com/stars", "tags": ["stars"], "_token": ""}]}
                if operation == "bili_like":
                    self.calls.append((operation, args.copy()))
                    return {"ok": False, "text": "unknown result"}
                return await super().call(operation, args, include_images=include_images)
        port = app.social_port = UnknownPort()
        async def model(envelope):
            count = len(envelope.tool_history)
            if count == 0:
                return requested(call("read", "social_browse", operation="bili_watch", id="BV1234567890"))
            if count in (1, 2):
                return requested(call("like" + str(count), "social_browse", operation="bili_like", id="BV1234567890"))
            return answered('{"reflection":"A synthetic browsing reflection.","feedback":[{"id":"BV1234567890","target":"item","reaction":"negative","reason":"Example"}]}')
        self.model_behavior = model
        capabilities = api.life_host_capabilities(plugin=plugin, registration_id=token)
        capabilities.pop("state_run")
        scheduler = DueSleepScheduler(life, capabilities)
        self.addAsyncCleanup(scheduler.close)
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.social_scheduler.workers.get(context.key)
        self.assertIsNotNone(worker, scheduler.last_failure)
        await worker
        pending = next(item for item in PreparedCommands(life).pending()
                       if item["command_id"].startswith("activity-social-complete-"))
        self.assertEqual(pending["state"], "committed")
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        saved = next(item for item in routine.activities() if item.work.kind == "social")
        self.assertEqual(saved.state, RoleActivityState.COMPLETED)
        actions = json.loads(saved.checkpoint)["actual_actions"]
        self.assertEqual([item["operation"] for item in actions], ["bili_recommend", "bili_watch"])
        self.assertEqual([name for name, _ in port.calls], ["bili_recommend", "bili_watch", "bili_like"])
        self.assertEqual({row[0] for row in life._connection.execute("SELECT consumer FROM life_event_deliveries")},
                         {"journal", "interest"})
        with closing(_open(self.runtime.database_path)) as host_copy, closing(_open(directory / "good-life.sqlite3")) as life_copy:
            _creation_evidence(host_copy, life_copy, True)
        runner = app.life_runtime.events
        runner.advance((routine,))
        await asyncio.gather(*tuple(runner._workers.values()), return_exceptions=True)
        await asyncio.sleep(0)
        target = Path(temporary.name) / "exposure-rollback.sqlite3"
        rollback = rehearse_exposure_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                              expected_version=version, destination=target)
        self.assertEqual((rollback["state"], rollback["sessions"], rollback["exposures"]), ("rehearsed", 1, 1))
        with closing(sqlite3.connect(target)) as db:
            row = db.execute("SELECT title,coverage FROM role_exposures WHERE content_id='BV1234567890'").fetchone()
            self.assertEqual(row, ("Synthetic stars", "text"))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM role_exposure_feedback").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT state FROM operations WHERE owner='role.social.action'").fetchone()[0], "unknown")
        await scheduler.close()
        host_snapshot = Path(temporary.name) / "host-snapshot.sqlite3"
        life_snapshot = Path(temporary.name) / "life-snapshot.sqlite3"
        with closing(sqlite3.connect(self.runtime.database_path)) as source, closing(sqlite3.connect(host_snapshot)) as backup:
            source.backup(backup)
        with life._lock, closing(sqlite3.connect(life_snapshot)) as backup:
            life._connection.backup(backup)
        combined = Path(temporary.name) / "combined-rollback.sqlite3"
        restored = rehearse_life_rollback(host_snapshot, life_snapshot, expected_version=version, destination=combined)
        self.assertEqual(restored["steps"]["activities_and_browsing"]["exposures"], 1)
        with closing(sqlite3.connect(combined)) as db:
            self.assertEqual(db.execute("SELECT title,coverage FROM role_exposures WHERE content_id='BV1234567890'").fetchone(),
                             ("Synthetic stars", "text"))
            self.assertEqual(db.execute("SELECT state FROM operations WHERE owner='role.social.action'").fetchone()[0], "unknown")
        with self.assertRaisesRegex(ValueError, "already exists"):
            rehearse_life_rollback(host_snapshot, life_snapshot, expected_version=version, destination=combined)
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()
