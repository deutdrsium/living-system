import base64
import io
import json
import unittest
import zipfile

from companion_v2.role_work.life.epub_import import parse_epub_upload
from companion_v2.execution.models import TaskError
from companion_v2.together.novel_library import _decode_text
from tests import test_role_life_api as life_api_test
from tests.test_role_life_runtime import LifeFixture


def make_epub(text="一小段测试正文。"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", '<container><rootfiles><rootfile full-path="book/package.opf"/></rootfiles></container>')
        archive.writestr("book/package.opf", '<package><metadata><title>测试书</title><creator>测试作者</creator></metadata><manifest><item id="c1" href="chapter.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>')
        archive.writestr("book/chapter.xhtml", '<html><body><h1>第一章</h1><p>' + text + '</p></body></html>')
    return base64.b64encode(output.getvalue()).decode("ascii")


class EpubLifeTests(LifeFixture, unittest.IsolatedAsyncioTestCase):
    async def test_epub_is_read_automatically_in_order(self):
        await self.setup_life()
        encoded = make_epub("甲" * 6000 + "乙" * 111)
        original = parse_epub_upload("book.epub", encoded)["text"]
        self.assertEqual(parse_epub_upload("book.epub", encoded)["author"], "测试作者")
        result = await self.command("add_epub", filename="book.epub", epub_base64=encoded)
        self.assertTrue(result.is_success, result)
        self.allow_reading_window()
        await self.configure(reading_enabled=True, reading_chunk_chars=6000)
        await self.until(lambda: sum(item["state"] == "completed" for item in self.status()["activities"]) == 1)
        self.advance(self.app, 20 * 60)
        await self.until(lambda: all(item["state"] == "completed" for item in self.status()["activities"]))
        inputs = [json.loads(envelope.original.context.materials[0].payload) for envelope in self.called
                  if envelope.original.output_format == "role_reading"]
        self.assertEqual("".join(item["text"] for item in sorted(inputs, key=lambda item: item["start"])), original)

    async def test_long_epub_is_one_ordered_original_and_can_be_read(self):
        await self.setup_life()
        encoded = make_epub("甲" * 270000 + "终点")
        parsed = parse_epub_upload("book.epub", encoded)
        request = dict(action="add_epub", operation_key="epub-test", view_token=self.status()["view_token"],
                       filename="book.epub", epub_base64=encoded)
        added = await self.manager.command(self.core, request)
        self.assertTrue(added.is_success, added)
        result = added.unwrap()
        activities = sorted(self.manager._activities(), key=lambda item: item.work.start_offset)
        self.assertEqual(result["title"], "测试书")
        self.assertEqual(result["total_segments"], (len(parsed["text"]) + 1499) // 1500)
        self.assertEqual(len(activities), 1)
        self.assertEqual(len({item.work.source.source_id for item in activities}), 1)
        self.assertEqual(activities[0].work.start_offset, 0)
        self.assertEqual(activities[0].work.end_offset, 6000)
        row = self.runtime.read_one("SELECT payload_json FROM sources WHERE source_id = ?",
                                    (activities[0].work.source.source_id,))
        self.assertEqual(json.loads(row["payload_json"]), parsed["text"])
        self.assertTrue((await self.manager.command(self.core, request)).unwrap()["replayed"])
        self.assertEqual(len(list(self.manager._activities())), 1)
        item = next(item for item in self.status()["activities"]
                    if item["activity_id"] == activities[0].activity_id)
        self.assertTrue((await self.command("read", item)).is_success)
        await self.settle()
        self.assertEqual(self.manager._activity(item["activity_id"]).state.value, "completed")
        self.assertEqual(len(self.called), 1)
        after = list(self.manager._activities())
        self.assertEqual(len(after), 2)
        next_part = next(a for a in after if a.state.value == "planned")
        self.assertEqual((next_part.work.start_offset, next_part.work.end_offset), (1500, 7500))

    async def test_authentication_and_corrupt_epub_leave_no_records(self):
        await self.setup_life()
        request = dict(action="add_epub", operation_key="bad", view_token=self.status()["view_token"],
                       filename="book.epub", epub_base64=base64.b64encode(b"not an epub").decode())
        self.assertFalse((await self.manager.command(self.important, request)).is_success)
        result = await self.manager.command(self.core, request)
        self.assertEqual(result.failure.code, "life_epub_invalid")
        self.assertEqual(list(self.manager._activities()), [])

    async def test_missing_epub_chapter_is_not_silently_dropped(self):
        original = base64.b64decode(make_epub())
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original)) as source, zipfile.ZipFile(output, "w") as target:
            for item in source.infolist():
                data = source.read(item.filename)
                if item.filename.endswith("package.opf"):
                    data = data.replace(b'</manifest>', b'<item id="c2" href="missing.xhtml" media-type="application/xhtml+xml"/></manifest>')
                    data = data.replace(b'</spine>', b'<itemref idref="c2"/></spine>')
                target.writestr(item.filename, data)
        with self.assertRaises(TaskError) as error:
            parse_epub_upload("book.epub", base64.b64encode(output.getvalue()).decode())
        self.assertEqual(error.exception.code, "life_epub_invalid")

    async def test_utf8_existing_replacement_character_does_not_drop_a_chapter(self):
        self.assertEqual(_decode_text("原文中已有�标记".encode("utf-8")), "原文中已有�标记")

    async def test_large_compilation_above_old_limit_is_accepted(self):
        encoded = make_epub("书" * 5_010_000)
        parsed = parse_epub_upload("large.epub", encoded)
        self.assertGreater(len(parsed["text"]), 5_000_000)


class EpubAPITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = life_api_test.LifeAPITests.asyncSetUp
    request = life_api_test.LifeAPITests.request

    async def test_upload_uses_authenticated_life_command(self):
        (await self.controller.initialize_profile()).unwrap()
        status = (await self.request())["data"]
        body = {"action": "add_epub", "operation_key": "epub-api", "view_token": status["view_token"],
                "filename": "book.epub", "epub_base64": make_epub()}
        denied = await self.request(username=None, data=body)
        self.assertEqual(denied["data"]["code"], "dashboard_identity_required")
        added = await self.request(data=body)
        self.assertEqual(added["status"], "ok", added)
        self.assertEqual(added["data"]["title"], "测试书")
        self.assertEqual(len((await self.request())["data"]["activities"]), 1)
