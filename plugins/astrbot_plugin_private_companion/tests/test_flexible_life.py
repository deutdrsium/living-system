import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from companion_v2.configuration.registry import FieldRegistry
from companion_v2.execution.models import digest
from companion_v2.model_gateway.routing import configuration_fields
from companion_v2.role_work.life.smart_reading import reading_window
from companion_v2.role_work.life.smart_social import social_window
from companion_v2.role_work.models import RoleActivityState as State
from companion_v2.role_work.reader import book_choice_profile
from companion_v2.role_work.social import social_profile
from tests.test_role_life_runtime import LifeFixture
from tests.test_role_tool_loop import answered, call, requested
from tests.test_social_integration import FakeSocialPort, social_answered


class AvailableTimeTests(unittest.TestCase):
    def test_short_free_period_is_available_at_night_but_busy_and_sleep_are_not(self):
        now = datetime(2026, 11, 14, 23, 30, tzinfo=timezone.utc)
        future = {"kind": "role_plan", "state": "accepted", "title": "上课",
                  "start": (now + timedelta(minutes=2)).isoformat(),
                  "end": (now + timedelta(hours=1)).isoformat()}
        for window in (reading_window, social_window):
            self.assertTrue(window({"items": [future]}, now, now, now + timedelta(minutes=1)))
            self.assertIsNone(window({"items": [future]}, now, now, now))
            active = {**future, "start": (now - timedelta(minutes=1)).isoformat()}
            self.assertIsNone(window({"items": [active]}, now, now, None))
            self.assertIsNone(window({"items": [], "partial": True}, now, now, None))


class FlexibleSocialTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.profiles += (social_profile("primary", max_output_tokens=100), book_choice_profile("primary", max_output_tokens=100))
        self.registry = FieldRegistry(version="flexible-social.test.v1", fields=configuration_fields(self.profiles))

    def calendar(self):
        now = self.clock.now()
        self.plan = {"state": "accepted", "kind": "role_plan", "title": "上网",
                     "start": (now - timedelta(minutes=1)).isoformat(),
                     "end": (now + timedelta(hours=1)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [self.plan], "partial": False}

    async def test_more_than_two_sessions_without_advancing_the_clock(self):
        await self.setup_life()
        self.app.social_port = FakeSocialPort()
        self.calendar()
        now, completed = self.clock.now(), 0
        async def model(envelope):
            nonlocal completed
            if not envelope.tool_history:
                return requested(call("recommend", "social_browse", operation="bili_recommend"))
            completed += 1
            if completed == 3:
                self.plan["title"] = "上课"
            return social_answered("看了推荐。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: len([a for a in self.manager._activities()
                                      if a.work.kind == "social" and a.state is State.COMPLETED]) == 3)
        await self.settle()
        self.assertEqual(self.clock.now(), now)
        self.assertEqual(completed, 3)

    async def test_more_than_two_distinct_interactions_after_reading(self):
        await self.setup_life()
        port = self.app.social_port = FakeSocialPort()
        self.calendar()
        ids = ["BV1111111111", "BV2222222222", "BV3333333333"]
        async def model(envelope):
            index = len(envelope.tool_history)
            if index < 6:
                return requested(call("call" + str(index), "social_browse",
                    operation="bili_watch" if index % 2 == 0 else "bili_like", id=ids[index // 2]))
            self.plan["title"] = "上课"
            return social_answered("看过后喜欢其中三条。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: any(a.work.kind == "social" and a.state is State.COMPLETED
                                     for a in self.manager._activities()))
        await self.settle()
        self.assertEqual([name for name, _ in port.calls].count("bili_like"), 3)

    async def test_declined_browse_does_not_retry_in_a_tight_loop(self):
        await self.setup_life()
        self.app.social_port = FakeSocialPort()
        self.calendar()
        async def model(envelope): return social_answered("现在先不浏览。")
        self.model_behavior = model
        self.app.life_runtime.advance()
        await self.until(lambda: any(a.work.kind == "social" and a.state is State.CANCELLED
                                     for a in self.manager._activities()))
        await self.settle()
        for _ in range(3):
            self.app.life_runtime.advance()
        self.assertEqual(len([a for a in self.manager._activities() if a.work.kind == "social"]), 1)
        self.assertGreater(self.manager.life._smart_social.next_check, self.clock.now())

    async def test_completed_reading_gives_browsing_an_opportunity(self):
        await self.setup_life()
        self.app.social_port = FakeSocialPort()
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [], "partial": False}
        await self.add_text("字" * 4500)
        async def model(envelope):
            if envelope.original.output_format == "social_reflection":
                if not envelope.tool_history:
                    return requested(call("recommend", "social_browse", operation="bili_recommend"))
                now = self.clock.now()
                busy = {"kind": "role_plan", "state": "accepted", "title": "上课",
                        "start": (now - timedelta(minutes=1)).isoformat(),
                        "end": (now + timedelta(hours=1)).isoformat()}
                self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [busy], "partial": False}
                return social_answered("看了推荐。")
            return await self.model(envelope)
        self.model_behavior = model
        await self.configure(reading_enabled=True)
        await self.until(lambda: any(a.work.kind == "social" and a.state is State.COMPLETED
                                     for a in self.manager._activities()))
        await self.settle()
        self.assertTrue(any(a.reading is not None for a in self.manager._activities()))

    async def test_completed_browsing_gives_reading_an_opportunity_on_social_first_days(self):
        await self.setup_life()
        self.app.social_port = FakeSocialPort()
        self.calendar()
        await self.add_text("字" * 4500)
        social_count = 0

        async def model(envelope):
            nonlocal social_count
            if envelope.original.output_format == "social_reflection":
                if not envelope.tool_history:
                    return requested(call("recommend", "social_browse", operation="bili_recommend"))
                social_count += 1
                self.clock.advance(timedelta(seconds=1))
                self.plan["title"] = "空闲" if social_count == 1 else "上课"
                if social_count == 1:
                    self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [], "partial": False}
                else:
                    self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [self.plan], "partial": False}
                return social_answered("看了推荐。")
            if envelope.original.output_format == "role_reading":
                self.plan["title"] = "上课"
                self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [self.plan], "partial": False}
            return await self.model(envelope)

        self.model_behavior = model

        def force_social_day(value):
            if isinstance(value, list) and len(value) == 2 and value[0] == self.manager.life.owner:
                return "0" * 64
            return digest(value)

        with patch("companion_v2.role_work.life.runtime.visible_interests", return_value=["兴趣"]), \
             patch("companion_v2.role_work.life.runtime.digest", side_effect=force_social_day):
            await self.configure(reading_enabled=True)
            await self.until(lambda: social_count >= 2 or any(a.reading is not None
                                                              for a in self.manager._activities()))
            await self.settle()
        self.assertEqual(social_count, 1)
        self.assertTrue(any(a.reading is not None for a in self.manager._activities()))


if __name__ == "__main__":
    unittest.main()
