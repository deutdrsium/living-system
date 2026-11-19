"""Run the good_life wallet page JS tests (tests/good_life_page/*.test.cjs) under Node."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node is not installed")
class GoodLifeWalletPageTest(unittest.TestCase):
    def test_wallet_view(self):
        tests = sorted(str(path) for path in (ROOT / "tests" / "good_life_page").glob("*.test.cjs"))
        self.assertTrue(tests)
        result = subprocess.run([NODE, "--test", *tests], cwd=ROOT, capture_output=True, text=True,
                                encoding="utf-8", timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
