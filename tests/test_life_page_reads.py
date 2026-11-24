"""Life page privacy, date reads and authorization through real SDK requests."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

import pytest
pytest.importorskip("astrbot")
from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request
from astrbot_host.life_api import LifeWebAPI
from astrbot_host.life_dashboard import dispatch
from astrbot_host.life_page_reads import (LIFE_PAGE_FIELDS, RESERVATION_FIELDS, TRANSACTION_FIELDS,
    WALLET_FIELDS, life_page_data)
from astrbot_host.management_identity import dashboard_operator
from companion_v2.agenda.models import Frequency, Schedule
from companion_v2.configuration.life_provider import LIFE_FIELDS
from companion_v2.execution.models import digest, encode
from companion_v2.foundation.refs import SourceRef, VersionRef
from companion_v2.identity_policy.models import AuthorizationTier
from companion_v2.identity_policy.object_access import ObjectAccessPolicy
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.role_work.models import RoleActivity, RoleActivityState as State
from companion_v2.role_work.work_models import RoleWorkSpec
from companion_v2.sources.models import SourceRecord
from good_life.web_api import LifePageAPI, PREFIX
from tests.multi_life_support import MultiLifeFixture
from tests.test_management_access import NOW
from tests.test_role_state import calendar_data


class LifePageReadTests(MultiLifeFixture, unittest.IsolatedAsyncioTestCase):
    async def setup_page(self):
        self.app = await self.initialize()
        await self.start()
        self.plugin = SimpleNamespace(_runtime=None)
        self.registry = SimpleNamespace(owns_registration=lambda token, caller: token == "lease" and caller is self.plugin)

        async def dashboard(**kwargs):
            return await dispatch(self.controller, self.registry, **kwargs)

        self.plugin._runtime = SimpleNamespace(api=SimpleNamespace(life_dashboard=dashboard), token="lease", ready=True)
        self.page = LifePageAPI(self.plugin)
        self.route = {"target": "first", "role": digest("role-a")}
        return self.page

    async def call(self, handler, *, query="", body=None, username="admin",
                   plugin_name="astrbot_plugin_good_life", route=None):
        raw = json.dumps(body).encode() if body is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        native = Request({"type": "http", "method": "POST" if body is not None else "GET", "path": "/",
            "headers": [(b"content-type", b"application/json")], "query_string": query.encode()}, receive=receive)
        with bind_request_context(PluginRequest(native, username=username, plugin_name=plugin_name)):
            return await handler(**(self.route if route is None else route))

    def calendar_event(self, identifier, *, context=None, data=None, sources=()):
        context = context or self.context()
        return self.app.calendar.command(context, identifier=identifier, expected_revision=0, action="create",
            operation_key="create-" + identifier, data=data or calendar_data(), sources=sources)

    async def test_life_whitelist_removes_private_fields_without_changing_host_response(self):
        page = await self.setup_page()
        raw_status = await self.status()
        self.assertIn("journal_unresolved", raw_status)
        self.assertIn("diary_enabled", raw_status["config"])
        original = LifeWebAPI.status
        captured = {}

        async def expanded(api, **kwargs):
            result = await original(api, **kwargs)
            data = result["data"]
            data.update(next_diary_at="private-marker", journal_unresolved=True,
                interests=[{"interest_id": "i" * 64, "name": "城市散步"}], profile_hobbies="摄影",
                disliked_items=[{"dislike_id": "d" * 64, "title": "太吵的视频"}],
                recent_exposures=[{"title": "街角咖啡店"}],
                future_private_field="private-marker", receipts=[{"operation_id": "private-marker"}],
                running=["diary:private-marker"])
            data["config"].update(diary_time="private-marker", future_private_field="private-marker")
            data["journals"] = [{"kind": "diary", "body": "private-marker"},
                {"kind": "dream", "body": "private-marker"},
                {"kind": "reading_note", "body": "阅读感想", "local_date": "2026-09-21", "private": "private-marker"},
                {"kind": "social_note", "body": "上网感想"}]
            captured.update(data)
            return result

        with patch.object(LifeWebAPI, "status", expanded):
            data = self.ok(await self.call(page.life))
        self.assertEqual(set(data), (LIFE_PAGE_FIELDS - {"next_note_cursor"}) | {"config", "journals", "receipts", "running"})
        self.assertEqual(set(data["config"]), LIFE_FIELDS)
        self.assertEqual(data["receipts"], [])
        self.assertEqual(data["running"], [])
        self.assertEqual([item["kind"] for item in data["journals"]], ["reading_note", "social_note"])
        self.assertNotIn("private-marker", json.dumps(data))
        self.assertEqual(captured["next_diary_at"], "private-marker", "projection must not mutate the full host result")
        for name in ("participant", "profile_id", "target_id", "next_diary_at", "journal_unresolved",
                     "future_private_field"):
            self.assertNotIn(name, data)
        # The interests panel renders the role's interests for the owner's dashboard.
        self.assertEqual(data["interests"], [{"interest_id": "i" * 64, "name": "城市散步"}])
        self.assertEqual(data["profile_hobbies"], "摄影")
        self.assertEqual(data["disliked_items"], [{"dislike_id": "d" * 64, "title": "太吵的视频"}])
        self.assertEqual(data["recent_exposures"], [{"title": "街角咖啡店"}])

    async def test_role_interests_reach_desk_and_life_page_in_multi_target_mode(self):
        page = await self.setup_page()
        await self.enable()
        context = self.context()
        start = NOW - timedelta(hours=8)
        activity = self.app.role_life.add_activity(RoleActivity("social-interest", "role-a", context.scope, start,
            State.ACTIVE, description="浏览", work=RoleWorkSpec("social", context.key, "browse")))
        completed = self.app.role_life.transition_activity(activity.activity_id, owner=activity.owner,
            expected_revision=1, state=State.COMPLETED, operation_id="complete-interest",
            observed_at=start + timedelta(hours=1), checkpoint="完成")
        self.assertTrue(self.app.role_interests.store.apply(context, completed, [{"name": "城市散步",
            "reaction": "positive", "wants_to_continue": True, "reason": "喜欢街角的光", "next_question": "去哪条街"}],
            self.app.now()))
        desk = await self.status()
        self.assertEqual([item["name"] for item in desk["interests"]], ["城市散步"])
        self.assertIn("profile_hobbies", desk)
        life = self.ok(await self.call(page.life))
        self.assertEqual([item["name"] for item in life["interests"]], ["城市散步"])
        self.assertEqual(life["interests"][0]["view_token"], desk["interests"][0]["view_token"])
        for name in ("profile_hobbies", "disliked_items", "recent_exposures"):
            self.assertIn(name, life)
        # The desk's interest actions are accepted by the host command route.
        item = desk["interests"][0]
        result = self.ok(await self.request(data={"action": "interest_clear_dislike", "operation_key": "clear-dislike",
            "view_token": item["view_token"], "expected_revision": item["revision"], "interest_id": item["interest_id"]}))
        self.assertNotIn(result.get("state"), ("failed", "unknown"), result)
        self.assertEqual((await self.status())["interests"][0]["revision"], item["revision"] + 1)

    async def test_life_note_pages_choose_visible_archive_before_date_pagination(self):
        page = await self.setup_page()
        calls = []
        notes = [{"kind": "reading_note", "local_date": (NOW.date() - timedelta(days=n)).isoformat()}
                 for n in range(51)]

        def status(_manager, _operator, **query):
            calls.append(query)
            if query["archive_kind"] != "reading_note":
                return {"config": {}, "journals": [{"kind": "diary", "local_date": "2026-09-21"}],
                        "next_date": "2026-09-21"}
            selected = notes[50:] if query["before"] else notes[:50]
            return {"config": {}, "journals": selected, "next_date": selected[-1]["local_date"] if len(selected) == 50 else None}

        with patch.object(RoleLifeManagement, "status", status):
            for query in ("", "archive_kind=diary", "archive_kind=reading_note"):
                first = self.ok(await self.call(page.life, query=query))
                self.assertEqual(len(first["journals"]), 50)
                self.assertEqual(first["next_date"], notes[49]["local_date"])
                second = self.ok(await self.call(page.life, query=urlencode({"before": first["next_date"]})))
                self.assertEqual(second["journals"], notes[50:])
                self.assertIsNone(second["next_date"])
            self.assertTrue(all(call["archive_kind"] == "reading_note" for call in calls))
            await self.status()
            self.assertIsNone(calls[-1]["archive_kind"], "the original host life API keeps its default archive")
        self.assertEqual((await self.call(page.life, query="archive_kind=unknown"))["message"], "life_query_invalid")

    async def test_state_cursor_tracks_requested_page_including_empty_last_page(self):
        page = await self.setup_page()
        for identifier in ("plan-a", "plan-b"):
            self.calendar_event(identifier)
        first = self.ok(await self.call(page.state, query="limit=1"))
        self.assertEqual(first["request_cursor"], "")
        cursor = first["calendar"]["next_cursor"]
        second = self.ok(await self.call(page.state, query=urlencode({"limit": 1, "after_id": cursor})))
        self.assertEqual(second["request_cursor"], cursor)
        self.assertEqual(second["calendar"]["items"][0]["id"], "plan-b")
        last = self.ok(await self.call(page.state, query="after_id=plan-b"))
        self.assertEqual(last["request_cursor"], "plan-b")
        self.assertEqual(last["calendar"]["items"], [])
        self.assertIsNone(last["calendar"]["next_cursor"])
        self.assertEqual(self.ok(await self.call(page.state))["request_cursor"], "")
        for query in ("after_id=a&after_id=b", "limit=0", "operator=admin"):
            self.assertEqual((await self.call(page.state, query=query))["message"], "state_query_invalid")

    async def test_calendar_range_uses_server_instances_exceptions_and_selected_role(self):
        page = await self.setup_page()
        schedule = Schedule(NOW - timedelta(hours=1), "Asia/Shanghai", Frequency.DAILY, count=4)
        self.calendar_event("daily", data=calendar_data(schedule=json.loads(encode(schedule))))
        context = self.context()
        self.app.calendar.command(context, identifier="daily", expected_revision=1, action="exception",
            operation_key="skip", data={"sequence": 1, "start": None})
        self.app.calendar.command(context, identifier="daily", expected_revision=2, action="exception",
            operation_key="move", data={"sequence": 2, "start": (NOW + timedelta(days=3, hours=5)).isoformat()})
        self.calendar_event("other-role", context=self.context(role="role-b"))
        result = self.ok(await self.call(page.calendar_occurrences, query="start=2026-09-21&end=2026-09-25"))
        self.assertEqual(result["timezone"], "Asia/Shanghai")
        self.assertEqual([item["sequence"] for item in result["items"]], [0, 3, 2])
        self.assertEqual({item["id"] for item in result["items"]}, {"daily"})
        self.assertEqual(set(result["items"][0]), {"id", "revision", "sequence", "kind", "state", "title",
            "detail", "start", "end", "presence", "sources"})
        self.assertFalse(result["partial"])
        self.assertEqual(self.native.provider.client.state.calls, [])

    async def test_overview_calendar_opts_into_cancelled_and_skipped_occurrences(self):
        page = await self.setup_page()
        context = self.context()
        self.calendar_event("cancelled")
        self.app.calendar.command(context, identifier="cancelled", expected_revision=1, action="cancel",
            operation_key="cancel-overview", data={})
        self.calendar_event("skipped")
        self.app.calendar.command(context, identifier="skipped", expected_revision=1, action="exception",
            operation_key="skip-overview", data={"sequence": 0, "start": None})
        self.calendar_event("ordinary")
        other = self.context(role="role-b")
        self.calendar_event("other-role-cancelled", context=other)
        self.app.calendar.command(other, identifier="other-role-cancelled", expected_revision=1, action="cancel",
            operation_key="cancel-other", data={})
        query = "start=2026-09-21&end=2026-09-22"
        expected = {"status": "ok", "message": "", "data": {
            "start": "2026-09-21", "end": "2026-09-22", "timezone": "Asia/Shanghai",
            "items": [{"id": "ordinary", "revision": 1, "sequence": 0, "kind": "role_plan", "state": "accepted",
                "title": "私人阅读安排", "detail": "读自己选的书",
                "start": (NOW + timedelta(hours=1)).isoformat(timespec="microseconds"),
                "end": (NOW + timedelta(minutes=90)).isoformat(timespec="microseconds"),
                "presence": "阅读中", "sources": []}],
            "partial": False, "skipped": 0, "skipped_reasons": {}, "scan_truncated": False,
            "occurrences_truncated": 0}}
        default = await self.call(page.calendar_occurrences, query=query)
        self.assertEqual(json.dumps(default).encode(), json.dumps(expected).encode())
        result = self.ok(await self.call(page.calendar_occurrences, query=query + "&include_cancelled=1"))
        items = {row["id"]: row for row in result["items"]}
        self.assertEqual(datetime.fromisoformat(items["cancelled"]["outcome_at"]), NOW)
        self.assertNotIn("outcome_at", items["skipped"])
        self.assertEqual({identifier: row["outcome"] for identifier, row in items.items() if identifier != "ordinary"}, {
            "cancelled": {"state": "cancelled", "reason_code": "cancelled", "reason_label": "被取消"},
            "skipped": {"state": "skipped", "reason_code": "skipped", "reason_label": "已跳过"},
        })

    async def test_life_overview_reads_only_authorized_scope_and_projects_optional_fields(self):
        page = await self.setup_page()
        self.assertEqual(self.ok(await self.call(page.life, query="overview=1"))["overview_activities"],
                         {"items": [], "partial": False})
        context = self.context()
        manager = RoleLifeManagement(self.app, context)
        activity = self.app.role_life.add_activity(RoleActivity("cancelled-social", "role-a", context.scope, NOW,
            State.ACTIVE, description="浏览", work=RoleWorkSpec("social", context.key, "overview")))
        self.app.role_life.transition_activity(activity.activity_id, owner=activity.owner, expected_revision=1,
            state=State.CANCELLED, operation_id="cancelled-social", observed_at=NOW)
        calls = []

        def read(**scope):
            calls.append(scope)
            return {"items": [{"activity_id": "instance", "kind": "purchase", "title": "买饮料",
                "state": "unknown", "simulated": True, "reason_code": "result_unknown", "checkpoint": "private"}],
                "partial": False}

        self.plugin._runtime.read_overview_activities = read
        # A deterministic pre-overview fixture preserves the full serialized
        # response, including the order of fields and the reading task state.
        legacy = [{"kind": "social", "state": "cancelled", "title": "浏览"},
                  {"kind": "reading", "state": "paused", "task": {"state": "unknown", "reason": "待确认"}}]
        with patch.object(RoleLifeManagement, "status", return_value={"activities": legacy, "config": {}}):
            default = await self.call(page.life)
        expected = {"status": "ok", "message": "", "data": {"activities": legacy, "config": {},
            "journals": [], "next_date": None, "receipts": [], "running": []}}
        self.assertEqual(json.dumps(default).encode(), json.dumps(expected).encode())
        self.assertEqual((await self.call(page.life, query="overview=1", username=None))["message"],
                         "dashboard_identity_required")
        self.assertEqual((await self.call(page.life, query="overview=1&owner_key=other"))["message"], "life_query_invalid")
        self.assertEqual(calls, [])
        result = self.ok(await self.call(page.life, query="overview=1"))
        self.assertEqual(calls, [{"owner_key": digest(manager.owner), "context_key": context.key, "now": NOW}])
        self.assertEqual(result["activities"][0]["outcome"]["reason_code"], "cancelled")
        self.assertEqual(result["overview_activities"]["items"], [{"activity_id": "instance", "kind": "purchase",
            "title": "买饮料", "state": "unknown", "outcome": {"state": "unknown", "source": "simulated",
                "reason_code": "result_unknown", "reason_label": "结果尚未确认"}}])

    async def test_calendar_source_revocation_is_reported_without_returning_hidden_event(self):
        page = await self.setup_page()
        context = self.context()
        source = SourceRef("range-source", "test", context.scope, VersionRef("range-source", 1), NOW)
        self.app.messages.sources.publish(SourceRecord(source, context.audience, "source", "test"), observed_at=NOW).unwrap()
        self.calendar_event("source-plan", sources=(source,))
        with self.app.runtime.transaction() as connection:
            connection.execute("UPDATE sources SET state='revoked' WHERE source_id=?", (source.source_id,))
        result = self.ok(await self.call(page.calendar_occurrences, query="start=2026-09-21&end=2026-09-22"))
        self.assertEqual(result["items"], [])
        self.assertTrue(result["partial"])
        self.assertEqual(result["skipped"], 1)
        self.assertEqual(result["skipped_reasons"], {"source_unavailable": 1})

    async def test_calendar_range_handles_dst_month_and_preserves_truncation_diagnostics(self):
        page = await self.setup_page()
        status = self.ok(await self.call(page.state))
        self.ok(await self.call(page.state_command, body={"action": "configure_role", "operation_key": "range-zone",
            "role_edit_token": status["role_edit_token"], "changes": {"planning_timezone": "Europe/Berlin"}}))
        calls = []

        def occurrences(_context, *, start, end, operator, diagnostics):
            self.assertLessEqual(end - start, timedelta(days=31))
            calls.append((start, end))
            return {"items": [], "partial": True, "skipped": 2, "skipped_reasons": {"source_unavailable": 2},
                    "scan_truncated": True, "occurrences_truncated": 1}

        with patch.object(self.app.calendar, "occurrences", occurrences):
            result = self.ok(await self.call(page.calendar_occurrences, query="start=2026-10-01&end=2026-11-01"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0], datetime(2026, 9, 30, 22, tzinfo=timezone.utc))
        self.assertEqual(calls[-1][1], datetime(2026, 10, 31, 23, tzinfo=timezone.utc))
        self.assertEqual(result["skipped"], 2)
        self.assertTrue(result["partial"])
        self.assertTrue(result["scan_truncated"])

    async def test_activity_history_returns_original_revisions_and_exclusive_local_day(self):
        page = await self.setup_page()
        context = self.context()
        start = NOW - timedelta(hours=8)
        activity = self.app.role_life.add_activity(RoleActivity("social-history", "role-a", context.scope, start,
            State.ACTIVE, description="浏览", work=RoleWorkSpec("social", context.key, "history")))
        self.app.role_life.transition_activity(activity.activity_id, owner=activity.owner, expected_revision=1,
            state=State.COMPLETED, operation_id="complete-history", observed_at=start + timedelta(hours=2), checkpoint="完成")
        self.app.role_life.add_activity(RoleActivity("next-day", "role-a", context.scope, start + timedelta(days=1),
            State.ACTIVE, work=RoleWorkSpec("social", context.key, "tomorrow")))
        self.app.role_life.add_activity(RoleActivity("host-private-activity", "role-a", context.scope, NOW, State.ACTIVE,
            description="private-marker"))
        self.app.role_life.add_activity(RoleActivity("private-other-context", "role-a", context.scope, NOW, State.ACTIVE,
            description="private-marker", work=RoleWorkSpec("social", "other-context", "hidden")))
        other = self.context(role="role-b")
        self.app.role_life.add_activity(RoleActivity("other-role", "role-b", other.scope, NOW, State.ACTIVE,
            description="private-marker", work=RoleWorkSpec("social", other.key, "other")))
        cursor, items = None, []
        while True:
            query = {"date": "2026-09-21", "limit": 1}
            if cursor:
                query["before"] = cursor
            result = self.ok(await self.call(page.activity_history, query=urlencode(query)))
            self.assertNotIn("host-private-activity", json.dumps(result))
            self.assertNotIn("private-other-context", json.dumps(result))
            self.assertNotIn("private-marker", json.dumps(result))
            items.extend(result["items"])
            cursor = result["next_cursor"]
            if cursor is None:
                break
        self.assertEqual([(item["activity_id"], item["revision"], item["state"]) for item in items],
                         [("social-history", 2, "completed"), ("social-history", 1, "active")])
        self.assertNotIn("private-marker", json.dumps(items))
        self.assertEqual(self.ok(await self.call(page.activity_history, query="date=2026-09-20"))["items"], [])

    async def test_reading_history_keeps_saved_span_after_current_configuration_changes(self):
        page = await self.setup_page()
        await self.command("configure", changes={"reading_chunk_chars": 600})
        await self.command("add_text", key="historical-book", title="原始片段", text="这是原文。" * 400)
        saved = next(activity for activity in self.routine().activities() if activity.work.kind == "reading")
        await self.command("configure", key="resize-current-shelf", changes={"reading_chunk_chars": 1200})
        with patch.object(RoleLifeManagement, "_project_activity", side_effect=AssertionError("current shelf projection used")):
            result = self.ok(await self.call(page.activity_history, query="date=2026-09-21"))
        item = next(item for item in result["items"] if item["activity_id"] == saved.activity_id)
        self.assertEqual(item["title"], saved.description)
        self.assertEqual(item["end_offset"], saved.work.end_offset)
        self.assertEqual(item["revision"], saved.revision)
        self.assertIsNone(item["task"], "a current task cannot stand in for an original task receipt")

    async def test_new_routes_reject_anonymous_wrong_plugin_registration_and_non_core_operator(self):
        page = await self.setup_page()
        for handler, query in ((page.calendar_occurrences, "start=2026-09-21&end=2026-09-22&include_cancelled=1"),
                               (page.life, "overview=1"), (page.activity_history, "date=2026-09-21"), (page.wallet, "")):
            for username in (None, "", "api_key:42"):
                self.assertEqual((await self.call(handler, query=query, username=username))["message"], "dashboard_identity_required")
            self.assertEqual((await self.call(handler, query=query, plugin_name="wrong"))["message"], "dashboard_identity_required")
            self.assertEqual((await self.call(handler, query=query, route=self.route | {"target": "missing"}))["message"], "life_dashboard_invalid")
            self.plugin._runtime.token = "expired"
            self.assertEqual((await self.call(handler, query=query))["message"], "life_provider_unavailable")
            self.plugin._runtime.token = "lease"
        operator = dashboard_operator(self.app.runtime, "admin", at=NOW)
        policy = ObjectAccessPolicy(self.app.runtime)
        policy.set_tier(operator.subject, AuthorizationTier.STANDARD, expected_revision=policy.revision, observed_at=NOW)
        for handler, query in ((page.calendar_occurrences, "start=2026-09-21&end=2026-09-22&include_cancelled=1"),
                               (page.life, "overview=1"), (page.activity_history, "date=2026-09-21"), (page.wallet, "")):
            self.assertIn((await self.call(handler, query=query))["message"], ("core_access_required", "authorization_denied"))

    async def test_new_route_query_validation_and_registration_cleanup(self):
        page = await self.setup_page()
        for handler, queries in ((page.calendar_occurrences, ("", "start=20260921&end=2026-09-22",
            "start=2026-09-22&end=2026-09-21", "start=2026-09-01&end=2026-11-01",
            "start=2026-09-21&end=2026-09-21", "start=2026-09-21&start=2026-09-22&end=2026-09-23",
            "start=2026-09-21&end=2026-09-22&operator=admin")),
            (page.activity_history, ("", "date=2026-09-31", "date=20260921", "date=2026-09-21&limit=101",
            "date=2026-09-21&before=not-json", "date=2026-09-21&before=%7B%7D",
            "date=2026-09-21&date=2026-09-22", "date=2026-09-21&participant=someone"))):
            for query in queries:
                with self.subTest(query=query):
                    self.assertEqual((await self.call(handler, query=query))["message"], "life_query_invalid")
        routes = []
        page.plugin.context = SimpleNamespace(register_web_api=lambda *args: routes.append(args), registered_web_apis=routes)
        page.register()
        paths = {path for path, _handler, methods, _description in routes if methods == ["GET"]}
        self.assertIn(PREFIX + "/targets/<target>/<role>/calendar/occurrences", paths)
        self.assertIn(PREFIX + "/targets/<target>/<role>/life/activities", paths)
        self.assertIn(PREFIX + "/targets/<target>/<role>/life/wallet", paths)
        page.unregister()
        self.assertEqual(routes, [])


    def setup_wallet(self, *, enabled=True, currency="CNY"):
        from test_kernel_support import Access, SimulationClock
        from good_life.activities.buy_drink import register_buy_drink
        from good_life.economy import WalletAccess
        from good_life.kernel import ComponentRegistry, KernelConfig
        from good_life.runtime import LifeRuntime
        from good_life.storage import LifeStorage
        temporary = TemporaryDirectory(prefix=".wallet-page-", dir=ROOT)
        self.addCleanup(temporary.cleanup)
        storage = LifeStorage.open(temporary.name)
        self.addCleanup(storage.close)
        host = Access()
        manager = RoleLifeManagement(self.app, self.context())
        host.scope = replace(host.scope, owner_key=digest(manager.owner), context_key=manager.context.key)
        host.inventory = (host.scope,)
        registry = ComponentRegistry()
        self.wallet_domain, self.purchase_template = register_buy_drink(registry, configured_currency=currency,
            reserve_minor=36, payments=({"at_s": 60, "amount_minor": 6}, {"at_s": 120, "amount_minor": 20},
                                        {"at_s": 180, "amount_minor": 6}))
        self.wallet_runtime = LifeRuntime(storage, self.plugin, kernel_config=KernelConfig(enabled=enabled),
            kernel_registry=registry, kernel_access=WalletAccess(host, self.wallet_domain))
        self.wallet_scope = host.scope
        self.plugin._runtime.read_wallet_page = self.wallet_runtime.read_wallet_page
        if self.wallet_runtime.kernel:
            self.addCleanup(self.wallet_runtime.kernel.close)
            self.wallet_runtime.kernel.kernel.clock = SimulationClock()
        return storage

    def wallet_income(self, amount=100, key="initial", *, owner=None, at=NOW):
        from good_life.contracts.models import Money, Provenance
        from good_life.economy import Evidence
        owner = owner or self.wallet_scope.owner_key
        return self.wallet_domain.record_income(owner, key, Money(amount, self.wallet_domain.currency),
            operation_id=key, expected_revision=self.wallet_domain.quote_available(owner).revision,
            evidence=Evidence("private-source-" + key, at, Provenance.SIMULATED))

    def start_wallet_purchase(self):
        from good_life.contracts.models import TemplateRef
        kernel, scope = self.wallet_runtime.kernel, self.wallet_scope
        candidate = kernel.propose(scope, TemplateRef(self.purchase_template.id, self.purchase_template.version),
            opportunity_key="purchase", reason="drink_requested", now=NOW, expires_at=NOW + timedelta(hours=1))
        instance = kernel.prepare_candidate(scope, candidate.candidate_id, command_id="prepare", now=NOW).instance
        return kernel.start(scope, instance.instance_id, expected_revision=instance.revision, command_id="start", now=NOW)

    async def test_wallet_off_missing_install_disabled_and_closed_do_not_write(self):
        page = await self.setup_page()
        self.assertEqual(self.ok(await self.call(page.wallet)), {"enabled": False})
        storage = self.setup_wallet(enabled=False)
        initial = storage._connection.total_changes
        self.assertEqual(self.ok(await self.call(page.wallet)), {"enabled": False})
        self.assertEqual(storage._connection.total_changes, initial)
        self.assertIsNone(storage._connection.execute("SELECT 1 FROM sqlite_master WHERE name='life_wallet_accounts'").fetchone())
        self.wallet_runtime.closed = True
        self.assertEqual(self.ok(await self.call(page.wallet)), {"enabled": False})

    async def test_wallet_empty_account_is_read_only_and_currency_units_are_correct(self):
        page = await self.setup_page()
        storage = self.setup_wallet(currency="JPY")
        initial, trace = storage._connection.total_changes, []
        storage._connection.set_trace_callback(trace.append)
        data = self.ok(await self.call(page.wallet))
        storage._connection.set_trace_callback(None)
        self.assertEqual(data, {"enabled": True, "currency": "JPY", "minor_unit_digits": 0,
            "balance_minor": 0, "reserved_minor": 0, "available_minor": 0,
            "reservations": [], "transactions": [], "next_before": None})
        self.assertEqual(storage._connection.total_changes, initial)
        self.assertFalse(any(sql.lstrip().upper().startswith(("BEGIN", "INSERT", "UPDATE", "DELETE", "CREATE")) for sql in trace))
        self.assertEqual(storage._connection.execute("SELECT COUNT(*) FROM life_wallet_accounts").fetchone()[0], 0)
        self.wallet_domain.storage = None
        self.assertEqual(self.ok(await self.call(page.wallet)), {"enabled": False})
        self.wallet_domain.storage = storage
        self.wallet_runtime.kernel.close()
        self.assertEqual(self.ok(await self.call(page.wallet)), {"enabled": False})

    async def test_wallet_100_reserve_36_spend_32_release_and_human_activity_label(self):
        page = await self.setup_page()
        storage = self.setup_wallet()
        self.wallet_income()
        instance = self.start_wallet_purchase()
        initial = storage._connection.total_changes
        data = self.ok(await self.call(page.wallet))
        self.assertEqual((data["balance_minor"], data["reserved_minor"], data["available_minor"]), (100, 36, 64))
        self.assertEqual(data["minor_unit_digits"], 2)
        self.assertEqual(data["reservations"][0], {"reservation_id": instance.instance_id + ":budget",
            "activity_label": "买饮料", "limit_minor": 36, "amount_remaining_minor": 36,
            "status": "active", "review_at": self.wallet_domain.reservation(self.wallet_scope.owner_key,
                instance.instance_id + ":budget").review_at.isoformat()})
        self.assertIsNone(data["transactions"][0]["activity_label"])
        self.assertEqual(storage._connection.total_changes, initial)
        for seconds in (60, 120, 180):
            instance = self.wallet_runtime.kernel.advance(self.wallet_scope, instance.instance_id,
                expected_revision=instance.revision, command_id="pay-" + str(seconds), now=NOW + timedelta(seconds=seconds))
        data = self.ok(await self.call(page.wallet))
        self.assertEqual((data["balance_minor"], data["reserved_minor"], data["available_minor"]), (68, 0, 68))
        self.assertEqual(data["reservations"], [])
        self.assertEqual([item["amount_minor"] for item in data["transactions"]], [6, 20, 6, 100])
        self.assertEqual([item["kind"] for item in data["transactions"]], ["spend", "spend", "spend", "income"])
        self.assertEqual([item["activity_label"] for item in data["transactions"]], ["买饮料"] * 3 + [None])
        self.assertEqual(set(data), WALLET_FIELDS | {"reservations", "transactions"})
        self.assertEqual(set(data["transactions"][0]), TRANSACTION_FIELDS)

    async def test_wallet_pagination_tied_timestamps_and_newer_insert_between_pages(self):
        page = await self.setup_page()
        self.setup_wallet()
        # Deliberately insert out of chronological order, with a full tied page.
        for index in range(23):
            self.wallet_income(1, "income-" + str(index), at=NOW + timedelta(seconds=index % 3))
        first = self.ok(await self.call(page.wallet))
        self.assertEqual(len(first["transactions"]), 20)
        self.assertIsNotNone(first["next_before"])
        self.assertEqual(self.ok(await self.call(page.wallet, query="limit=50"))["next_before"], None)
        original = self.ok(await self.call(page.wallet, query="limit=50"))["transactions"]
        self.wallet_income(1, "newer", at=NOW + timedelta(days=1))
        second = self.ok(await self.call(page.wallet, query=urlencode({"before": first["next_before"]})))
        self.assertEqual(len(second["transactions"]), 3)
        self.assertIsNone(second["next_before"])
        self.assertEqual(first["transactions"] + second["transactions"], original)
        ordered = [(item["occurred_at"], item["transaction_id"]) for item in original]
        self.assertEqual(ordered, sorted(ordered, reverse=True))
        cursor, items = None, []
        while True:
            query = {"limit": 1, **({"before": cursor} if cursor else {})}
            result = self.ok(await self.call(page.wallet, query=urlencode(query)))
            items += result["transactions"]
            cursor = result["next_before"]
            if cursor is None:
                break
        self.assertEqual(len(items), 24)
        self.assertEqual(len({item["transaction_id"] for item in items}), 24)

    async def test_wallet_whitelist_trims_all_levels_and_disabled_payload(self):
        page = await self.setup_page()
        self.setup_wallet()
        self.wallet_income()
        self.start_wallet_purchase()
        original = self.plugin._runtime.read_wallet_page
        private = {key: "private-marker" for key in ("source_ref", "operation_id", "instance_id", "evidence",
            "owner_key", "proposal", "future_private_field")}
        captured = {}

        def expanded(**query):
            result = original(**query)
            result.update(private)
            for item in result["reservations"] + result["transactions"]:
                item.update(private)
            captured.update(result)
            return result

        self.plugin._runtime.read_wallet_page = expanded
        data = self.ok(await self.call(page.wallet))
        self.assertEqual(set(data), WALLET_FIELDS | {"reservations", "transactions"})
        self.assertEqual(set(data["reservations"][0]), RESERVATION_FIELDS)
        self.assertEqual(set(data["transactions"][0]), TRANSACTION_FIELDS)
        self.assertNotIn("private-marker", json.dumps(data))
        self.assertIn("private-marker", json.dumps(captured), "projection must not mutate the provider result")
        self.plugin._runtime.read_wallet_page = lambda **query: {"enabled": False, **private, "transactions": [private]}
        self.assertEqual(self.ok(await self.call(page.wallet)), {"enabled": False})

    async def test_wallet_owner_isolation_and_unknown_or_other_context_labels_are_null(self):
        from good_life.contracts.models import Money, Provenance
        from good_life.economy import Evidence
        page = await self.setup_page()
        storage = self.setup_wallet()
        self.wallet_income()
        instance = self.start_wallet_purchase()
        self.wallet_income(777, "other-owner-income", owner="other-owner")
        self.wallet_domain.reserve("other-owner", "private-marker", Money(500, "CNY"), review_at=NOW + timedelta(hours=1),
            operation_id="other-reserve", expected_revision=1,
            evidence=Evidence("private-marker", NOW, Provenance.SIMULATED, instance.instance_id))
        data = self.ok(await self.call(page.wallet))
        self.assertEqual((data["balance_minor"], len(data["transactions"]), len(data["reservations"])), (100, 1, 1))
        self.assertNotIn("private-marker", json.dumps(data))
        other = self.ok(await self.call(page.wallet, route=self.route | {"role": digest("role-b")}))
        self.assertEqual((other["balance_minor"], other["transactions"], other["reservations"]), (0, [], []))
        for changes in ({"context_key": "other-context"}, {"template_id": "unknown-template"}):
            with storage.transaction() as db:
                row = db.execute("SELECT record_json FROM life_kernel_instances WHERE owner_key=? AND instance_id=?",
                    (self.wallet_scope.owner_key, instance.instance_id)).fetchone()
                saved = json.loads(row[0])
                db.execute("UPDATE life_kernel_instances SET record_json=? WHERE owner_key=? AND instance_id=?",
                    (json.dumps(saved | changes), self.wallet_scope.owner_key, instance.instance_id))
            self.assertIsNone(self.ok(await self.call(page.wallet))["reservations"][0]["activity_label"])

    async def test_wallet_sqlite_errors_return_failure_envelope(self):
        page = await self.setup_page()
        storage = self.setup_wallet()
        self.wallet_income()
        instance = self.start_wallet_purchase()
        with storage.transaction() as db:
            db.execute("UPDATE life_kernel_instances SET record_json=? WHERE owner_key=? AND instance_id=?",
                ("private-malformed-json", self.wallet_scope.owner_key, instance.instance_id))
        initial = storage._connection.total_changes
        malformed = await self.call(page.wallet)
        self.assertEqual(storage._connection.total_changes, initial)
        storage.close()
        closed = await self.call(page.wallet)
        for result in (malformed, closed):
            self.assertEqual(result["status"], "error")
            self.assertEqual(result["message"], "wallet_unavailable")
            self.assertEqual(result["data"], {"code": "wallet_unavailable"})
            self.assertEqual(result["reason"], "wallet storage is unavailable")
            self.assertNotIn("private-malformed-json", json.dumps(result))

    async def test_wallet_invariant_errors_preserve_kernel_code(self):
        from good_life.contracts.models import ReasonCode
        from good_life.kernel import KernelError
        page = await self.setup_page()
        storage = self.setup_wallet()
        self.wallet_income()
        with storage.transaction() as db:
            db.execute("UPDATE life_wallet_accounts SET currency='USD' WHERE owner_key=?", (self.wallet_scope.owner_key,))
        initial = storage._connection.total_changes
        # AstrBot's relative plugin imports need not expose a top-level package.
        with patch.dict(sys.modules, {"good_life.kernel.codec": None}):
            mismatch = await self.call(page.wallet)
        self.assertEqual(storage._connection.total_changes, initial)
        self.assertEqual(mismatch["status"], "error")
        self.assertEqual(mismatch["message"], "invalid_config")
        self.assertEqual(mismatch["data"], {"code": "invalid_config"})
        self.assertEqual(mismatch["reason"], "wallet currency mismatch")
        with patch.object(self.wallet_domain, "_quote", side_effect=KernelError(ReasonCode.RESULT_UNKNOWN, "wallet result unknown")):
            result = await self.call(page.wallet)
        self.assertEqual(result["status"], "error")
        self.assertEqual(result["message"], "result_unknown")
        self.assertEqual(result["data"], {"code": "result_unknown"})

    async def test_wallet_invalid_queries_use_life_errors_even_when_off(self):
        page = await self.setup_page()
        invalid_cursors = ["not-json", "{}", "[]", '[null,"id"]', '["2026-11-16","id"]',
            '["2026-11-16T00:00:00","id"]', '["2026-11-16T00:00:00+08:00","id"]',
            '["2026-11-16T00:00:00+00:00",1]', '["2026-11-16T00:00:00+00:00",""]',
            json.dumps(["2026-11-16T00:00:00+00:00", "x" * 257]), "x" * 2049, "[" * 1000]
        queries = ["limit=0", "limit=51", "limit=-1", "limit=1.5", "limit=", "limit=true",
            "limit=1&limit=2", "before=a&before=b", "owner_key=other", "date=2026-11-16"]
        queries += [urlencode({"before": cursor}) for cursor in invalid_cursors]
        for query in queries:
            with self.subTest(query=query):
                result = await self.call(page.wallet, query=query)
                self.assertEqual(result["status"], "error")
                self.assertEqual(result["message"], "life_query_invalid")
                self.assertEqual(result["data"], {"code": "life_query_invalid"})

    async def class_request(self, query, *, username="admin"):
        path = self.paths[("first", "role-a")] + "/classes"
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        native = Request({"type": "http", "method": "GET", "path": path,
            "headers": [], "query_string": query.encode()}, receive=receive)
        with bind_request_context(PluginRequest(native, username=username)):
            return await self.routes[path]()

    async def test_cottage_class_read_uses_local_dates_and_fails_closed_without_academics(self):
        await self.setup_page()
        self.assertEqual((await self.class_request("start=2026-09-21&end=2026-09-22"))["message"], "missing_dependency")
        calendar, calls = self.app.calendar, []
        def sessions(context, start, end):
            calls.append((context.key, start, end))
            return [{"course": "马克思主义哲学导论", "date": "2026-09-21", "start": "2026-09-21T09:55:00+08:00",
                     "end": "2026-09-21T11:35:00+08:00", "place": "示例教学楼", "cancelled": False, "private": "x"},
                    {"course": "大学英语", "date": "2026-09-21", "start": "2026-09-21T14:00:00+08:00",
                     "end": "2026-09-21T15:40:00+08:00", "place": None, "cancelled": True},
                    {"course": "明天的课", "date": "2026-09-22", "start": "2026-09-22T08:00:00+08:00",
                     "end": "2026-09-22T09:40:00+08:00", "place": None, "cancelled": False}]
        with patch.object(calendar, "class_sessions", sessions):
            result = self.ok(await self.class_request("start=2026-09-21&end=2026-09-22"))
            self.assertEqual((result["start"], result["end"], result["timezone"]), ("2026-09-21", "2026-09-22", "Asia/Shanghai"))
            self.assertEqual([(item["course"], item["cancelled"]) for item in result["items"]],
                             [("马克思主义哲学导论", False), ("大学英语", True)])
            self.assertEqual(set(result["items"][0]), {"course", "date", "start", "end", "place", "cancelled"})
            self.assertEqual(calls[-1][0], self.context().key)
            self.assertEqual((calls[-1][1].isoformat(), calls[-1][2].isoformat()),
                             ("2026-09-20T16:00:00+00:00", "2026-09-21T16:00:00+00:00"), "local midnight, end exclusive")
            for query in ("", "start=2026-09-21", "start=2026-09-22&end=2026-09-21", "start=2026-09-21&end=2026-09-21",
                          "start=2026-09-01&end=2026-09-30", "start=20260921&end=2026-09-22",
                          "start=2026-09-21&end=2026-09-22&owner_key=other", "start=2026-09-21&start=2026-09-21&end=2026-09-22"):
                with self.subTest(query=query):
                    self.assertEqual((await self.class_request(query))["message"], "life_query_invalid")
            for username in (None, ""):
                self.assertEqual((await self.class_request("start=2026-09-21&end=2026-09-22", username=username))["message"],
                                 "dashboard_identity_required")
        self.assertEqual(len(calls), 1, "rejected requests never read classes")


class LifePageProjectionTests(unittest.TestCase):
    def test_note_pagination_uses_visible_dates_and_never_a_hidden_diary_date(self):
        data = {"config": {}, "journals": [{"kind": "diary", "local_date": "2026-01-01"}],
                "next_date": "2026-01-01"}
        self.assertIsNone(life_page_data(data)["next_date"])
        first = NOW.date()
        data["journals"] += [{"kind": "reading_note", "local_date": (first - timedelta(days=n)).isoformat()}
                             for n in range(50)]
        self.assertEqual(life_page_data(data)["next_date"], (first - timedelta(days=49)).isoformat())
