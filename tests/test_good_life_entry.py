import asyncio
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


class FakeStar:
    def __init__(self, context, config=None):
        self.context = context


@contextmanager
def load_entry(data_root, *, log=lambda *_: None):
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    api.logger = SimpleNamespace(info=log)
    web = ModuleType("astrbot.api.web")
    web.request = SimpleNamespace()
    star = ModuleType("astrbot.api.star")
    star.Context, star.Star = object, FakeStar
    star.StarTools = SimpleNamespace(get_data_dir=lambda _name: data_root)
    star.register = lambda *_args, **_kwargs: lambda cls: cls
    path = Path(__file__).resolve().parents[1] / "plugins" / "astrbot_plugin_good_life" / "main.py"
    package = ModuleType("test_good_life_entry_package")
    package.__path__ = [str(path.parent)]
    spec = importlib.util.spec_from_file_location("test_good_life_entry_package.main", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"astrbot": astrbot, "astrbot.api": api, "astrbot.api.star": star,
                                  "astrbot.api.web": web, "test_good_life_entry_package": package}):
        spec.loader.exec_module(module)
        yield module.GoodLifePlugin


class GoodLifeEntryTests(unittest.TestCase):
    def test_watcher_logs_readiness_once_after_successful_binding(self):
        messages = []
        plugin_class = self.enterContext(load_entry("unused", log=messages.append))
        plugin = plugin_class(SimpleNamespace())
        api = SimpleNamespace(life_provider_status=lambda: {"ready": True})
        plugin._cutover_ready = lambda: True
        plugin._host_api = lambda: api
        runtime = SimpleNamespace(api=None, ready=False)
        plugin._runtime = runtime

        async def bind(bound):
            runtime.api, runtime.ready = bound, True

        runtime.bind = bind
        ticks = []

        async def sleep(_seconds):
            ticks.append(None)
            if len(ticks) == 2:
                plugin._stopped = True

        with patch.object(asyncio, "sleep", sleep):
            asyncio.run(plugin._watch_dependency())
        self.assertEqual(messages, ["[GoodLife] host binding ready"])

    def test_dependency_discovery_handles_load_order_and_conflicts_without_running_life(self):
        stars = []
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        context = SimpleNamespace(get_all_stars=lambda: stars, register_web_api=lambda *_: None)
        plugin = self.enterContext(load_entry(temporary.name))(context)
        self.addCleanup(lambda: asyncio.run(plugin.terminate()))
        asyncio.run(plugin.initialize())
        self.assertEqual(plugin.companion_v2_status()["dependency_state"], "missing")
        companion = SimpleNamespace(root_dir_name="astrbot_plugin_private_companion", activated=False, star_cls=None)
        stars.append(companion)
        self.assertEqual(plugin.companion_v2_status()["dependency_state"], "disabled")
        companion.activated = True
        companion.star_cls = SimpleNamespace(extension_api=SimpleNamespace(api_version="wrong"))
        self.assertEqual(plugin.companion_v2_status()["dependency_state"], "incompatible")
        companion.star_cls.extension_api = SimpleNamespace(api_version="companion.product.v1",
            life_provider_status=lambda: {"api_version": "companion.life.v1", "host_ready": True, "ready": False})
        self.assertEqual(plugin.companion_v2_status()["dependency_state"], "available")
        self.assertFalse(plugin.companion_v2_status()["runtime_ready"])
        stars.append(companion)
        self.assertEqual(plugin.companion_v2_status()["dependency_state"], "conflict")
        asyncio.run(plugin.terminate())
        self.assertEqual(plugin.companion_v2_status()["dependency_state"], "stopped")


if __name__ == "__main__":
    unittest.main()
