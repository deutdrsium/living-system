from pathlib import Path
import json
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

import pytest
pytest.importorskip("astrbot")
from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request
from astrbot_host.life_dashboard import dispatch
from companion_v2.execution.models import digest
from good_life.web_api import LifePageAPI
from tests.multi_life_support import MultiLifeFixture


class LifeDashboardTests(MultiLifeFixture, unittest.IsolatedAsyncioTestCase):
    async def setUp_page(self):
        await self.initialize()
        await self.start()
        plugin = SimpleNamespace(_runtime=None)
        registry = SimpleNamespace(owns_registration=lambda token, caller: token == "lease" and caller is plugin)
        async def dashboard(**kwargs):
            return await dispatch(self.controller, registry, **kwargs)
        plugin._runtime = SimpleNamespace(api=SimpleNamespace(life_dashboard=dashboard), token="lease", ready=True)
        return LifePageAPI(plugin)

    async def call(self, handler, *, body=None, username="admin", plugin_name="astrbot_plugin_good_life", **kwargs):
        encoded = json.dumps(body).encode() if body is not None else b""
        async def receive():
            return {"type": "http.request", "body": encoded, "more_body": False}
        native = Request({"type": "http", "method": "POST" if body is not None else "GET", "path": "/",
            "headers": [(b"content-type", b"application/json")], "query_string": b""}, receive=receive)
        with bind_request_context(PluginRequest(native, username=username, plugin_name=plugin_name)):
            return await handler(**kwargs)

    async def test_authenticated_life_and_calendar_routes_keep_host_authority(self):
        page = await self.setUp_page()
        directory = self.ok(await self.call(page.targets))
        self.assertTrue(directory["targets"])
        target = next(item for item in directory["targets"] if item["id"] == "first")
        role = next(item for item in target["roles"] if item["token"] == digest("role-a"))
        kwargs = {"target": target["id"], "role": role["token"]}
        status = self.ok(await self.call(page.life, **kwargs))
        self.assertIn("activities", status)
        self.assertIn("calendar", self.ok(await self.call(page.state, **kwargs)))
        changes = {"action": "configure", "operation_key": "new-life-page-config", "view_token": status["view_token"],
                   "edit_token": status["edit_token"], "changes": {"reading_chunk_chars": 2400}}
        self.ok(await self.call(page.life_command, body=changes, **kwargs))
        self.assertEqual(self.ok(await self.call(page.life, **kwargs))["config"]["reading_chunk_chars"], 2400)
        self.assertEqual((await self.call(page.life, username=None, **kwargs))["message"], "dashboard_identity_required")
        self.assertEqual((await self.call(page.life, plugin_name="other_plugin", **kwargs))["message"], "dashboard_identity_required")
        self.assertEqual((await self.call(page.life_command, body={"action": "diary"}, **kwargs))["message"], "life_dashboard_invalid")
