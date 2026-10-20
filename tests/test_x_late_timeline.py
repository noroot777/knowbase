"""Late protocol events keep their scope until the timeline is finalized."""
import contextlib
import json
import time
import unittest
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_page_policy import Clock, PolicySession
from tests.test_x_page_state import response
from tests.test_x_post import BODY, OLD


class LateSession(PolicySession):
    def __init__(self, phase, status, initial=200, operation='UserTweets', truncated=False):
        super().__init__({'Example': ([response(initial, operation='UserTweets')], {}),
                          'Next': ([], {})})
        self.phase, self.status, self.operation = phase, status, operation
        self.truncated = truncated
        self.calls = {}
        self.injected = False

    def _post(self, message, deadline):
        super()._post(message, deadline)
        if (self.phase == 'during_navigation' and message['method'] == 'Page.navigate'
                and self.handle.lower() == 'next' and not self.injected):
            self.injected = True
            self.incoming[-1]['result']['loaderId'] = 'next'
            self.incoming.insert(len(self.incoming) - 1, response(self.status, operation=self.operation))
        if message['method'] != 'Runtime.evaluate' or self.handle.lower() != 'example':
            return
        script = message['params']['expression']
        self.calls[script] = self.calls.get(script, 0) + 1
        count = self.calls[script]
        result = self.incoming[-1]['result']['result']
        if script == x_browser._COLLECT_JS:
            items = [tweet('123', text=BODY)]
            if self.truncated:
                items[0]['truncated'] = True
            if self.phase not in {'scroll', 'settle'}:
                items += [tweet(str(identity), when='2026-09-01T00:00:00Z') for identity in range(3)]
            result['value'] = json.dumps(dict(articles=len(items), parsed=len(items), items=items))
        elif script == x_browser._SCROLL_JS:
            result['value'] = json.dumps(dict(bottom=True))
        targets = {'reset': (x_browser._RESET_JS, 1), 'collect': (x_browser._COLLECT_JS, 1),
                   'scroll': (x_browser._SCROLL_JS, 1), 'settle': (x_browser._COLLECT_JS, 2),
                   'finish': (x_browser._STATE_JS, 4), 'finalize': (x_browser._STATE_JS, 5),
                   'next_navigation': (x_browser._STATE_JS, 6)}
        target = targets.get(self.phase)
        if not self.injected and target == (script, count):
            self.injected = True
            self.incoming.insert(len(self.incoming) - 1, response(self.status, operation=self.operation))


