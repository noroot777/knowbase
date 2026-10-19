"""HTTP status evidence is scoped to the current page's own core X requests."""
import json
import unittest
from collections import deque
from unittest.mock import patch

from kb.feeds import x_browser, x_page_state, x_post
from kb.feeds.x_cdp import Session
from tests.test_x_post import URL, snapshot


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
    def __init__(self, events):
        super().__init__()
        self._sid = 'owned'
        self.incoming = deque()
        self.events = events
        self.reads = 0

    def _post(self, message, deadline):
        result = {}
        if message['method'] == 'Page.navigate':
            self.incoming.extend(self.events)
            result = dict(loaderId='current')
        if message['method'] == 'Runtime.evaluate':
            self.reads += 1
            result = dict(result=dict(value=json.dumps(dict(
                snapshot(''), id='', ready='complete', limited=False, failed=False, empty=False))))
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
        with patch.object(x_post, 'STEP', 0), self.assertRaises(x_post.XRateLimited):
            x_post.fetch(URL, session)
        self.assertEqual(2, session.reads)

    def test_timeline_core_http_429_reaches_the_shared_state(self):
        session = EventSession([response(operation='UserTweets')])
        x_page_state.navigate(session, 'https://x.com/Example')
        state = x_page_state.read(session, x_browser._STATE_JS)
        self.assertTrue(state['limited'])
        self.assertEqual('limited', x_page_state.problem(state))

    def test_http_errors_are_not_permanent_absence_or_necessarily_rate_limits(self):
        for status in [400, 403, 404, 500, 503]:
            with self.subTest(status=status):
                state = self.state()
                state.receive(response(status))
                snap = state.apply(dict(ready='complete', empty=True, limited=False))
                self.assertTrue(snap['failed'])
                self.assertFalse(snap['empty'])
                self.assertFalse(snap['limited'])
                self.assertEqual('failed', x_page_state.problem(snap))

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
        self.assertEqual({}, state.apply({}))
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
