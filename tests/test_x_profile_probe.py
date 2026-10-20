"""Local readiness facts must survive connection checks without guessing remote causes."""
import contextlib
import time
from unittest.mock import patch

from kb.feeds import x_browser, x_cdp, x_channels
from tests.test_x_channels import ChannelBase
from tests.test_x_login_import import FakePage
from tests.test_x_page_policy import Clock


class ProfileProbeTests(ChannelBase):
    def cookies(self):
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True, exist_ok=True)
        cookies.touch()
        return cookies

    def test_unused_profile_check_does_not_launch_chrome_or_replace_known_local_fact(self):
        self.settings(x_channels.BROWSER)
        self.assertFalse(x_cdp.has_login_footprint(x_channels.BROWSER_PROFILE))
        self.assertIn('还没有登录过 X', x_channels.readiness(self.settings(x_channels.BROWSER))[1])
        with patch.object(x_browser, 'browser_session') as browser, patch.object(
                x_browser.time, 'sleep') as sleep:
            result = self.x_act('/api/intake/social/x-channel/check')
        self.assertEqual(200, result.status_code)
        self.assertFalse(result.json()['ready'])
        detail = result.json()['detail']
        for phrase in ('还没有登录过 X', '打开登录窗口', '从日常浏览器导入登录态'):
            self.assertIn(phrase, detail)
        browser.assert_not_called()
        sleep.assert_not_called()
        self.assertFalse(x_cdp.has_login_footprint(x_channels.BROWSER_PROFILE))

    def test_readiness_and_probe_share_the_existing_cookies_footprint(self):
        self.settings(x_channels.BROWSER)
        self.cookies()
        self.assertTrue(x_channels.readiness(self.settings(x_channels.BROWSER))[0])
        page = FakePage(dict(articles=3))
        with patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)):
            ready, detail = x_browser.probe(str(x_channels.BROWSER_PROFILE))
        self.assertTrue(ready)
        self.assertIn('首页读到 3 条', detail)

    def test_unknown_remote_timeouts_keep_local_context_and_an_actionable_next_step(self):
        self.cookies()
        for attach in (False, True):
            for ready in ('loading', 'complete'):
                clock = Clock()
                page = FakePage(dict(ready=ready))
                with self.subTest(attach=attach, ready=ready), patch.object(
                        x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                        patch.object(time, 'monotonic', clock.monotonic), \
                        patch.object(time, 'sleep', clock.sleep), \
                        patch.object(x_browser, 'ACCOUNT_SECONDS', 1), \
                        patch('kb.feeds.cdp_attach.discover', return_value=object()):
                    valid, detail = x_browser.probe(str(x_channels.BROWSER_PROFILE), attach=attach)
                self.assertFalse(valid)
                self.assertIn('未能判出原因', detail)
                self.assertIn('检查连接', detail)
                self.assertIn('对应浏览器' if attach else '本地已有登录痕迹', detail)
                if not attach:
                    self.assertIn('打开登录窗口', detail)
                for guess in ('已失效', '网络', '代理'):
                    self.assertNotIn(guess, detail)

    def test_attach_does_not_require_a_standalone_profile_footprint(self):
        page = FakePage(dict(articles=2))
        with patch.object(x_browser, 'has_login_footprint', side_effect=AssertionError('standalone only')), \
                patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                patch('kb.feeds.cdp_attach.discover', return_value=object()):
            ready, detail = x_browser.probe(str(x_channels.BROWSER_PROFILE), attach=True)
        self.assertTrue(ready)
        self.assertIn('登录态有效', detail)
