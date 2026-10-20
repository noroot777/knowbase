"""Sibling responses can recover one navigation; explicit DOM errors stay actionable."""
import contextlib
import json
import time
import unittest
from unittest.mock import patch

from kb.feeds import x_browser, x_channels, x_page_state, x_post
from tests.test_x_channels import ChannelBase
from tests.test_x_page_state import EventSession, response
from tests.test_x_post import BODY, URL, snapshot
from tests.test_x_timeline_verification import state


class StateSession(EventSession):
    def __init__(self, events, **changes):
        super().__init__(events)
        self.snapshot = state() | snapshot(BODY) | changes

    def open_page(self, url):
        return self._sid

    def _post(self, message, deadline):
        if message['method'] == 'Runtime.evaluate':
            self.reads += 1
            self.incoming.append(dict(id=message['id'], result=dict(
                result=dict(value=json.dumps(self.snapshot)))))
        else:
            super()._post(message, deadline)


class HttpRecoveryTests(unittest.TestCase):
    def responses(self):
        responses = x_page_state.Responses('owned')
        responses.reset(URL)
        responses.loader = 'current'
        return responses

    def test_a_later_sibling_success_cannot_retire_core_failures_in_any_page_family(self):
        for url, failed, succeeded in [(URL, 'TweetDetail', 'TweetResultByRestId'),
                ('https://x.com/home', 'HomeTimeline', 'HomeLatestTimeline'),
                ('https://x.com/Example', 'UserTweets', 'UserByScreenName')]:
            for code in (404, 429, 503):
                for success in (200, 206, 299):
                    with self.subTest(url=url, code=code, success=success):
                        responses = self.responses()
                        responses.reset(url)
                        responses.loader = 'current'
                        responses.receive(response(code, operation=failed))
                        self.assertTrue(x_page_state.problem(responses.apply(state(ready='loading'))))
                        responses.receive(response(success, operation=succeeded))
                        self.assertEqual('limited' if code == 429 else 'failed',
                                         x_page_state.problem(responses.apply(state(ready='loading'))))

    def test_foreign_old_loader_and_untracked_successes_cannot_retire_current_failure(self):
        for changes in [dict(loaderId='old'), dict(sessionId='other'), dict(type='Image'),
                        dict(operation='UserTweets'),
                        dict(url='https://foreign.invalid/i/api/graphql/hash/TweetDetail')]:
            with self.subTest(changes=changes):
                responses = self.responses()
                responses.receive(response(503))
                responses.receive(response(200, **changes))
                self.assertEqual('failed', x_page_state.problem(responses.apply(state())))

    def test_a_new_failure_still_counts_until_a_later_success_or_navigation(self):
        responses = self.responses()
        responses.receive(response(200))
        responses.receive(response(429))
        self.assertEqual('limited', x_page_state.problem(responses.apply(state())))
        responses.receive(response(200))
        self.assertEqual('', x_page_state.problem(responses.apply(state())))
        responses.receive(response(503))
        responses.reset(URL)
        responses.loader = 'next'
        self.assertEqual('', x_page_state.problem(responses.apply(state())))

    def test_detail_recovers_after_a_sibling_failure_and_still_needs_stable_text(self):
        for code in (404, 429, 503):
            page = StateSession([response(code, operation='TweetResultByRestId'), response(200)])
            with self.subTest(code=code), patch.object(x_post, 'STEP', 0):
                self.assertEqual(BODY, x_post.fetch(URL, page))
            self.assertEqual(2, page.reads)

    def test_unrendered_detail_failure_cannot_be_cancelled_by_a_sibling_success(self):
        for code, exception in [(429, x_post.XRateLimited), (503, x_post.PostRequestFailed)]:
            page = StateSession([response(code), response(200, operation='TweetResultByRestId')],
                                text='', id='', ready='loading')
            with self.subTest(code=code), patch.object(x_post, 'STEP', 0), \
                    patch.object(x_post, 'SECONDS', 0.01), self.assertRaises(exception):
                x_post.fetch(URL, page, deadline=time.monotonic() + 1)
            if code == 429:
                self.assertEqual(2, page.reads)
            else:
                self.assertGreaterEqual(page.reads, 2)

    def test_only_auxiliary_timeline_failures_lose_after_the_login_check(self):
        for home in (False, True):
            page = StateSession([response(503, operation='HomeTimeline' if home else 'UserByScreenName')],
                                articles=18)
            x_page_state.navigate(page, 'https://x.com/home' if home else 'https://x.com/Example')
            with self.subTest(home=home), patch.object(x_browser.time, 'sleep'):
                if home:
                    self.assertEqual('failed', x_browser.verify_timeline(page, timeout=0.01)[0])
                else:
                    self.assertEqual(18, x_browser._wait_state(page, 'Example', time.monotonic() + 1)['articles'])
            if home:
                self.assertGreater(page.reads, 2)
            else:
                self.assertEqual(1, page.reads)
        page = StateSession([], articles=18, login=True)
        self.assertEqual('login', x_browser.verify_timeline(page)[0])

    def test_loading_dom_errors_reach_detail_and_collection_after_two_observations(self):
        for signal, detail_error, account_error in [('failed', x_post.PostRequestFailed, x_browser.XBrowserError),
                                                    ('limited', x_post.XRateLimited, x_browser.XRateLimited)]:
            for detail in (False, True):
                page = StateSession([], ready='loading', articles=0, text='', id='',
                                    dom_problem=signal, **{signal: True})
                with self.subTest(signal=signal, detail=detail), patch.object(x_post, 'STEP', 0), \
                        patch.object(x_browser.time, 'sleep'), \
                        self.assertRaises(detail_error if detail else account_error):
                    if detail:
                        x_post.fetch(URL, page, deadline=time.monotonic() + 1)
                    else:
                        x_browser._wait_state(page, 'Example', time.monotonic() + 1)
                self.assertEqual(2, page.reads)


class CheckConnectionRecoveryTests(ChannelBase):
    def test_real_route_reports_core_retry_recovery_and_loading_dom_causes(self):
        self.settings(x_channels.BROWSER)
        cookies = x_channels.BROWSER_PROFILE / 'Default' / 'Cookies'
        cookies.parent.mkdir(parents=True)
        cookies.touch()
        for changes, events, ready, phrase in [
                (dict(articles=18), [response(503, operation='HomeTimeline'),
                                    response(200, operation='HomeLatestTimeline')], False, '请求出错'),
                (dict(articles=18), [response(503, operation='HomeTimeline')], False, '请求出错'),
                (dict(articles=18), [response(503, operation='HomeTimeline'),
                                    response(200, operation='HomeTimeline')], True, '登录态有效'),
                (dict(ready='loading', limited=True, dom_problem='limited'), [], False, '限制了访问频率'),
                (dict(ready='loading', failed=True, dom_problem='failed'), [], False, '请求出错')]:
            page = StateSession(events, **changes)
            with self.subTest(changes=changes, events=events), \
                    patch.object(x_browser, 'profile_busy', return_value=False), \
                    patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                    patch.object(x_browser, 'ACCOUNT_SECONDS', 0.01), \
                    patch.object(x_browser.time, 'sleep'):
                result = self.x_act('/api/intake/social/x-channel/check')
            self.assertEqual(200, result.status_code)
            self.assertEqual(ready, result.json()['ready'])
            self.assertIn(phrase, result.json()['detail'])
            self.assertNotIn('网络不通', result.json()['detail'])
            if ready:
                self.assertEqual(1, page.reads)
            elif events:
                self.assertGreater(page.reads, 2)
            else:
                self.assertEqual(2, page.reads)
