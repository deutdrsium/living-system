"""旧版配置里的「用户档案」提示词要能迁进 V2 关系档案。

用户库只有昵称/关系等级/备注；性别、生日、职业、相处方式只存在这份提示词里，
不读它就永远是空的。
"""

from __future__ import annotations

import unittest

from companion_v2.legacy.relationship_profiles import config_relationship_profile
from companion_v2.relationships.profile import FIELD_LIMITS

PROMPT = """对用户的称呼：测试用户
用户性别：男
用户生日：2000-01-01
用户职业：开发者
是角色的XX：在示例图书馆一起工作的同事。
与角色的相处方式：轮流整理图书，有问题时一起核对。
其他补充信息：值班交接时要检查归还清单。
"""


class ConfigRelationshipProfileTests(unittest.TestCase):
    def test_prompt_fields_map_to_the_v2_profile(self):
        seed = config_relationship_profile(PROMPT, primary_user_id="10100")
        self.assertEqual(seed["user_id"], "10100")
        self.assertEqual(seed["fields"], {
            "default_nickname": "测试用户",
            "user_gender": "男",
            "user_birthday": "2000-01-01",
            "user_occupation": "开发者",
            "role_relation": "在示例图书馆一起工作的同事。",
            "interaction": "轮流整理图书，有问题时一起核对。",
            "extra": "值班交接时要检查归还清单。",
        })
        self.assertTrue(any("roleplay_user_profile_prompt" in item for item in seed["source_fields"]))

    def test_continuation_lines_belong_to_the_previous_field(self):
        seed = config_relationship_profile("与角色的相处方式：第一行\n第二行\n用户职业：开发者\n")
        self.assertEqual(seed["fields"]["interaction"], "第一行\n第二行")
        self.assertEqual(seed["fields"]["user_occupation"], "开发者")

    def test_overlong_field_is_dropped_not_truncated(self):
        long_gender = "男" * (FIELD_LIMITS["user_gender"] + 1)
        seed = config_relationship_profile(f"用户性别：{long_gender}\n用户职业：开发者\n")
        self.assertNotIn("user_gender", seed["fields"])
        self.assertIn("user_gender", seed["dropped"])
        self.assertEqual(seed["fields"]["user_occupation"], "开发者")

    def test_empty_or_unknown_text_yields_nothing(self):
        self.assertIsNone(config_relationship_profile(""))
        self.assertIsNone(config_relationship_profile("这里没有任何已知标签"))
        self.assertIsNone(config_relationship_profile(None))

    def test_fullwidth_and_halfwidth_colons_both_work(self):
        seed = config_relationship_profile("用户性别: 女\n用户职业：学生\n")
        self.assertEqual(seed["fields"]["user_gender"], "女")
        self.assertEqual(seed["fields"]["user_occupation"], "学生")


if __name__ == "__main__":
    unittest.main()
