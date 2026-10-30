"""浏览器补抓的**客户端**那几条 node 探针的入口(不起真浏览器,纯 `vm` 跑脚本)。

五条各钉一件事,都是实测过的事故形态:共用的 HTTP 错误解析、一次性反馈不被
2 秒轮询盖掉、准入判据一律问服务端(扩展自己不判)、网址那一栏人动过就不许被
填回去、浏览器停在另一个地址时那条出路开在待办行里。硬约束与事故形态见
`docs/decisions/browser-capture.md`。
"""

from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which('node'), '需要 node 检查浏览器补抓的客户端脚本')
class BrowserCaptureClientTests(unittest.TestCase):
    def probe(self, name: str) -> None:
        result = subprocess.run([shutil.which('node'), str(Path(__file__).with_name(name))],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_shared_feedback_contract(self):
        self.probe('browser_capture_feedback.mjs')

    def test_capture_action_feedback_survives_polling(self):
        self.probe('browser_capture_notices.mjs')

    def test_extension_asks_the_server_before_reading_a_page(self):
        self.probe('browser_capture_gate.mjs')

    def test_edited_address_is_not_overwritten_by_polling(self):
        self.probe('browser_capture_url_field.mjs')

    def test_page_the_browser_landed_on_can_be_adopted_in_one_click(self):
        self.probe('browser_capture_other_page.mjs')
