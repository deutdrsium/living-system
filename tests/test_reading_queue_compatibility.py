from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]
from companion_v2.execution.models import encode
from companion_v2.foundation.refs import ScopeKind, ScopeRef, SourceRef, SubjectKind, SubjectRef, VersionRef
from companion_v2.role_work.life import reading_queue as original
from good_life import reading_queue as moved


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class ReadingQueueCompatibilityTests(unittest.TestCase):
    def test_original_queue_identity_and_unread_spans_match(self):
        scope = ScopeRef("installation", "platform", ScopeKind.PRIVATE, "native", "bot", "account", "conversation", "session")
        source = SourceRef("book", "reading", scope, VersionRef("原文", 1), NOW)
        context = SimpleNamespace(key="会话", scope=scope,
                                  role_binding=SimpleNamespace(role=SubjectRef("installation", SubjectKind.BOT, "role")))
        self.assertEqual(moved.queue_key(context.key, json.loads(encode(source))), original.queue_key(context.key, source))
        for text in ("", "  \n", "开头" + "字" * 7000, " " * 6000 + "𠀀续读", "\n" * 12000 + "结尾"):
            for start in (0, 2, 6000, len(text)):
                for enabled in (False, True):
                    with self.subTest(length=len(text), start=start, enabled=enabled):
                        ledger = SimpleNamespace(runtime=SimpleNamespace(read_one=lambda *_args: (1,) if enabled else None),
                                                 add_activity=lambda activity: activity)
                        before = original.queue_next(ledger, context, source, "hash", text, "书名", start, NOW)
                        after = moved.next_span(enabled=enabled, lineage_id=source.version.lineage_id,
                                                text=text, title="书名", start=start)
                        if before is None:
                            self.assertIsNone(after)
                        else:
                            self.assertEqual(after, {"activity_id": before.activity_id,
                                "start_offset": before.work.start_offset, "end_offset": before.work.end_offset,
                                "description": before.description})


if __name__ == "__main__":
    unittest.main()
