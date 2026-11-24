"""Unprompted output skips the desktop window while the desktop app is closed."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from astrbot_host.desktop_api import DesktopAPI
from astrbot_host.desktop_presence import DesktopPresence
from astrbot_host.multi_authority import MultiAuthority
from astrbot_host.multi_controller import AstrBotMultiController
from astrbot_host.multi_session import MultiHostSession
from astrbot_host.multi_settings import MultiHostSettings
from companion_v2.execution.models import TaskError
from companion_v2.foundation.refs import ScopeKind
from companion_v2.proactive.configuration import GROUP as CONTACT_OWNER
from companion_v2.proactive import scheduling
from companion_v2.proactive.scheduling import ContactSchedule
from tests.contact_schedule_support import ContactScheduleFixture
from tests.multi_host_support import multi_config


DESKTOP = SimpleNamespace(platform="webchat")
QQ = SimpleNamespace(platform="aiocqhttp")
SESSION = "a" * 32


def desktop_api(presence):
    controller = SimpleNamespace(desktop_presence=presence)
    api = DesktopAPI(controller)
    api._authorize = lambda: None
    return api


async def screen(api, action, *, session=SESSION, generation=0):
    api._body = AsyncMock(return_value={"session": session, "generation": generation, "action": action})
    return await api.screen()


class DesktopPresenceSignalTests(unittest.IsolatedAsyncioTestCase):
    async def test_window_open_and_close_drive_presence(self):
        presence = DesktopPresence()
        api = desktop_api(presence)
        self.assertFalse(presence.present)

        self.assertEqual((await screen(api, "open"))["status"], "ok")
        self.assertTrue(presence.present)
        self.assertTrue(presence.allows(DESKTOP))

        self.assertEqual((await screen(api, "end"))["status"], "ok")
        self.assertFalse(presence.present)
        self.assertFalse(presence.allows(DESKTOP))
        self.assertTrue(presence.allows(QQ), "QQ windows never depend on the desktop app")

    async def test_end_from_a_replaced_window_does_not_close_the_new_one(self):
        presence = DesktopPresence()
        api = desktop_api(presence)
        await screen(api, "open", session="a" * 32)
        await screen(api, "open", session="b" * 32)
        self.assertEqual((await screen(api, "end", session="a" * 32))["status"], "error")
        self.assertTrue(presence.present)

    async def test_host_restart_counts_the_desktop_absent_until_it_reconnects(self):
        with tempfile.TemporaryDirectory() as root:
            first = AstrBotMultiController(SimpleNamespace(), {}, root)
            first.desktop_presence.connect(SESSION)
            restarted = AstrBotMultiController(SimpleNamespace(), {}, root)
        self.assertFalse(restarted.desktop_presence.present)
        self.assertFalse(restarted.desktop_presence.allows(DESKTOP))

        # The still-open client's next screen action, with its old session, reconnects it.
        api = desktop_api(restarted.desktop_presence)
        await screen(api, "stop", generation=3)
        self.assertTrue(restarted.desktop_presence.present)


class DesktopDeliveryGateTests(unittest.IsolatedAsyncioTestCase):
    class Dispatched(Exception):
        pass

    def session(self, presence):
        session = object.__new__(MultiHostSession)
        session.desktop_presence = presence

        def platform_name(scope):
            raise self.Dispatched(scope.platform)
        session.bridge = SimpleNamespace(platform_name=platform_name)
        return session

    def envelope(self, scope):
        return SimpleNamespace(context=SimpleNamespace(scope=SimpleNamespace(
            kind=SimpleNamespace(), platform=scope.platform)))

    async def test_closed_desktop_refuses_background_output_and_qq_still_sends(self):
        session = self.session(DesktopPresence())
        with self.assertRaises(TaskError) as refused:
            await session.send(self.envelope(DESKTOP))
        self.assertEqual(refused.exception.code, "desktop_absent")
        with self.assertRaises(self.Dispatched):
            await session.send(self.envelope(QQ))

    async def test_connected_desktop_sends_as_before(self):
        presence = DesktopPresence()
        presence.connect(SESSION)
        with self.assertRaises(self.Dispatched):
            await self.session(presence).send(self.envelope(DESKTOP))

    async def test_reply_to_a_desktop_message_still_reaches_the_desktop(self):
        # After a restart the desktop is absent; the user's message itself
        # reconnects it, so the reply's own follow-up parts (e.g. an image
        # fallback through this sender) are delivered to the desktop.
        presence = DesktopPresence()
        with tempfile.TemporaryDirectory() as root:
            (Path(root) / "desktop").mkdir()
            (Path(root) / "desktop" / "device.json").write_text(json.dumps({"target_id": "desktop-1"}),
                                                                encoding="utf-8")
            controller = SimpleNamespace(desktop_presence=presence, data_root=root,
                target_for_event=lambda event: SimpleNamespace(platform_id="webchat", target_id="desktop-1"))
            DesktopAPI(controller).inject_observation(SimpleNamespace(), SimpleNamespace())
        with self.assertRaises(self.Dispatched):
            await self.session(presence).send(self.envelope(DESKTOP))


def owner_setup(presence):
    """Real settings with the desktop paired to the owner's QQ window, plus a catch-all QQ target."""
    settings = MultiHostSettings.parse(multi_config(targets=[
        dict(target_id="qq-owner", platform_id="qq-main", account_id="100", bot_id="bot-a", participant_id="200"),
        dict(target_id="qq-anyone", platform_id="qq-main", account_id="100", bot_id="bot-a", participant_id="*"),
        dict(target_id="desktop-1", platform_id="webchat", account_id="webchat", bot_id="bot-a",
             participant_id="webchat!admin!" + SESSION)],
        personas=[dict(bot_id="bot-a", persona_id="original", role_id="role-a")],
        window_links=[dict(primary_target_id="qq-owner", alias_target_ids=["desktop-1"], mode="role")]))
    authority = object.__new__(MultiAuthority)
    authority.runtime, authority.settings = SimpleNamespace(installation_id="inst"), settings
    authority.platform_name = lambda platform_id: "webchat" if platform_id == "webchat" else "aiocqhttp"
    session = object.__new__(MultiHostSession)
    session.settings, session.authority, session.desktop_presence = settings, authority, presence

    def context(target_id, participant=None):
        target = settings.target(target_id)
        return SimpleNamespace(scope=SimpleNamespace(platform=target.platform_id, kind=ScopeKind.PRIVATE),
                               actor=SimpleNamespace(subject=authority.subject(target, participant)))
    windows = {"desktop": context("desktop-1"), "owner": context("qq-owner"),
               "anyone": context("qq-anyone", "300")}
    return session, windows


