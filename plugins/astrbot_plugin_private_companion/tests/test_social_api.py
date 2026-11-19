import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request
from astrbot_host.social_port import SocialPort
from tests.multi_life_support import MultiLifeFixture


class SocialApiTests(MultiLifeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_routes_require_authenticated_private_role_and_do_not_expose_credentials(self):
        app = await self.initialize()
        app.social_port = SocialPort(self.root / "social-test")
        inventory = self.controller.web_api.target_inventory()
        paths = {(target["target_id"], role["role_id"]): role["social_api"]
                 for target in inventory for role in target["roles"]}

        async def send(target, role, body=None, username="admin"):
            path = paths[(target, role)] + ("/command" if body is not None else "")
            async def receive():
                return {"type": "http.request", "body": json.dumps(body or {}).encode(), "more_body": False}
            native = Request({"type": "http", "method": "POST" if body is not None else "GET", "path": path,
                "headers": [(b"content-type", b"application/json")], "query_string": b""}, receive=receive)
            with bind_request_context(PluginRequest(native, username=username)):
                return await self.routes[path]()

        denied = await send("first", "role-a", {"action": "enabled", "enabled": True}, username="")
        self.assertEqual(denied["data"]["code"], "dashboard_identity_required")
        self.assertEqual((await send("first", "role-a"))["status"], "ok")
        directory_path = "/astrbot_plugin_private_companion/page/targets"
        directory_request = Request({"type": "http", "method": "GET", "path": directory_path,
                                     "headers": [], "query_string": b""})
        with bind_request_context(PluginRequest(directory_request, username="admin")):
            directory = await self.routes[directory_path]()
        public_roles = [r for t in directory["data"]["targets"] for r in t["roles"]]
        self.assertTrue(any(r.get("social_api") == paths[("first", "role-a")] for r in public_roles))
        saved = await send("first", "role-a", {"action": "credential", "platform": "brightdata", "value": "private-key"})
        self.assertEqual(saved["status"], "ok")
        self.assertEqual((await send("first", "role-a"))["data"]["verification"]["brightdata"], "unverified")
        with patch.object(app.social_port, "login_status", AsyncMock(return_value={"ok": True, "logged_in": True})):
            await send("first", "role-a", {"action": "check", "platform": "brightdata"})
        self.assertEqual((await send("first", "role-a"))["data"]["verification"]["brightdata"], "passed")
        with patch.object(app.social_port, "login_status", AsyncMock(return_value={"ok": False, "logged_in": False})):
            await send("first", "role-a", {"action": "check", "platform": "brightdata"})
        self.assertEqual((await send("first", "role-a"))["data"]["verification"]["brightdata"], "failed")
        self.assertNotIn("private-key", json.dumps(await send("first", "role-a")))
        self.assertTrue((await send("first", "role-a"))["data"]["enabled"])
        context = self.context("first", "role-a")
        alternate = SimpleNamespace(scope=replace(context.scope, conversation_id="alternate-conversation"),
                                    actor=context.actor, role_binding=context.role_binding)
        self.assertEqual(app.social_port.context_key(context), app.social_port.context_key(alternate))
        self.assertEqual((await send("first", "role-b"))["status"], "error")
        wrong_fields = await send("first", "role-a", {"action": "enabled", "enabled": True, "role_id": "role-b"})
        self.assertEqual(wrong_fields["data"]["code"], "social_request_invalid")
