"""把阅读器关闭契约接入统一验收回归，不需要浏览器或外网。"""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "需要 node 执行阅读器关闭契约回归")
class ReaderCloseTests(unittest.TestCase):
    def test_reader_close_contract(self):
        probe = Path(__file__).with_name("reader_close_test.mjs")
        result = subprocess.run([shutil.which("node"), "--test", str(probe)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
