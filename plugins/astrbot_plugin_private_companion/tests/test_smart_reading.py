import json
import unittest
from datetime import timedelta
from zoneinfo import ZoneInfo

from companion_v2.role_work.life.smart_reading import choice_slot, reading_window
from tests.test_role_life_runtime import LifeFixture
from tests.test_role_tool_loop import answered


class SmartReadingTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_persona_reselects_and_reads_multiple_books_without_a_daily_limit(self):
        await self.setup_life()
        now = self.clock.now()
        active = {"kind": "role_plan", "state": "accepted", "title": "慢慢读几段",
                  "start": (now - timedelta(hours=1)).isoformat(),
                  "end": (now + timedelta(hours=3)).isoformat()}
        self.app.calendar.occurrences = lambda *args, **kwargs: {"items": [active], "partial": False}
        await self.command("add_text", text="甲" * 100, title="另一本资料")
        await self.command("add_text", text="诗" * 4000, title="示例诗集")

        async def model(envelope):
            if envelope.original.output_format == "role_book_selection":
                candidates = [json.loads(m.payload) for m in envelope.original.context.materials if m.purpose == "book_candidate"]
                selected = next((c for c in candidates if c["title"] == "示例诗集"), candidates[0])
                return answered(json.dumps({"choice": selected["number"], "reason": "我想读诗"}, ensure_ascii=False))
            return await self.model(envelope)
        self.model_behavior = model
        await self.configure(reading_enabled=True)
        routine = self.manager.life
        for completed in range(1, 5):
            await self.until(lambda: sum(a.reading is not None for a in self.manager._activities()) == completed)
            if completed < 4:
                pace_until = routine.reading_pace_until()
                self.advance(self.app, (pace_until - self.clock.now()).total_seconds())
        await self.settle()
        readings = [a for a in self.manager._activities() if a.reading]
        self.assertEqual(len([a for a in readings if "诗集" in a.description]), 3)
        self.assertEqual(len([a for a in readings if "另一本" in a.description]), 1)
        zone = ZoneInfo(routine.settings["timezone"])
        self.assertEqual(self.clock.now().astimezone(zone).date(), now.astimezone(zone).date())
        calls = [e for e in self.called if e.original.output_format == "role_reading"]
        self.assertTrue(any(m.purpose == "previous_reading_reflections" for m in calls[1].original.context.materials))
        self.assertTrue(all(e.original.response_format == "json_object" for e in calls))
        self.assertTrue(all(e.original.response_format == "json_object" for e in self.called
                            if e.original.output_format == "role_book_selection"))
        notes = [j for j in self.status()["journals"] if j["kind"] == "reading_note"]
        self.assertEqual(len(notes), 4)
        self.assertEqual(len({n["activity_id"] for n in notes}), 4)
        self.assertEqual(len([e for e in self.called if e.original.output_format == "role_book_selection"]), 4)

    async def test_persisted_book_choice_reason_is_filtered(self):
        from companion_v2.conversation.banned_phrases import BannedPhraseFilter, DEFAULT_PATTERNS

        await self.setup_life()
        self.app.role_reader.banned_phrases = BannedPhraseFilter(DEFAULT_PATTERNS)
        await self.add_text("一段文字")
        self.allow_reading_window()

        async def model(envelope):
            if envelope.original.output_format == "role_book_selection":
                return answered('{"choice":"none","reason":"想慢悠悠的休息一会儿"}')
            return await self.model(envelope)

        self.model_behavior = model
        await self.configure(reading_enabled=True)
        await self.until(lambda: self.status()["reading_plan"]["state"] == "declined")
        row = self.runtime.read_one("SELECT value FROM runtime_metadata WHERE key LIKE 'role.reading.choice:%'", ())
        self.assertEqual(json.loads(row["value"])["reason"], "想休息一会儿")

    async def test_other_plans_block_reading_and_bad_choice_is_not_replayed(self):
        await self.setup_life()
        now = self.clock.now()
        busy = {"kind": "role_plan", "state": "accepted", "title": "上课",
                "start": (now - timedelta(minutes=10)).isoformat(),
                "end": (now + timedelta(hours=3)).isoformat()}
        calendar = {"items": [busy], "partial": False}
        self.app.calendar.occurrences = lambda *args, **kwargs: calendar
        await self.add_text()
        async def bad_choice(envelope):
            return answered('{"choice":"999","reason":"错"}')
        self.model_behavior = bad_choice
        await self.configure(reading_enabled=True)
        self.app.life_runtime.advance()
        self.assertEqual(len(self.called), 0)
        busy["title"] = "阅读时间"
        self.advance(self.app, 31)
        await self.until(lambda: len(self.called) == 1)
        await self.until(lambda: not self.app.life_runtime.active_handles)
        self.app.life_runtime.advance()
        self.assertEqual(len(self.called), 1)
        self.assertTrue(all(a.state.value == "planned" for a in self.manager._activities()))

    async def test_declining_is_reconsidered_later_without_waiting_for_tomorrow(self):
        await self.setup_life()
        await self.add_text("一段文字")
        self.allow_reading_window()
        selections = 0
        async def model(envelope):
            nonlocal selections
            if envelope.original.output_format == "role_book_selection":
                selections += 1
                choice = "none" if selections == 1 else "1"
                return answered(json.dumps({"choice": choice, "reason": "随心情"}, ensure_ascii=False))
            return await self.model(envelope)
        self.model_behavior = model
        await self.configure(reading_enabled=True)
        await self.until(lambda: self.status()["reading_plan"]["state"] == "declined")
        await self.settle()
        self.app.life_runtime.advance()
        self.advance(self.app, 31)
        await self.until(lambda: any(a.reading is not None for a in self.manager._activities()))
        self.assertEqual(selections, 2)

    async def test_known_failed_choice_can_be_reconsidered_without_replaying_it(self):
        await self.setup_life()
        await self.add_text("一段文字")
        self.allow_reading_window()
        selections = 0
        async def model(envelope):
            nonlocal selections
            if envelope.original.output_format == "role_book_selection":
                selections += 1
                choice = "999" if selections == 1 else "1"
                return answered(json.dumps({"choice": choice, "reason": "重选"}, ensure_ascii=False))
            return await self.model(envelope)
        self.model_behavior = model
        await self.configure(reading_enabled=True)
        await self.until(lambda: self.status()["reading_plan"]["state"] == "incomplete")
        await self.settle()
        self.app.life_runtime.advance()
        self.advance(self.app, 31)
        await self.until(lambda: any(a.reading is not None for a in self.manager._activities()))
        self.assertEqual(selections, 2)

    async def test_unavailable_selected_book_does_not_block_another_book(self):
        await self.setup_life()
        withdrawn = (await self.command("add_text", text="第一本", title="第一本")).unwrap()["activity_ids"][0]
        await self.command("add_text", text="第二本", title="第二本")
        old = next(a for a in self.manager._activities() if a.activity_id == withdrawn)
        day = self.clock.now().astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        slot = choice_slot(list(self.manager._activities()), day) + ":0"
        with self.runtime.transaction() as connection:
            connection.execute("INSERT INTO runtime_metadata(key,value) VALUES (?,?)", (
                self.app.role_reader.choice_key(self.task_context, slot),
                json.dumps({"source_id": old.work.source.source_id, "reason": "旧选择"})))
        item = next(a for a in self.status()["activities"] if a["activity_id"] == withdrawn)
        (await self.command("withdraw", item)).unwrap()
        self.allow_reading_window()
        await self.configure(reading_enabled=True)
        await self.until(lambda: any(a.reading is not None for a in self.manager._activities()))
        done = next(a for a in self.manager._activities() if a.reading is not None)
        self.assertIn("第二本", done.description)

    async def test_source_withdrawal_hides_reading_note(self):
        await self.setup_life()
        book = (await self.add_text("一段有来源的原文"))[0]
        (await self.command("read", book)).unwrap()
        await self.settle()
        note = next(j for j in self.status()["journals"] if j["kind"] == "reading_note")
        self.assertEqual(note["excerpt"], "一段有来源的原文")
        current = self.status()["activities"][0]
        (await self.command("withdraw", current)).unwrap()
        note = next(j for j in self.status()["journals"] if j["kind"] == "reading_note")
        self.assertTrue(note["unavailable_reason"])
        self.assertIsNone(note["body"])
        self.assertIsNone(note["excerpt"])

    def test_calendar_window_rejects_partial_conflicts_and_imminent_sleep(self):
        from datetime import datetime, timezone
        now = datetime(2026, 11, 13, 14, 30, tzinfo=timezone.utc)
        local = now.replace(hour=22)
        plan = {"kind": "role_plan", "state": "accepted", "title": "慢慢读几段",
                "start": (now - timedelta(minutes=2)).isoformat(), "end": (now + timedelta(minutes=30)).isoformat()}
        self.assertTrue(reading_window({"items": [plan]}, now, local, now + timedelta(hours=1)))
        self.assertIsNone(reading_window({"items": [plan], "partial": True}, now, local, None))
        self.assertTrue(reading_window({"items": [plan]}, now, local, now + timedelta(minutes=5)))
        self.assertIsNone(reading_window({"items": [plan]}, now, local, now))
        other = {**plan, "kind": "commitment", "title": "会面"}
        self.assertIsNone(reading_window({"items": [plan, other]}, now, local, None))
