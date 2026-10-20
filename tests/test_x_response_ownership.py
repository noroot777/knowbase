"""Old-loader responses retain business ownership after a navigation reply."""
import contextlib
import json
import time
import unittest
from unittest.mock import patch

from kb.feeds import registry as reg, x_browser, x_channels, x_page_state, x_post
from tests import test_x_late_timeline as late
from tests.test_x_channels import ChannelBase
from tests.test_x_page_policy import Clock, PolicySession
from tests.test_x_page_state import response
from tests.test_x_post import BODY, OLD, PREVIEW


def request_event(method, request_id='pending', **changes):
    event = dict(method=method, sessionId='owned', params=dict(requestId=request_id))
    if method == 'Network.requestWillBeSent':
        event['params'].update(loaderId='current', type='Fetch', request=dict(
            url='https://x.com/i/api/graphql/hash/UserTweets'))
    event['params'].update(changes)
    return event


class OwnershipSession(PolicySession):
    def __init__(self, status, *, trigger='Next', tracked=True, operation='UserTweets', fault=False):
        events = [response(200, operation='UserTweets', requestId='initial'),
                  request_event('Network.loadingFinished', 'initial')]
        if tracked:
            events.append(request_event('Network.requestWillBeSent'))
        super().__init__({'Example': (events, {}), 'Next': ([], {}),
                          'Last': ([], {}), 'Further': ([], {})})
        self.status, self.trigger, self.tracked = status, trigger, tracked
        self.operation, self.fault = operation, fault
        self.injected = False
        self.in_detail = False

    def _post(self, message, deadline):
        method = message['method']
        if method == 'Page.navigate' and '/status/' in message['params']['url']:
            self.in_detail = True
            self.cases['example'] = ([], {})
        super()._post(message, deadline)
        target = (method == 'Target.closeTarget' if self.trigger == 'cleanup' else
                  self.in_detail if self.trigger == 'detail' else
                  self.handle.lower() == self.trigger.lower())
        if target and not self.injected and (
                method == 'Runtime.evaluate' or (method == 'Page.navigate' and self.fault)
                or self.trigger == 'cleanup'):
            self.injected = True
            changes = dict(requestId='pending') if self.tracked else {}
            event = response(self.status, operation=self.operation, **changes)
            self.incoming.insert(len(self.incoming) - 1, event)
            self.incoming.insert(len(self.incoming) - 1, request_event('Network.loadingFinished'))
            if self.fault:
                self.incoming[-1] = dict(id=message['id'], error=dict(message='navigation failed'))
        if method != 'Runtime.evaluate':
            return
        result = self.incoming[-1]['result']['result']
        script = message['params']['expression']
        if self.trigger == 'detail' and script == x_browser._COLLECT_JS and self.handle == 'Example':
            snapshot = json.loads(result['value'])
            snapshot['items'][0].update(text=PREVIEW, truncated=True)
            result['value'] = json.dumps(snapshot)
        if self.in_detail and script == x_post.READ_JS:
            result['value'] = json.dumps(dict(json.loads(result['value']), text=BODY, id='123'))


def harvest(session, channel=x_channels.BROWSER):
    clock = Clock()
    with (patch.object(time, 'monotonic', clock.monotonic),
          patch.object(time, 'sleep', clock.sleep),
          patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(session))):
        return x_browser.harvest([(handle, OLD) for handle in ['Example', 'Next', 'Last', 'Further']],
                                 profile='unused', attach=channel == x_channels.ATTACH)