class LateTimelineTests(unittest.TestCase):
    def harvest(self, session, channel=x_channels.BROWSER):
        clock = Clock()
        with (patch.object(time, 'monotonic', clock.monotonic),
              patch.object(time, 'sleep', clock.sleep),
              patch.object(x_browser, '_BOTTOM_TRIES', 1),
              patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(session))):
            return x_browser.harvest([('Example', OLD), ('Next', OLD)], profile='unused',
                                     attach=channel == x_channels.ATTACH)

    def test_late_core_limit_stops_both_channels_at_every_collection_boundary(self):
        for phase in ['reset', 'collect', 'scroll', 'settle', 'finish', 'finalize', 'next_navigation']:
            for channel in [x_channels.BROWSER, x_channels.ATTACH]:
                with self.subTest(phase=phase, channel=channel):
                    session = LateSession(phase, 429)
                    results = self.harvest(session, channel)
                    self.assertTrue(session.injected)
                    self.assertEqual(['https://x.com/Example'], session.urls)
                    for result in results.values():
                        self.assertIn('频率', result['error'])
                        self.assertEqual([], result['entries'])
                        self.assertNotIn('watermark', result)

    def test_late_regular_failures_freeze_watermark_without_stopping_the_next_account(self):
        for phase in ['reset', 'collect', 'scroll', 'settle', 'finish', 'finalize', 'next_navigation']:
            for status in [404, 503]:
                with self.subTest(phase=phase, status=status):
                    session = LateSession(phase, status)
                    results = self.harvest(session)
                    self.assertTrue(session.injected)
                    self.assertIn('请求出错', results['Example']['incomplete'])
                    self.assertEqual(OLD, results['Example']['watermark'])
                    self.assertTrue(results['Example']['entries'])
                    self.assertEqual('', results['Example']['error'])
                    self.assertTrue(results['Next']['entries'])
                    self.assertEqual('', results['Next']['error'])

    def test_partial_collection_cannot_hide_a_new_limit(self):
        session = LateSession('collect', 429, initial=503)
        results = self.harvest(session)
        self.assertTrue(session.injected)
        self.assertEqual(['https://x.com/Example'], session.urls)
        self.assertIn('频率', results['Next']['error'])

    def test_next_detail_navigation_consumes_timeline_limit_before_resetting_responses(self):
        session = LateSession('finalize', 429, truncated=True)
        results = self.harvest(session)
        self.assertTrue(session.injected)
        self.assertEqual(['https://x.com/Example'], session.urls)
        self.assertIn('频率', results['Example']['error'])

    def test_auxiliary_failures_do_not_freeze_the_watermark_or_stop_the_batch(self):
        for phase in ['collect', 'scroll', 'settle', 'finalize', 'next_navigation']:
            with self.subTest(phase=phase):
                session = LateSession(phase, 429, operation='UserByScreenName')
                results = self.harvest(session)
                self.assertTrue(session.injected)
                self.assertEqual('', results['Example']['incomplete'])
                self.assertNotEqual(OLD, results['Example']['watermark'])
                self.assertTrue(results['Next']['entries'])

    def test_old_loader_events_drained_during_navigation_are_not_erased_by_reset(self):
        for status in [429, 503]:
            with self.subTest(status=status):
                session = LateSession('during_navigation', status)
                results = self.harvest(session)
                self.assertTrue(session.injected)
                self.assertEqual(['https://x.com/Example', 'https://x.com/Next'], session.urls)
                if status == 429:
                    self.assertIn('频率', results['Example']['error'])
                    self.assertIn('频率', results['Next']['error'])
                    self.assertFalse(results['Next']['entries'])
                else:
                    self.assertEqual(OLD, results['Example']['watermark'])
                    self.assertIn('请求出错', results['Example']['incomplete'])
                    self.assertTrue(results['Next']['entries'])
                    self.assertEqual('', results['Next']['incomplete'])


class StoredLateTimelineTests(ChannelBase):
    def batch(self, sources, channel, session):
        settings = self.settings(channel)
        clock = Clock()
        with (patch.object(time, 'monotonic', clock.monotonic),
              patch.object(time, 'sleep', clock.sleep),
              patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(session))):
            x_channels.prefetch(sources, settings)
        for source in sources:
            try:
                result = x_channels.take(source, settings)
            except ValueError as exc:
                sync._save_failure(source, exc, dict(errors=[], failed_ids=[]))
            else:
                sync._save_result(source, result, FetchScope())
        return [reg.subscription('social', source['id']) for source in sources]

    def test_late_errors_persist_partial_or_batch_failure_and_successful_next_round_recovers(self):
        sources = [self.sub('Example'), self.sub('Next')]
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for phase, initial, status in [('collect', 200, 503), ('collect', 503, 429),
                                           ('next_navigation', 200, 503)]:
                with self.subTest(channel=channel, phase=phase, status=status):
                    for source in sources:
                        x_channels.save_watermark(source['id'], channel, OLD)
                    sources = [reg.subscription('social', source['id']) for source in sources]
                    saved = self.batch(sources, channel, LateSession(phase, status, initial))
                    self.assertEqual(OLD, x_channels.watermark(saved[0], channel))
                    if status == 429:
                        self.assertTrue(all('频率' in source['last_error'] for source in saved))
                    else:
                        self.assertIn('请求出错', saved[0]['last_note'])
                        self.assertEqual('', saved[0]['last_error'])
                        self.assertNotEqual(OLD, x_channels.watermark(saved[1], channel))
                        with reg.db() as conn:
                            self.assertIsNotNone(conn.execute(
                                "SELECT key FROM feed_items WHERE url='https://x.com/Example/status/123'"
                            ).fetchone())
                    recovered = self.batch(saved, channel, LateSession('collect', 200))
                    for source in recovered:
                        self.assertEqual('', source['last_error'])
                        self.assertEqual('', source['last_note'])
                        self.assertNotEqual(OLD, x_channels.watermark(source, channel))
