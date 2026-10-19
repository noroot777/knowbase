"""Unavailable-post audit trails survive storage, partial runs and batch aborts."""
import json
from contextlib import nullcontext
from datetime import datetime, timedelta
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels, x_post
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import FRESH_ISO, tweet
from tests.test_x_post import OLD, URL, Page, preview


class UnavailableNoteTests(ChannelBase):
    def collect(self, channel, outcomes, entries=None):
        source = reg.subscriptions('social')[0] if reg.subscriptions('social') else self.sub()
        self.settings(channel)
        x_channels.save_watermark(source['id'], channel, OLD)
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=nullcontext(Page())), \
             patch.object(x_browser, '_harvest_one', return_value=(entries or [preview()], '')), \
             patch.object(x_post, 'fetch', side_effect=outcomes), \
             patch.object(sync, '_stage') as stage:
            summary = sync.collect('social', FetchScope())
        saved = reg.subscription('social', source['id'])
        with reg.db() as conn:
            run = json.loads(conn.execute('SELECT summary FROM feed_runs ORDER BY id DESC').fetchone()[0])
        return summary, saved, run, stage

    def test_skips_are_not_incomplete_and_are_persisted_in_both_channel_run_records(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            with self.subTest(channel=channel):
                summary, saved, run, stage = self.collect(channel, [x_post.PostGone('页面不存在')])
                self.assertEqual([], summary['errors'])
                self.assertEqual([], summary['partial'])
                self.assertEqual(0, summary['added'])
                self.assertEqual(1, summary['notices'][0]['count'])
                self.assertEqual([dict(url=URL, reason='页面不存在')], summary['notices'][0]['items'])
                self.assertEqual(summary, run)
                self.assertEqual('', saved['last_note'])
                self.assertIn('跳过 1 条', saved['last_notice'])
                self.assertNotEqual(OLD, x_channels.watermark(saved, channel))
                self.assertIn('跳过说明', stage.call_args.args[2])
                self.assertNotIn('没读完', stage.call_args.args[2])

    def test_gone_and_retryable_failure_keep_separate_notices_and_freeze_the_watermark(self):
        entries = [preview(), dict(preview(), url='https://x.com/Example/status/124')]
        summary, saved, run, _ = self.collect(x_channels.BROWSER,
            [x_post.PostGone('页面不存在'), x_post.PostUnavailable('未加载')], entries)
        self.assertEqual(1, len(summary['partial']))
        self.assertNotIn('跳过', summary['partial'][0]['reason'])
        self.assertEqual(1, summary['notices'][0]['count'])
        self.assertEqual(OLD, x_channels.watermark(saved, x_channels.BROWSER))
        self.assertIn('下次同步', saved['last_note'])
        self.assertIn(URL, saved['last_notice'])
        self.assertEqual(summary, run)

    def test_empty_details_are_audited_without_insertion_or_freezing_either_channel(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for kind in ['photo', 'video', 'quote', 'link']:
                with self.subTest(channel=channel, kind=kind):
                    summary, saved, run, _ = self.collect(channel, [x_post.PostEmpty(kind)])
                    self.assertEqual([], summary['errors'])
                    self.assertEqual([], summary['partial'])
                    self.assertEqual(0, summary['added'])
                    self.assertEqual(1, summary['notices'][0]['count'])
                    self.assertEqual([dict(url=URL, reason=f'详情页已确认无自写文字({kind})',
                                           empty_kind=kind)], summary['notices'][0]['items'])
                    self.assertEqual('', saved['last_note'])
                    self.assertIn('已确认无自写文字', saved['last_notice'])
                    self.assertEqual(x_browser._newest([preview()], OLD),
                                     x_channels.watermark(saved, channel))
                    self.assertEqual(summary, run)
                    with reg.db() as conn:
                        self.assertEqual(0, conn.execute('SELECT count(*) FROM feed_items').fetchone()[0])

    def test_empty_and_unknown_details_keep_terminal_and_retryable_results_separate(self):
        entries = [preview(), dict(preview(), url='https://x.com/Example/status/124')]
        summary, saved, run, _ = self.collect(x_channels.BROWSER,
            [x_post.PostEmpty('photo'), x_post.PostUnavailable('未加载')], entries)
        self.assertEqual(1, len(summary['partial']))
        self.assertEqual(1, summary['notices'][0]['count'])
        self.assertEqual('photo', summary['notices'][0]['items'][0]['empty_kind'])
        self.assertEqual(OLD, x_channels.watermark(saved, x_channels.BROWSER))
        self.assertIn('下次同步', saved['last_note'])
        self.assertIn(URL, saved['last_notice'])
        self.assertEqual(summary, run)

    def test_a_clean_next_round_receives_new_posts_after_a_confirmed_empty_skip(self):
        source = self.sub()
        self.settings(x_channels.BROWSER)
        x_channels.save_watermark(source['id'], x_channels.BROWSER, OLD)
        later = datetime.fromisoformat(FRESH_ISO.replace('Z', '+00:00')) + timedelta(hours=1)
        next_post = x_browser._entry(tweet('124', text='A new post', when=later.isoformat()))
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=nullcontext(Page())), \
             patch.object(x_browser, '_harvest_one', side_effect=[([preview()], ''), ([next_post], '')]), \
             patch.object(x_post, 'fetch', side_effect=x_post.PostEmpty('photo')) as fetch:
            first = sync.collect('social', FetchScope())
            second = sync.collect('social', FetchScope())
        self.assertEqual(1, first['notices'][0]['count'])
        self.assertEqual([], first['partial'])
        self.assertEqual(1, second['added'])
        self.assertEqual([], second['notices'])
        self.assertEqual([], second['partial'])
        fetch.assert_called_once()
        saved = reg.subscription('social', source['id'])
        self.assertEqual('', saved['last_notice'])
        self.assertEqual(x_browser._newest([next_post], OLD),
                         x_channels.watermark(saved, x_channels.BROWSER))
        with reg.db() as conn:
            self.assertEqual([(next_post['url'], 'A new post')],
                             [tuple(row) for row in conn.execute('SELECT url,body FROM feed_items')])

    def test_skip_evidence_survives_a_later_batch_rate_limit(self):
        entries = [preview(), dict(preview(), url='https://x.com/Example/status/124')]
        summary, saved, run, _ = self.collect(x_channels.ATTACH,
            [x_post.PostGone('页面不存在'), x_post.XRateLimited('访问频率受限')], entries)
        self.assertTrue(summary['errors'])
        self.assertIn('访问频率受限', saved['last_error'])
        self.assertIn('跳过 1 条', saved['last_error'])
        self.assertIn(URL, saved['last_error'])
        self.assertEqual(OLD, x_channels.watermark(saved, x_channels.ATTACH))
        self.assertIn(URL, run['errors'][0]['reason'])

    def test_only_actual_gone_entries_are_counted_and_their_urls_remain_distinct(self):
        unavailable = []
        entries = [preview(), dict(preview(), url='https://x.com/Example/status/124')]
        with patch.object(x_post, 'fetch', side_effect=[x_post.PostGone('已删除'),
                                                       x_post.PostGone('无权查看')]):
            kept, incomplete = x_post.complete(entries, session=Page(), unavailable=unavailable)
        self.assertEqual([], kept)
        self.assertEqual('', incomplete)
        self.assertEqual([entry['url'] for entry in entries], [item['url'] for item in unavailable])
        self.assertIn('跳过 2 条', x_post.unavailable_note(unavailable))

    def test_next_clean_success_clears_the_notice_and_repointing_clears_old_evidence(self):
        _, saved, _, _ = self.collect(x_channels.BROWSER, [x_post.PostGone('已删除')])
        self.assertTrue(saved['last_notice'])
        sync._save_result(saved, ([], {key: saved[key] for key in
                                      ('etag', 'modified', 'resolved_url')}), FetchScope())
        self.assertEqual('', reg.subscription('social', saved['id'])['last_notice'])
        with reg.db() as conn:
            conn.execute('UPDATE subscriptions SET last_notice=? WHERE id=?',
                         ('旧账号跳过', saved['id']))
        reg.save_subscription('social', dict(saved, url='https://x.com/NewExample'), saved['id'])
        self.assertEqual('', reg.subscription('social', saved['id'])['last_notice'])

    def test_migrating_an_old_database_adds_only_an_empty_notice(self):
        source = self.sub()
        with reg.db() as conn:
            conn.execute('ALTER TABLE subscriptions DROP COLUMN last_notice')
        saved = reg.subscription('social', source['id'])
        self.assertEqual('', saved['last_notice'])
        self.assertEqual(source['url'], saved['url'])
        self.assertEqual(source['config'], saved['config'])

    def test_failure_message_limit_cannot_truncate_the_structured_skip_audit(self):
        entries = [dict(preview(), url=f'https://x.com/Example/status/{123 + index}')
                   for index in range(31)]
        outcomes = [x_post.PostGone('X 明确答复该推文页面不存在或已不可用') for _ in range(30)]
        summary, saved, run, _ = self.collect(x_channels.BROWSER,
            outcomes + [x_post.XRateLimited('访问频率受限')], entries)
        self.assertLessEqual(len(saved['last_error']), 1200)
        self.assertEqual(30, summary['notices'][0]['count'])
        self.assertEqual(entries[29]['url'], run['notices'][0]['items'][-1]['url'])