class DesktopPreferredHoldTests(unittest.TestCase):
    def test_desktop_present_makes_it_the_owners_only_proactive_window(self):
        presence = DesktopPresence()
        presence.connect(SESSION)
        session, windows = owner_setup(presence)
        self.assertIsNone(session.window_hold(windows["desktop"]))
        self.assertEqual(session.window_hold(windows["owner"]), "desktop_preferred")

    def test_another_qq_users_window_is_unaffected(self):
        presence = DesktopPresence()
        session, windows = owner_setup(presence)
        self.assertIsNone(session.window_hold(windows["anyone"]))
        presence.connect(SESSION)
        self.assertIsNone(session.window_hold(windows["anyone"]))

    def test_desktop_absent_leaves_qq_normal(self):
        session, windows = owner_setup(DesktopPresence())
        self.assertEqual(session.window_hold(windows["desktop"]), "window_absent")
        self.assertIsNone(session.window_hold(windows["owner"]))

    def test_attach_installs_scheduler_gate_and_contact_only_delivery_hold(self):
        presence = DesktopPresence()
        presence.connect(SESSION)
        session, windows = owner_setup(presence)
        session.application = SimpleNamespace(contact_schedule=SimpleNamespace(), outbound=SimpleNamespace())
        session.attach_desktop_presence(presence)
        self.assertEqual(session.application.contact_schedule.window_gate(windows["owner"]), "desktop_preferred")
        hold = session.application.outbound.hold
        self.assertEqual(hold(SimpleNamespace(owner=CONTACT_OWNER, context=windows["owner"])), "desktop_preferred")
        self.assertIsNone(hold(SimpleNamespace(owner="reminder.deliver", context=windows["owner"])),
                          "only proactive contact follows the desktop; reminders keep their window")


