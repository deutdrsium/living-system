from dataclasses import replace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from astrbot_host.life_provider_registry import API_VERSION, HANDLERS, PROVIDER_ID, LifeProviderRegistry
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import ScopeKind, ScopeRef, SubjectKind, SubjectRef
from companion_v2.integration.exposure_session_proxy import ExposureSessionReadProxy
from companion_v2.role_work.exposure import ExposureStore
from companion_v2.role_work.interests import scope_key
from good_life.exposure_import import DDL
from good_life.exposure_records import StagedExposureRecords
from good_life.storage import LifeStorage


class ExposureSessionProxyTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = LifeStorage.open(temporary.name)
        self.addCleanup(self.storage.close)
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        role = SubjectRef("installation", SubjectKind.BOT, "role")
        actor = SubjectRef("installation", SubjectKind.HUMAN, "person")
        self.context = SimpleNamespace(scope=scope, role_binding=SimpleNamespace(role=role), actor=SimpleNamespace(subject=actor))
        self.key = scope_key(self.context)
        with self.storage.transaction() as db:
            for statement in DDL:
                db.execute(statement)
            db.executemany("INSERT INTO staged_role_exposure_sessions VALUES (?, ?, ?, ?, '', 0)", [
                (5, self.key, "one", "zhihu"), (8, self.key, "two", "bilibili"),
                (9, self.key, "three", "unavailable"), (10, "foreign", "foreign", "douban")])
            db.executemany("INSERT INTO staged_role_exposures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
                (self.key, "one", "zhihu", "article", "First", "", "", "2026-01-01T00:00:00+00:00",
                 "2026-01-01T00:00:00+00:00", "2026-01-01T00:01:00+00:00", "text", 0),
                (self.key, "two", "bilibili", "video", "Second", "", "", "2026-01-02T00:00:00+00:00",
                 None, None, "", 0),
                ("foreign", "foreign", "zhihu", "article", "Foreign", "", "", "2026-01-03T00:00:00+00:00",
                 None, None, "", 0)])
        self.records = StagedExposureRecords(self.storage)
        self.plugin = object()
        self.star = SimpleNamespace(root_dir_name=PROVIDER_ID, activated=True, star_cls=self.plugin)
        registry = LifeProviderRegistry(SimpleNamespace(get_all_stars=lambda: [self.star]), owner=object(), host_active=lambda: True)
        self.addCleanup(registry.close)
        handlers = {name: (lambda *_args, **_kwargs: None) for name in HANDLERS}
        handlers["browsing_read"] = self.records.dispatch
        registry.register(plugin=self.plugin, api_version=API_VERSION, handlers=handlers)
        self.registry = registry
        self.proxy = ExposureSessionReadProxy(SimpleNamespace(installation_id="installation"), registry)

    def test_scoped_selection_and_previous_platform_keep_original_order(self):
        self.assertEqual(self.proxy.session_platform(self.context, "two"), "bilibili")
        self.assertEqual(self.proxy.session_platform(self.context, "three"), "unavailable")
        self.assertIsNone(self.proxy.session_platform(self.context, "foreign"))
        self.assertEqual(self.proxy.previous_platform(self.context, "one"), "bilibili")
        self.assertEqual(self.proxy.previous_platform(self.context, "two"), "zhihu")
        other = SimpleNamespace(scope=replace(self.context.scope, session_id="other"),
                                role_binding=self.context.role_binding, actor=self.context.actor)
        self.assertIsNone(self.proxy.previous_platform(other, "missing"))

    def test_exposure_store_never_uses_host_fallback_if_provider_is_disabled(self):
        runtime = SimpleNamespace(installation_id="installation", read_one=lambda *_: self.fail("old session table read"))
        store = ExposureStore(runtime, session_reader=self.proxy)
        self.assertEqual(store.session_platform(self.context, "one"), "zhihu")
        self.star.activated = False
        with self.assertRaises(TaskError) as error:
            store.session_platform(self.context, "one")
        self.assertEqual(error.exception.code, "life_provider_unavailable")

    def test_provider_cannot_return_a_foreign_scope(self):
        self.registry._entry.handlers["browsing_read"] = lambda *_args, **_kwargs: {
            "scan_id": 10, "scope_key": "foreign", "activity_id": "one", "platform": "zhihu"}
        with self.assertRaises(TaskError):
            self.proxy.session_platform(self.context, "one")

    def test_receipts_and_attempts_are_scoped_and_obey_visibility(self):
        self.assertEqual(self.proxy.exposure(self.context, "one", "zhihu", "article")["title"], "First")
        self.assertIsNone(self.proxy.exposure(self.context, "foreign", "zhihu", "article"))
        self.assertEqual([row["title"] for row in self.proxy.recent(self.context)], ["Second", "First"])
        self.assertTrue(self.proxy.attempted(self.context, "zhihu", "article"))
        limited = ExposureSessionReadProxy(SimpleNamespace(installation_id="installation"), self.registry,
                                           visible=lambda _context, activity: activity != "one")
        self.assertIsNone(limited.exposure(self.context, "one", "zhihu", "article"))
        self.assertEqual([row["title"] for row in limited.recent(self.context)], ["Second"])
        self.assertFalse(limited.attempted(self.context, "zhihu", "article"))

    def test_receipt_from_another_scope_is_rejected(self):
        foreign = dict(self.records.recent(scope_key="foreign")[0])
        self.registry._entry.handlers["browsing_read"] = lambda *_args, **_kwargs: foreign
        with self.assertRaises(TaskError):
            self.proxy.exposure(self.context, "one", "zhihu", "article")


if __name__ == "__main__":
    unittest.main()
