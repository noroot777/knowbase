# -*- coding: utf-8 -*-
"""安全验证页在**采集之外**那两个入口的结论:「检查连接」和导入登录态。

判据本体在 `kb/feeds/x_page_state.py`(`challenge` 字段),采集那一侧的中止由
`tests/test_x_access_gate.py` 钉。这一份只钉**另外两个消费口径**:同一份判据
不认它的后果不是文案难看,是两条链在验证页上**各自白等满超时**、然后报
`no-tweets` —— 而那一档的面板文案把人推去重新登录,登录一万次也过不了那张页。

**判据落在后果上**:问了几次(而不是"等了多久")、面板那句话指向哪一步。
**每条都反过来钉一边**:正常首页照旧 `ok`,原来那几档的文案一个字没被动。
"""
import contextlib
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.feeds import x_browser, x_login_words
from tests.x_state_fixture import page_state

_CHALLENGE_STATE = page_state(url='https://x.com/account/access', challenge=True)


class Page:
    """只认 `verify_timeline` 要的那一次 eval,并数它被问了几次。"""

    def __init__(self, state):
        self.state, self.evals = state, 0

    def eval(self, js, **kw):
        self.evals += 1
        return json.dumps(self.state)

    def open_page(self, url):
        pass

    def goto(self, url):
        pass


@contextlib.contextmanager
def _session(page):
    yield page


class ChallengeVerdictTests(unittest.TestCase):
    def test_it_reports_the_challenge_instead_of_waiting_out_the_timeout(self):
        """**判据是问了几次**:不认它的话要问满 timeout 才返回 `no-tweets`。"""
        page = Page(_CHALLENGE_STATE)
        verdict, _ = x_browser.verify_timeline(page, timeout=30)
        self.assertEqual('challenge', verdict)
        self.assertEqual(2, page.evals)

    def test_a_normal_timeline_is_still_ok(self):
        """反过来钉一边:正常首页照旧 `ok`,没被这一档抢走。"""
        page = Page(page_state(url='https://x.com/home', articles=5))
        self.assertEqual('ok', x_browser.verify_timeline(page, timeout=30)[0])

    def test_the_connection_check_does_not_blame_the_login(self):
        """「检查连接」那句话要指到过验证,不是指到重新登录。"""
        page = Page(_CHALLENGE_STATE)
        with patch.object(x_browser, 'profile_busy', lambda *a, **k: False), \
                patch.object(x_browser, 'has_login_footprint', return_value=True), \
                patch.object(x_browser, 'browser_session',
                             lambda *a, **k: _session(page)):
            ok, detail = x_browser.probe('/tmp/unused')
        self.assertFalse(ok)
        self.assertIn('安全验证', detail)
        self.assertIn('x.com/account/access', detail)
        self.assertNotIn('打开登录窗口', detail)

    def test_the_import_wording_has_its_own_line_for_it(self):
        """导入登录态那条链的结论码 → 人话,也要有这一档。"""
        tail = x_login_words._VERDICT_TAIL['challenge']
        self.assertIn('安全验证', tail)
        self.assertNotIn('打开登录窗口', tail)
        # 反过来钉一边:原来那几档一个字都没被动。
        self.assertIn('打开登录窗口', x_login_words._VERDICT_TAIL['login'])


if __name__ == '__main__':
    unittest.main()
