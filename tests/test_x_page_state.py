"""HTTP status evidence is scoped to the current page's own core X requests."""
import copy
import json
import unittest
from collections import deque
from unittest.mock import patch

from kb.feeds import x_browser, x_page_state, x_post
from kb.feeds.x_cdp import Session
from tests.test_x_post import BODY, URL, snapshot


def response(status=429, operation='TweetDetail', **changes):
    event = dict(method='Network.responseReceived', sessionId='owned', params=dict(
        loaderId='current', type='Fetch', response=dict(status=status,
        url=f'https://x.com/i/api/graphql/hash/{operation}?variables=value')))
    for key, value in changes.items():
        if key in {'url', 'status'}:
            event['params']['response'][key] = value
        elif key == 'sessionId':
            event[key] = value
        else:
            event['params'][key] = value
    return event


class EventSession(Session):
    def __init__(self, events, available=False):
        super().__init__()
        self._sid = 'owned'
        self.incoming = deque()
        self.events = events
        self.reads = 0
        self.available = available
        self._navigation_count = 0

    def _post(self, message, deadline):
        result = {}
        if message['method'] == 'Page.navigate':
            self._navigation_count += 1
            loader = 'current' if self._navigation_count == 1 else f'page-{self._navigation_count}'
            for event in copy.deepcopy(self.events):
                if event.get('params', {}).get('loaderId') == 'current':
                    event['params']['loaderId'] = loader
                self.incoming.append(event)
            result = dict(loaderId=loader)
        if message['method'] == 'Runtime.evaluate':
            self.reads += 1
            result = dict(result=dict(value=json.dumps(dict(
                snapshot(BODY if self.available else ''), id='123' if self.available else '',
                articles=int(self.available), ready='complete', limited=False,
                failed=False, empty=False))))
        self.incoming.append(dict(id=message['id'], result=result))

    def _take(self, deadline):
        return self.incoming.popleft()


