"""HTTP evidence must retain its scope at timeout and preserve known DOM terminal states."""
import contextlib
import json
import time
import unittest
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels, x_page_state, x_post
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_page_state import EventSession, response
from tests.test_x_post import BODY, OLD, URL


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class PolicySession(EventSession):
    def __init__(self, cases, *, detail=False):
        super().__init__([])
        self.cases = {handle.lower(): case for handle, case in cases.items()}
        self.detail = detail
        self.handle = ''
        self.urls = []
        self.state_reads = {}

    def open_page(self, url):
        pass

    def _post(self, message, deadline):
        if message['method'] == 'Page.navigate':
            url = message['params']['url']
            self.urls.append(url)
            self.handle = url.split('/')[3]
            self.events = self.cases.get(self.handle.lower(), ([], {}))[0]
        super()._post(message, deadline)
        if message['method'] != 'Runtime.evaluate':
            return
        result = self.incoming[-1]['result']['result']
        script = message['params']['expression']
        dom = self.cases.get(self.handle.lower(), ([], {}))[1]
        if script in {x_browser._STATE_JS, x_post.READ_JS}:
            self.state_reads[self.handle] = self.state_reads.get(self.handle, 0) + 1
            state = dict(json.loads(result['value']), url=self.urls[-1], articles=1)
            if self.detail:
                state.update(id='', text='', articles=0)
            state.update(dom)
            result['value'] = json.dumps(state)
        elif script == x_browser._COLLECT_JS:
            items = [tweet('123', handle=self.handle, text=BODY)] + [
                tweet(str(identity), handle=self.handle, when='2026-09-01T00:00:00Z')
                for identity in range(3)]
            result['value'] = json.dumps(dict(articles=4, parsed=4, items=items))


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        for replacement in [patch.object(time, 'monotonic', self.clock.monotonic),
                            patch.object(time, 'sleep', self.clock.sleep),
                            patch.object(x_post, 'SECONDS', 1.0)]:
            replacement.start()
            self.addCleanup(replacement.stop)

    def timeline(self, events, dom=None):
        session = PolicySession({'Example': (events, dom or {})})
        x_page_state.navigate(session, 'https://x.com/Example')
        return session

    def test_persistent_auxiliary_errors_expire_as_account_failures_not_batch_limits(self):
        for status in [429, 403, 404, 503]:
            with self.subTest(status=status):
                session = self.timeline([response(status, operation='UserByScreenName')])
                started = self.clock.now
                with self.assertRaisesRegex(x_browser.XBrowserError, '页面请求出错') as caught:
                    x_browser._wait_state(session, 'Example', started + 1)
                self.assertNotIsInstance(caught.exception, x_browser.XRateLimited)
                self.assertGreaterEqual(self.clock.now - started, 1)
                self.assertGreaterEqual(session.reads, 2)

    def test_known_terminal_dom_wins_over_http_errors_without_waiting_the_account_budget(self):
        for operation in ['UserByScreenName', 'UserTweets']:
            for status in [429, 403, 404, 503]:
                with self.subTest(operation=operation, status=status):
                    session = self.timeline([response(status, operation=operation)],
                                            dict(empty=True, articles=0))
                    started = self.clock.now
                    with self.assertRaisesRegex(x_browser.XBrowserError, '账号在 X 上打不开'):
                        x_browser._wait_state(session, 'Example', started + 1)
                    self.assertLess(self.clock.now - started, 1)

    def test_auxiliary_429_cannot_turn_a_dom_request_error_into_a_batch_limit(self):
        session = self.timeline([response(operation='UserByScreenName')],
                                dict(failed=True, articles=0))
        with self.assertRaisesRegex(x_browser.XBrowserError, '页面请求出错') as caught:
            x_browser._wait_state(session, 'Example', self.clock.now + 1)
        self.assertNotIsInstance(caught.exception, x_browser.XRateLimited)
        self.assertEqual(2, session.reads)
        self.assertLess(self.clock.now, 1)

    def test_core_429_and_dom_limits_or_gates_still_stop_after_two_observations(self):
        cases = [([response(operation='UserTweets')], {}, x_browser.XRateLimited),
                 ([], dict(limited=True), x_browser.XRateLimited),
                 ([response(503, operation='UserByScreenName')],
                  dict(challenge=True), x_browser.XAccessBlocked),
                 ([response(operation='UserTweets')],
                  dict(failed=True), x_browser.XRateLimited)]
        for events, dom, expected in cases:
            with self.subTest(events=events, dom=dom):
                session = self.timeline(events, dom)
                started = self.clock.now
                with self.assertRaises(x_browser.XBrowserError) as caught:
                    x_browser._wait_state(session, 'Example', started + 1)
                self.assertIsInstance(caught.exception, expected)
                self.assertEqual(2, session.reads)
                self.assertLess(self.clock.now - started, 1)

    def test_http_only_ordinary_detail_failure_waits_then_withholds_without_claiming_absence(self):
        for status in [403, 404, 503]:
            with self.subTest(status=status):
                session = PolicySession({'Example': ([response(status)], {})}, detail=True)
                started = self.clock.now
                with self.assertRaises((x_post.PostUnavailable, x_page_state.XBrowserError)) as caught:
                    x_post.fetch(URL, session)
                self.assertIsInstance(caught.exception, x_post.PostRequestFailed)
                self.assertIn('不能据此确认限流或推文不存在', str(caught.exception))
                self.assertGreaterEqual(self.clock.now - started, 1)

    def test_detail_terminal_dom_requires_two_matching_complete_observations(self):
        for status in [404, 429, 503]:
            for dom, expected in [(dict(empty=True), x_post.PostGone),
                                  (dict(empty=True, at='999'), x_post.PostUnavailable),
                                  (dict(empty=True, ready='loading'), x_post.PostUnavailable),
                                  (dict(empty=True, id='123'), x_post.PostUnavailable)]:
                with self.subTest(status=status, dom=dom):
                    session = PolicySession({'Example': ([response(status)], dom)}, detail=True)
                    with self.assertRaises((x_post.PostUnavailable, x_page_state.XBrowserError)) as caught:
                        x_post.fetch(URL, session)
                    self.assertIsInstance(caught.exception, expected)
                    if expected is not x_post.PostGone:
                        self.assertNotIsInstance(caught.exception, x_post.PostGone)
                    else:
                        self.assertEqual(2, session.reads)

    def test_both_readers_use_the_shared_escalation_for_polling_and_timeout(self):
        for detail in [False, True]:
            with self.subTest(detail=detail):
                operation = 'TweetDetail' if detail else 'UserByScreenName'
                session = PolicySession({'Example': ([response(503, operation=operation)], {})},
                                        detail=detail)
                with patch.object(x_page_state, 'escalate', wraps=x_page_state.escalate) as policy:
                    if detail:
                        with self.assertRaises(x_post.PostUnavailable) as caught:
                            x_post.fetch(URL, session)
                        self.assertIsInstance(caught.exception, x_post.PostRequestFailed)
                    else:
                        x_page_state.navigate(session, 'https://x.com/Example')
                        with self.assertRaises(x_browser.XBrowserError):
                            x_browser._wait_state(session, 'Example', self.clock.now + 1)
                self.assertTrue(any(call.kwargs.get('expired') for call in policy.call_args_list))
                self.assertTrue(any(not call.kwargs.get('expired') for call in policy.call_args_list))

    def test_shared_policy_keeps_the_same_batch_scope_at_timeout(self):
        cases = [(dict(limited=True, http_only=True), None, None),
                 (dict(failed=True, http_only=True), None, None),
                 (dict(limited=True, http_only=True, http_content_limited=True),
                  x_page_state.XRateLimited, x_page_state.XRateLimited),
                 (dict(limited=True), x_page_state.XRateLimited, x_page_state.XRateLimited),
                 (dict(challenge=True, http_only=True),
                  x_page_state.XAccessBlocked, x_page_state.XAccessBlocked)]
        failure = x_post.PostRequestFailed('单条请求失败')
        for flags, polling, expired in cases:
            state = dict(ready='complete', **flags)
            kind = x_page_state.problem(state)
            with self.subTest(flags=flags):
                self.assertIsNone(x_page_state.escalate(state, '', failure, expired=True))
                early = x_page_state.escalate(state, kind, failure)
                late = x_page_state.escalate(state, kind, failure, expired=True)
                if polling is None:
                    self.assertIsNone(early)
                    self.assertIs(failure, late)
                else:
                    self.assertIsInstance(early, polling)
                    self.assertIsInstance(late, expired)


