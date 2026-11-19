import asyncio
import json
import unittest

from tests.test_role_life_runtime import LifeFixture
from tests.test_role_tool_loop import answered


class ReadingPacingTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_default_and_changed_segment_size_preserve_the_original(self):
        await self.setup_life()
        original = "甲" * 6000 + "乙" * 111
        (await self.add_text(original))
        status = self.status()
        self.assertEqual(status["config"]["reading_chunk_chars"], 1500)
        self.assertEqual(status["config"]["reading_daily_segments"], 2)
        first = next(a for a in status["activities"] if a["state"] == "planned")
        self.assertEqual((first["start_offset"], first["end_offset"]), (0, 1500))
        self.assertIsNone(first["started_at"])
        self.assertIn("1–1500", first["title"])
        (await self.command("read", first)).unwrap()
        await self.settle()
        await self.configure(reading_chunk_chars=2100)
        second = next(a for a in self.status()["activities"] if a["state"] == "planned")
        self.assertEqual((second["start_offset"], second["end_offset"]), (1500, 3600))
        self.assertIn("1501–3600", second["title"])
        while True:
            pending = [a for a in self.status()["activities"] if a["state"] == "planned"]
            if not pending:
                break
            (await self.command("read", pending[0])).unwrap()
            await self.settle()
        read = [json.loads(e.original.context.materials[0].payload) for e in self.called
                if e.original.output_format == "role_reading"]
        self.assertEqual([(v["start"], v["end"]) for v in read],
                         [(0, 1500), (1500, 3600), (3600, 5700), (5700, 6111)])
        self.assertEqual("".join(v["text"] for v in read), original)
        self.assertEqual(len([j for j in self.status()["journals"] if j["kind"] == "reading_note"]), 4)

    async def test_legacy_daily_limit_does_not_stop_unlimited_reading(self):
        await self.setup_life()
        await self.add_text("一" * 4000)
        self.allow_reading_window()
        await self.configure(reading_enabled=True, reading_daily_segments=1)
        now = self.clock.now()
        await self.until(lambda: sum(a.reading is not None for a in self.manager._activities()) == 3)
        await self.settle()
        self.assertEqual(self.clock.now(), now)
        self.assertEqual(sum(a.reading is not None for a in self.manager._activities()), 3)

    async def test_changing_size_does_not_change_an_in_progress_segment(self):
        await self.setup_life()
        book = (await self.add_text("字" * 6000))[0]
        entered, release = asyncio.Event(), asyncio.Event()
        async def model(envelope):
            entered.set()
            await release.wait()
            return answered(json.dumps({"reflection": "读了一小段。"}, ensure_ascii=False))
        self.model_behavior = model
        (await self.command("read", book)).unwrap()
        await asyncio.wait_for(entered.wait(), 10)
        await self.configure(reading_chunk_chars=3000)
        release.set()
        await self.settle()
        complete = next(a for a in self.manager._activities() if a.reading)
        self.assertEqual(complete.reading.end_offset, 1500)
        pending = next(a for a in self.status()["activities"] if a["state"] == "planned")
        self.assertEqual((pending["start_offset"], pending["end_offset"]), (1500, 4500))

    async def test_cancel_skips_only_the_displayed_planned_segment(self):
        await self.setup_life()
        planned = (await self.add_text("书" * 5000))[0]
        self.assertEqual(planned["end_offset"], 1500)
        (await self.command("cancel", planned)).unwrap()
        activities = self.status()["activities"]
        cancelled = next(a for a in activities if a["state"] == "cancelled")
        next_part = next(a for a in activities if a["state"] == "planned")
        self.assertEqual(cancelled["end_offset"], 1500)
        self.assertEqual(next_part["start_offset"], 1500)
        self.assertEqual(len(self.called), 0)

    async def test_out_of_range_settings_are_rejected(self):
        await self.setup_life()
        for change in ({"reading_chunk_chars": 199}, {"reading_chunk_chars": 6001},
                       {"reading_daily_segments": 0}, {"reading_daily_segments": 7}):
            result = await self.command("configure", changes=change, edit_token=self.status()["edit_token"])
            self.assertFalse(result.is_success, change)
        self.assertEqual(self.status()["config"]["reading_chunk_chars"], 1500)
        self.assertEqual(self.status()["config"]["reading_daily_segments"], 2)