class ResponseTests(unittest.TestCase):
    def state(self, url=URL):
        state = x_page_state.Responses('owned')
        state.reset(url)
        state.loader = 'current'
        return state

    def test_http_429_during_navigation_is_retained_and_aborts_after_two_observations(self):
        session = EventSession([response()])
        with patch.object(x_post, 'STEP', 0), patch.object(x_post, 'SECONDS', 0.01), \
             self.assertRaises(x_post.XRateLimited):
            x_post.fetch(URL, session)
        self.assertGreaterEqual(session.reads, 2)

    def test_timeline_core_http_429_reaches_the_shared_state(self):
        session = EventSession([response(operation='UserTweets')])
        x_page_state.navigate(session, 'https://x.com/Example')
        state = x_page_state.read(session, x_browser._STATE_JS)
        self.assertTrue(state['limited'])
        self.assertEqual('limited', x_page_state.problem(state))

    def test_content_and_auxiliary_statuses_are_separate_and_only_content_can_veto(self):
        routes = [('https://x.com/Example', 'UserTweets', 'UserByScreenName'),
                  ('https://x.com/Example', 'UserTweets', 'UserTweetsAndReplies'),
                  (URL, 'TweetDetail', 'TweetResultByRestId')]
        for url, content, auxiliary in routes:
            for content_status, auxiliary_status, expected in [
                    (200, 429, ''), (200, 503, ''), (503, 429, 'failed'),
                    (503, 200, 'failed'), (429, 200, 'limited'), (429, 503, 'limited')]:
                with self.subTest(content=content, auxiliary=auxiliary,
                                  statuses=(content_status, auxiliary_status)):
                    state = self.state(url)
                    state.receive(response(content_status, operation=content))
                    state.receive(response(auxiliary_status, operation=auxiliary))
                    snap = state.apply(dict(ready='complete', articles=1))
                    self.assertEqual({content: content_status}, snap.get('http_content'))
                    self.assertEqual({auxiliary: auxiliary_status}, snap.get('http_auxiliary'))
                    self.assertEqual(expected, x_page_state.problem(snap))
                    self.assertIs(content_status == 429, snap.get('http_content_limited', False))
                    self.assertIs(content_status == 429, snap.get('limited', False))
                    self.assertNotIn('empty', snap)

    def test_auxiliary_evidence_alone_does_not_create_any_error_or_terminal_flag(self):
        for url, operation in [('https://x.com/Example', 'UserByScreenName'),
                               ('https://x.com/Example', 'UserTweetsAndReplies'),
                               (URL, 'TweetResultByRestId')]:
            for status in [429, 403, 404, 503]:
                with self.subTest(operation=operation, status=status):
                    state = self.state(url)
                    state.receive(response(status, operation=operation))
                    self.assertEqual({'http_auxiliary': {operation: status}}, state.apply({}))

    def test_http_errors_are_not_permanent_absence_or_necessarily_rate_limits(self):
        for status in [400, 403, 404, 500, 503]:
            with self.subTest(status=status):
                state = self.state()
                state.receive(response(status))
                snap = state.apply(dict(ready='complete', limited=False))
                self.assertTrue(snap['failed'])
                self.assertNotIn('empty', snap)
                self.assertFalse(snap['limited'])
                self.assertEqual('failed', x_page_state.problem(snap))

    def test_http_errors_preserve_dom_evidence_but_veto_an_unavailable_conclusion(self):
        for status in [400, 403, 404, 429, 500, 503]:
            with self.subTest(status=status):
                state = self.state()
                state.receive(response(status))
                snap = state.apply(dict(ready='complete', empty=True, limited=False))
                self.assertTrue(snap['empty'])
                self.assertEqual('limited' if status == 429 else 'failed', x_page_state.problem(snap))
                self.assertFalse(x_page_state.unavailable(snap))

    def test_unrelated_origins_operations_sessions_loaders_and_resource_types_do_not_count(self):
        events = [response(url='https://unrelated.invalid/i/api/graphql/hash/TweetDetail'),
                  response(operation='UserTweets'), response(operation='NotificationsTimeline'),
                  response(sessionId='someone-else'), response(loaderId='old'),
                  response(type='Image'), response(url='https://x.com/unrelated/TweetDetail')]
        for event in events:
            with self.subTest(event=event):
                state = self.state()
                state.receive(event)
                self.assertEqual({}, state.apply({}))

    def test_successful_retry_clears_http_errors_and_new_navigation_cannot_reuse_them(self):
        state = self.state()
        state.receive(response())
        state.receive(response(200))
        self.assertEqual({'http_content': {'TweetDetail': 200}}, state.apply({}))
        state.receive(response())
        state.reset('https://x.com/Other/status/456')
        state.loader = 'next'
        state.receive(response())
        self.assertEqual({}, state.apply({}))

    def test_one_session_registers_only_one_observer_and_resets_for_each_navigation(self):
        session = EventSession([response()])
        x_page_state.navigate(session, URL)
        self.assertTrue(x_page_state.read(session, x_post.READ_JS)['limited'])
        session.events = [response(200)]
        x_page_state.navigate(session, URL)
        self.assertFalse(x_page_state.read(session, x_post.READ_JS)['limited'])
        self.assertEqual(1, len(session._event_observers))

    def test_a_new_owned_page_session_is_enabled_without_reusing_old_page_responses(self):
        session = EventSession([response()])
        x_page_state.navigate(session, URL)
        session._sid = 'new-owned'
        x_page_state.navigate(session, URL)
        self.assertFalse(x_page_state.read(session, x_post.READ_JS)['limited'])
        session.events = [response(sessionId='new-owned')]
        x_page_state.navigate(session, URL)
        self.assertTrue(x_page_state.read(session, x_post.READ_JS)['limited'])
        self.assertEqual(1, len(session._event_observers))
