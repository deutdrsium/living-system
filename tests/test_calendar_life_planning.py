import asyncio
from contextlib import closing
from datetime import timedelta
import json
import sqlite3
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life"), str(ROOT / "tools")]
from astrbot_host.integration_registry import UnifiedCompanionExtensionAPI
from astrbot_host.life_provider_registry import API_VERSION, PROVIDER_ID
from audit_calendar_ownership import adoption_plan
from companion_v2.agenda.calendar import LIFE_CALENDAR_OWNER_KEY
from companion_v2.agenda.planning import planning_profile
from companion_v2.agenda.planning_output import INSTRUCTION, MODE_INSTRUCTIONS
from companion_v2.execution.models import digest
from companion_v2.role_work.state.management import StateManagement
from companion_v2.role_work.state.ownership import role_owner
from companion_v2.configuration.registry import FieldRegistry
from companion_v2.integration.calendar_adoption_guard import CERTIFICATE_KEY
from companion_v2.integration.calendar_batch_handoff import CalendarBatchHandoff
from companion_v2.integration.life_ledger_router import LIFE_ACTIVITY_OWNER_KEY
from companion_v2.integration import manual_calendar_queue
from companion_v2.identity_policy.interventions import CALENDAR_INTERVENTION
from companion_v2.role_work.state.service import LIFE_STATE_OWNER_KEY
from companion_v2.model_gateway.routing import configuration_fields
from good_life.academics import Academics, AcademicsConfig
from good_life.activity_import import stage_activities
from good_life.calendar_adoption import adopt_staged_calendar
from good_life.calendar_import import stage_calendar
from good_life.exposure_import import stage_exposures
from good_life.metadata_import import stage_metadata
from good_life.contracts.models import Money, Provenance
from good_life.economy import Evidence, Wallet
from good_life.economy.access import wallet_page
from good_life.economy.planned_spending import PlannedSpending
from good_life.prepared_commands import PreparedCommands
from good_life.runtime import LifeRuntime
from good_life.sleep_scheduler import DueSleepScheduler
from good_life.state_import import stage_state
from good_life.storage import LifeStorage
from rehearse_calendar_rollback import rehearse as rehearse_calendar_rollback
from rehearse_life_rollback import rehearse as rehearse_life_rollback
from tests.test_management_access import NOW
from tests.test_role_state import StateFixture, state_data
from tests.test_role_tool_loop import answered
from verify_calendar_adoption import verify
from verify_exposure_authority import verify_contexts


class CalendarLifePlanningTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (planning_profile("primary", max_output_tokens=100),)
        self.registry = FieldRegistry(version="life-planning-tests", fields=configuration_fields(self.profiles))

    async def setup_life_planning(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        host, app, context, manager = self.setup_state()
        self.configure(manager, enabled=True, calendar_enabled=True, planning_enabled=True)
        app.calendar_planning.next_checks[context.key] = NOW + timedelta(days=1)
        await self.start(host)
        instruction = "test persona"
        app.state_maintenance.bind(context, instruction)
        app.life_runtime.bind(context, instruction)
        version = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key='schema_version'", ())["value"]
        plan = adoption_plan(self.runtime.database_path, expected_version=version)
        directory = Path(temporary.name) / "life"
        stage_calendar(self.runtime.database_path, expected_version=version, destination=directory)
        life = LifeStorage.open(directory)
        self.addCleanup(life.close)
        adopt_staged_calendar(life, plan)
        stage_activities(self.runtime.database_path, expected_version=version, destination=directory)
        stage_state(self.runtime.database_path, expected_version=version, destination=directory)
        stage_metadata(self.runtime.database_path, expected_version=version, destination=directory)
        stage_exposures(self.runtime.database_path, expected_version=version, destination=directory, validate_source=verify_contexts)
        certificate = verify(self.runtime.database_path, directory / "good-life.sqlite3", expected_version=version)["certificate"]
        plugin = object()
        star = SimpleNamespace(root_dir_name=PROVIDER_ID, star_cls=plugin, activated=True)
        controller = SimpleNamespace(context=SimpleNamespace(get_all_stars=lambda: [star]), _terminated=False,
                                     session=SimpleNamespace(application=app))
        api = UnifiedCompanionExtensionAPI(controller)
        self.addCleanup(api.close_life_providers)
        self.addCleanup(api.close_memory_providers)
        with self.runtime.transaction() as db:
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_CALENDAR_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_ACTIVITY_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, 'good_life')", (LIFE_STATE_OWNER_KEY,))
            db.execute("INSERT INTO runtime_metadata VALUES (?, ?)", (CERTIFICATE_KEY, json.dumps(certificate)))
        return app, context, directory, life, plugin, api, version

    async def test_supervised_planning_recovers_one_atomic_life_batch(self):
        app, context, directory, life, plugin, api, version = await self.setup_life_planning()
        provider = LifeRuntime(life, plugin)
        provider._wallet = Wallet("CNY")
        provider._wallet.install(life)
        original = provider.handlers["calendar_command"]
        interrupted = [True]
        def calendar_command(operation, *args, **kwargs):
            result = original(operation, *args, **kwargs)
            if operation == "batch.resolve" and interrupted[0]:
                interrupted[0] = False
                raise ValueError("simulated interruption after batch commit")
            return result
        provider.handlers["calendar_command"] = calendar_command
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"items": [
            {"title": "First plan", "start": (NOW + timedelta(hours=2)).isoformat(), "minutes": 30, "detail": "First"},
            {"title": "Second plan", "start": (NOW + timedelta(hours=4)).isoformat(), "minutes": 30, "detail": "Second"}],
            "spending": [{"label": "No charge", "amount": 10, "at": (NOW + timedelta(hours=3)).isoformat()}]})))
        capabilities = api.life_host_capabilities(plugin=plugin, registration_id=token)
        capabilities.pop("state_run")  # State writes below are explicit in this combined rollback test.
        scheduler = DueSleepScheduler(life, capabilities)
        self.addAsyncCleanup(scheduler.close)
        await scheduler.tick(now=self.clock.now())
        worker = scheduler.calendar_scheduler.worker
        self.assertIsNotNone(worker, scheduler.last_failure)
        report = await worker
        self.assertNotEqual(report["state"], "succeeded")
        pending = next(item for item in PreparedCommands(life).pending() if item["kind"] == "calendar.batch")
        self.assertEqual(pending["state"], "committed")
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        saved = app.calendar.inventory(context, limit=100)["items"]
        self.assertEqual({item["title"] for item in saved}, {"First plan", "Second plan"})
        operation = self.runtime.read_one("SELECT state FROM operations WHERE operation_id=?", (report["task_id"],))
        self.assertEqual(operation["state"], "completed")
        self.assertEqual(len(self.called), 1)
        prompt = self.called[0].original
        self.assertEqual(prompt.instruction, INSTRUCTION + "\n" + MODE_INSTRUCTIONS["daily"])
        self.assertNotIn("wallet", json.loads(prompt.context.materials[0].payload))
        self.assertIsNone(life._connection.execute("SELECT 1 FROM sqlite_master WHERE name='life_planned_spending'").fetchone())
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"items": [
            {"title": "Manual plan", "start": (NOW + timedelta(hours=6)).isoformat(), "minutes": 30, "detail": "Manual"}]})))
        operation_id = manual_calendar_queue.enqueue(app, context, mode="daily", slot="manual:daily:synthetic", refresh=False)
        await scheduler.tick(now=self.clock.now())
        manual_worker = scheduler.calendar_scheduler.worker
        self.assertIsNotNone(manual_worker)
        self.assertEqual((await manual_worker)["state"], "succeeded")
        self.assertEqual((await manual_calendar_queue.wait(app, operation_id, timeout=1))["state"], "succeeded")
        self.assertEqual(manual_calendar_queue.enqueue(app, context, mode="daily", slot="manual:daily:synthetic", refresh=False), operation_id)
        self.assertEqual(len(self.called), 2)
        self.grant(context.actor, context.scope, CALENDAR_INTERVENTION)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        stale = manual_calendar_queue.enqueue(app, context, mode="intervention", slot="intervention:stale",
                                              refresh=False, expected_input_epoch=epoch)
        app.state_maintenance.note_input(context, "new-message")
        await scheduler.tick(now=self.clock.now())
        self.assertEqual((await scheduler.calendar_scheduler.worker)["code"], "planning_inputs_changed")
        self.assertEqual((await manual_calendar_queue.wait(app, stale, timeout=1))["state"], "failed")
        self.assertEqual(len(self.called), 2)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        restarted = manual_calendar_queue.enqueue(app, context, mode="intervention", slot="intervention:restart",
                                                  refresh=False, expected_input_epoch=epoch)
        app.state_maintenance._intervention_generation = "restarted-state-maintenance"
        await scheduler.tick(now=self.clock.now())
        self.assertEqual((await scheduler.calendar_scheduler.worker)["code"], "planning_inputs_changed")
        self.assertEqual((await manual_calendar_queue.wait(app, restarted, timeout=1))["state"], "failed")
        self.assertEqual(len(self.called), 2)
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered('{"changes":[]}'))
        valid = manual_calendar_queue.enqueue(app, context, mode="intervention", slot="intervention:current",
                                              refresh=False, expected_input_epoch=epoch)
        await scheduler.tick(now=self.clock.now())
        self.assertEqual((await scheduler.calendar_scheduler.worker)["state"], "succeeded")
        self.assertEqual((await manual_calendar_queue.wait(app, valid, timeout=1))["state"], "succeeded")
        self.assertEqual(len(self.called), 3)
        target = directory.parent / "calendar-rollback.sqlite3"
        restored = rehearse_calendar_rollback(self.runtime.database_path, directory / "good-life.sqlite3",
                                              expected_version=version, destination=target)
        self.assertEqual((restored["state"], restored["records"]), ("rehearsed", 3))
        with closing(sqlite3.connect(target)) as db:
            titles = {json.loads(row[0])["title"] for row in db.execute("SELECT record_json FROM calendar_records")}
        self.assertEqual(titles, {"First plan", "Second plan", "Manual plan"})
        await scheduler.close()
        app.role_state.write(context, data=state_data(energy=57, hunger="正常"), expected_revision=0,
                             operation_key="combined-rollback-state")
        host_snapshot = directory.parent / "host-snapshot.sqlite3"
        life_snapshot = directory.parent / "life-snapshot.sqlite3"
        with closing(sqlite3.connect(self.runtime.database_path)) as source, closing(sqlite3.connect(host_snapshot)) as backup:
            source.backup(backup)
        with life._lock, closing(sqlite3.connect(life_snapshot)) as backup:
            life._connection.backup(backup)
        combined = directory.parent / "combined-rollback.sqlite3"
        restored = rehearse_life_rollback(host_snapshot, life_snapshot, expected_version=version, destination=combined)
        self.assertEqual((restored["state"], restored["ownership_changed"]), ("rehearsed", False))
        with closing(sqlite3.connect(combined)) as db:
            titles = {json.loads(row[0])["title"] for row in db.execute("SELECT record_json FROM calendar_records")}
            state = json.loads(db.execute("SELECT record_json FROM role_state_records WHERE record_id='current'").fetchone()[0])
        self.assertEqual(titles, {"First plan", "Second plan", "Manual plan"})
        self.assertEqual(state["values"]["energy"], 57)
        self.assertEqual(len(self.sent), 0)


    async def test_planned_spending_through_life_calendar_and_wallet(self):
        app, context, directory, life, plugin, api, _version = await self.setup_life_planning()
        self.grant(context.actor, context.scope, "role.life.read")
        with self.assertRaisesRegex(ValueError, "planned_spending_entry_cap <= planned_spending_daily_cap"):
            PlannedSpending(life, enabled=True, entry_cap=2, daily_cap=1, wallet=lambda: None)
        provider = LifeRuntime(life, plugin, planned_spending_config={"enabled": True})
        owner = digest(role_owner(context))
        wallet = provider._wallet = Wallet("CNY")
        wallet.install(life)
        wallet.record_income(owner, "test-income", Money(2000, "CNY"), operation_id="test-income", expected_revision=0,
                             evidence=Evidence("test-income", NOW, Provenance.SIMULATED))
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        capabilities = api.life_host_capabilities(plugin=plugin, registration_id=token)
        capabilities.pop("state_run")
        scheduler = DueSleepScheduler(life, capabilities, spending_tick=provider.planned_spending.tick)
        self.addAsyncCleanup(scheduler.close)

        async def plan(slot, items, spending, *, refresh=False):
            self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"items": items, "spending": spending})))
            manual_calendar_queue.enqueue(app, context, mode="daily", slot=slot, refresh=refresh)
            await scheduler.tick(now=self.clock.now())
            report = await scheduler.calendar_scheduler.worker
            self.assertEqual(report["state"], "succeeded", report)
            prompt = self.called[-1].original
            self.assertIn("可选的 spending 数组", prompt.instruction)
            return json.loads(prompt.context.materials[0].payload)["wallet"]

        def event(title, hours):
            return {"title": title, "start": (NOW + timedelta(hours=hours)).isoformat(), "minutes": 30}

        def cost(label, amount, hours, **link):
            return {"label": label, "amount": amount, "at": (NOW + timedelta(hours=hours)).isoformat(), **link}

        summary = await plan("spending-first", [event("Lunch", 2), event("Trip", 3)], [
            cost("校园午饭", 15, 2.25, item=0), cost("车费", 20, 3, item=1),
            cost("饮料", 8, 4), cost("大额日用品", 500, 5), cost("茶", 3, 5.5), cost("超过日上限", 500, 6)])
        self.assertEqual((summary["available_minor"], summary["month_income_minor"], summary["today"]), (2000, 2000, []))
        summary = await plan("spending-rerun", [], [cost("校园 午饭", 17, 2.5), cost("饮料", 8, 4)], refresh=True)
        self.assertEqual({row["label"] for row in summary["today"]}, {"校园午饭", "车费", "饮料", "大额日用品", "茶"})
        self.assertEqual((summary["today_planned_minor"], summary["today_pending_minor"]), (54600, 54600))
        with app.role_life.consistent_snapshot():
            plans = {item["title"]: item for item in app.calendar.inventory(context, limit=100)["items"]}
        for title, action, data in (("Lunch", "exception", {"sequence": 0, "start": (NOW + timedelta(hours=3)).isoformat()}),
                                    ("Trip", "cancel", None)):
            entry = plans[title]
            app.calendar.command(context, identifier=entry["id"], action=action, expected_revision=entry["revision"],
                                 operation_key="test-" + title, data=data)

        async def tick(hours):
            await scheduler.tick(now=NOW + timedelta(hours=hours))

        await tick(3)
        self.assertEqual(len(wallet.transactions(owner)), 1)
        await tick(3.25)
        self.assertEqual(wallet.quote_available(owner).balance_minor, 500)
        await tick(4)
        await tick(4)
        page = wallet_page(wallet, owner_key=owner, context_key=context.key)
        self.assertEqual([(row["kind"], row["activity_label"]) for row in page["transactions"]],
                         [("spend", "校园午饭"), ("income", None)])
        self.assertEqual(wallet.quote_available(owner).balance_minor, 500)
        with app.role_life.consistent_snapshot():
            conversation = app.messages.role_state_context
            at = NOW + timedelta(hours=4)
            snapshot = conversation.capture(context, at)
            material = next(item for item in conversation.contributions(snapshot, context, at) if item.purpose == "recent_life")
        recent = json.loads(material.payload)["simulated_wallet_spending"]
        self.assertEqual([(row["label"], row["status"]) for row in recent], [("饮料", "refused"), ("校园午饭", "charged")])
        self.assertEqual(recent[0]["reason"], "insufficient_funds")

        await scheduler.close()
        life.close()
        reopened = LifeStorage.open(directory)
        self.addCleanup(reopened.close)
        wallet = Wallet("CNY")
        wallet.install(reopened)
        disabled = PlannedSpending(reopened, wallet=lambda: wallet)
        disabled.tick(NOW + timedelta(hours=5))
        self.assertEqual(reopened._connection.execute("SELECT status FROM life_planned_spending WHERE label=?",
                                                     ("大额日用品",)).fetchone()[0], "pending")
        restarted = PlannedSpending(reopened, enabled=True, wallet=lambda: wallet)
        at = NOW + timedelta(hours=5.5)
        restarted.tick(at)
        restarted.tick(at)
        self.assertEqual(len(wallet.transactions(owner)), 3)
        summary = restarted.read(owner_key=owner, context_key=context.key, at=at, timezone_name="Asia/Shanghai")
        self.assertEqual((summary["available_minor"], summary["month_spend_minor"]), (200, 1800))
        self.assertEqual({item["label"]: item["status"] for item in summary["today"]},
                         {"校园午饭": "charged", "饮料": "refused", "大额日用品": "refused", "茶": "charged"})


    def spending_wallet(self, context, life, plugin, api, **caps):
        provider = LifeRuntime(life, plugin, planned_spending_config={"enabled": True, **caps})
        wallet = provider._wallet = Wallet("CNY")
        wallet.install(life)
        owner = digest(role_owner(context))
        wallet.record_income(owner, "test-income", Money(100000, "CNY"), operation_id="test-income", expected_revision=0,
                             evidence=Evidence("test-income", NOW, Provenance.SIMULATED))
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        capabilities = api.life_host_capabilities(plugin=plugin, registration_id=token)
        capabilities.pop("state_run")
        scheduler = DueSleepScheduler(life, capabilities)
        self.addAsyncCleanup(scheduler.close)
        return provider, owner, scheduler, token

    async def plan_expenses(self, app, context, scheduler, slot, *entries):
        items, spending = [], []
        for index, (title, label, amount, hours) in enumerate(entries):
            start = (NOW + timedelta(hours=hours)).isoformat()
            items.append({"title": title, "start": start, "minutes": 30})
            spending.append({"label": label, "amount": amount, "at": start, "item": index})
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"items": items, "spending": spending})))
        manual_calendar_queue.enqueue(app, context, mode="daily", slot=slot, refresh=False)
        await scheduler.tick(now=self.clock.now())
        report = await scheduler.calendar_scheduler.worker
        self.assertEqual(report["state"], "succeeded", report)
        return {plan["title"]: plan for plan in app.calendar.inventory(context, limit=100)["items"]}

    def change_expense_plan(self, app, context, plan, *, move_to=None):
        return app.calendar.command(context, identifier=plan["id"], expected_revision=plan["revision"],
            operation_key="change-" + plan["id"], action="cancel" if move_to is None else "exception",
            data=None if move_to is None else {"sequence": 0, "start": move_to.isoformat()})

    async def test_overdue_pending_expenses_follow_calendar_changes_but_charged_expenses_do_not(self):
        app, context, _, life, plugin, api, _ = await self.setup_life_planning()
        provider, owner, scheduler, _ = self.spending_wallet(context, life, plugin, api)
        plans = await self.plan_expenses(app, context, scheduler, "overdue-expenses",
            ("Already charged", "Breakfast", 10, 1), ("Cancel pending", "Lunch", 20, 2),
            ("Move pending", "Dinner", 30, 3))
        provider.planned_spending.tick(NOW + timedelta(hours=1))
        charged = life._connection.execute("SELECT * FROM life_planned_spending WHERE label='Breakfast'").fetchone()
        self.clock.advance(timedelta(hours=4))
        self.change_expense_plan(app, context, plans["Already charged"], move_to=NOW + timedelta(hours=7))
        self.change_expense_plan(app, context, plans["Cancel pending"])
        self.change_expense_plan(app, context, plans["Move pending"], move_to=NOW + timedelta(hours=6))
        provider.planned_spending.tick(self.clock.now())
        self.assertEqual(provider._wallet.quote_available(owner).balance_minor, 99000)
        self.assertEqual(life._connection.execute("SELECT * FROM life_planned_spending WHERE label='Breakfast'").fetchone(), charged)
        self.assertEqual(life._connection.execute("SELECT status FROM life_planned_spending WHERE label='Lunch'").fetchone()[0],
                         "cancelled")
        provider.planned_spending.tick(NOW + timedelta(hours=6))
        dinner = life._connection.execute("SELECT status, at FROM life_planned_spending WHERE label='Dinner'").fetchone()
        self.assertEqual((dinner["status"], dinner["at"]), ("charged", (NOW + timedelta(hours=6)).isoformat(timespec="microseconds")))
        self.assertEqual(provider._wallet.quote_available(owner).balance_minor, 96000)

    async def test_move_into_inclusive_duplicate_window_only_charges_original_expense(self):
        app, context, _, life, plugin, api, _ = await self.setup_life_planning()
        provider, owner, scheduler, _ = self.spending_wallet(context, life, plugin, api)
        plans = await self.plan_expenses(app, context, scheduler, "duplicate-expenses",
            ("Keep coffee", "Coffee", 10, 2), ("Move coffee", " cOf Fee ", 20, 5))
        self.change_expense_plan(app, context, plans["Move coffee"], move_to=NOW + timedelta(hours=3))
        provider.planned_spending.tick(NOW + timedelta(hours=6))
        self.assertEqual(provider._wallet.quote_available(owner).balance_minor, 99000)
        rows = life._connection.execute("SELECT label, status, reason FROM life_planned_spending ORDER BY at").fetchall()
        self.assertEqual([tuple(row) for row in rows], [("Coffee", "charged", None), ("cOf Fee", "cancelled", "duplicate")])

    async def test_moved_expense_waits_for_outer_batch_acknowledgment(self):
        app, context, _, life, plugin, api, _ = await self.setup_life_planning()
        provider, owner, scheduler, _ = self.spending_wallet(context, life, plugin, api)
        plans = await self.plan_expenses(app, context, scheduler, "expense-before-move", ("Lunch", "Lunch", 20, 4))
        self.grant(context.actor, context.scope, CALENDAR_INTERVENTION)
        plan = plans["Lunch"]
        moved_at = NOW + timedelta(hours=2)
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"changes": [{"action": "move",
            "id": plan["id"], "revision": plan["revision"], "sequence": 0, "start": moved_at.isoformat()}]})))
        epoch = app.state_maintenance._input_epochs.get(context.key, 0)
        manual_calendar_queue.enqueue(app, context, mode="intervention", slot="move-expense", refresh=False,
                                      expected_input_epoch=epoch)
        with patch.object(CalendarBatchHandoff, "acknowledge", side_effect=ValueError("interruption before acknowledgment")):
            await scheduler.tick(now=self.clock.now())
            report = await scheduler.calendar_scheduler.worker
        self.assertNotEqual(report["state"], "succeeded")
        pending, = PreparedCommands(life).pending()
        self.assertEqual((pending["kind"], pending["state"]), ("calendar.batch", "committed"))
        expense = life._connection.execute("SELECT at FROM life_planned_spending").fetchone()
        self.assertEqual(expense["at"], moved_at.isoformat(timespec="microseconds"))
        provider.planned_spending.tick(moved_at)
        self.assertEqual(provider._wallet.quote_available(owner).balance_minor, 100000)
        self.clock.advance(timedelta(hours=2))
        self.assertEqual((await scheduler.tick(now=self.clock.now()))["recovered"], 1)
        provider.planned_spending.tick(moved_at)
        provider.planned_spending.tick(moved_at)
        self.assertEqual(provider._wallet.quote_available(owner).balance_minor, 98000)
        self.assertEqual(len(provider._wallet.transactions(owner)), 2)

    async def test_timezone_change_regroups_existing_expenses_and_applies_destination_cap(self):
        app, context, _, life, plugin, api, _ = await self.setup_life_planning()
        provider, owner, scheduler, _ = self.spending_wallet(context, life, plugin, api, entry_cap=100, daily_cap=100)
        plans = await self.plan_expenses(app, context, scheduler, "expenses-before-zone-change",
            ("Move later", "Transport", 60, 2), ("Already planned", "Lunch", 40, 7))
        before = life._connection.execute("SELECT at FROM life_planned_spending ORDER BY at").fetchall()
        self.configure(StateManagement(app, context), planning_timezone="America/New_York")
        self.assertEqual(life._connection.execute("SELECT at FROM life_planned_spending ORDER BY at").fetchall(), before)
        self.clock.advance(timedelta(hours=5))
        await self.plan_expenses(app, context, scheduler, "expenses-after-zone-change", ("Add dinner", "Dinner", 55, 10))
        material = json.loads(self.called[-1].original.context.materials[0].payload)
        self.assertEqual([row["label"] for row in material["wallet"]["today"]], ["Lunch"])
        self.assertEqual(material["wallet"]["today_pending_minor"], 4000)
        moved_at = NOW + timedelta(hours=17)
        self.change_expense_plan(app, context, plans["Move later"], move_to=moved_at)
        expense = life._connection.execute("SELECT status, reason, at FROM life_planned_spending WHERE label='Transport'").fetchone()
        self.assertEqual(tuple(expense), ("refused", "daily_cap", moved_at.isoformat(timespec="microseconds")))
        summary = provider.planned_spending.read(owner_key=owner, context_key=context.key, at=self.clock.now(),
                                                  timezone_name="America/New_York")
        self.assertEqual(summary["today_pending_minor"], 9500)

    async def test_disabled_spending_ignores_invalid_caps_and_reconciles_historical_expenses(self):
        app, context, _, life, plugin, api, _ = await self.setup_life_planning()
        provider, owner, scheduler, token = self.spending_wallet(context, life, plugin, api)
        plans = await self.plan_expenses(app, context, scheduler, "expense-before-disable", ("Lunch", "Lunch", 50, 2))
        await scheduler.close()
        api.unregister_life_provider(token)
        disabled = LifeRuntime(life, plugin, planned_spending_config={"enabled": False, "entry_cap": None, "daily_cap": 1})
        disabled._wallet = provider._wallet
        api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=disabled.handlers)
        moved_at = NOW + timedelta(hours=4)
        self.change_expense_plan(app, context, plans["Lunch"], move_to=moved_at)
        disabled.planned_spending.tick(moved_at)
        expense = life._connection.execute("SELECT status, at FROM life_planned_spending").fetchone()
        self.assertEqual(tuple(expense), ("pending", moved_at.isoformat(timespec="microseconds")))
        self.assertEqual(disabled._wallet.quote_available(owner).balance_minor, 100000)

    def class_provider(self, context, life, plugin, api, *, academics=True):
        provider = LifeRuntime(life, plugin)
        if academics:
            provider.academics = Academics(config=AcademicsConfig(enabled=True))
            provider.academics.install(life)
            owner = digest(role_owner(context))

            def session(session_id, course, hours, *, owner_key=owner, cancelled=False):
                start = NOW + timedelta(hours=hours)
                record = {"session_id": session_id, "slot_id": "slot", "term_id": "2026-fall", "course_id": "c-" + session_id,
                          "course_name": course, "start_at": start.isoformat().replace("+00:00", "Z"),
                          "end_at": (start + timedelta(minutes=95)).isoformat().replace("+00:00", "Z"),
                          "local_date": start.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat(),
                          "place_id": "place-x", "revision": 1, "cancelled": cancelled, "source": "manual"}
                life._connection.execute("INSERT INTO life_academic_sessions VALUES (?,?,?,?)",
                                         (owner_key, session_id, "2026-fall", json.dumps(record)))

            with life.transaction():
                life._connection.execute("INSERT INTO life_academic_owners VALUES (?,?,1)", (owner, context.key))
                life._connection.execute("INSERT INTO life_academic_owners VALUES ('other-role','other-context',1)")
                life._connection.execute("INSERT INTO life_academic_records VALUES (?,?,?,?,?,?)", (owner, "term", "2026-fall", 1, 0,
                    json.dumps({"term_id": "2026-fall", "timezone": "Asia/Shanghai"})))
                session("today", "马克思主义哲学导论", 3)
                session("next-week", "线性代数", 24 * 7 + 2)
                session("next-week-off", "大学英语", 24 * 8 + 2, cancelled=True)
                session("far", "体育", 24 * 20)
                session("foreign", "别人的课", 4, owner_key="other-role")
        token = api.register_life_provider(plugin=plugin, api_version=API_VERSION, handlers=provider.handlers)
        capabilities = api.life_host_capabilities(plugin=plugin, registration_id=token)
        capabilities.pop("state_run")
        scheduler = DueSleepScheduler(life, capabilities)
        self.addAsyncCleanup(scheduler.close)
        return scheduler

    async def plan_with_classes(self, app, context, scheduler, slot):
        self.model_behavior = lambda _env: asyncio.sleep(0, result=answered(json.dumps({"items": []})))
        manual_calendar_queue.enqueue(app, context, mode="daily", slot=slot, refresh=False)
        await scheduler.tick(now=self.clock.now())
        report = await scheduler.calendar_scheduler.worker
        self.assertEqual(report["state"], "succeeded", report)
        prompt = self.called[-1].original
        return prompt, json.loads(prompt.context.materials[0].payload)

    async def test_calendar_query_and_planner_see_own_class_sessions(self):
        app, context, _, life, plugin, api, _ = await self.setup_life_planning()
        scheduler = self.class_provider(context, life, plugin, api)
        with app.role_life.consistent_snapshot():
            payload = app.tools.calendar.query(context, {})
        self.assertEqual([(item["course"], item["cancelled"]) for item in payload["classes"]],
                         [("马克思主义哲学导论", False), ("线性代数", False), ("大学英语", True)])
        week = payload["classes"][1]
        self.assertEqual((week["date"], week["start"], week["place"]),
                         ("2026-09-28", "2026-09-28T10:00:00+08:00", None))
        self.assertIn("classes", app.tools.calendar.instruction)
        self.assertNotIn("classes", app.tools.calendar.query(context, {"after_id": "zzz"}))
        # QQ/AstrBot chat replies read the calendar through the core-reply tool session, not the V2 loop.
        from types import SimpleNamespace as Session
        from astrbot_host.core_reply_tools import CoreReplyToolSession
        core = CoreReplyToolSession._read(Session(application=app, context=context), "calendar", "calendar.query", {})
        self.assertEqual(len(core["payload"]["classes"]), 3)
        prompt, material = await self.plan_with_classes(app, context, scheduler, "classes-daily")
        self.assertEqual([item["course"] for item in material["classes"]], ["马克思主义哲学导论"])
        self.assertIn("材料中的 classes", prompt.instruction)
        self.assertNotIn("不臆造固定课表", prompt.instruction)

    async def test_academics_off_shows_no_classes(self):
        app, context, _, life, plugin, api, _ = await self.setup_life_planning()
        scheduler = self.class_provider(context, life, plugin, api, academics=False)
        with app.role_life.consistent_snapshot():
            self.assertNotIn("classes", app.tools.calendar.query(context, {}))
        _prompt, material = await self.plan_with_classes(app, context, scheduler, "no-classes-daily")
        self.assertNotIn("classes", material)


if __name__ == "__main__":
    unittest.main()