class StoredPolicyTests(ChannelBase):
    def setUp(self):
        super().setUp()
        self.sources = [self.sub('Example'), self.sub('Other')]

    def run_batch(self, channel, cases):
        sources = self.sources
        for source in sources:
            x_channels.save_watermark(source['id'], channel, OLD)
        sources = [reg.subscription('social', source['id']) for source in sources]
        session = PolicySession(cases)
        clock = Clock()
        settings = self.settings(channel)
        with patch.object(x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(session)), \
             patch.object(x_browser, 'ACCOUNT_SECONDS', 1.0), \
             patch.object(time, 'monotonic', clock.monotonic), \
             patch.object(time, 'sleep', clock.sleep):
            x_channels.prefetch(sources, settings)
        for source in sources:
            try:
                result = x_channels.take(source, settings)
            except ValueError as exc:
                sync._save_failure(source, exc, dict(errors=[], failed_ids=[]))
            else:
                sync._save_result(source, result, FetchScope())
        saved = [reg.subscription('social', source['id']) for source in sources]
        with reg.db() as conn:
            urls = {row['url'] for row in conn.execute('SELECT url FROM feed_items')}
        return saved, urls, session

    def test_account_failure_keeps_its_watermark_but_later_account_is_saved_and_advances(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for status in [429, 503]:
                with self.subTest(channel=channel, status=status):
                    cases = {'Example': ([response(status, operation='UserByScreenName')], {})}
                    saved, urls, session = self.run_batch(channel, cases)
                    self.assertIn('页面请求出错', saved[0]['last_error'])
                    self.assertNotIn('限制', saved[0]['last_error'])
                    self.assertEqual(OLD, x_channels.watermark(saved[0], channel))
                    self.assertEqual('', saved[1]['last_error'])
                    self.assertNotEqual(OLD, x_channels.watermark(saved[1], channel))
                    self.assertEqual({'https://x.com/other/status/123'}, urls)
                    self.assertEqual(['https://x.com/example', 'https://x.com/other'], session.urls)

    def test_dom_terminal_reason_survives_http_while_the_next_account_still_saves(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            with self.subTest(channel=channel):
                cases = {'Example': ([response(404, operation='UserByScreenName')],
                                     dict(empty=True, articles=0))}
                saved, urls, session = self.run_batch(channel, cases)
                self.assertIn('账号在 X 上打不开', saved[0]['last_error'])
                self.assertEqual(OLD, x_channels.watermark(saved[0], channel))
                self.assertEqual('', saved[1]['last_error'])
                self.assertNotEqual(OLD, x_channels.watermark(saved[1], channel))
                self.assertEqual({'https://x.com/other/status/123'}, urls)
                self.assertEqual(1, session.state_reads['example'])

    def test_core_limits_still_keep_the_remaining_batch_unwritten(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            with self.subTest(channel=channel):
                cases = {'Example': ([response(operation='UserTweets')], {})}
                saved, urls, session = self.run_batch(channel, cases)
                self.assertEqual(set(), urls)
                self.assertEqual(['https://x.com/example'], session.urls)
                for source in saved:
                    self.assertIn('限制了访问频率', source['last_error'])
                    self.assertEqual(OLD, x_channels.watermark(source, channel))

    def test_retry_after_an_auxiliary_error_recovers_the_withheld_account(self):
        channel = x_channels.BROWSER
        cases = {'Example': ([response(operation='UserByScreenName')], {})}
        saved, urls, session = self.run_batch(channel, cases)
        self.assertIn('页面请求出错', saved[0]['last_error'])
        self.assertNotIn('https://x.com/example/status/123', urls)
        saved, urls, session = self.run_batch(channel, {})
        self.assertEqual({'https://x.com/example/status/123', 'https://x.com/other/status/123'}, urls)
        for source in saved:
            self.assertEqual('', source['last_error'])
            self.assertNotEqual(OLD, x_channels.watermark(source, channel))
