"""Paired W6 release and rollback against local SQLite and synthetic providers."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins"), str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
pytest.importorskip("astrbot")

from astrbot.api.star import StarTools
from astrbot.api.web import PluginRequest, bind_request_context
from starlette.requests import Request
from astrbot_plugin_good_life import main as life_entry
from astrbot_plugin_good_life.good_life.academics import attendance, grades
from astrbot_plugin_good_life.good_life.academics.models import zone
from astrbot_plugin_good_life.good_life.academics.service import timestamp
from astrbot_plugin_good_life.good_life.activity_import import stage_activities
from astrbot_plugin_good_life.good_life.city import amap
from astrbot_plugin_good_life.good_life.contracts.models import ActivityStatus, Money, Provenance
from astrbot_plugin_good_life.good_life.economy import Evidence
from astrbot_plugin_good_life.good_life.exposure_import import stage_exposures
from astrbot_plugin_good_life.good_life.graph_package_storage import GraphPackageStore
from astrbot_plugin_good_life.good_life.kernel.binding import OptionalKernel
from astrbot_plugin_good_life.good_life.metadata_import import stage_metadata
from astrbot_plugin_good_life.good_life.sleep_scheduler import DueSleepScheduler
from astrbot_plugin_good_life.good_life.storage import LifeStorage, StorageReader
from astrbot_plugin_good_life.good_life.travel import propose_walk
from astrbot_plugin_good_life.good_life.web_api import PREFIX
from astrbot_plugin_private_companion import main as host_entry
from companion_v2.execution.models import digest, encode, stamp
from companion_v2.foundation.clock import DeterministicClock
from companion_v2.integration.life_event_inbox import ACADEMIC_NOTICES, academic_notice_ids
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.role_work.models import RoleActivity, RoleActivityState, RoleSleepReceipt
from companion_v2.role_work.state.ownership import role_owner
from companion_v2.role_work.work_models import RoleWorkSpec
from tests.multi_host_support import MultiFixture, multi_config
from test_academics_domain import entry, term
from test_amap_navigation import response
from test_kernel_support import NOW
from test_shared_travel import SETTINGS, graph
from test_study import assignment, syllabus


START = NOW - timedelta(hours=9, minutes=25)


class W6Scenario(MultiFixture, IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.clock = DeterministicClock.at(START)
        self.life_root = self.root / "life"
        self.stars = []
        self.native.get_all_stars = lambda: self.stars
        self.enterContext(patch.object(StarTools, "get_data_dir", side_effect=lambda name:
                                       self.life_root if name == life_entry.PLUGIN_ID else self.root / "host"))
        controller_type = host_entry.AstrBotMultiController
        self.enterContext(patch.object(host_entry, "AstrBotMultiController", side_effect=lambda *args:
                                       controller_type(*args, native_types=self.types, now=self.clock.now)))
        self.enterContext(patch.object(host_entry, "retire_plugin_imports"))
        self.enterContext(patch.object(host_entry, "warm_session_settings", new_callable=AsyncMock))
        self.enterContext(patch("astrbot_plugin_private_companion.astrbot_host.local_config.LocalConfigServer.start",
                                new_callable=AsyncMock))
        self.enterContext(patch.object(DueSleepScheduler, "_run", new=asyncio.Event().wait))
        self.enterContext(patch.object(OptionalKernel, "_run", new=self.idle_kernel))
        runtime_type = life_entry.LifeRuntime
        self.enterContext(patch.object(life_entry, "LifeRuntime", side_effect=lambda *args, **kwargs:
            runtime_type(*args, **kwargs, wall_clock=self.clock.now,
                         monotonic_clock=lambda: (self.clock.now() - START).total_seconds())))

        self.host = host_entry.PrivateCompanionV2StagingPlugin(self.native, multi_config())
        self.addAsyncCleanup(self.host.terminate)
        self.controller = self.host.staging
        await self.host.initialize()
        self.app = self.controller.session.application
        self.assertEqual(self.app.state, "running")
        self.stars.append(SimpleNamespace(root_dir_name=host_entry.PLUGIN_NAME, star_cls=self.host, activated=True))
        self.api = self.host.extension_api
        self.plugin = None
        self.context = self.controller.authority.context("first", "role-a")
        self.role = digest("role-a")
        self.app.role_life.add_activity(RoleActivity("browse-fixture", "role-a", self.context.scope, START,
            RoleActivityState.COMPLETED, work=RoleWorkSpec("social", self.context.key, "browse-fixture")))
        self.app.role_social.exposure.save_batch(self.context, "browse-fixture", "local", [{"id": "article",
            "title": "Local article", "summary": "Fixture", "url": "https://example.invalid/article"}], START)
        db = self.app.runtime
        version = db.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        stage_activities(db.database_path, expected_version=version, destination=self.life_root)
        stage_exposures(db.database_path, expected_version=version, destination=self.life_root,
                        validate_source=lambda _connection: {"state": "matched"})
        stage_metadata(db.database_path, expected_version=version, destination=self.life_root)
        with db.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
        storage = LifeStorage.open(self.life_root)
        try:
            with storage.transaction() as connection:
                connection.execute("INSERT INTO life_metadata VALUES (?, 'ready')", (life_entry.READY_KEY,))
        finally:
            storage.close()
        self.addAsyncCleanup(self.stop_life)
        self.http = AsyncMock(side_effect=self.amap_response)
        self.enterContext(patch.object(amap, "_http", self.http))
        self.addCleanup(amap.UsageGuard._next.clear)
        self.settings = {}
        self.revision = 0

    async def idle_kernel(self, _lease):
        await asyncio.Event().wait()

    async def amap_response(self, params, timeout, *, endpoint=amap.ENDPOINT):
        if endpoint == amap.POI_ENDPOINT:
            return {"status": "1", "infocode": "10000", "pois": [{"id": "museum", "name": "Museum",
                    "citycode": "021", "location": "121.4500,31.2200"}]}
        return response()

    async def stop_life(self):
        if self.plugin is not None:
            await self.plugin.terminate()
            self.stars.pop()
            self.plugin = None

    async def restart(self, **settings):
        self.settings.update(settings)
        await self.stop_life()
        self.plugin = life_entry.GoodLifePlugin(self.native, self.settings)
        self.stars.append(SimpleNamespace(root_dir_name=life_entry.PLUGIN_ID, star_cls=self.plugin, activated=True))
        await self.plugin.initialize()
        for _ in range(20):
            if self.plugin.companion_v2_status()["runtime_ready"]:
                break
            await asyncio.sleep(0)
        self.assertTrue(self.plugin.companion_v2_status()["runtime_ready"], self.plugin._failure)
        self.assertTrue(self.api.life_provider_status()["ready"])
        self.runtime = self.plugin._runtime
        self.storage = self.plugin._storage
        self.kernel = self.runtime.kernel

    async def request(self, path, *, query="", body=None, role=None, username="admin", artifact_id=None):
        route = PREFIX + "/targets/<target>/<role>/" + path
        handler = next(handler for registered, handler, _, _ in self.native.registered_web_apis if registered == route)
        encoded = json.dumps(body).encode() if body is not None else b""

        async def receive():
            return {"type": "http.request", "body": encoded, "more_body": False}

        native = Request({"type": "http", "method": "POST" if body is not None else "GET", "path": route,
                          "headers": [(b"content-type", b"application/json")], "query_string": query.encode()}, receive)
        with bind_request_context(PluginRequest(native, username=username, plugin_name=life_entry.PLUGIN_ID)):
            params = {"artifact_id": artifact_id} if artifact_id is not None else {}
            return await handler(target="first", role=role or self.role, **params)

    def ok(self, result):
        self.assertEqual(result["status"], "ok", result)
        return result["data"]

    async def core(self, *, suffix):
        page = self.ok(await self.request("life"))
        self.assertIn("reading_plan", page)
        self.assertIn("body_sleep", page)
        self.assertNotIn("overview_activities", page)
        self.ok(await self.request("calendar/occurrences", query="start=2026-11-16&end=2026-11-17"))
        command = {"action": "configure", "operation_key": "core-" + suffix, "view_token": page["view_token"],
                   "edit_token": page["edit_token"], "changes": {"enabled": True, "sleep_enabled": False,
                                                                "reading_enabled": True}}
        self.ok(await self.request("life/command", body=command))
        page = self.ok(await self.request("life"))
        self.ok(await self.request("life/command", body={"action": "add_text", "operation_key": "book-" + suffix,
            "view_token": page["view_token"], "title": "Local book " + suffix, "text": "原文" * 1000}))
        article = self.app.role_social.exposure.exposure(self.context, "browse-fixture", "local", "article")
        self.assertEqual(article["title"], "Local article")
        page = self.ok(await self.request("life"))
        sleep = self.ok(await self.request("life/command", body={"action": "sleep", "minutes": 30,
            "operation_key": "sleep-" + suffix, "view_token": page["view_token"]}))
        self.clock.advance(timedelta(minutes=15))
        page = self.ok(await self.request("life"))
        activity = next(item for item in page["activities"] if item["activity_id"] == sleep["activity_id"])
        self.ok(await self.request("life/command", body={"action": "wake", "operation_key": "wake-" + suffix,
            "activity_id": activity["activity_id"], "expected_revision": activity["revision"],
            "view_token": activity["view_token"]}))

    def scope(self):
        scopes = self.kernel.access.scopes()
        self.assertTrue(scopes, self.runtime.optional_diagnostics)
        return next(scope for scope in scopes if scope.context_key == self.context.key)

    async def tick(self, at, *, deliver=True):
        self.clock.set(at)
        self.kernel.recover_pending(self.scope(), now=at)
        optional = self.runtime._optional
        for phase in ("settle", "commitments", "propose"):
            await optional._run_phase(self.runtime._lease, phase, at)
        self.assertEqual(self.runtime.advance_kernel(now=at)["failures"], ())
        if deliver:
            await optional._run_phase(self.runtime._lease, "deliver", at)
        self.assertFalse(optional.work_failures)

    async def write(self, kind, record, *, expected=None):
        identifier = record[{"term": "term_id", "timetable": "slot_id", "syllabus": "unit_id",
                             "assignment": "assignment_id"}[kind]]
        receipt = await self.request("academics/command", body={"command_id": "create-" + identifier,
            "expected_revision": self.revision if expected is None else expected, "action": "create", "kind": kind,
            "id": identifier, "record": record})
        if expected is None:
            self.revision = self.ok(receipt)["revision"]
        return receipt

    def wallet(self):
        return self.runtime._optional.groups["wallet"].value

    def tables(self, storage):
        return {row[0] for row in StorageReader(storage).all("SELECT name FROM sqlite_master WHERE type='table'", ())}

    async def test_grades_are_flag_gated_and_reach_private_role_context_once(self):
        await self.restart(grades_enabled=True)
        self.assertNotIn("life_term_grades", self.tables(self.storage))
        await self.restart(academics_enabled=True, grades_enabled=False)
        page = self.ok(await self.request("life"))
        self.ok(await self.request("life/command", body={"action": "configure", "operation_key": "grade-life",
            "view_token": page["view_token"], "edit_token": page["edit_token"],
            "changes": {"enabled": True, "sleep_enabled": False}}))
        school_term = term(academic_year="2026-2027 学年秋季学期")
        await self.write("term", school_term)
        await self.write("timetable", entry())
        owner = digest(role_owner(self.context))
        last = timestamp("2026-11-23T08:20:00Z")
        release = grades.release_time(owner, "fall", "ethics", last, zone(school_term["timezone"]))
        self.clock.set(release - timedelta(seconds=1))
        self.assertNotIn("life_term_grades", self.tables(self.storage))
        self.assertEqual((await self.request("academics/grades", query="term_id=fall"))["message"], "missing_dependency")

        await self.restart(grades_enabled=True)
        self.assertIsNone(self.runtime.kernel)
        self.assertEqual(self.ok(await self.request("academics/grades", query="term_id=fall"))["grades"], [])
        self.clock.set(release)
        await self.runtime.bind(self.api)
        self.assertFalse(self.runtime.optional_diagnostics)
        grade, = self.ok(await self.request("academics/grades", query="term_id=fall"))["grades"]
        event, = self.storage.pending_events(ACADEMIC_NOTICES)

        events = self.app.life_runtime.events
        await self.restart(grades_enabled=False)
        events._academic_notices_at = 0
        self.app.life_runtime.advance()
        self.assertEqual(academic_notice_ids(self.app.runtime, self.context, release), [])
        self.assertEqual(self.storage.pending_events(ACADEMIC_NOTICES), (event,))

        await self.restart(grades_enabled=True, kernel_enabled=True, class_enabled=True)
        await self.tick(release)
        with patch.object(self.storage, "acknowledge", side_effect=ValueError("interrupted before ack")):
            events._academic_notices_at = 0
            self.app.life_runtime.advance()
        self.assertEqual(self.storage.pending_events(ACADEMIC_NOTICES), (event,))
        self.assertEqual(academic_notice_ids(self.app.runtime, self.context, release), [event["event_id"]])
        self.assertEqual(events.active_handles, ())
        life_context = self.app.messages.life_context
        snapshot = life_context.capture(self.context, release)
        self.assertEqual(snapshot.get("academic_notices"), [event["event_id"]], snapshot)
        notice, = [item for item in life_context.contributions(snapshot, self.context, release)
                   if item.purpose == "academic_grade_notice"]
        self.assertEqual(json.loads(notice.payload), event["payload"])

        await self.restart()
        await self.tick(release + timedelta(minutes=2))
        events._academic_notices_at = 0
        self.app.life_runtime.advance()
        self.assertEqual(self.ok(await self.request("academics/grades", query="term_id=fall"))["grades"], [grade])
        self.assertEqual(self.storage.pending_events(ACADEMIC_NOTICES), ())
        self.assertEqual(academic_notice_ids(self.app.runtime, self.context, release), [event["event_id"]])

    async def test_monthly_wallet_allowance_pays_once_per_local_month_across_restarts(self):
        await self.restart(kernel_enabled=True, wallet_enabled=False, wallet_monthly_allowance=2000)
        page = self.ok(await self.request("life"))
        self.ok(await self.request("life/command", body={"action": "configure", "operation_key": "allowance-life",
            "view_token": page["view_token"], "edit_token": page["edit_token"],
            "changes": {"enabled": True, "timezone": "Asia/Shanghai"}}))
        at = timestamp("2026-11-20T04:00:00Z")
        await self.tick(at)
        self.assertNotIn("life_wallet_transactions", self.tables(self.storage))

        await self.restart(wallet_enabled=True, wallet_monthly_allowance=0)
        await self.tick(at)
        self.assertEqual(self.ok(await self.request("life/wallet"))["transactions"], [])

        await self.restart(wallet_monthly_allowance=2000)
        await self.tick(at)
        first = self.ok(await self.request("life/wallet"))
        self.assertEqual(first["balance_minor"], 200_000)
        self.assertEqual(len(first["transactions"]), 1)
        credit = first["transactions"][0]
        self.assertEqual((credit["kind"], credit["currency"], credit["activity_label"], credit["occurred_at"]),
                         ("income", "CNY", "每月生活费", at.isoformat()))
        self.assertEqual(self.wallet().transactions(self.scope().owner_key)[0].operation_id, "monthly-allowance:2026-11")

        later = at + timedelta(minutes=1)
        await self.tick(later)
        await self.restart()
        await self.tick(later)
        await self.restart(wallet_monthly_allowance=2500)
        await self.tick(later)
        self.assertEqual(self.ok(await self.request("life/wallet")), first)

        await self.tick(timestamp("2026-11-30T16:00:00Z"))
        next_month = self.ok(await self.request("life/wallet"))
        self.assertEqual(next_month["balance_minor"], 450_000)
        self.assertEqual([tx["amount_minor"] for tx in next_month["transactions"]], [250_000, 200_000])
        self.assertEqual([tx.operation_id for tx in self.wallet().transactions(self.scope().owner_key)],
                         ["monthly-allowance:2026-11", "monthly-allowance:2026-12"])

    async def test_monthly_wallet_allowance_waits_for_configured_local_day(self):
        await self.restart(kernel_enabled=True, wallet_enabled=True, wallet_monthly_allowance=2000,
                           wallet_allowance_day=20)
        page = self.ok(await self.request("life"))
        self.ok(await self.request("life/command", body={"action": "configure", "operation_key": "allowance-day",
            "view_token": page["view_token"], "edit_token": page["edit_token"],
            "changes": {"enabled": True, "timezone": "America/Los_Angeles"}}))
        await self.tick(timestamp("2026-11-20T07:59:00Z"))
        self.assertEqual(self.ok(await self.request("life/wallet"))["balance_minor"], 0)
        await self.tick(timestamp("2026-11-20T08:00:00Z"))
        self.assertEqual(self.ok(await self.request("life/wallet"))["balance_minor"], 200_000)

    async def test_monthly_allowance_funds_planned_expense_in_the_same_tick(self):
        await self.restart(kernel_enabled=True, wallet_enabled=True, wallet_monthly_allowance=100,
                           planned_spending_enabled=True)
        page = self.ok(await self.request("life"))
        self.ok(await self.request("life/command", body={"action": "configure", "operation_key": "spending-allowance-life",
            "view_token": page["view_token"], "edit_token": page["edit_token"],
            "changes": {"enabled": True, "timezone": "Asia/Shanghai"}}))
        midnight = timestamp("2026-11-30T16:00:00Z")
        before = midnight - timedelta(seconds=1)
        await self.tick(before)
        self.assertEqual(self.ok(await self.request("life/wallet"))["balance_minor"], 10_000)

        # Seed an acknowledged plan so this test can exercise the production spending tick directly.
        command = self.runtime.handlers["calendar_command"]
        request = {"command_id": "month-boundary-expense", "owner_key": self.scope().owner_key,
            "context_key": self.context.key, "kind": "calendar.batch", "expected_revision": 0,
            "payload": {"commands": [], "storage_keys": [], "spending": {"timezone": "Asia/Shanghai",
                "now": before.isoformat(), "end": (midnight + timedelta(hours=1)).isoformat(), "calendar_keys": [],
                "entries": [{"label": "日用品", "amount_minor": 15_000, "at": midnight.isoformat()}]}}}
        prepared = command("batch.prepare", request, prepared_at=before)
        decision = {"command_id": request["command_id"], "request_hash": prepared["request_hash"], "decision": "commit",
                    "decision_id": "month-boundary-expense-decision"}
        command("batch.resolve", decision, verify_host_decision=lambda _: True)
        command("batch.acknowledge", request["command_id"], verify_host_completion=lambda _: True)

        self.clock.set(midnight)
        await self.runtime.scheduler.tick(now=midnight)
        page = self.ok(await self.request("life/wallet"))
        self.assertEqual(page["balance_minor"], 5_000)
        self.assertCountEqual([(row["kind"], row["amount_minor"], row["activity_label"]) for row in page["transactions"]],
                              [("income", 10_000, "每月生活费"), ("income", 10_000, "每月生活费"),
                               ("spend", 15_000, "日用品")])

    async def test_management_wallet_cursor_paginates_after_flags_off_rollback(self):
        await self.restart(kernel_enabled=True, wallet_enabled=True)
        owner = digest(role_owner(self.context))
        for revision in range(21):
            key = f"income-{revision}"
            self.wallet().record_income(owner, key, Money(revision + 1, "CNY"), operation_id=key,
                expected_revision=revision, evidence=Evidence(key, START + timedelta(seconds=revision), Provenance.SIMULATED))
        await self.restart(kernel_enabled=False, wallet_enabled=False)

        first = self.ok(await self.request("life/management"))
        self.assertEqual(first["features"]["wallet"]["state"], "disabled")
        wallet = first["wallet"]
        self.assertEqual([item["amount_minor"] for item in wallet["transactions"]], list(range(21, 1, -1)))
        self.assertIsNotNone(wallet["next_before"])
        query = urlencode({"before": wallet["next_before"]})
        second = self.ok(await self.request("life/management", query=query))["wallet"]
        self.assertEqual([item["amount_minor"] for item in second["transactions"]], [1])
        self.assertIsNone(second["next_before"])

    async def test_release_and_flags_off_rollback(self):
        with patch.object(self.api, "life_async_capabilities", side_effect=RuntimeError("injected A3 failure")) as a3:
            await self.restart()
            await self.core(suffix="off")
            a3.assert_not_called()
            before = self.tables(self.storage)
            status = self.ok(await self.request("life/management"))
            self.assertEqual(status["activities"], {"items": [], "partial": False})
            self.assertIsNone(status["wallet"])
            self.assertTrue(all(item["state"] == "disabled" for item in status["features"].values()))
            self.assertEqual(self.tables(self.storage), before)
            self.assertFalse(any(name.startswith(("life_kernel_", "life_academic_", "life_study_", "life_wallet_",
                "life_city_", "life_shared_", "life_campus_", "life_class_", "life_state_model_", "life_reading_",
                "geo_graph_")) for name in before))
            # Enable only the async probe to demonstrate optional A3 failure isolation.
            await self.restart(reading_executor_enabled=True)
            a3.assert_called_once()
            self.assertEqual(self.runtime.optional_diagnostics["reading_execute"]["reason"], "optional_binding_failed")
            await self.core(suffix="a3")
            self.assertEqual(self.tables(self.storage), before)

        await self.restart(reading_executor_enabled=False, kernel_enabled=True)
        old_access = self.kernel.access
        old_scope = self.scope()
        self.assertIsNotNone(self.runtime._optional.worker)
        old_worker = self.runtime._optional.worker
        await self.restart(**SETTINGS, campus_enabled=True, home_place_id="home")
        self.assertFalse(old_access.authorize(old_scope, "kernel.propose"))
        self.assertTrue(old_worker.done())
        package = graph()
        GraphPackageStore(self.storage, enabled=True).confirm(package, expected_current_digest=None,
            expected_proposed_digest=package["digest"], confirmed=True)
        await self.restart(academics_enabled=True, class_enabled=True, attendance_enabled=True)
        await self.write("term", term())
        await self.write("timetable", entry(slot_id="early", start_time="08:10", end_time="08:20", place_id="far"))
        await self.write("timetable", entry(start_time="20:10", end_time="20:20", place_id="far"))
        self.assertEqual((await self.write("timetable", entry(slot_id="stale"), expected=0))["message"], "stale_version")
        await self.restart(study_enabled=True)
        self.assertIsNotNone(self.runtime.study_ports)
        await self.write("syllabus", syllabus())
        await self.write("assignment", assignment(chunk_work_s=60, estimated_work_s=660))
        await self.restart(wallet_enabled=True)
        owner = self.scope().owner_key
        self.wallet().record_income(owner, "initial", Money(100, "CNY"), operation_id="initial",
            expected_revision=0, evidence=Evidence("initial", START, Provenance.SIMULATED))
        self.http.assert_not_awaited()
        await self.restart(city_enabled=True, amap_navigation_enabled=True, amap_poi_search_enabled=True,
                           amap_navigation_key="fixture", amap_navigation_city_code="021")

        scope = self.scope()
        early = self.runtime.academics.next_sessions(owner, scope.context_key, now=START)[0]
        wake_at = timestamp(early["start_at"]) - timedelta(minutes=5)
        # The role wakes too late to finish preparing before this short class ends.
        night = RoleActivity("night-fixture", "role-a", self.context.scope, wake_at - timedelta(hours=8),
            RoleActivityState.COMPLETED, updated_at=wake_at,
            work=RoleWorkSpec("sleep", self.context.key, "night-fixture", due_at=wake_at),
            sleep=RoleSleepReceipt("night", 480, 480, slept_seconds=8 * 3600))
        with self.storage.transaction() as connection:
            connection.execute("INSERT INTO staged_role_activities VALUES (?,?,?,?,?,?,?)",
                (owner, night.activity_id, encode(night.owner), night.revision, night.state.value, stamp(wake_at), encode(night)))
        await self.tick(wake_at)
        wake_sql = "SELECT record_json FROM life_class_attendance_wakes WHERE owner_key=? AND session_id=?"
        wake_args = (owner, early["session_id"])
        wake = StorageReader(self.storage).one(wake_sql, wake_args)[0]
        self.assertEqual(timestamp(json.loads(wake)["ready_at"]), wake_at + timedelta(minutes=20))
        await self.restart()
        await self.tick(wake_at + timedelta(seconds=1))
        self.assertEqual(StorageReader(self.storage).one(wake_sql, wake_args)[0], wake)

        end = timestamp(early["end_at"])
        # Interrupt after persisting the catch-up work and before acknowledging its event.
        with patch.object(self.storage, "acknowledge", side_effect=RuntimeError("interrupted before event ACK")):
            await self.tick(end, deliver=False)
            await self.runtime._optional._run_phase(self.runtime._lease, "deliver", end)
            self.assertIn(("classes", "deliver"), self.runtime._optional.work_failures)
        self.assertTrue(self.storage.pending_events(attendance.STUDY_CONSUMER, context_key=self.context.key))
        followups = "SELECT record_json FROM life_study_work_followups WHERE owner_key=? AND kind='borrow_notes'"
        saved = [row[0] for row in StorageReader(self.storage).all(followups, (owner,))]
        self.assertEqual(len(saved), 1)
        await self.restart()
        await self.tick(end)
        self.assertEqual(self.storage.pending_events(attendance.STUDY_CONSUMER, context_key=self.context.key), ())
        self.assertEqual([row[0] for row in StorageReader(self.storage).all(followups, (owner,))], saved)
        management = self.ok(await self.request("life/management"))
        missed = next(item for item in management["activities"]["items"] if item.get("attendance"))
        self.assertEqual(missed["attendance"], {"outcome": "absent", "minutes_missed": 10})
        self.assertEqual(missed["outcome"]["reason_code"], "overslept")

        study_at = end + timedelta(minutes=20)
        await self.tick(study_at)
        travel = self.runtime._optional.groups["shared_travel"].value
        scope = self.scope()
        study = self.runtime.academics.study_artifacts
        window = self.kernel.access.snapshot(scope, study_at).free_until - study_at
        plan = study.plan(owner, scope.context_key, window_s=int(window.total_seconds()), travel_s=420)
        self.assertGreaterEqual(plan["allocated_s"], 60)
        walk = propose_walk(self.kernel, travel.campus, scope, "far", now=study_at, opportunity_key="walk-to-study")
        self.runtime._optional.groups["classes"].value._start_candidate(scope, walk, study_at)
        await self.tick(study_at + timedelta(seconds=420))

        async def arrived(_owner, _context, _cursor):
            return travel.position.origin(owner)[0] == "far"

        chunk = plan["cursor"]
        cursor = await study.advance(owner, scope.context_key, "essay", 0, expected_revision=chunk["revision"],
                                     now=self.clock.now(), walk=SimpleNamespace(arrived=arrived))
        self.assertEqual(cursor["step"], "work")
        self.clock.advance(timedelta(seconds=20))
        cursor = await study.advance(owner, scope.context_key, "essay", 0, expected_revision=cursor["revision"],
                                     now=self.clock.now(), work_s=20, allocated_s=60)
        draft, = self.ok(await self.request("academics/study_artifacts", query="assignment_id=essay"))["items"]
        self.assertEqual(draft["kind"], "draft")
        await self.restart()
        study = self.runtime.academics.study_artifacts
        resumed = study.cursor(owner, self.context.key, "essay", 0)
        self.assertEqual(resumed, {key: value for key, value in cursor.items() if key != "used_s"})
        for seconds in (40, 0):
            self.clock.advance(timedelta(seconds=seconds))
            cursor = await study.advance(owner, self.context.key, "essay", 0, expected_revision=cursor["revision"],
                                         now=self.clock.now(), work_s=seconds, allocated_s=60)
        self.assertEqual(cursor["step"], "done")
        artifacts = self.ok(await self.request("academics/study_artifacts", query="assignment_id=essay"))
        self.assertEqual({item["kind"] for item in artifacts["items"]}, {"draft", "note", "review"})
        self.assertIn(draft, artifacts["items"])
        artifact = artifacts["items"][0]["artifact_id"]
        for item in artifacts["items"]:
            detail = self.ok(await self.request("academics/study_artifacts/<artifact_id>", artifact_id=item["artifact_id"]))
            self.assertTrue(detail["text"])
        denied = await self.request("academics/study_artifacts/<artifact_id>", role=digest("role-b"),
                                    artifact_id=artifact)
        self.assertEqual(denied["status"], "error")
        self.assertIn(denied["message"], ("life_dashboard_invalid", "unauthorized"))
        self.assertEqual((await self.request("life/management", username="api_key:test"))["message"],
                         "dashboard_identity_required")

        trip = await self.start_trip("museum")
        departed = self.clock.now()
        await self.tick(departed + timedelta(seconds=self.first_fare_offset(trip) + 1))
        self.assertEqual(self.wallet().quote_available(owner).balance_minor, 94)
        self.assertIn("in_progress", [item["outcome"]["state"] for item in
                                     self.ok(await self.request("life/management"))["activities"]["items"]])
        requests = self.http.await_count
        await self.restart()
        await self.tick(self.clock.now())
        self.assertEqual(self.kernel.repo.instance(owner, trip.instance_id).checkpoint["params"], trip.checkpoint["params"])
        self.assertEqual(self.wallet().quote_available(owner).balance_minor, 94)
        current = self.kernel.repo.instance(owner, trip.instance_id)
        self.clock.advance(timedelta(seconds=current.planned_duration_s - current.elapsed_s))
        with patch.object(self.kernel, "commit_result", side_effect=RuntimeError("interrupted after staging")):
            with self.assertRaisesRegex(RuntimeError, "interrupted after staging"):
                self.kernel.advance(self.scope(), trip.instance_id, expected_revision=current.revision,
                                    command_id="finish-trip", now=self.clock.now())
        result_id, = self.kernel.repo.pending_results(owner, self.context.key)
        await self.restart()
        await self.tick(self.clock.now())
        settled = self.kernel.repo.instance(owner, trip.instance_id)
        self.assertEqual(settled.status, ActivityStatus.COMPLETED)
        self.assertEqual(self.kernel.commit_result(self.scope(), result_id, now=self.clock.now()), settled)
        self.assertFalse(self.kernel.repo.pending_results(owner, self.context.key))
        self.assertEqual([tx.amount_minor for tx in self.wallet().transactions(owner)], [100, 6, 6])
        self.assertEqual(self.http.await_count, requests)

        evening = NOW + timedelta(hours=4)
        await self.tick(evening - timedelta(minutes=5))
        await self.tick(evening + timedelta(minutes=5))
        await self.tick(evening + timedelta(minutes=10))
        class_end = evening + timedelta(minutes=20)
        await self.tick(class_end)
        on_time = next(item for item in self.ok(await self.request("life/management"))["activities"]["items"]
                         if item.get("attendance", {}).get("outcome") == "on_time")
        self.assertEqual(on_time["outcome"], {"state": "completed", "source": "simulated"})
        home_walk, = self.kernel.repo.active(owner)
        self.assertTrue(home_walk.template_id.startswith("campus_walk_"))
        await self.tick(class_end + timedelta(seconds=home_walk.planned_duration_s))
        travel = self.runtime._optional.groups["shared_travel"].value
        self.assertEqual(travel.position.origin(owner)[0], "home")

        rollback_trip = await self.start_trip("rollback")
        self.clock.advance(timedelta(seconds=self.first_fare_offset(rollback_trip) + 1))
        await self.tick(self.clock.now())
        self.assertEqual(self.wallet().quote_available(owner).balance_minor, 82)
        requests = self.http.await_count
        await self.restart(**dict.fromkeys(("kernel_enabled", "campus_enabled", "academics_enabled", "class_enabled",
            "attendance_enabled", "study_enabled", "grades_enabled", "wallet_enabled", "city_enabled", "amap_navigation_enabled",
            "amap_poi_search_enabled"), False))
        await self.tick(self.clock.now() + timedelta(seconds=1))
        self.assertFalse(self.kernel.repo.foreground_busy(owner))
        self.assertEqual(self.kernel.repo.instance(owner, rollback_trip.instance_id).status, ActivityStatus.CANCELLED)
        self.assertEqual(self.http.await_count, requests)
        rolled_back = self.ok(await self.request("life/management"))
        self.assertTrue(all(item["state"] == "disabled" for item in rolled_back["features"].values()))
        self.assertEqual((rolled_back["wallet"]["balance_minor"], rolled_back["wallet"]["reserved_minor"]), (82, 0))
        self.assertIsNotNone(self.runtime.study_ports)
        self.assertEqual([tx.amount_minor for tx in self.wallet().transactions(owner)], [100, 6, 6, 6])
        self.assertEqual(self.ok(await self.request("life/wallet")), {"enabled": False})
        self.assertEqual(self.ok(await self.request("academics/study_artifacts", query="assignment_id=essay")), artifacts)
        self.assertTrue(self.ok(await self.request("academics/study_artifacts/<artifact_id>", query="", role=self.role,
                                                   artifact_id=artifact))["text"])
        self.assertIn("cancelled", [item["outcome"]["state"] for item in rolled_back["activities"]["items"]])
        await self.core(suffix="rollback")
        self.clock.advance(timedelta(days=2))
        history = self.ok(await self.request("life/management"))["activities"]["items"]
        historic_ids = {item["activity_id"] for item in history}
        self.assertTrue({"kernel:" + trip.instance_id, "kernel:" + rollback_trip.instance_id,
                         missed["activity_id"]} <= historic_ids)

    @staticmethod
    def first_fare_offset(trip):
        offset = 0
        for segment in trip.checkpoint["params"]["outbound"]["segments"]:
            if segment["cost_minor"]:
                return offset
            offset += segment["duration_s"]
        raise AssertionError("synthetic Amap route has no fare")

    async def start_trip(self, key):
        scope = self.scope()
        result = await self.runtime.search_city_pois(scope, "Museum", opportunity_key=key)
        self.assertEqual(result["items"], ("Museum",))
        trip = await self.runtime.start_city_trip(scope, opportunity_key=key, index=0)
        self.assertTrue(trip.gate.allowed, trip.gate)
        self.assertEqual(trip.instance.status, ActivityStatus.RUNNING)
        return trip.instance
