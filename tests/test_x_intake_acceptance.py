"""Acceptance failures must retain body proofs and shared non-X policy seams."""
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_article, x_body, x_browser, x_channels, x_post
from kb.feeds import x_intake_store as store
from tests.test_x_intake import IntakeBase, Session
from tests.test_x_feedgrab_contract import TODAY, FeedgrabRound, tweet


class AcceptanceContractTests(IntakeBase):
    def article(self, prefix=''):
        text = '介绍\n\nhttps://x.com/Example/article/100'
        entry = self.entry()
        entry.update(body=prefix + text)
        return x_body.record(entry, text, prefix=prefix)

    def test_article_merge_renews_the_complete_body_proof(self):
        entry = self.article()
        merged = x_article.merge(entry, dict(title='文章', body='独立读到的完整长文'))
        self.assertTrue(store.complete(merged))
        self.assertTrue(x_body.parts(merged).source_known)
        self.assertNotEqual(entry['body_provenance'], merged['body_provenance'])
        self.assertEqual(merged['body'], '介绍\n\n独立读到的完整长文')

    def test_article_merge_preserves_the_recorded_context_boundary(self):
        merged = x_article.merge(self.article('> 转推上下文\n\n'), dict(title='文章', body='完整长文'))
        fragments = x_body.parts(merged)
        self.assertTrue(store.complete(merged))
        self.assertEqual(fragments.prefix, '> 转推上下文\n\n')
        self.assertEqual(fragments.text, '介绍\n\n完整长文')

    def test_editing_a_merged_article_does_not_reuse_the_old_proof(self):
        merged = x_article.merge(self.article(), dict(title='文章', body='完整长文'))
        merged['body'] += '未确认改写'
        self.assertFalse(store.complete(merged))
        self.assertFalse(x_body.parts(merged).source_known)

    def test_shared_failure_backoff_uses_the_collectors_clock(self):
        with patch.object(sync, 'time', SimpleNamespace(time=lambda: 1000)):
            sync._save_failure(self.source, ValueError('来源失败'), dict(errors=[], failed_ids=[]))
        source = reg.subscription('social', self.source['id'])
        self.assertEqual(source['retry_at'], 2800)
        self.assertEqual(source['last_error'], '来源失败')

    def test_terminal_body_evidence_survives_database_delivery(self):
        self.prepare()
        entry = self.entry(truncated=True)
        store.stage(self.source, [entry], phase='discovery', incomplete='')
        source, task = store.pending([self.source])[0]
        audit = dict(url=entry['url'], reason='详情页已确认无自写文字(photo)', empty_kind='photo')
        store.finish(source, task, [], [audit], '')
        self.assertEqual(store.delivery(self.source)[2], [audit])
        with reg.db() as conn:
            self.assertEqual(conn.execute('SELECT reason FROM x_intake_candidates').fetchone()[0], audit['reason'])

    def test_another_posts_terminal_evidence_cannot_close_the_pending_job(self):
        self.prepare()
        store.stage(self.source, [self.entry(truncated=True)], phase='discovery', incomplete='')
        source, task = store.pending([self.source])[0]
        store.finish(source, task, [], [dict(url='https://x.com/Other/status/999', reason='已删除')], '')
        self.assertEqual(store.delivery(self.source)[1:3], (1, []))

    def test_existing_candidate_tables_receive_the_empty_audit_column(self):
        self.prepare()
        with reg.db() as conn:
            conn.execute('ALTER TABLE x_intake_candidates DROP COLUMN unavailable')
        with reg.db() as conn:
            columns = {row['name'] for row in conn.execute('PRAGMA table_info(x_intake_candidates)')}
        self.assertIn('unavailable', columns)

    def collect_automatic(self, entry):
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', return_value=([entry], '')):
            return sync.collect('social', FetchScope(force=True))

    def test_real_automatic_sync_delivers_the_terminal_reason_and_empty_kind(self):
        entry = self.entry(truncated=True)
        with patch.object(x_post, 'fetch', side_effect=x_post.PostEmpty('photo')):
            summary = self.collect_automatic(entry)
        expected = dict(url=entry['url'], reason='详情页已确认无自写文字(photo)', empty_kind='photo')
        self.assertEqual(summary['notices'][0]['items'], [expected])
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['partial'], [])
        source = reg.subscription('social', self.source['id'])
        self.assertIn(expected['reason'], source['last_notice'])
        self.assertEqual(x_channels.watermark(source, 'browser'), entry['x_observed_at'])
        x_channels.reset()
        self.assertEqual(store.delivery(source)[2], [expected])

    def test_real_automatic_sync_retries_the_body_without_rewinding_discovery(self):
        entry = self.entry(truncated=True)
        with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('暂未展开')):
            first = self.collect_automatic(entry)
        self.assertEqual(first['added'], 0)
        source = reg.subscription('social', self.source['id'])
        self.assertEqual(x_channels.watermark(source, 'browser'), entry['x_observed_at'])
        self.assertEqual(store.delivery(source)[1], 1)
        with patch.object(x_post, 'fetch') as fetch:
            self.assertEqual(self.collect_automatic(entry)['added'], 0)
        fetch.assert_not_called()
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_candidates SET retry_at=0')
        with patch.object(x_post, 'fetch', return_value='a thought with its complete verified ending'):
            self.assertEqual(self.collect_automatic(entry)['added'], 1)
        self.assertEqual(store.delivery(source)[1], 0)


class FeedgrabCheckpointTests(FeedgrabRound):
    def test_day_only_feedgrab_checkpoint_keeps_its_shanghai_calendar_date(self):
        source = self.start_from()
        self.cli(example=dict(display='Example Person', tweets=[tweet()]))
        summary = self.collect()
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['errors'], [])
        self.assertEqual(self.rows(source['id'])['items'][0]['published'], TODAY)
        self.assertEqual(self.water(source['id']), TODAY)
