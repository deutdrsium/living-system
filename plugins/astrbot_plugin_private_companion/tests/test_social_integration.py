import json
import base64
import re
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
from astrbot_host.social_port import SocialPort
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.life.smart_social import social_window
from companion_v2.role_work.models import RoleActivityState
from companion_v2.role_work.social import available_operations, social_image_profile, social_profile, write_allowed
from companion_v2.execution.models import ProviderBinding, digest
from tests.test_role_life_runtime import LifeFixture
from tests.test_role_tool_loop import answered, call, requested


def social_answered(text):
    return answered(json.dumps({"reflection": text}, ensure_ascii=False))


def untrusted_json(text):
    match = re.search(r"=====UNTRUSTED_([0-9a-f]{32})_BEGIN=====\s*(.*?)\s*=====UNTRUSTED_\1_END=====", text, re.S)
    if not match:
        raise AssertionError("untrusted result is missing its complete boundary")
    return json.loads(match.group(2))


class SocialPortTests(unittest.TestCase):
    def test_only_explicit_routes_are_available_and_google_is_selected(self):
        platform, name, args = SocialPort.route("google_search", {"query": "星空"})
        self.assertEqual((platform, name, args["engine"]), ("brightdata", "search_engine", "google"))
        self.assertEqual(SocialPort.route("bili_follow", {"id": "123"})[2]["args"], ["user", "follow", "123"])
        with self.assertRaises(ValueError):
            SocialPort.route("bili_follow", {"id": "123", "command": "dynamic post"})
        with self.assertRaises(ValueError):
            SocialPort.route("bili_post", {"query": "x"})
        with self.assertRaises(ValueError):
            SocialPort.route("google_read", {"id": "http://127.0.0.1/admin"})
        with self.assertRaises(ValueError):
            SocialPort.route("google_read", {"id": "https://192.168.1.2/"})

    def test_search_results_omit_unusable_redirects(self):
        nonce = "a" * 32
        raw = ("notice\n=====UNTRUSTED_" + nonce + "_BEGIN=====\n" + json.dumps({"organic": [
            {"title": "Turing", "link": "https://en.wikipedia.org/wiki/Alan_Turing"},
            {"title": "Opaque redirect", "link": "/goto?url=opaque"},
            {"title": "Private", "link": "http://127.0.0.1/private"},
        ]}) + "\n=====UNTRUSTED_" + nonce + "_END=====")
        data = untrusted_json(SocialPort._filter_google_results(raw))
        self.assertEqual([x["title"] for x in data["organic"]], ["Turing"])
        self.assertEqual(data["unusable_links_omitted"], 2)

    def test_private_credentials_and_binding_are_not_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            port = SocialPort(directory)
            port.save_credential("brightdata", "secret-value")
            port.set_enabled("a" * 64, True)
            self.assertTrue(port.configured()["brightdata"])
            self.assertNotIn("secret-value", json.dumps(port.settings()))
            self.assertEqual((Path(directory) / "accounts" / "brightdata.token").read_text(), "secret-value")
            port.block_platform("a" * 64, "bilibili")
            self.assertTrue(port.blocked_for("a" * 64, "bilibili"))
            port.set_enabled("a" * 64, False)
            self.assertTrue(port.blocked_for("a" * 64, "bilibili"))

    def test_foreign_proxy_is_limited_to_the_search_process(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict("os.environ", {
                "HTTPS_PROXY": "http://unrelated-proxy:80", "NODE_USE_ENV_PROXY": "1"}):
            port = SocialPort(directory)
            domestic = port._child_env()
            self.assertNotIn("HTTPS_PROXY", domestic)
            self.assertNotIn("NODE_USE_ENV_PROXY", domestic)
            self.assertIsNone(port._foreign_environment(dict(domestic)))
            accounts = Path(directory) / "accounts"
            accounts.mkdir()
            proxy = "http://proxy-user:" + "x" * 40 + "@companion-social-proxy:7890"
            (accounts / "foreign-proxy.url").write_text(proxy, encoding="utf-8")
            foreign = dict(domestic)
            self.assertEqual(port._foreign_environment(foreign), proxy)
            self.assertEqual(foreign["HTTPS_PROXY"], proxy)
            self.assertEqual(foreign["NODE_USE_ENV_PROXY"], "1")
            (accounts / "foreign-proxy.url").write_text("http://other-proxy:7890", encoding="utf-8")
            with self.assertRaises(ValueError):
                port._foreign_environment(dict(domestic))

    def test_verification_tracks_the_saved_value_and_search_zone(self):
        with tempfile.TemporaryDirectory() as directory:
            port = SocialPort(directory)
            self.assertEqual(port.verification()["brightdata"], "missing")
            port.set_enabled("a" * 64, True)
            port.save_credential("brightdata", "first-private-key")
            self.assertEqual(port.verification()["brightdata"], "unverified")
            port.record_verification("brightdata", "passed")
            self.assertEqual(port.verification()["brightdata"], "passed")
            port.set_google_zone("a" * 64, "new-zone")
            self.assertEqual(port.verification()["brightdata"], "unverified")
            port.record_verification("brightdata", "failed")
            self.assertEqual(port.verification()["brightdata"], "failed")
            port.save_credential("brightdata", "second-private-key")
            self.assertEqual(port.verification()["brightdata"], "unverified")
            self.assertNotIn("private-key", json.dumps(port.verification()))

    def test_zhihu_block_overrides_a_successful_saved_session(self):
        with tempfile.TemporaryDirectory() as directory:
            port = SocialPort(directory)
            path = Path(directory) / "accounts" / "zhihu"
            path.mkdir(parents=True)
            (path / "zhihu-activity-session.json").write_text('{"cookies": []}', encoding="utf-8")
            (path / "zhihu-activity-login.json").write_text('{"saved": true, "savedAt": 1, "account": {"id": 1}}', encoding="utf-8")
            (path / "zhihu-credentials.json").write_text('{"accessSecret": "private-key", "verifiedAt": "today"}', encoding="utf-8")
            self.assertEqual(port.verification()["zhihu"], "passed")
            self.assertEqual(port.verification()["zhihu_search"], "passed")
            (path / "zhihu-account-api-access.json").write_text('{"blocked": true, "code": "LOGIN_REQUIRED"}', encoding="utf-8")
            self.assertEqual(port.verification()["zhihu"], "failed")
            self.assertEqual(port.verification()["zhihu_search"], "passed")
            self.assertIn("zhihu_search", available_operations(port.available()))
            self.assertNotIn("zhihu_home", available_operations(port.available()))

    def test_interactions_require_the_matching_content(self):
        viewed = {("bili_watch", "BV1234567890"), ("xhs_read", "abcdef1234")}
        self.assertTrue(write_allowed("bili_like", {"id": "BV1234567890"}, viewed, set(), []))
        self.assertFalse(write_allowed("bili_like", {"id": "BV0000000000"}, viewed, set(), []))
        self.assertFalse(write_allowed("xhs_like", {"id": "other"}, viewed, set(), []))
        self.assertFalse(write_allowed("zhihu_like", {"token": "invalid"}, viewed, {"real"}, []))

    def test_calendar_does_not_interrupt_other_plans(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
        plan = {"state": "accepted", "kind": "role_plan", "title": "慢慢刷视频",
                "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        self.assertTrue(social_window({"items": [plan]}, now, now, None))
        self.assertIsNone(social_window({"items": [dict(plan, title="上课")]}, now, now, None))
        self.assertIsNone(social_window({"items": [], "partial": True}, now, now, None))
        self.assertTrue(social_window({"items": []}, now, now, now + timedelta(minutes=2)))
        self.assertIsNone(social_window({"items": []}, now, now, now))


class GoogleRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.port = SocialPort(self.directory.name)
        self.port.save_credential("brightdata", "synthetic-token-for-test")
        proxy = "http://proxy-user:" + "x" * 40 + "@companion-social-proxy:7890"
        (self.port.accounts / "foreign-proxy.url").write_text(proxy, encoding="utf-8")

    async def test_transient_google_failure_retries_and_returns_real_success(self):
        failure = {"ok": False, "text": 'Search failed: Google search failed for "stars".'}
        success = {"ok": True, "text": "real search result"}
        formats, results = [], iter([failure, failure, success])
        async def run(params, invoke):
            formats.append(params.env.get("COMPANION_SERP_FULL_JSON"))
            return next(results)
        runner = AsyncMock(side_effect=run)
        with patch.object(self.port, "_run_stdio", runner), \
             patch("astrbot_host.social_port.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.port.call("google_search", {"query": "stars"}), success)
        self.assertEqual(runner.await_count, 3)
        self.assertEqual([call.args[0] for call in sleep.await_args_list], [5, 30])
        self.assertEqual(formats, [None, "1", "1"])
        for call in runner.await_args_list:
            self.assertEqual(call.args[0].env["HTTPS_PROXY"],
                             (self.port.accounts / "foreign-proxy.url").read_text())

    async def test_exhaustion_returns_original_failure_and_auth_does_not_retry(self):
        failure = {"ok": False, "text": "Search failed: Google search failed; try again."}
        with patch.object(self.port, "_run_stdio", new=AsyncMock(return_value=failure)) as runner, \
             patch("astrbot_host.social_port.asyncio.sleep", new_callable=AsyncMock):
            self.assertEqual(await self.port.call("google_search", {"query": "stars"}), failure)
        self.assertEqual(runner.await_count, 3)
        denied = {"ok": False, "text": "HTTP 403: account not authorized"}
        with patch.object(self.port, "_run_stdio", new=AsyncMock(return_value=denied)) as runner, \
             patch("astrbot_host.social_port.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.port.call("google_search", {"query": "stars"}), denied)
        self.assertEqual(runner.await_count, 1)
        sleep.assert_not_awaited()

    async def test_network_exceptions_retry_but_domestic_operations_do_not(self):
        success = {"ok": True, "text": "search result"}
        with patch.object(self.port, "_run_stdio", new=AsyncMock(side_effect=[TimeoutError(), success])) as runner, \
             patch("astrbot_host.social_port.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.port.call("google_search", {"query": "stars"}), success)
        self.assertEqual(runner.await_count, 2)
        sleep.assert_awaited_once_with(5)
        failure = {"ok": False, "text": "HTTP 503"}
        with patch.object(self.port, "_run_stdio", new=AsyncMock(return_value=failure)) as runner, \
             patch("astrbot_host.social_port.asyncio.sleep", new_callable=AsyncMock) as sleep:
            self.assertEqual(await self.port.call("bili_recommend", {}), failure)
        self.assertEqual(runner.await_count, 1)
        self.assertNotIn("HTTPS_PROXY", runner.await_args.args[0].env)
        sleep.assert_not_awaited()

    async def test_rate_limit_reply_is_retried(self):
        limited = {"ok": False, "text": "Rate limited: Too many requests right now."}
        success = {"ok": True, "text": "search result"}
        with patch.object(self.port, "_run_stdio", new=AsyncMock(side_effect=[limited, success])) as runner, \
             patch("astrbot_host.social_port.asyncio.sleep", new_callable=AsyncMock):
            self.assertEqual(await self.port.call("google_search", {"query": "stars"}), success)
        self.assertEqual(runner.await_count, 2)


class SocialCredentialTests(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_scan_status_preserves_pending_and_reports_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            port = SocialPort(directory)
            replies = [
                {"ok": False, "error": {"code": "login_pending", "detail": {"scanned": False}}},
                {"ok": False, "error": {"code": "login_pending", "detail": {"scanned": True}}},
                {"ok": False, "error": {"code": "login_expired"}},
                {"ok": False, "error": {"code": "api_error", "message": "private-data"}},
                {"ok": True, "data": {"logged_in": True}},
            ]
            with patch.object(port, "_bili", AsyncMock(side_effect=replies)):
                unscanned = await port.login_status("bilibili")
                scanned = await port.login_status("bilibili")
                expired = await port.login_status("bilibili")
                failed = await port.login_status("bilibili")
                logged_in = await port.login_status("bilibili")
            self.assertTrue(unscanned["pending"])
            self.assertIn("扫码", unscanned["message"])
            self.assertIn("确认", scanned["message"])
            self.assertFalse(scanned["record_verification"])
            self.assertTrue(expired["terminal"])
            self.assertIn("过期", expired["message"])
            self.assertFalse(expired["record_verification"])
            self.assertEqual(failed["error_code"], "api_error")
            self.assertNotIn("private-data", json.dumps(failed))
            self.assertTrue(logged_in["logged_in"])

    async def test_zhihu_import_only_persists_a_verified_account(self):
        class Response:
            status_code = 403
            def json(self): return {"url_token": "verified-user", "name": "测试角色"}
        class Client:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            async def get(self, *args, **kwargs): return Response()
        with tempfile.TemporaryDirectory() as directory, patch("astrbot_host.social_port.httpx.AsyncClient", return_value=Client()):
            port = SocialPort(directory)
            gate = Path(directory) / "accounts" / "zhihu" / "zhihu-account-api-access.json"
            gate.parent.mkdir(parents=True)
            gate.write_text('{"blocked": true, "code": "LOGIN_REQUIRED"}', encoding="utf-8")
            failed = await port.save_zhihu_cookie('z_c0="private-cookie"; _xsrf=other')
            self.assertFalse(failed["ok"])
            self.assertFalse(port.configured()["zhihu"])
            self.assertTrue(json.loads(gate.read_text())["blocked"])
            Response.status_code = 200
            saved = await port.save_zhihu_cookie('z_c0="private-cookie"; _xsrf=other')
            self.assertTrue(saved["ok"])
            self.assertTrue(port.configured()["zhihu"])
            self.assertFalse(json.loads(gate.read_text())["blocked"])
            self.assertNotIn("private-cookie", json.dumps(saved))

    async def test_zhihu_blocked_account_is_not_reported_as_logged_in(self):
        with tempfile.TemporaryDirectory() as directory:
            port = SocialPort(directory)
            with patch.object(port, "_zhihu_start", AsyncMock(return_value="private-token")), \
                 patch.object(port, "_http_json", AsyncMock(return_value={
                     "web": {"saved": True, "accessScopes": {"account": {"blocked": True}}},
                     "settings": {"upvote": {"enabled": False}},
                 })) as request:
                result = await port.login_status("zhihu")
                self.assertFalse(result["logged_in"])
                self.assertEqual(request.await_count, 1)


class SocialWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_links_can_be_followed_and_internal_redirects_are_rejected(self):
        calls = []
        def response(request):
            calls.append(str(request.url))
            if request.url.path == "/start":
                body = '<html><title>First</title><nav>Ignore navigation</nav><main><p>First page <a href="/next">Next topic</a> <a href="http://127.0.0.1/private">private</a></p><script>ignore me</script></main></html>'
                return httpx.Response(200, text=body, headers={"content-type": "text/html"})
            if request.url.path == "/next":
                return httpx.Response(200, text="<main><h1>Second page</h1><p>More to read.</p></main>", headers={"content-type": "text/html"})
            if request.url.path == "/retry":
                if calls.count("https://example.org/retry") == 1:
                    raise httpx.ConnectError("synthetic connection failure", request=request)
                return httpx.Response(200, text="<main>Recovered page</main>", headers={"content-type": "text/html"})
            return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

        original_client = httpx.AsyncClient
        def client(**kwargs):
            kwargs.pop("proxy", None)
            return original_client(transport=httpx.MockTransport(response), **kwargs)

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(SocialPort, "_foreign_proxy", return_value="http://unused"), \
             patch("astrbot_host.social_port.httpx.AsyncClient", side_effect=client):
            port = SocialPort(directory)
            first = await port.call("google_read", {"id": "https://example.org/start"})
            self.assertTrue(first["ok"])
            page = untrusted_json(first["text"])
            self.assertEqual(page["links"], [{"text": "Next topic", "url": "https://example.org/next"}])
            self.assertNotIn("ignore me", page["content"])
            second = await port.call("google_read", {"id": page["links"][0]["url"]})
            self.assertTrue(second["ok"])
            self.assertIn("Second page", untrusted_json(second["text"])["content"])
            rejected = await port.call("google_read", {"id": "https://example.org/redirect"})
            self.assertFalse(rejected["ok"])
            recovered = await port.call("google_read", {"id": "https://example.org/retry"})
            self.assertTrue(recovered["ok"])
            self.assertEqual(calls, ["https://example.org/start", "https://example.org/next", "https://example.org/redirect",
                                     "https://example.org/retry", "https://example.org/retry"])


class FakeSocialPort:
    route = staticmethod(SocialPort.route)
    context_key = staticmethod(SocialPort.context_key)
    def __init__(self):
        self.calls = []
    def enabled_for(self, context): return True
    def configured(self): return {"bilibili": True}
    def available(self): return {"bilibili": True}
    def blocked_for(self, context, platform): return False
    def block_platform(self, context, platform): raise AssertionError("unexpected block")
    async def call(self, operation, args, *, include_images=False):
        self.calls.append((operation, args.copy()))
        if operation == "bili_watch":
            return {"ok": True, "text": json.dumps({"ok": True, "data": {"transcript": {"text": "夜空有星星"}}})}
        return {"ok": True, "text": json.dumps({"ok": True, "data": {"liked": True}})}


class SocialLifecycleTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (social_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="social.test.v1", fields=configuration_fields(self.profiles))

    async def test_growing_tool_context_finishes_within_the_input_reservation(self):
        from companion_v2.adapters.astrbot.model_wire import build_wire
        from companion_v2.execution.models import TaskError

        estimates = []
        reserve = self.providers[0].reserve
        def bounded_reserve(envelope):
            estimate = build_wire(envelope).input_estimate()
            estimates.append(estimate)
            if estimate > 131072:
                raise TaskError("host_model_limit_input", "fixture input limit exceeded")
            return reserve(envelope)
        self.providers = tuple(replace(provider, reserve=bounded_reserve) for provider in self.providers)
        await self.setup_life()
        await self.configure(social_recommendation_enabled=False)
        class LargePort(FakeSocialPort):
            async def call(self, operation, args, *, include_images=False):
                self.calls.append((operation, args.copy()))
                return {"ok": True, "text": json.dumps({"data": {"transcript": {"text": "星" * 10000}}},
                                                      ensure_ascii=False)}
        port = self.app.social_port = LargePort()
        now = self.clock.now()
        plan = {"state": "accepted", "kind": "role_plan", "title": "刷视频",
                "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [plan], "partial": False}
        async def model(envelope):
            self.assertLessEqual(build_wire(envelope).input_estimate(), 131072)
            # A missing round cap must fail assertions, not leave this test looping.
            if not envelope.allow_tool_calls or len(envelope.tool_history) >= 12:
                plan["title"] = "上课"
                return social_answered("读到一些字幕，先停下来记下感想。")
            count = len(envelope.tool_history)
            return requested(call("read-" + str(count), "social_browse", operation="bili_watch", id="BV1234567890"))
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: bool(self.called))
        await self.settle()
        activity = next(a for a in self.manager._activities() if a.work.kind == "social")
        self.assertEqual(activity.state, RoleActivityState.COMPLETED)
        self.assertEqual(len(port.calls), 8)
        turns = [envelope for envelope in self.called if envelope.original.output_format == "social_reflection"]
        self.assertEqual(len(turns), 9)
        self.assertTrue(turns[-1].tools)
        self.assertFalse(turns[-1].allow_tool_calls)
        history = turns[-1].tool_history
        self.assertEqual([exchange.call.call_id for exchange in history], ["read-" + str(i) for i in range(8)])
        self.assertTrue(any(json.loads(exchange.result_json).get("context_omitted") for exchange in history))
        self.assertTrue(json.loads(history[-1].result_json)["ok"])
        self.assertLessEqual(max(estimates), 131072)
        untrimmed = tuple(replace(exchange, result_json=turns[1].tool_history[0].result_json) for exchange in history)
        self.assertGreater(build_wire(replace(turns[-1], tool_history=untrimmed)).input_estimate(), 131072)
        checkpoint = json.loads(activity.checkpoint)
        self.assertEqual(len(checkpoint["actual_actions"]), 8)
        self.assertIn("感想", checkpoint["subjective_reflection"])

    async def test_real_scheduler_records_read_then_confirmed_interaction(self):
        await self.setup_life()
        port = self.app.social_port = FakeSocialPort()
        now = self.clock.now()
        item = {"state": "accepted", "kind": "role_plan", "title": "刷视频",
                "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [item], "partial": False}
        async def model(envelope):
            count = len(envelope.tool_history)
            if count == 0:
                return requested(call("read", "social_browse", operation="bili_watch", id="BV1234567890"))
            if count == 1:
                return requested(call("like", "social_browse", operation="bili_like", id="BV1234567890"))
            item["title"] = "上课"
            return social_answered("看到了有关星空的字幕，我喜欢这个话题。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: any(a.work.kind == "social" and a.state is RoleActivityState.COMPLETED
                                     for a in self.manager._activities()))
        await self.settle()
        activities = [a for a in self.manager._activities() if a.work.kind == "social"]
        self.assertEqual([name for name, _ in port.calls], ["bili_recommend", "bili_watch", "bili_like"])
        checkpoint = json.loads(activities[0].checkpoint)
        self.assertEqual([x["operation"] for x in checkpoint["actual_actions"]], ["bili_watch", "bili_like"])
        self.assertEqual(checkpoint["actual_actions"][0]["coverage"], "text")
        self.assertEqual(checkpoint["actual_actions"][1]["coverage"], "interaction")
        self.assertIn("星空", checkpoint["subjective_reflection"])
        operation_id = "social-complete-" + digest(activities[0].activity_id)
        from companion_v2.memory.activity_receipts import RoleReceiptReader
        experience = RoleReceiptReader(self.app.life_context).read(self.task_context, "activity", operation_id, self.clock.now())
        self.assertIn("星空", experience.text)
        from companion_v2.adapters.astrbot.model_wire import build_wire, decode_text_response
        from companion_v2.adapters.astrbot.memory_output import response_format
        wire = build_wire(self.called[0], model_name="test")
        self.assertEqual(len(wire.tools), 1)
        self.assertEqual(wire.output_format, "social_reflection")
        self.assertIsNone(response_format(self.called[0].original))
        final, calls = decode_text_response('{"reflection":"我喜欢星空。"}', wire)
        self.assertEqual(json.loads(final.text)["reflection"], "我喜欢星空。")
        self.assertFalse(calls)
        fenced, calls = decode_text_response('```json\n{"reflection":"我喜欢星空。"}\n```', wire)
        self.assertEqual(json.loads(fenced.text)["reflection"], "我喜欢星空。")
        self.assertFalse(calls)
        explained, calls = decode_text_response('说明：\n```json\n{"reflection":"我喜欢星空。"}\n```', wire)
        self.assertEqual(json.loads(explained.text)["reflection"], "我喜欢星空。")
        self.assertFalse(calls)
        for invalid in ('说明：{"reflection":"甲"}和{"reflection":"乙"}',
                        '说明：{"reflection":"甲","reflection":"乙"}',
                        '说明：{"name":"social_browse","arguments":{}}'):
            with self.assertRaises(ValueError):
                decode_text_response(invalid, wire)

    async def test_unknown_write_is_not_retried_or_recorded_as_success(self):
        await self.setup_life()
        class UnknownPort(FakeSocialPort):
            async def call(self, operation, args, *, include_images=False):
                if operation == "bili_like":
                    self.calls.append((operation, args.copy()))
                    return {"ok": False, "text": "结果不明"}
                return await super().call(operation, args, include_images=include_images)
        port = self.app.social_port = UnknownPort()
        now = self.clock.now()
        plan = {"state": "accepted", "kind": "role_plan", "title": "刷视频",
                "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [plan], "partial": False}
        async def model(envelope):
            count = len(envelope.tool_history)
            if count == 0:
                return requested(call("early", "social_browse", operation="bili_like", id="BV1234567890"))
            if count == 1:
                return requested(call("read", "social_browse", operation="bili_watch", id="BV1234567890"))
            if count in (2, 3):
                return requested(call("like" + str(count), "social_browse", operation="bili_like", id="BV1234567890"))
            plan["title"] = "上课"
            return social_answered("读到一段关于星空的字幕。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: any(a.work.kind == "social" and a.state is RoleActivityState.COMPLETED
                                     for a in self.manager._activities()))
        await self.settle()
        self.assertEqual([name for name, _ in port.calls], ["bili_recommend", "bili_watch", "bili_like"])
        receipt = json.loads(next(a for a in self.manager._activities() if a.work.kind == "social").checkpoint)
        self.assertEqual([event["operation"] for event in receipt["actual_actions"]], ["bili_watch"])

    async def test_model_can_decline_without_fabricating_an_experience(self):
        await self.setup_life()
        self.app.social_port = FakeSocialPort()
        now = self.clock.now()
        item = {"state": "accepted", "kind": "role_plan", "title": "上网",
                "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [item], "partial": False}
        async def model(envelope): return social_answered("现在不想浏览。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: any(a.work.kind == "social" and a.state is RoleActivityState.CANCELLED
                                     for a in self.manager._activities()))
        self.assertEqual(self.app.social_port.calls, [])

    async def test_retrieved_image_reaches_a_vision_capable_model(self):
        self.providers = tuple(replace(provider, capabilities=("text", "tools", "image")) for provider in self.providers)
        await self.setup_life()
        class ImagePort(FakeSocialPort):
            async def call(self, operation, args, *, include_images=False):
                result = await super().call(operation, args, include_images=include_images)
                if operation == "bili_watch" and include_images:
                    from PIL import Image
                    from io import BytesIO
                    buffer = BytesIO()
                    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
                    result["_images"] = [buffer.getvalue()]
                return result
        self.app.social_port = ImagePort()
        now = self.clock.now()
        plan = {"state": "accepted", "kind": "role_plan", "title": "看视频",
                "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [plan], "partial": False}
        async def model(envelope):
            if not envelope.tool_history:
                return requested(call("read", "social_browse", operation="bili_watch", id="BV1234567890"))
            self.assertEqual(len(envelope.original.images), 1)
            self.assertEqual(envelope.original.images[0].descriptor.mime_type, "image/png")
            plan["title"] = "上课"
            return social_answered("图片中有红色。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: any(a.work.kind == "social" and a.state is RoleActivityState.COMPLETED
                                     for a in self.manager._activities()))
        await self.settle()
        self.assertEqual(len(self.called[1].original.images), 1)

    async def test_text_browser_receives_a_separate_vision_description(self):
        self.limits = replace(self.limits, background_slots=3)
        vision = replace(self.providers[0], binding=ProviderBinding("model.text", "vision", "1", "vision-1"),
                         capabilities=("text", "image"))
        self.providers += (vision,)
        self.profiles += (social_image_profile("vision"),)
        self.registry = FieldRegistry(version="social.vision.test.v1", fields=configuration_fields(self.profiles))
        await self.setup_life()
        class ImagePort(FakeSocialPort):
            async def call(self, operation, args, *, include_images=False):
                result = await super().call(operation, args, include_images=include_images)
                if operation == "bili_watch" and include_images:
                    from PIL import Image
                    from io import BytesIO
                    buffer = BytesIO()
                    Image.new("RGB", (2, 2), "red").save(buffer, format="PNG")
                    result["_images"] = [buffer.getvalue()]
                return result
        self.app.social_port = ImagePort()
        now = self.clock.now()
        plan = {"state": "accepted", "kind": "role_plan", "title": "看视频",
                "start": (now - timedelta(minutes=1)).isoformat(), "end": (now + timedelta(hours=1)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [plan], "partial": False}
        async def model(envelope):
            if envelope.original.output_format == "social_image_narration":
                self.assertEqual(len(envelope.original.images), 1)
                return answered('{"description":"画面中是一片红色。"}')
            if not envelope.tool_history:
                return requested(call("read", "social_browse", operation="bili_watch", id="BV1234567890"))
            self.assertFalse(envelope.original.images)
            result = json.loads(envelope.tool_history[0].result_json)
            self.assertIn("红色", result["image_narration"])
            self.assertEqual(result["images_delivered_to_model"], 0)
            self.assertEqual(result["images_described_by_visual_model"], 1)
            plan["title"] = "上课"
            return social_answered("视觉转述提到了红色画面。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: any(a.work.kind == "social" and a.state is RoleActivityState.COMPLETED
                                     for a in self.manager._activities()))
        await self.settle()
        checkpoint = json.loads(next(a for a in self.manager._activities() if a.work.kind == "social").checkpoint)
        self.assertEqual(checkpoint["actual_actions"][0]["visual_coverage"], "image_description")
