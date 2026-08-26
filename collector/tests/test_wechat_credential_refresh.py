from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from src.services.wechat_credential_refresh import (
    WeChatRefreshError,
    trigger_wechat_credential_refresh,
)


class WeChatCredentialRefreshTests(unittest.TestCase):
    @patch("src.services.wechat_credential_refresh.platform.system", return_value="Darwin")
    @patch("src.services.wechat_credential_refresh.subprocess.run")
    def test_account_name_is_passed_as_an_argument(self, run, _system) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "{}", "")

        result = trigger_wechat_credential_refresh('测试公众号 "安全"')

        command = run.call_args.args[0]
        self.assertEqual(command[-1], '测试公众号 "安全"')
        self.assertNotIn('测试公众号 "安全"', command[-2])
        self.assertTrue(result.started)

    @patch("src.services.wechat_credential_refresh.platform.system", return_value="Darwin")
    @patch("src.services.wechat_credential_refresh.subprocess.run")
    def test_accessibility_error_has_actionable_message(self, run, _system) -> None:
        run.return_value = subprocess.CompletedProcess(
            [], 1, "", "Error: ACCESSIBILITY_DISABLED"
        )

        with self.assertRaisesRegex(WeChatRefreshError, "辅助功能"):
            trigger_wechat_credential_refresh("测试公众号")

    def test_rejects_control_characters(self) -> None:
        with self.assertRaisesRegex(WeChatRefreshError, "格式"):
            trigger_wechat_credential_refresh("测试\n公众号")


if __name__ == "__main__":
    unittest.main()