class OwnershipTests(unittest.TestCase):
    def test_old_response_after_new_navigation_reply_reaches_its_original_account(self):
        for tracked in [True, False]:
            for channel in [x_channels.BROWSER, x_channels.ATTACH]:
                for status in [429, 503]:
                    with self.subTest(tracked=tracked, channel=channel, status=status):
                        session = OwnershipSession(status, tracked=tracked)
                        results = harvest(session, channel)
                        self.assertTrue(session.injected)
                        self.assertFalse(session.incoming)
                        self.assertFalse(session._x_response_scopes.requests)
                        if status == 429:
                            self.assertEqual(['https://x.com/Example', 'https://x.com/Next'], session.urls)
                            self.assertTrue(all('频率' in result['error'] for result in results.values()))
                            self.assertTrue(all(not result['entries'] for result in results.values()))
                            self.assertEqual(1, session.state_reads['Next'])
                        else:
                            self.assertEqual(OLD, results['Example']['watermark'])
                            self.assertIn('请求出错', results['Example']['incomplete'])
                            for handle in ['Next', 'Last', 'Further']:
                                self.assertTrue(results[handle]['entries'])
                                self.assertFalse(results[handle]['incomplete'])
                                self.assertNotEqual(OLD, results[handle]['watermark'])

    def test_more_than_one_new_loader_does_not_lose_or_reassign_the_original_owner(self):
        for status in [429, 503]:
            with self.subTest(status=status):
                session = OwnershipSession(status, trigger='Last')
                results = harvest(session)
                self.assertTrue(results['Next']['entries'])
                self.assertFalse(results['Next']['incomplete'])
                self.assertNotEqual(OLD, results['Next']['watermark'])
                if status == 429:
                    self.assertEqual(3, len(session.urls))
                    self.assertIn('频率', results['Example']['error'])
                    self.assertIn('频率', results['Further']['error'])
                else:
                    self.assertEqual(OLD, results['Example']['watermark'])
                    self.assertFalse(results['Last']['incomplete'])

    def test_timeline_request_ownership_survives_switching_to_detail_operations(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for status in [429, 503]:
                with self.subTest(channel=channel, status=status):
                    session = OwnershipSession(status, trigger='detail')
                    results = harvest(session, channel)
                    self.assertTrue(session.injected)
                    self.assertEqual('https://x.com/Example/status/123', session.urls[1])
                    if status == 429:
                        self.assertEqual(2, len(session.urls))
                        self.assertIn('频率', results['Example']['error'])
                        self.assertIn('频率', results['Next']['error'])
                    else:
                        self.assertEqual(OLD, results['Example']['watermark'])
                        self.assertIn('请求出错', results['Example']['incomplete'])
                        self.assertIn(BODY, results['Example']['entries'][0]['body'])
                        self.assertFalse(results['Next']['incomplete'])

    def test_navigation_exception_does_not_discard_already_received_old_errors(self):
        for status in [429, 503]:
            with self.subTest(status=status):
                session = OwnershipSession(status, fault=True)
                results = harvest(session)
                self.assertFalse(session.incoming)
                if status == 429:
                    self.assertEqual(2, len(session.urls))
                    self.assertTrue(all('频率' in result['error'] for result in results.values()))
                else:
                    self.assertEqual(OLD, results['Example']['watermark'])
                    self.assertIn('请求出错', results['Example']['incomplete'])
                    self.assertIn('navigation failed', results['Next']['error'])
                    self.assertTrue(results['Last']['entries'])

    def test_old_auxiliary_errors_do_not_gain_core_scope_after_navigation(self):
        for operation in ['UserByScreenName', 'UserTweetsAndReplies']:
            with self.subTest(operation=operation):
                session = OwnershipSession(429, operation=operation, tracked=False)
                results = harvest(session)
                self.assertEqual(4, len(session.urls))
                self.assertFalse(results['Example']['incomplete'])
                self.assertNotEqual(OLD, results['Example']['watermark'])

    def test_a_failed_page_without_a_ready_timeline_still_owns_its_late_core_limit(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for status in [429, 503]:
                with self.subTest(channel=channel, status=status):
                    session = OwnershipSession(status)
                    events, _ = session.cases['example']
                    session.cases['example'] = (events, dict(articles=0, empty=True))
                    results = harvest(session, channel)
                    self.assertIn('打不开', results['Example']['error'])
                    self.assertNotIn('watermark', results['Example'])
                    if status == 429:
                        self.assertEqual(2, len(session.urls))
                        self.assertIn('频率', results['Next']['error'])
                        self.assertIn('频率', results['Last']['error'])
                    else:
                        self.assertTrue(results['Next']['entries'])
                        self.assertFalse(results['Next']['incomplete'])

    def test_request_owner_is_released_only_on_finish_or_explicit_cancellation(self):
        for method in ['Network.loadingFinished', 'Network.loadingFailed']:
            with self.subTest(method=method):
                session = OwnershipSession(503)
                x_page_state.navigate(session, 'https://x.com/Example')
                scopes = session._x_response_scopes
                owner = session._x_responses
                self.assertIs(owner, scopes.requests[('owned', 'pending')])
                x_page_state.navigate(session, 'https://x.com/Next')
                self.assertIs(owner, scopes.requests[('owned', 'pending')])
                scopes.receive(request_event(method, errorText='net::ERR_ABORTED', canceled=True))
                self.assertNotIn(('owned', 'pending'), scopes.requests)
                self.assertIs(owner, scopes.pages[('owned', 'current')])
                self.assertEqual(1, len(session._event_observers))
                self.assertEqual(1, len(session._call_observers))

    def test_other_session_terminal_event_cannot_cancel_the_owned_request(self):
        session = OwnershipSession(503)
        x_page_state.navigate(session, 'https://x.com/Example')
        event = request_event('Network.loadingFailed')
        event['sessionId'] = 'other'
        session._x_response_scopes.receive(event)
        self.assertIn(('owned', 'pending'), session._x_response_scopes.requests)

    def test_cleanup_commands_are_not_interrupted_by_the_batch_stop_signal(self):
        session = OwnershipSession(429)
        harvest(session)
        scopes = session._x_response_scopes
        scopes.failure = x_page_state.XRateLimited('stop')
        session.call('Target.closeTarget', dict(targetId='owned'))
        session.call('Browser.close')
        self.assertFalse(session.incoming)

    def test_a_finalized_last_page_still_consumes_errors_received_while_closing(self):
        for status in [429, 503]:
            with self.subTest(status=status):
                session = OwnershipSession(status, trigger='cleanup')

                @contextlib.contextmanager
                def closing_session():
                    try:
                        yield session
                    finally:
                        session.call('Target.closeTarget', dict(targetId='owned'))

                with patch.object(x_browser, 'browser_session', return_value=closing_session()):
                    results = x_browser.harvest([('Example', OLD)], profile='unused')
                self.assertTrue(session.injected)
                self.assertFalse(session.incoming)
                if status == 429:
                    self.assertEqual([], results['Example']['entries'])
                    self.assertIn('频率', results['Example']['error'])
                else:
                    self.assertEqual(OLD, results['Example']['watermark'])
                    self.assertIn('请求出错', results['Example']['incomplete'])


class StoredOwnershipTests(ChannelBase):
    def test_late_response_scope_survives_take_persistence_and_next_round_recovery(self):
        sources = [self.sub(name) for name in ['Example', 'Next', 'Last']]
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for status in [429, 503]:
                with self.subTest(channel=channel, status=status):
                    with reg.db() as connection:
                        before = connection.execute('SELECT count(*) FROM feed_items').fetchone()[0]
                    for source in sources:
                        x_channels.save_watermark(source['id'], channel, OLD)
                    sources = [reg.subscription('social', source['id']) for source in sources]
                    session = OwnershipSession(status)
                    saved = late.StoredLateTimelineTests.batch(self, sources, channel, session)
                    self.assertEqual(OLD, x_channels.watermark(saved[0], channel))
                    with reg.db() as connection:
                        stored = connection.execute('SELECT count(*) FROM feed_items').fetchone()[0]
                    if status == 429:
                        self.assertTrue(all('频率' in source['last_error'] for source in saved))
                        self.assertEqual(before, stored)
                        self.assertEqual(2, len(session.urls))
                        self.assertTrue(all(x_channels.watermark(source, channel) == OLD for source in saved))
                    else:
                        self.assertEqual('', saved[0]['last_error'])
                        self.assertIn('请求出错', saved[0]['last_note'])
                        self.assertEqual(3, stored)
                        for source in saved[1:]:
                            self.assertEqual('', source['last_note'])
                            self.assertNotEqual(OLD, x_channels.watermark(source, channel))
                    recovered = late.StoredLateTimelineTests.batch(
                        self, saved, channel, OwnershipSession(200))
                    for source in recovered:
                        self.assertEqual('', source['last_note'])
                        self.assertEqual('', source['last_error'])
                        self.assertNotEqual(OLD, x_channels.watermark(source, channel))
