"""Lifecycle role/target ids accept the Chinese names the page actually selects."""

import unittest

from tests import astrbot_data_root  # noqa: F401 - isolate the SDK before import
from astrbot_host.integration_api import _lifecycle_identifier


class LifecycleIdentifierTests(unittest.TestCase):
    def test_chinese_role_ids_are_valid(self):
        self.assertTrue(_lifecycle_identifier("晨星"))
        self.assertTrue(_lifecycle_identifier("晨星-私聊"))
        self.assertTrue(_lifecycle_identifier("target-0721"))

    def test_blank_or_control_ids_are_rejected(self):
        for value in ("", " 晨星", "晨星 ", "star\nlight", None, 42):
            self.assertFalse(_lifecycle_identifier(value), repr(value))


if __name__ == "__main__":
    unittest.main()
