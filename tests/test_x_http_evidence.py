"""HTTP evidence is authoritative before DOM completion in every shared-state consumer."""
import json
import time
import unittest
from unittest.mock import patch

from kb.feeds import x_browser, x_page_state, x_post
from tests.test_x_page_state import EventSession, response
from tests.test_x_post import URL, snapshot


class LoadingSession(EventSession):
    def _post(self, message, deadline):
        if message['method'] == 'Runtime.evaluate':
            self.reads += 1
            state = dict(snapshot(''), id='', ready='loading', articles=0, limited=False,
                         failed=False, empty=False)
            self.incoming.append(dict(id=message['id'], result=dict(
                result=dict(value=json.dumps(state)))))
        else:
            super()._post(message, deadline)


class HttpEvidenceTests(unittest.TestCase):
    def test_http_conclusions_do_not_wait_for_dom_completion(self):
        for status, verdict in [(429, 'limited'), (403, 'failed'), (500, 'failed')]:
            responses = x_page_state.Responses('owned')
            responses.reset(URL)
            responses.loader = 'current'
            responses.receive(response(status))
            state = responses.apply(dict(ready='loading', limited=False, failed=False))
            with self.subTest(status=status):
                self.assertEqual(verdict, state['http_problem'])
                self.assertEqual(verdict, x_page_state.problem(state))
                self.assertEqual(verdict, x_page_state.problem(dict(ready='complete', http_problem=verdict)))

    def test_only_scoped_terminal_dom_evidence_bypasses_loading(self):
        for kind in ('limited', 'failed'):
            with self.subTest(kind=kind):
                self.assertEqual('', x_page_state.problem(dict(ready='loading', **{kind: True})))
                self.assertEqual(kind, x_page_state.problem(dict(ready='loading', dom_problem=kind)))
                self.assertEqual('', x_page_state.problem(dict(ready='complete', **{kind: True})))
                self.assertEqual('', x_page_state.problem(dict(ready='complete', dom_problem='', **{kind: True})))

    def test_connection_check_confirms_loading_http_failures_twice(self):
        for status, verdict in [(429, 'limited'), (503, 'failed')]:
            page = LoadingSession([response(status, operation='HomeTimeline')])
            x_page_state.navigate(page, 'https://x.com/home')
            with self.subTest(status=status), patch.object(x_browser.time, 'sleep') as sleep:
                self.assertEqual(verdict, x_browser.verify_timeline(page)[0])
            self.assertEqual(2, page.reads)
            sleep.assert_called_once()

    def test_timeline_retains_two_observations_but_does_not_wait_for_complete(self):
        for status, kind, message in [(429, x_browser.XRateLimited, '访问频率'),
                                      (503, x_browser.XBrowserError, '请求出错')]:
            page = LoadingSession([response(status, operation='UserTweets')])
            x_page_state.navigate(page, 'https://x.com/Example')
            with self.subTest(status=status), patch.object(x_browser.time, 'sleep'), \
                    self.assertRaisesRegex(kind, message):
                x_browser._wait_state(page, 'Example', time.monotonic() + 0.1)
            self.assertEqual(2, page.reads)

    def test_detail_retains_two_observations_and_page_identity_with_http_failures(self):
        for status, kind in [(429, x_post.XRateLimited), (503, x_post.PostRequestFailed)]:
            page = LoadingSession([response(status)])
            with self.subTest(status=status), patch.object(x_post, 'STEP', 0), \
                    self.assertRaises(kind):
                x_post.fetch(URL, page, deadline=time.monotonic() + 0.1)
            self.assertEqual(2, page.reads)

    def test_http_retry_and_new_loader_clear_the_authoritative_source(self):
        page = LoadingSession([response(429), response(200)])
        x_page_state.navigate(page, URL)
        self.assertEqual('', x_page_state.problem(x_page_state.read(page, 'state')))
        page.events = [response(503, loaderId='old')]
        x_page_state.navigate(page, URL)
        self.assertEqual('', x_page_state.problem(x_page_state.read(page, 'state')))
