"""Timeline checks and collection share success, scope, and terminal timing."""
import json
import time
import unittest
from unittest.mock import patch

from kb.feeds import x_browser, x_page_state, x_post
from tests.test_x_page_policy import Clock
from tests.test_x_page_state import EventSession, response
from tests.test_x_post import BODY, URL, snapshot
from tests.test_x_timeline_verification import state


class TimelineSession(EventSession):
    def __init__(self, operation, status, *, retry=0, rendered=False, login=False):
        super().__init__([response(status, operation=operation)])
        self.operation = operation
        self.retry = retry
        self.rendered = rendered
        self.login = login

    def _post(self, message, deadline):
        if message['method'] != 'Runtime.evaluate':
            return super()._post(message, deadline)
        self.reads += 1
        if self.reads == self.retry:
            self.incoming.append(response(200, operation=self.operation))
        ready = self.rendered or (self.retry and self.reads >= self.retry)
        snap = state(articles=18 if ready else 0, ready='complete' if ready else 'loading',
                     login=self.login)
        self.incoming.append(dict(id=message['id'], result=dict(
            result=dict(value=json.dumps(snap)))))


class TimelineOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for replacement in [patch.object(time, 'monotonic', self.clock.monotonic),
                            patch.object(time, 'sleep', self.clock.sleep)]:
            replacement.start()
            self.addCleanup(replacement.stop)

    def read_timeline(self, page, home):
        x_page_state.navigate(page, 'https://x.com/home' if home else 'https://x.com/Example')
        if home:
            return x_browser.verify_timeline(page, timeout=1)
        return 'ok', x_browser._wait_state(page, 'Example', self.clock.now + 1)

    def test_http_only_failure_can_retry_after_two_frames_in_both_timelines(self):
        for home, operation in [(True, 'HomeTimeline'), (False, 'UserTweets')]:
            for rendered in (False, True):
                with self.subTest(home=home, rendered=rendered):
                    page = TimelineSession(operation, 500, retry=4, rendered=rendered)
                    verdict, last = self.read_timeline(page, home)
                    self.assertEqual(('ok', 18), (verdict, last['articles']))
                    if not home and rendered:
                        self.assertIn('页面请求出错', last['incomplete'])
                        self.assertEqual(2, page.reads)
                    else:
                        self.assertNotIn('incomplete', last)
                        self.assertEqual(4, page.reads)

    def test_persistent_core_errors_veto_rendered_cards_in_both_timelines(self):
        for home, operation in [(True, 'HomeLatestTimeline'), (False, 'UserTweets')]:
            for status in (403, 500, 503):
                with self.subTest(home=home, status=status):
                    page = TimelineSession(operation, status, rendered=True)
                    started = self.clock.now
                    if home:
                        self.assertEqual('failed', self.read_timeline(page, home)[0])
                        self.assertGreaterEqual(self.clock.now - started, 1)
                        self.assertGreater(page.reads, 2)
                    else:
                        state = self.read_timeline(page, home)[1]
                        self.assertIn('页面请求出错', state['incomplete'])
                        self.assertNotEqual('ok', x_page_state.terminal(state, 'failed'))
                        self.assertLess(self.clock.now - started, 1)
                        self.assertEqual(2, page.reads)

    def test_core_rate_limit_is_prompt_but_auxiliary_errors_never_veto_content(self):
        for home in (False, True):
            for status in (429, 503):
                with self.subTest(home=home, status=status):
                    auxiliary = 'UserTweets' if home else 'UserByScreenName'
                    page = TimelineSession(auxiliary, status, rendered=True)
                    self.assertEqual('ok', self.read_timeline(page, home)[0])
                    self.assertEqual(1, page.reads)
            page = TimelineSession('HomeTimeline' if home else 'UserTweets', 429, rendered=True)
            if home:
                self.assertEqual('limited', self.read_timeline(page, home)[0])
            else:
                with self.assertRaises(x_browser.XRateLimited):
                    self.read_timeline(page, home)
            self.assertEqual(2, page.reads)

    def test_login_precedes_rendered_cards_and_http_failure(self):
        for home in (False, True):
            page = TimelineSession('HomeTimeline' if home else 'UserTweets', 503,
                                   rendered=True, login=True)
            if home:
                self.assertEqual('login', self.read_timeline(page, home)[0])
            else:
                with self.assertRaisesRegex(x_browser.XBrowserError, '登录态已失效'):
                    self.read_timeline(page, home)
            self.assertEqual(1, page.reads)

    def test_verified_stable_detail_text_still_precedes_core_http_errors(self):
        for status in (429, 503):
            page = EventSession([response(status)], available=True)
            with self.subTest(status=status):
                self.assertEqual(BODY, x_post.fetch(URL, page))
                self.assertEqual(2, page.reads)

    def test_shared_terminal_requires_confirmed_authoritative_errors_at_expiry(self):
        failure = state(ready='loading', http_problem='failed', http_only=True)
        self.assertEqual('', x_page_state.terminal(failure, '', expired=True))
        self.assertEqual('', x_page_state.terminal(failure, 'failed'))
        self.assertEqual('failed', x_page_state.terminal(failure, 'failed', expired=True))
        self.assertEqual('ok', x_page_state.terminal(state(articles=18, failed=True), ''))
        self.assertEqual('', x_page_state.terminal(snapshot(''), ''))
