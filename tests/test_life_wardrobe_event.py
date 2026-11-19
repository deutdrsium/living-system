import asyncio
import json
from unittest.mock import patch
import unittest

import test_natural_wake_handoff as wake
from companion_v2.integration.life_activity_control_proxy import LifeActivityTransitionProxy
from companion_v2.integration.life_event_inbox import inbox_key, receive_pending
from companion_v2.integration.natural_wake_handoff import NaturalWakeHandoff
from tests.test_role_state import StateFixture


class LifeWardrobeEventTests(StateFixture, unittest.IsolatedAsyncioTestCase):
    prepare_sleep = wake.NaturalWakeHandoffTests.prepare_sleep
    configured = wake.NaturalWakeHandoffTests.configured

    async def test_completed_sleep_dispatches_one_host_wardrobe_choice(self):
        manager, life, lease = await self.configured(identifier="existing-night")
        await NaturalWakeHandoff(manager.app, manager.life, LifeActivityTransitionProxy(lease)).execute(
            "existing-night", expected_revision=1)
        event = life.pending_events("wardrobe")[0]
        receive_pending(manager.app, manager.context, consumer="wardrobe", observed_at=self.clock.now())
        key = inbox_key(manager.context, "wardrobe", event["event_id"])
        class Wardrobe:
            def __init__(self):
                self.saved, self.retries, self.calls = None, {}, 0
            def _read_choice(self, _owner, _day):
                return self.saved
            def _key(self, _owner, _day):
                return "daily-choice"
            async def consume_life_event(self, routine, activity, _now):
                self.calls += 1
                with routine.app.runtime.transaction():
                    self.assert_pinned(routine.app.role_life.activity(activity.activity_id, owner=activity.owner) == activity)
                self.saved = {"state": "applied"}
                return True
            assert_pinned = staticmethod(lambda value: None if value else self.fail("sleep snapshot was not pinned"))

        wardrobe = Wardrobe()
        runner = manager.app.life_runtime.events
        del runner.advance
        with patch.object(manager.app, "role_wardrobe", wardrobe), patch(
                "companion_v2.integration.life_event_runner.binding", return_value={"activated_at": "2026-01-01T00:00:00+00:00"}), patch(
                "companion_v2.integration.life_event_runner.receive_pending", return_value={"next_after": 0}):
            runner.advance((manager.life,))
            await asyncio.gather(*tuple(runner._workers.values()))
            await asyncio.sleep(0)
            runner.advance((manager.life,))
            await asyncio.sleep(0)
        final = json.loads(self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key=?", (key,))["value"])
        self.assertEqual((final["state"], final["result"], wardrobe.calls), ("completed", "applied", 1))


if __name__ == "__main__":
    unittest.main()
