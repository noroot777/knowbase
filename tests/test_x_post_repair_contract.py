"""Check persisted watermarks and rows, not only page-state flags."""
import contextlib
import io
import json
import time
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels, x_page_state, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import Quick, tweet
from tests.test_x_page_state import EventSession, response
from tests.test_x_post import BODY, OLD, PREVIEW, Page, preview, snapshot
from tests.test_x_post_backfill import backfill_entry, linked_snapshot


class ReplySession(EventSession):
    def __init__(self, operation='TweetDetail', status=429):
        super().__init__([response(status, operation=operation)])
        self.navigations = []

    def open_page(self, url):
        pass

    def _post(self, message, deadline):
        super()._post(message, deadline)
        if message['method'] == 'Page.navigate':
            self.navigations.append(message['params']['url'])
        if message['method'] == 'Runtime.evaluate':
            result = self.incoming[-1]['result']['result']
            result['value'] = json.dumps(dict(json.loads(result['value']), articles=1))


class RepairContractTests(ChannelBase):
    def seed(self, body):
        source = self.sub()
        reg.put_items(source, [backfill_entry(tweet('123', text=body))])
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='保留译文',translated_at='old'")
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def saved(self, row):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (row['key'],)).fetchone())

    def test_detail_core_limit_with_replies_stops_both_channel_batches(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            page = ReplySession()
            with self.subTest(channel=channel), patch.object(x_post, 'STEP', 0), \
                 patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                 patch.object(x_browser, '_harvest_one', return_value=([preview()], '')) as collect:
                result = x_browser.harvest([('Example', OLD), ('Other', OLD)], profile='',
                                           attach=channel == x_channels.ATTACH)
            self.assertEqual(1, collect.call_count)
            self.assertEqual(2, page.reads)
            self.assertEqual(1, len(page.navigations))
            for handle in ['Example', 'Other']:
                self.assertIn('频率', result[handle]['error'])
                self.assertNotIn('watermark', result[handle])
                self.assertEqual([], result[handle]['entries'])

    def test_timeline_core_limit_cannot_be_hidden_by_cached_cards(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            page = ReplySession('UserTweets')
            with self.subTest(channel=channel), patch.object(x_browser.time, 'sleep'), \
                 patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)):
                result = x_browser.harvest([('Example', OLD), ('Other', OLD)], profile='',
                                           attach=channel == x_channels.ATTACH)
            self.assertEqual(2, page.reads)
            self.assertEqual(['https://x.com/Example'], page.navigations)
            self.assertIn('频率', result['Example']['error'])
            self.assertEqual(result['Example']['error'], result['Other']['error'])

    def test_timeline_503_with_cards_is_not_success_or_a_rate_limit(self):
        page = ReplySession('UserTweets', 503)
        x_page_state.navigate(page, 'https://x.com/Example')
        with patch.object(x_browser.time, 'sleep'), \
             self.assertRaises(x_page_state.XBrowserError) as caught:
            x_browser._wait_state(page, 'Example', time.monotonic() + 0.005)
        self.assertNotIsInstance(caught.exception, x_page_state.XRateLimited)
        self.assertIn('请求出错', str(caught.exception))

    def test_timeline_limit_with_cached_cards_cannot_return_success(self):
        page = ReplySession('UserTweets')
        x_page_state.navigate(page, 'https://x.com/Example')
        with patch.object(x_browser.time, 'sleep'), \
             self.assertRaises(x_page_state.XRateLimited):
            x_browser._wait_state(page, 'Example', time.monotonic() + 0.01)
        self.assertEqual(2, page.reads)

    def test_backfill_limit_with_replies_stops_once_without_touching_original_rows(self):
        row = self.seed('Read a post.')
        page = ReplySession()
        with patch.object(x_post, 'STEP', 0):
            stats = backfill.repair([row, row], page, apply=True)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(1, stats['deferred'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual('batch', stats['results'][0]['scope'])
        self.assertIn('频率', stats['stop_reason'])
        self.assertEqual(1, len(page.navigations))
        self.assertEqual(row, self.saved(row))

    def test_informational_quote_skips_do_not_clear_retryable_incomplete_reasons(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            with self.subTest(channel=channel), Quick():
                handle = 'Example' + channel
                source = self.sub(handle)
                x_channels.save_watermark(source['id'], channel, OLD)
                source = reg.subscription('social', source['id'])
                entries = x_browser._select([tweet('124', handle=handle),
                    dict(tweet('123', handle=handle, text=''), quoted=True)], None, handle)
                with patch.object(x_browser, 'browser_session',
                                  return_value=contextlib.nullcontext(Page())), \
                     patch.object(x_browser, '_harvest_one', return_value=(entries, '滚动次数用完')):
                    x_channels.prefetch([source], self.settings(channel))
                sync._save_result(source, x_channels.take(source, self.settings(channel)), FetchScope())
                saved = reg.subscription('social', source['id'])
                self.assertEqual(OLD, x_channels.watermark(saved, channel))
                self.assertIn('滚动次数用完', saved['last_note'])
                self.assertIn('跳过 1 条本人无正文的引用推文', saved['last_notice'])
                self.assertNotIn('跳过 1 条本人无正文的引用推文', saved['last_note'])
                with reg.db() as conn:
                    self.assertEqual(['a thought'], [row['body'] for row in conn.execute(
                        'SELECT body FROM feed_items WHERE key IN '
                        '(SELECT item_key FROM subscription_items WHERE subscription_id=?)',
                        (source['id'],))])

    def test_unexpanded_detail_remains_retryable_and_is_saved_on_a_later_round(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            source = self.sub('Retry' + channel)
            entry = dict(preview(), url=f'https://x.com/Retry{channel}/status/123')
            x_channels.save_watermark(source['id'], channel, OLD)
            for text in [PREVIEW, BODY]:
                source = reg.subscription('social', source['id'])
                page = Page(dict(snapshot(text), ready='complete', articles=1))
                with self.subTest(channel=channel, text=text), patch.object(x_post, 'STEP', 0), \
                     patch.object(x_browser, 'browser_session',
                                  return_value=contextlib.nullcontext(page)), \
                     patch.object(x_browser, '_harvest_one', return_value=([entry], '')):
                    settings = self.settings(channel)
                    x_channels.prefetch([source], settings)
                saved_count = sync._save_result(source, x_channels.take(source, settings), FetchScope())
                saved = reg.subscription('social', source['id'])
                self.assertEqual(int(text == BODY), saved_count)
                if text == PREVIEW:
                    self.assertEqual(OLD, x_channels.watermark(saved, channel))
                    self.assertIn('下次同步', saved['last_note'])
                else:
                    self.assertNotEqual(OLD, x_channels.watermark(saved, channel))
                    self.assertEqual('', saved['last_note'])
                    with reg.db() as conn:
                        self.assertEqual(BODY, conn.execute('SELECT body FROM feed_items WHERE url=?',
                                                           (entry['url'],)).fetchone()[0])

    def test_empty_quote_with_an_own_show_more_signal_is_retryable_not_an_informational_skip(self):
        item = dict(tweet('123', text=''), quoted=True, truncated=True)
        entries = x_browser._select([item], None, 'Example')
        self.assertEqual(0, entries.skipped_quotes)
        self.assertEqual(1, len(entries))
        self.assertIn(x_post.HINT, entries[0])

    def test_mismatched_source_text_has_its_own_reason_and_preserves_version_and_translation(self):
        row = self.seed('Read 😂 \n@AsideAI\n today\n\nKeep this paragraph.')
        detail = row['body'].replace(' \n@AsideAI\n', ' @AsideAI')
        evidence = linked_snapshot(detail)
        evidence['inline_legacy'] = evidence['inline_legacy'].replace('😂', '')
        with patch.object(x_post, 'STEP', 0):
            stats = backfill.repair([row], Page(evidence), apply=True)
        self.assertEqual(1, stats['blocked_evidence'])
        self.assertEqual('source_evidence_mismatch', stats['results'][0]['reason'])
        self.assertEqual(0, stats['blocked_length'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.saved(row))
        with contextlib.redirect_stdout(io.StringIO()) as output, \
             patch.object(x_post, 'STEP', 0), \
             patch.object(x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(Page(evidence))):
            self.assertEqual(0, backfill.main(['run', '--yes']))
        self.assertIn('来源读数证据不一致 1 条', output.getvalue())
        self.assertEqual(row, self.saved(row))

    def test_proven_emoji_layout_repair_keeps_paragraphs_and_invalidates_old_translation(self):
        row = self.seed('Read 😂 \n@AsideAI\n today\n\nKeep this paragraph.')
        detail = row['body'].replace(' \n@AsideAI\n', ' @AsideAI')
        with patch.object(x_post, 'STEP', 0):
            stats = backfill.repair([row], Page(linked_snapshot(detail)), apply=True)
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(1, stats['updated'])
        saved = self.saved(row)
        self.assertEqual(detail, saved['body'])
        self.assertIn('\n\nKeep this paragraph.', saved['body'])
        self.assertEqual(row['content_rev'] + 1, saved['content_rev'])
        self.assertEqual('', saved['body_zh'])
        self.assertEqual('', saved['translated_at'])

    def test_a_matching_source_prefix_is_not_evidence_for_the_whole_detail(self):
        row = self.seed('Read \n@AsideAI\n today\n\nKeep this paragraph.')
        detail = row['body'].replace(' \n@AsideAI\n', ' @AsideAI')
        evidence = linked_snapshot(detail)
        evidence['inline_legacy'] = evidence['inline_legacy'].split(' today')[0]
        with patch.object(x_post, 'STEP', 0):
            stats = backfill.repair([row], Page(evidence), apply=True)
        self.assertEqual(1, stats['blocked_evidence'])
        self.assertEqual(row, self.saved(row))
