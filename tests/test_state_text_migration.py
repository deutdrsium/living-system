import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "plugins" / "astrbot_plugin_private_companion"),
                str(ROOT / "plugins" / "astrbot_plugin_good_life")]

from companion_v2.conversation.state_context import ConversationStateContext
from companion_v2.role_work.state import maintenance
from good_life.state_records import StagedStateRecords


class StateTextMigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.provider = StagedStateRecords(self.db)

    def test_supervised_state_task_keeps_its_original_instruction(self):
        mood = {"axes": True, "gate": "open", "reference": {"joy": 6, "anger": 1, "sorrow": 1, "fear": 1},
                "allowed": {name: [1, 10] for name in ("joy", "anger", "sorrow", "fear")},
                "baseline": 6, "interest": 1}
        settings = {"cycle_enabled": True, "mood_enabled": True, "mood_decay_half_life_minutes": 90,
                    "mood_neutral_band": 3, "mood_cross_window": True, "energy_expression_prompt": "平稳"}
        expected = (maintenance.PROMPT + maintenance.CYCLE_INSTRUCTION + maintenance.CYCLE_MAINTENANCE
            + "".join(maintenance.PHASE_HINTS.values()) + maintenance._expression_preferences(settings)
            + maintenance._feature_instruction(settings)
            + maintenance._mood_instruction(enabled=True, axes=True, gate="open", reference=mood["reference"],
                allowed=mood["allowed"], baseline=6, interest=1, half_life=90, band=3, cross_window=True)
            + maintenance._skills_instruction(refs=True, partial=False) + maintenance.STATE_INTERVENTION_INSTRUCTION)
        actual = self.provider.dispatch("maintenance_instruction", owner_key="role", settings=settings, mood_round=mood,
            has_growth_refs=True, skills_partial=False, intervention=True)
        self.assertEqual(actual, expected)
        self.assertIn("busy 和 mood.description 用简短的日常措辞", actual)
        self.assertIn("避免沿用自己在旧状态或对话里反复使用的自我形容", actual)

    def test_chat_state_text_is_unchanged_through_the_life_provider(self):
        simulation = {"values": {"energy": 33, "hunger": "有点饿", "mood": None},
                      "sleep_execution": {"state": "interrupted", "early_interruptions": 1}}
        materials = [SimpleNamespace(purpose="role_state", payload=json.dumps({"simulation": simulation})),
                     SimpleNamespace(purpose="role_state_intensity", payload=json.dumps({"intensity": 70}))]
        provider = self.provider
        class Records:
            def reply_blocks(self, context, *, simulation, intensity):
                return provider.dispatch("reply_blocks", owner_key="role", simulation=simulation, intensity=intensity)
        expected = ConversationStateContext.instruction(materials)
        actual = ConversationStateContext.instruction(materials, life_records=Records(), context=object())
        self.assertEqual(actual, expected)
        self.assertIn("精力偏低", actual)


if __name__ == "__main__":
    unittest.main()
