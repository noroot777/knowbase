"""Main's account/skip evidence contracts coexist with delayed response ownership."""
import contextlib
import json
import time
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_account, x_browser, x_channels, x_page_state, x_post
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_page_policy import Clock, PolicySession
from tests.test_x_page_state import response
from tests.test_x_post import OLD, PREVIEW, URL
from tests.test_x_response_ownership import OwnershipSession


class CombinedSession(OwnershipSession):
    def _post(self, message, deadline):
        super()._post(message, deadline)
        if (message['method'] == 'Runtime.evaluate' and self.handle.lower() == 'example'
                and message['params']['expression'] == x_browser._COLLECT_JS):
            result = self.incoming[-1]['result']['result']
            snapshot = json.loads(result['value'])
            snapshot['items'][0].update(text=PREVIEW, truncated=True)
            snapshot['items'].insert(1, dict(tweet('125', text=''), quoted=True))
            snapshot['articles'] = snapshot['parsed'] = len(snapshot['items'])
            result['value'] = json.dumps(snapshot)


class SyncMergeTests(ChannelBase):
    def test_late_errors_preserve_both_kinds_of_skip_evidence_through_actual_sync(self):
        sources = [self.sub(name) for name in ['Example', 'Next', 'Last']]
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for status in [429, 503]:
                with self.subTest(channel=channel, status=status):
                    self.settings(channel)
                    for source in sources:
                        x_channels.save_watermark(source['id'], channel, OLD)
                    with reg.db() as connection:
                        connection.execute("UPDATE subscriptions SET last_attempt='',retry_at=0,failures=0")
                    session = CombinedSession(status)
                    clock = Clock()
                    with (patch.object(time, 'monotonic', clock.monotonic),
                          patch.object(time, 'sleep', clock.sleep),
                          patch.object(reg, 'configuration_error', return_value=''),
                          patch.object(x_browser, 'browser_session',
                                       return_value=contextlib.nullcontext(session)),
                          patch.object(x_post, 'fetch', side_effect=x_post.PostGone('页面不存在')),
                          patch.object(sync, '_stage')):
                        summary = sync.collect('social', FetchScope())
                    saved = reg.subscription('social', sources[0]['id'])
                    self.assertTrue(session.injected)
                    self.assertEqual(OLD, x_channels.watermark(saved, channel))
                    notice = summary['notices'][0]
                    expected_url = URL.lower()
                    self.assertIn('本人无正文的引用推文', notice['reason'])
                    self.assertIn('页面不存在', notice['reason'])
                    self.assertEqual([dict(url=expected_url, reason='页面不存在')], notice['items'])
                    if status == 429:
                        self.assertEqual(2, len(session.urls))
                        self.assertIn('频率', saved['last_error'])
                        self.assertIn(expected_url, saved['last_error'])
                        self.assertIn('本人无正文的引用推文', saved['last_error'])
                        self.assertTrue(summary['errors'])
                    else:
                        self.assertEqual([], summary['errors'])
                        self.assertEqual('Example', summary['partial'][0]['title'])
                        self.assertEqual(2, notice['count'])
                        self.assertIn('请求出错', saved['last_note'])
                        self.assertNotIn('跳过', saved['last_note'])
                        self.assertIn(expected_url, saved['last_notice'])
                        self.assertIn('本人无正文的引用推文', saved['last_notice'])

    def test_structured_account_terminal_states_do_not_override_core_http_conflicts(self):
        for terminal in ['missing', 'suspended', 'protected']:
            for status in [404, 429, 503]:
                with self.subTest(terminal=terminal, status=status):
                    session = PolicySession({'Example': ([response(status, operation='UserTweets')],
                        dict(articles=0, empty=True, account=dict(
                            state=terminal, handle='example', evidence='structured_fixture')))})
                    clock = Clock()
                    with (patch.object(time, 'monotonic', clock.monotonic),
                          patch.object(time, 'sleep', clock.sleep)):
                        x_page_state.navigate(session, 'https://x.com/Example')
                        with self.assertRaises(x_browser.XBrowserError) as caught:
                            x_browser._wait_state(session, 'Example', 1)
                    self.assertNotIsInstance(caught.exception, x_account.XAccountUnavailable)
                    self.assertEqual(status == 429, isinstance(caught.exception, x_browser.XRateLimited))
                    self.assertEqual(2, session.reads)
