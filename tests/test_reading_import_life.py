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
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tests"), str(ROOT / "tools")]

from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.models import RoleActivityState
from companion_v2.role_work.reader import reading_profile
from good_life.activity_import import stage_activities
from good_life.exposure_import import stage_exposures
from good_life.metadata_import import stage_metadata
from good_life.prepared_commands import PreparedCommands
from good_life.runtime import LifeRuntime
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.storage import LifeStorage
from rehearse_activity_rollback import rehearse as rehearse_activity_rollback
from rehearse_reading_markers_rollback import rehearse as rehearse_marker_rollback
from tests.test_life_epub_import import make_epub
from tests.test_role_state import StateFixture
from tests.test_role_tool_loop import answered


class ReadingImportLifeTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (reading_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="reading-import-tests", fields=configuration_fields(self.profiles))

    async def test_text_and_epub_keep_one_host_source_and_recover_life_import(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        self.grant(context.actor, context.scope, "role.reading.record")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        manager = RoleLifeManagement(app, context)
        status = manager.status(self.core)
        enabled = await manager.command(self.core, {"action": "configure", "operation_key": "enable-life",
            "view_token": status["view_token"], "edit_token": status["edit_token"],
            "changes": {"enabled": True, "diary_enabled": False}})
        self.assertTrue(enabled.is_success, enabled)
        routine.refresh()
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        directory = Path(temporary.name) / "life"
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
        from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        provider = LifeRuntime(life, plugin)
        original_control = provider.handlers["activity_control"]
        interrupted = [False]
        def activity_control(operation, *args, **kwargs):
            result = original_control(operation, *args, **kwargs)
            if operation == "activity_creation.resolve" and interrupted[0]:
                interrupted[0] = False
                raise ValueError("simulated interruption after importing to life")
            return result
        provider.handlers["activity_control"] = activity_control
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        status = manager.status(self.core)
        text = "原始材料" * 500
        request = {"action": "add_text", "operation_key": "text-import", "view_token": status["view_token"],
                   "title": "测试文本", "author": "作者", "text": text}
        self.assertFalse((await manager.command(self.core, {**request, "view_token": "invalid"})).is_success)
        added = await manager.command(self.core, request)
        self.assertTrue(added.is_success, added)
        first = app.role_life.activity(added.unwrap()["activity_ids"][0], owner=routine.owner)
        self.assertEqual((first.state, first.work.start_offset, first.work.end_offset), (RoleActivityState.PLANNED, 0, len(text)))
        source = self.runtime.read_one("SELECT payload_json FROM sources WHERE source_id=?", (first.work.source.source_id,))
        self.assertEqual(json.loads(source["payload_json"]), text)
        self.assertEqual(len(app.role_life.artifacts_for_role(routine.owner)), 1)
        self.assertTrue((await manager.command(self.core, request)).unwrap()["replayed"])
        status = manager.status(self.core)
        epub = {"action": "add_epub", "operation_key": "epub-import", "view_token": status["view_token"],
                "filename": "book.epub", "epub_base64": make_epub("甲" * 6200 + "乙")}
        interrupted[0] = True
        self.assertFalse((await manager.command(self.core, epub)).is_success)
        pending = next(item for item in PreparedCommands(life).pending()
                       if item["command_id"].startswith("activity-reading-import-"))
        self.assertEqual(pending["state"], "committed")
        from test_reading_life_execution import bound_default_scheduler
        scheduler = await bound_default_scheduler(provider, api, token)
        self.addAsyncCleanup(scheduler.close)
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        replay = await manager.command(self.core, epub)
        self.assertTrue(replay.is_success, replay)
        self.assertTrue(replay.unwrap()["replayed"])
        self.assertEqual(replay.unwrap()["title"], "测试书")
        second = app.role_life.activity(replay.unwrap()["activity_ids"][0], owner=routine.owner)
        self.assertEqual((second.state, second.work.start_offset, second.work.end_offset), (RoleActivityState.PLANNED, 0, 6000))
        source = self.runtime.read_one("SELECT payload_json FROM sources WHERE source_id=?", (second.work.source.source_id,))
        self.assertIn("甲" * 200, json.loads(source["payload_json"]))
        self.assertEqual(len(app.role_life.artifacts_for_role(routine.owner)), 2)
        rehearsal = rehearse_activity_rollback(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)
        self.assertEqual((rehearsal["state"], rehearsal["activities"], rehearsal["artifacts"]), ("dry_run", 2, 2))
        status = manager.status(self.core)
        card = next(item for item in status["activities"] if item["activity_id"] == second.activity_id)
        read = {"action": "read", "operation_key": "read-imported-epub", "activity_id": second.activity_id,
                "expected_revision": second.revision, "view_token": card["view_token"]}
        self.assertTrue((await manager.command(self.core, read)).is_success)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"reflection":"阅读导入的原文"}'))
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.reading_scheduler.workers.get(context.key)
        self.assertIsNotNone(worker, scheduler.last_failure)
        await worker
        await scheduler.tick(now=self.clock.now())
        completed = app.role_life.activity(second.activity_id, owner=routine.owner)
        self.assertEqual((completed.state, completed.reading.start_offset, completed.reading.end_offset),
                         (RoleActivityState.COMPLETED, 0, 1500))
        following = next(item for item in app.role_life.activities_for_role(routine.owner, limit=100)
                         if item.state is RoleActivityState.PLANNED and item.work.source == second.work.source)
        self.assertEqual(following.work.start_offset, 1500)
        self.assertEqual(len(self.called), 1)
        copy = Path(temporary.name) / "marker-rollback.sqlite3"
        rollback = rehearse_marker_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                           expected_version=version, destination=copy)
        self.assertEqual((rollback["state"], rollback["markers"]), ("rehearsed", 3))
        with closing(sqlite3.connect(copy)) as db:
            pace = json.loads(db.execute("SELECT value FROM runtime_metadata WHERE key=?",
                                         ("role.life.reading_pace:" + context.key,)).fetchone()[0])
        self.assertEqual(pace["characters"], 1500)
        pace_key = "role.life.reading_pace:" + context.key
        with life.transaction() as db:
            original_pace = db.execute("SELECT value FROM staged_life_runtime_metadata WHERE key=?", (pace_key,)).fetchone()[0]
            db.execute("UPDATE staged_life_runtime_metadata SET value=? WHERE key=?",
                       (json.dumps({**pace, "characters": 1499}), pace_key))
        with self.assertRaisesRegex(ValueError, "audited history"):
            rehearse_marker_rollback(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)
        with life.transaction() as db:
            db.execute("UPDATE staged_life_runtime_metadata SET value=? WHERE key=?", (original_pace, pace_key))
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()
