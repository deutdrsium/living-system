import json
from pathlib import Path
import unittest

from companion_v2.conversation.banned_phrases import BannedPhraseFilter, DEFAULT_PATTERNS


class BannedPhrasesTests(unittest.TestCase):
    def setUp(self):
        self.phrases = BannedPhraseFilter(DEFAULT_PATTERNS)

    def test_filters_default_phrases_and_repairs_adjacent_text(self):
        for original, expected in (
            ("慢吞吞地往下看", "往下看"),
            ("慢悠悠的对话", "对话"),
            ("正好慢吞吞洗漱吃个早饭", "正好洗漱吃个早饭"),
            ("想接着 慢慢悠悠地 往下看看", "想接着 往下看看"),
            ("，不紧不慢的日常感，慢吞吞，继续。", "日常感，继续。"),
            ("今天慢慢来，不着急。", "今天慢慢来，不着急。"),
        ):
            with self.subTest(original=original):
                self.assertEqual(self.phrases.text(original, surface="test"), expected)

    def test_drops_empty_segments_but_preserves_an_entire_reply(self):
        clean = self.phrases.segments
        self.assertEqual(clean(("慢吞吞的。", "？！", "慢悠悠地接着聊。", "好呀"), surface="test"),
                         ("", "", "接着聊。", "好呀"))
        original = ("慢吞吞。", "慢悠悠！")
        with self.assertLogs("companion_v2.conversation.banned_phrases") as records:
            self.assertEqual(clean(original, surface="test"), original)
        self.assertIn("surface=test pattern_index=0 count=1", records.output[0])
        self.assertIn("surface=test count=2", records.output[-1])
        self.assertNotIn("慢吞吞", "\n".join(records.output))

    def test_empty_list_disables_filter_and_custom_entries_use_regex(self):
        original = ("慢吞吞  。", "慢悠悠地看。")
        self.assertIs(BannedPhraseFilter([]).segments(original, surface="test"), original)
        self.assertEqual(BannedPhraseFilter([r"(?i)slow(?:ly)?"]).text("Read SLOWLY please.", surface="test"),
                         "Read please.")

    def test_schema_defaults_and_invalid_regex_at_config_load(self):
        from astrbot_host.multi_controller import AstrBotMultiController

        root = Path(__file__).resolve().parents[1]
        schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
        self.assertEqual(schema["banned_phrases"]["default"], list(DEFAULT_PATTERNS))
        with self.assertRaisesRegex(ValueError, r"banned_phrases\[1\] is an invalid regular expression"):
            AstrBotMultiController(object(), {"banned_phrases": ["有效", "["]}, root)