class ContactPlanWindowGateTests(unittest.TestCase):
    def plan_all(self, presence):
        session, windows = owner_setup(presence)
        scheduler = object.__new__(ContactSchedule)
        scheduler._closed, scheduler._workers = False, {}
        scheduler.window_gate = session.window_hold
        scheduler._peek = lambda row, now: False
        scheduler._plan = lambda row, now: "plan"
        notes = {}
        scheduler._note_window = lambda context, row, now, *args, **kwargs: notes.__setitem__(
            row["origin_key"], args[2])
        planned = {}
        with patch.object(scheduling.TaskContext, "restore", side_effect=lambda document: windows[document["key"]]):
            for key in windows:
                planned[key] = scheduler._plan_one(
                    {"origin_key": key, "context_json": json.dumps({"key": key})}, None)
        return planned, notes

    def test_desktop_present_plans_only_desktop_for_its_owner(self):
        presence = DesktopPresence()
        presence.connect(SESSION)
        planned, notes = self.plan_all(presence)
        self.assertEqual(planned, {"desktop": "plan", "owner": None, "anyone": "plan"})
        self.assertEqual(notes, {"owner": "desktop_preferred"})

    def test_desktop_absent_skips_desktop_and_plans_qq(self):
        planned, notes = self.plan_all(DesktopPresence())
        self.assertEqual(planned, {"desktop": None, "owner": "plan", "anyone": "plan"})
        self.assertEqual(notes, {"desktop": "window_absent"})


class ContactScheduleWindowGateIntegrationTests(ContactScheduleFixture, unittest.IsolatedAsyncioTestCase):
    async def test_held_qq_window_resumes_without_backlog_once_the_desktop_closes(self):
        host, _ = await self.scheduled()
        app = host.application
        presence = DesktopPresence()
        presence.connect(SESSION)
        window = self.contact_context().scope
        app.contact_schedule.window_gate = (
            lambda context: "desktop_preferred" if presence.present and context.scope == window else None)
        self.advance(app, 60)
        app.contact_schedule.advance()
        await asyncio.sleep(0.05)
        self.assertEqual(self.contact_calls(), [])
        self.assertEqual(len(self.contacts(app)), 0)
        diagnostic = app.contact_schedule.diagnostic_for(self.contact_context())
        self.assertEqual(diagnostic["evaluation"]["reason_code"], "desktop_preferred")

        presence.disconnect()
        app.notify()
        await self.automatic_delivery(app)
        self.assertEqual(len(self.contact_calls()), 1, "the held period is not replayed")

    async def test_qq_draft_reaching_delivery_while_desktop_present_is_dropped(self):
        host, _ = await self.scheduled()
        app = host.application
        presence = DesktopPresence()
        presence.connect(SESSION)
        app.outbound.hold = lambda request: "desktop_preferred" if presence.present else None
        self.advance(app, 60)
        await self.until(lambda: len(self.contacts(app)) >= 1 or app.state == "faulted")
        task_id = self.contacts(app)[-1]["task_id"]
        row = await self.delivery_state(app, task_id, "cancelled")
        self.assertIsNone(row["operation_id"], "a held draft never reaches the sender")

        presence.disconnect()
        app.notify()
        app.contact_schedule.advance()
        await asyncio.sleep(0.05)
        self.assertEqual(app.proactive._delivery_row(task_id)["state"], "cancelled", "dropped, not queued")


if __name__ == "__main__":
    unittest.main()
