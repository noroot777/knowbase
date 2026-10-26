from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), '需要 node 检查浏览器补抓共用错误反馈')
class BrowserCaptureFeedbackTests(unittest.TestCase):
    def test_shared_feedback_contract(self):
        probe = Path(__file__).with_name('browser_capture_feedback.mjs')
        result = subprocess.run([shutil.which('node'), str(probe)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
