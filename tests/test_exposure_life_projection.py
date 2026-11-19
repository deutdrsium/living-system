from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
from companion_v2.execution.models import TaskError
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.interests import visible_interests
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.models import RoleActivity, RoleActivityState
from companion_v2.role_work.work_models import RoleWorkSpec
from good_life.activity_import import stage_activities
from good_life.exposure_import import stage_exposures
from good_life.metadata_import import stage_metadata
from good_life.runtime import LifeRuntime
from good_life.storage import LifeStorage
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture


class ExposureLifeProjectionTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    async def test_history_interests_and_dedup_use_life_records_and_fail_closed(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, _ = self.setup_state()
        self.grant(context.actor, context.scope, "role.life.run")
        await self.start(host)
        routine = app.life_runtime.bind(context, "test persona")
        activity = RoleActivity("social", routine.owner.persona_id, context.scope, NOW, RoleActivityState.COMPLETED,
                                work=RoleWorkSpec("social", context.key, "social-request"))
        app.role_life.add_activity(activity)
        app.role_life.add_activity(RoleActivity("active-social", routine.owner.persona_id, context.scope, NOW,
            RoleActivityState.ACTIVE, work=RoleWorkSpec("social", context.key, "active-social-request")))
        store = app.role_social.exposure
        store.save_batch(context, "social", "zhihu", [{"id": "article", "title": "Original title",
            "summary": "Summary", "url": "https://example.com/article"}], NOW)
        store.topics(context, "social", "zhihu", {"article": ["Astronomy"]})
        store.mark_exposed(context, "social", "zhihu", ["article"], NOW)
        store.mark_attempt(context, "social", "zhihu", "article", "text", NOW)
        store.feedback(context, "social", "zhihu", [
            {"id": "article", "reaction": "negative", "target": "item", "reason": "Not interesting"},
            {"id": "article", "reaction": "positive", "target": "topic", "topic": "Astronomy"}], NOW)
        manager = RoleLifeManagement(app, context)
        before = manager.status(self.core)
        interests = visible_interests(app, context, NOW)
        self.assertEqual(before["recent_exposures"][0]["title"], "Original title")
        self.assertEqual(before["disliked_items"][0]["title"], "Original title")
        self.assertEqual(interests[0]["contacts"][0]["title"], "Original title")

        directory = Path(temporary.name) / "life"
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory,
                        validate_source=lambda _connection: {"state": "matched"})
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
        write = provider.handlers["browsing_command"]
        interrupted = [False]
        def browsing_command(operation, **request):
            result = write(operation, **request)
            if operation == "save_batch" and interrupted[0]:
                interrupted[0] = False
                raise RuntimeError("simulated interruption after life saved the receipts")
            return result
        provider.handlers["browsing_command"] = browsing_command
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
            db.execute("UPDATE role_exposures SET title='sealed copy',exposed_at=NULL,attempted_at=NULL")

        after = manager.status(self.core)
        self.assertEqual(after["recent_exposures"], before["recent_exposures"])
        self.assertEqual(after["disliked_items"], before["disliked_items"])
        self.assertEqual(visible_interests(app, context, NOW), interests)
        self.assertTrue(store.already_attempted(context, "zhihu", "article"))
        self.assertTrue(store.blocked(context, "zhihu", "article"))

        card = {"id": "new-item", "title": "New title", "summary": "New summary", "url": "https://example.com/new"}
        interrupted[0] = True
        with self.assertRaises(RuntimeError):
            store.save_batch(context, "active-social", "zhihu", [card], NOW)
        store.save_batch(context, "active-social", "zhihu", [card], NOW)
        self.assertEqual(store.selected(context, "active-social", "new-item"), "new-item")
        self.assertEqual(store.selected(context, "active-social", "other"), "new-item")
        store.topics(context, "active-social", "zhihu", {"new-item": ["Astronomy"]})
        store.mark_exposed(context, "active-social", "zhihu", ["new-item"], NOW)
        store.mark_attempt(context, "active-social", "zhihu", "new-item", "text", NOW)
        store.feedback(context, "active-social", "zhihu", [
            {"id": "new-item", "reaction": "negative", "target": "item", "reason": "No"}], NOW)
        receipt = store.exposure(context, "active-social", "zhihu", "new-item")
        self.assertEqual((receipt["title"], receipt["coverage"], bool(receipt["attempted_at"])), ("New title", "text", True))
        self.assertTrue(store.already_attempted(context, "zhihu", "new-item"))
        self.assertTrue(store.blocked(context, "zhihu", "new-item"))
        host_index = self.runtime.read_one("""SELECT title,summary,source_url,exposed_at,attempted_at FROM role_exposures
            WHERE activity_id='active-social' AND content_id='new-item'""", ())
        self.assertEqual(tuple(host_index), ("", "", "", None, None))
        self.assertIn("New title", [item["title"] for item in manager.status(self.core)["recent_exposures"]])
        with self.assertRaises(TaskError):
            store.save_batch(context, "social", "zhihu", [card], NOW)
        api.unregister_life_provider(token)
        with self.assertRaises(TaskError):
            manager.status(self.core)
        self.assertEqual(len(self.sent), 0)


if __name__ == "__main__":
    unittest.main()
