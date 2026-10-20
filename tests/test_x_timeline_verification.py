"""Connection checks and both import checks share prompt, cause-specific verdicts."""
import contextlib
import json
import tempfile
import time
import unittest
from unittest.mock import patch

from kb.feeds import x_browser, x_cdp, x_login_import, x_login_words, x_page_state
from tests.test_x_login_import import FakePage
from tests.test_x_page_state import EventSession, response
from tests.x_login_fixture import FakeChrome, logged_in
from tests import test_x_page_dom_evidence as dom_fixture


def state(**changes):
    return dict(ready='complete', articles=0, login=False, limited=False, failed=False,
                empty=False) | changes


class HomeSession(EventSession):
    def open_page(self, url):
        return self._sid

    def _post(self, message, deadline):
        if message['method'] == 'Runtime.evaluate':
            self.reads += 1
            self.incoming.append(dict(id=message['id'], result=dict(
                result=dict(value=json.dumps(state())))))
        else:
            super()._post(message, deadline)


class TimelineVerificationTests(unittest.TestCase):
    def setUp(self):
        footprint = patch.object(x_browser, 'has_login_footprint', return_value=True)
        footprint.start()
        self.addCleanup(footprint.stop)

    def test_shell_less_gateway_evidence_reaches_checks_and_batch_errors_promptly(self):
        for text, verdict in [('Rate limit exceeded', 'limited'),
                              ('Something went wrong. Try reloading', 'failed')]:
            snap = dom_fixture.DomEvidenceTests().snapshot(missing=True, body=text)
            with self.subTest(verdict=verdict), patch.object(x_browser.time, 'sleep'):
                page = FakePage(snap)
                self.assertEqual(verdict, x_browser.verify_timeline(page, timeout=0.02)[0])
                self.assertEqual(2, page.seen)
                page = FakePage(snap)
                error = x_browser.XRateLimited if verdict == 'limited' else x_browser.XBrowserError
                with self.assertRaises(error):
                    x_browser._wait_state(page, 'Example', time.monotonic() + 0.02)
                self.assertEqual(2, page.seen)

    def test_unknown_timeouts_do_not_diagnose_login_network_or_proxy_failure(self):
        for ready in ('loading', 'complete'):
            with self.subTest(ready=ready), patch.object(x_browser, 'ACCOUNT_SECONDS', 0.002), \
                    patch.object(x_browser.time, 'sleep'), patch.object(
                    x_browser, 'profile_busy', return_value=False), patch.object(
                    x_browser, 'browser_session', return_value=contextlib.nullcontext(FakePage(
                        dict(ready=ready)))):
                ok, detail = x_browser.probe('/unused')
                self.assertFalse(ok)
                self.assertIn('未能判出原因', detail)
                self.assertIn('检查连接', detail)
                for guess in ('失效', '网络', '代理'):
                    self.assertNotIn(guess, detail)
                with self.assertRaisesRegex(x_browser.XBrowserError, '未能判出原因'):
                    x_browser._wait_state(FakePage(dict(ready=ready)), 'Example',
                                          time.monotonic() + 0.002)
                verdict = 'not-loaded' if ready == 'loading' else 'no-tweets'
                for result, last in [(verdict, {}), ('unconfirmed', {'again': verdict})]:
                    detail = x_login_words._detail(result, 2, last, '')
                    self.assertIn('未能判出原因', detail)
                    self.assertNotIn('网络', detail)
                    self.assertNotIn('请求出错', detail)
                    self.assertNotIn('限流', detail)

    def test_error_and_limit_confirm_twice_but_expired_login_is_immediate(self):
        for flags, expected in [({'failed': True, 'dom_problem': 'failed'}, 'failed'),
                                ({'limited': True, 'dom_problem': 'limited'}, 'limited'),
                                ({'failed': True, 'limited': True, 'dom_problem': 'limited'}, 'limited'),
                                ({'login': True, 'failed': True}, 'login')]:
            with self.subTest(expected=expected), patch.object(x_browser.time, 'sleep') as sleep:
                page = FakePage(flags)
                verdict, last = x_browser.verify_timeline(page, timeout=50)
                self.assertEqual(expected, verdict)
                self.assertEqual(1 if expected == 'login' else 2, page.seen)
                self.assertEqual(0 if expected == 'login' else 1, sleep.call_count)
                self.assertEqual('complete', last['ready'])

    def test_unsettled_text_flags_do_not_preempt_a_successful_render(self):
        page = FakePage({'ready': 'loading', 'failed': True}, {'articles': 3})
        with patch.object(x_browser.time, 'sleep'):
            self.assertEqual('ok', x_browser.verify_timeline(page)[0])
        self.assertEqual(2, page.seen)

    def test_probe_uses_home_http_evidence_and_cause_specific_next_steps(self):
        for code, phrase in [(503, '请求出错'), (429, '限制了访问频率')]:
            with self.subTest(code=code):
                page = HomeSession([response(code, operation='HomeTimeline')])
                with patch.object(x_browser, 'profile_busy', return_value=False), patch.object(
                        x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                        patch.object(x_browser, 'ACCOUNT_SECONDS', 0.01), \
                        patch.object(x_browser.time, 'sleep'):
                    ok, detail = x_browser.probe('/unused')
                self.assertFalse(ok)
                self.assertIn(phrase, detail)
                self.assertNotIn('登录态已失效', detail)
                if code == 429:
                    self.assertEqual(2, page.reads)
                else:
                    self.assertGreater(page.reads, 2)

    def test_home_observation_ignores_noncore_foreign_and_old_navigation_responses(self):
        events = [response(operation='UserTweets'), response(operation='HomeTimeline', loaderId='old'),
                  response(operation='HomeLatestTimeline', sessionId='other'),
                  response(url='https://other.test/i/api/graphql/hash/HomeTimeline')]
        page = HomeSession(events)
        x_page_state.navigate(page, 'https://x.com/home')
        self.assertEqual('', x_page_state.problem(x_page_state.read(page, 'state')))

    def test_home_retry_can_clear_a_failed_http_response(self):
        page = HomeSession([response(503, operation='HomeLatestTimeline'),
                            response(200, operation='HomeLatestTimeline')])
        x_page_state.navigate(page, 'https://x.com/home')
        self.assertEqual('', x_page_state.problem(x_page_state.read(page, 'state')))

    def test_failed_probe_has_the_same_retry_advice_in_both_browser_channels(self):
        for attach in (False, True):
            page = FakePage({'failed': True, 'dom_problem': 'failed'})
            with self.subTest(attach=attach), patch.object(x_browser, 'profile_busy', return_value=False), \
                    patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                    patch('kb.feeds.cdp_attach.discover', return_value=object()):
                ok, detail = x_browser.probe('/unused', attach=attach)
            self.assertFalse(ok)
            self.assertIn('请求出错', detail)
            self.assertIn('检查连接', detail)
            self.assertIn('不必因此重新登录', detail)
            self.assertNotIn('登录态已失效', detail)

    def test_actual_first_and_reopened_import_checks_preserve_the_failure_reason(self):
        for first_ok in (False, True):
            for flag, verdict, phrase in [('failed', 'failed', '请求出错'),
                                          ('limited', 'limited', '限流'),
                                          ('login', 'login', '判成未登录')]:
                with self.subTest(first_ok=first_ok, flag=flag), tempfile.TemporaryDirectory() as profile:
                    chrome = FakeChrome()
                    reads = []

                    def evaluate(_script):
                        boot = len(chrome.opens)
                        reads.append(boot)
                        return json.dumps(state() | ({'articles': 2} if first_ok and boot == 1
                                                     else {flag: True, 'dom_problem': flag if flag != 'login' else ''}))

                    chrome.eval = evaluate
                    with patch.object(x_cdp, 'Chrome', chrome), patch.object(
                            x_browser.time, 'sleep'):
                        result, last = x_login_import.install(logged_in(), profile)
                    expected = ('relogin' if verdict == 'login' else 'unconfirmed') if first_ok else verdict
                    self.assertEqual(expected, result)
                    self.assertEqual(([1] if first_ok or flag == 'login' else [1, 1])
                                     + ([2] if flag == 'login' else [2, 2]), reads)
                    detail = x_login_words._detail(result, 2, last, '')
                    self.assertIn(phrase, detail)
                    self.assertNotIn('重新打开之后它们还留在', detail)
                    if flag == 'failed':
                        self.assertIn('不必因此重新登录', detail)
                        self.assertIn('检查连接', detail)
                    if flag == 'limited':
                        self.assertNotIn('或者首页没加载完', detail)
                        self.assertNotIn('请求出错、限流', detail)
                        self.assertIn('检查连接', detail)
                        if first_ok:
                            self.assertIn('重新打开之后那一次验证没跑成：', detail)
