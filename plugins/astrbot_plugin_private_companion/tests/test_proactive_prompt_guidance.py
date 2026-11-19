"""Casual one-liners stay eligible without weakening source or contact boundaries."""

import unittest

from companion_v2.proactive.persona_judge import INSTRUCTION as JUDGE_INSTRUCTION
from companion_v2.proactive.prompt_templates import EXPRESSION_INSTRUCTION
from companion_v2.proactive.prompts import (GREETING_INSTRUCTION, INSTRUCTION, INSOMNIA_GREETING_INSTRUCTION,
                                           ORIGIN_INSTRUCTIONS, PURPOSE_INSTRUCTIONS, VOICE_INSTRUCTION)
from companion_v2.proactive.staged_prompts import PLAN_INSTRUCTION, WRITE_INSTRUCTION


class ProactivePromptGuidanceTests(unittest.TestCase):
    def test_compose_and_staged_writing_allow_one_line_without_scene_setting(self):
        for prompt, cue in ((INSTRUCTION, "这样一句也可以"),
                            (EXPRESSION_INSTRUCTION, "半句或一行都行"),
                            (WRITE_INSTRUCTION, "半句或一行也行"),
                            (PLAN_INSTRUCTION, "一句问候、吐槽或轻问题也可以成为切口")):
            with self.subTest(cue=cue):
                self.assertIn(cue, prompt)
                for removed in ("带出一个具体画面", "手里的小物件", "生活切片", "正文要有具体内容"):
                    self.assertNotIn(removed, prompt)
        self.assertNotIn("眼前的小画面", PURPOSE_INSTRUCTIONS["chat"])

    def test_judge_does_not_score_for_length_or_richness(self):
        self.assertIn("不因短、没细节或没留问题而拦下或扣分", JUDGE_INSTRUCTION)
        self.assertIn("不因描写丰富而加分", JUDGE_INSTRUCTION)
        self.assertIn("明确要求停止、短时间连续追问同一邀约或重复索取评价时仍应拦下", JUDGE_INSTRUCTION)
        self.assertIn("必须有材料直接支持，否则 drop", JUDGE_INSTRUCTION)

    def test_compose_and_staged_prompts_keep_source_and_contact_boundaries(self):
        self.assertIn("事实只说材料明确支持的范围", INSTRUCTION)
        self.assertIn("计划不等于完成，作品、梦、联想不等于亲历", INSTRUCTION)
        self.assertIn("对方明确要求停止的事项仍须尊重", INSTRUCTION)
        self.assertIn("不把回复当成义务", INSTRUCTION)
        self.assertIn("用户已回答、收尾或要求停止的事项不重新追问", PLAN_INSTRUCTION)
        self.assertIn("brief 中没有来源支持的事实不得照写", WRITE_INSTRUCTION)
        self.assertIn("不暴露整篇或补写缺失细节", ORIGIN_INSTRUCTIONS["journal_event"])
        self.assertIn("接受后仍按现有能力与授权执行", PURPOSE_INSTRUCTIONS["invitation"])

    def test_meal_greeting_and_voice_do_not_require_extra_content(self):
        meal = PURPOSE_INSTRUCTIONS["meal"]
        self.assertIn("不用补食物细节", meal)
        self.assertIn("不作健康建议", meal)
        self.assertIn("用户已答过同一餐次时不要再换说法确认", meal)
        self.assertIn("顺口问候一句就行", GREETING_INSTRUCTION)
        self.assertIn("不证明对方刚醒", GREETING_INSTRUCTION)
        self.assertIn("不报时、不报日期和星期", INSOMNIA_GREETING_INSTRUCTION)
        self.assertIn("短句或半句也行", VOICE_INSTRUCTION)
        self.assertIn("只能表达正文里已有的意思", VOICE_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
