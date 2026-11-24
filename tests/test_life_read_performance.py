"""Bounded page reads, live authorization and an unblocked Dashboard loop."""
import asyncio
from datetime import timedelta
from dataclasses import replace
import unittest
from threading import Event, get_ident
from time import perf_counter
from unittest.mock import patch
from urllib.parse import urlencode

import test_life_page_reads as page_support
from profile_life_read import Fixture as BenchmarkFixture
from tests.multi_life_support import MultiLifeFixture
from companion_v2.role_work.life.management import RoleLifeManagement
from companion_v2.sources.repositories import SourceRepository
from tests.test_management_access import NOW


class LifeReadPerformanceTests(MultiLifeFixture, unittest.IsolatedAsyncioTestCase):
    setup_page = page_support.LifePageReadTests.setup_page
    call = page_support.LifePageReadTests.call
    async def test_compact_pages_bound_notes_and_load_details_with_live_authority(self):
        page = await self.setup_page()
        # The shared synthetic benchmark builds 500 notes on 50 dates, 170
        # interests, ten originals, real evidence and real authorization checks.
        # No scheduler/model work is part of this read benchmark.
        (await self.controller.stop()).unwrap()
        self.app = await BenchmarkFixture.populate(self)
        query = {"compact": "1", "limit": 20, "archive_kind": "reading_note"}
        checks = []
        original = SourceRepository.validate_reference_for_audience
        def counted(repository, reference, **kwargs):
            checks.append((reference, kwargs.get("expected_content_hash")))
            return original(repository, reference, **kwargs)
        with patch.object(SourceRepository, "validate_reference_for_audience", counted):
            data = self.ok(await self.call(page.life, query=urlencode(query)))
        self.assertEqual(len(data["journals"]), 20)
        self.assertEqual(len(data["interests"]), 170)
        self.assertLess(len(checks), 100, "same originals must not be checked once per evidence item")
        interest = data["interests"][0]
        self.assertTrue(interest["compact"])
        self.assertNotIn("evidence", interest)
        self.assertNotIn("contacts", interest)
        detail = self.ok(await self.call(page.life, query=urlencode({"view": "interests", "interest_id": interest["interest_id"]})))
        self.assertEqual(len(detail["interests"]), 1)
        self.assertTrue(detail["interests"][0]["evidence"])
        self.assertFalse(detail["journals"])
        seen = {item["activity_id"] for item in data["journals"]}
        while data["next_note_cursor"]:
            data = self.ok(await self.call(page.life, query=urlencode({**query, "view": "archive", "note_cursor": data["next_note_cursor"]})))
            ids = {item["activity_id"] for item in data["journals"]}
            self.assertFalse(seen & ids)
            self.assertLessEqual(len(ids), 20)
            seen.update(ids)
        self.assertEqual(len(seen), 500)
        reading = self.ok(await self.call(page.life, query="compact=1&view=reading"))
        self.assertEqual(reading["journals"], [])
        self.assertEqual(reading["interests"], [])
        self.assertFalse(reading["refresh_active"])
        # A fresh request must not reuse a previous snapshot's positive checks.
        manager = RoleLifeManagement(self.app, self.context())
        activity = manager._activity("fixture-note-0000")
        mismatched = replace(activity, activity_id="fixture-zbad", reading=replace(activity.reading, content_hash="f" * 64),
                             work=replace(activity.work, content_hash="f" * 64))
        self.app.role_life.add_activity(mismatched)
        checked = self.ok(await self.call(page.life, query=urlencode(query)))
        wrong = next(item for item in checked["journals"] if item["activity_id"] == "fixture-zbad")
        self.assertEqual(wrong["unavailable_reason"], "source_content_changed")
        self.assertIsNone(wrong["body"])
        valid = next(item for item in checked["journals"] if item["activity_id"] == activity.activity_id)
        self.assertTrue(valid["excerpt"], "a live original read grant still supplies the excerpt")
        row = self.app.runtime.read_one("SELECT governance_revision FROM sources WHERE source_id=?", (activity.reading.source.source_id,))
        self.app.role_journal.sources.revoke(activity.reading.source, expected_governance_revision=row["governance_revision"], observed_at=NOW).unwrap()
        revoked = self.ok(await self.call(page.life, query=urlencode(query)))
        note = next(item for item in revoked["journals"] if item["activity_id"] == activity.activity_id)
        self.assertEqual(note["unavailable_reason"], "source_revoked")
        self.assertIsNone(note["body"])
        self.assertIsNone(note["excerpt"])

    async def test_reading_plan_controls_metadata_reads_and_paced_refresh(self):
        await self.setup_page()
        manager = RoleLifeManagement(self.app, self.context())
        with patch.object(manager.life, "reading_pace_until", side_effect=AssertionError("disabled reading metadata")) as pace:
            disabled = manager.status(self.operator())
            self.assertFalse(disabled["config"]["enabled"])
            self.assertEqual(disabled["reading_plan"]["state"], "disabled")
            self.assertFalse(disabled["refresh_active"])
            await self.enable(reading_enabled=False)
            disabled = manager.status(self.operator())
            self.assertTrue(disabled["config"]["enabled"])
            self.assertFalse(disabled["config"]["reading_enabled"])
            self.assertEqual(disabled["reading_plan"]["state"], "disabled")
            self.assertFalse(disabled["refresh_active"])
            pace.assert_not_called()
        await self.command("configure", operation_key="enable-reading", changes={"reading_enabled": True})
        with patch.object(manager.life, "reading_pace_until", return_value=NOW - timedelta(seconds=1)) as pace:
            self.assertFalse(manager.status(self.operator(), compact=True, view="reading")["refresh_active"])
            pace.assert_called_once()
        with patch.object(manager.life, "reading_pace_until", return_value=NOW + timedelta(seconds=1)) as pace:
            paced = manager.status(self.operator(), compact=True, view="reading")
            self.assertEqual(paced["reading_plan"]["state"], "paced")
            self.assertTrue(paced["refresh_active"])
            pace.assert_called_once()

    def operator(self):
        from astrbot_host.management_identity import dashboard_operator
        return dashboard_operator(self.app.runtime, "admin", at=NOW)

    async def test_slow_life_read_does_not_hold_event_loop_or_control_lock(self):
        page = await self.setup_page()
        entered, release = Event(), Event()
        loop_thread = get_ident()
        worker_threads = []
        original = RoleLifeManagement.status
        def slow(manager, operator, **kwargs):
            worker_threads.append(get_ident())
            entered.set()
            if not release.wait(5):
                raise AssertionError("worker was not released")
            return original(manager, operator, **kwargs)
        pending = None
        try:
            with patch.object(RoleLifeManagement, "status", slow):
                pending = asyncio.create_task(self.call(page.life, query="compact=1&view=reading"))
                self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait, 2), 3))
                start = perf_counter()
                wallet = await asyncio.wait_for(self.call(page.wallet), .5)
                self.assertEqual(wallet["status"], "ok")
                self.assertLess(perf_counter() - start, .5)
                self.assertNotEqual(worker_threads, [loop_thread])
                release.set()
                self.ok(await pending)
        finally:
            release.set()
            if pending is not None:
                await pending

    async def test_companion_diary_date_pages_keep_the_existing_cursor(self):
        from companion_v2.role_work.journal.models import JournalContext, JournalKey, JournalKind, JournalDraft
        await self.setup_page()
        context = self.context()
        manager = RoleLifeManagement(self.app, context)
        for n in range(3):
            day = (NOW.date() - timedelta(days=n)).isoformat()
            target = JournalContext(JournalKey(manager.owner, JournalKind.DIARY, day), context.scope, "UTC")
            self.app.role_journal.save(JournalDraft(target, body="合成日记"), expected_revision=0,
                                       operation_id="fixture-diary-" + str(n))
        query = "compact=1&limit=2&view=archive&archive_kind=diary"
        first = self.ok(await self.request(query=query))
        self.assertEqual(len(first["journals"]), 2)
        self.assertEqual(first["next_date"], (NOW.date() - timedelta(days=1)).isoformat())
        second = self.ok(await self.request(query=query + "&before=" + first["next_date"]))
        self.assertEqual(len(second["journals"]), 1)
        self.assertIsNone(second["next_date"])
        self.assertEqual(first["interests"], [])

    async def test_compact_query_validation(self):
        page = await self.setup_page()
        for query in ("limit=0", "limit=51", "compact=0", "view=invalid", "note_cursor=not-json", "interest_id=bad", "limit=2&limit=3"):
            self.assertEqual((await self.call(page.life, query=query))["message"], "life_query_invalid")
