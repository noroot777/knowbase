from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.intake_diag import sources as source_diagnostics
from kb.youtube import collector, diagnosis, discovery, media, notes, source_status, storage
from tests.youtube_fixture import YouTubeFixture


class YouTubeSourceStatus(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.sources = [self.channel(number) for number in range(1, 6)]
        self.settings(cookies_browser='firefox')

    def collect(self, fetch):
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'firefox'}), \
                patch.object(collector, 'DISCOVERY_SPACING', 0), \
                patch.object(discovery, 'fetch', side_effect=fetch), \
                patch.object(notes, 'available', return_value=(False, '本测试不调用模型')):
            return collector.collect(FetchScope())

    def summary(self, **values):
        return {'added': 0, 'stopped': False, 'message': '全局汇总中其他频道的错误不能落到本订阅',
                'errors': [], 'failed_ids': [], 'succeeded_ids': [], 'notices': [], 'partial': [],
                'walls': {}, 'cautions': {}, 'browser': 'firefox', **values}

    def test_notices_and_partial_state_are_owned_by_source_id_even_when_names_collide(self):
        for source in self.sources[:2]:
            reg.save_subscription('youtube', {'name': '同名频道'}, source['id'])
        identities = [source['id'] for source in self.sources]

        def fetch(source, settings, timeout, estimates, evidence):
            if source['id'] in identities[:2]:
                count = identities.index(source['id']) + 1
                source['discovery_unavailable'] = [{'video_id': str(index).zfill(11), 'reason': '条目无法读取'}
                                                   for index in range(count)]
            elif source['id'] == identities[3]:
                evidence.append('只列到 7 条就中断了：频道预算用尽')
            elif source['id'] == identities[4]:
                diagnosis.record('WARNING: failed to decrypt cookies')
            return []

        summary = self.collect(fetch)
        first, second, clean, partial, warned = [reg.subscription('youtube', identity) for identity in identities]
        self.assertIn('1 条视频无法读取', first['last_notice'])
        self.assertNotIn('2 条视频', first['last_notice'])
        self.assertIn('2 条视频无法读取', second['last_notice'])
        self.assertNotIn('1 条视频', second['last_notice'])
        self.assertEqual(('', ''), (clean['last_note'], clean['last_notice']))
        self.assertIn('频道没列完', partial['last_note'])
        self.assertIn('7 条', partial['last_note'])
        self.assertEqual(('', '', 0), (partial['last_success'], partial['last_error'], partial['failures']))
        self.assertEqual('', partial['last_notice'])
        self.assertNotIn(str(partial['id']), summary['succeeded_ids'])
        self.assertEqual('频道 4', summary['partial'][0]['title'])
        self.assertEqual(partial['id'], summary['partial'][0]['source_id'])
        self.assertIn('Firefox', warned['last_notice'])
        self.assertNotIn('Firefox', first['last_notice'])
        self.assertIn('同名频道：1 条视频无法读取', summary['message'])
        self.assertEqual({identities[0], identities[1]}, {row['source_id'] for row in summary['notices']})
        recovered = self.collect(lambda *arguments: [])
        self.assertEqual({str(identity) for identity in identities}, set(recovered['succeeded_ids']))
        for identity in identities:
            stored = reg.subscription('youtube', identity)
            self.assertEqual(('', ''), (stored['last_note'], stored['last_notice']))
            self.assertTrue(stored['last_success'])

    def test_partial_notification_does_not_clear_the_original_failure_or_watermark(self):
        source = self.sources[0]
        previous = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
        with reg.db() as connection:
            connection.execute('''UPDATE subscriptions SET last_success=?,last_error='原有失败',
                failures=3,retry_at=123 WHERE id=?''', (previous, source['id']))

        def fetch(selected, settings, timeout, estimates, evidence):
            if selected['id'] == source['id']:
                evidence.append('只列到 9 条就中断了：等待下轮续扫')
            return []

        result = self.collect(fetch)
        stored = reg.subscription('youtube', source['id'])
        self.assertEqual((previous, '原有失败', 3, 123), tuple(
            stored[key] for key in ('last_success', 'last_error', 'failures', 'retry_at')))
        self.assertIn('9 条', stored['last_note'])
        self.assertNotIn(str(source['id']), result['succeeded_ids'])
        other = reg.subscription('youtube', self.sources[1]['id'])
        self.assertEqual(('', ''), (other['last_note'], other['last_notice']))

    def test_both_commit_boundaries_ignore_global_messages_and_other_source_facts(self):
        first, second = self.sources[:2]
        summary = self.summary(notices=[{'source_id': first['id'], 'reason': '仅第一个频道跳过了 2 条'}],
                               partial=[{'source_id': first['id'], 'reason': '仅第一个频道未完成'}])
        self.assertTrue(collector.commit_notes(second, [], summary, pending_failure=True))
        stored = reg.subscription('youtube', second['id'])
        self.assertEqual('', stored['last_note'])
        self.assertNotIn('第一个', stored['last_notice'])
        self.assertIn('未完成处理', stored['last_notice'])
        collector.commit_successes([second], summary)
        stored = reg.subscription('youtube', second['id'])
        self.assertEqual(('', ''), (stored['last_note'], stored['last_notice']))
        self.assertTrue(stored['last_success'])

    def test_content_backlog_is_not_a_source_coverage_problem(self):
        source = self.sources[0]
        entries = [self.entry(number) for number in range(1, 6)]
        with patch.object(media, 'probe', return_value={'duration': 600}):
            result = self.collect(lambda selected, *arguments: entries if selected['id'] == source['id'] else [])
        stored = reg.subscription('youtube', source['id'])
        self.assertEqual(5, len(storage.rows([source['id']])))
        self.assertFalse(result['partial'])
        self.assertEqual('', stored['last_note'])
        self.assertIn('未完成处理', stored['last_notice'])
        self.assertFalse(stored['last_error'])
        self.assertFalse([item for item in source_diagnostics.problems('youtube')
                          if item['source_id'] == source['id']])

    def test_only_actual_listing_partial_creates_a_source_coverage_problem(self):
        source = self.sources[0]

        def fetch(selected, settings, timeout, estimates, evidence):
            if selected['id'] == source['id']:
                evidence.append('只列到 7 条就中断了：频道预算用尽')
            return []

        self.collect(fetch)
        problems = [item for item in source_diagnostics.problems('youtube')
                    if item['source_id'] == source['id']]
        self.assertEqual(1, len(problems))
        self.assertEqual('partial', problems[0]['problem'])
        self.assertIn('7 条', problems[0]['evidence'])

    def test_same_source_warning_is_preserved_but_not_repeated_as_caution(self):
        first, second = self.sources[:2]
        summary = self.summary(walls={'bot_wall': [str(first['id'])]},
                               cautions={'bot_wall': [str(first['id']), str(second['id'])]})
        self.assertEqual(('', collector.login.describe('bot_wall', 'firefox')),
                         source_status.fields(first, summary))
        note, notice = source_status.fields(second, summary)
        self.assertEqual('', note)
        self.assertEqual(collector.login.cautioned('bot_wall', 'firefox'), notice)

    def test_failure_boundary_assigns_identity_to_new_skip_notices(self):
        error = RuntimeError('本频道请求失败')
        error.unavailable = [{'video_id': '00000000999', 'reason': '本频道不可读视频'}]
        error.notice = '本频道有 1 条无法读取'
        summary = self.summary()
        self.assertTrue(collector.save_failure(self.sources[0], error, summary))
        self.assertEqual(self.sources[0]['id'], summary['notices'][0]['source_id'])
        self.assertEqual(('', ''), source_status.fields(self.sources[1], summary))
