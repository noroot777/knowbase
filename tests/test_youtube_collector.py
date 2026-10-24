import json
import time
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, ranking, storage
from tests.youtube_fixture import YouTubeFixture


class YouTubeCollector(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.fetched = [self.entry(number, views=number * 1000) for number in range(1, 6)]
        self.calls = []

    def collect(self, **values):
        self.settings(**values)

        def probe(row, *rest):
            self.calls.append(row['video_id'])
            self.assertEqual(5, len(storage.rows()))
            self.assertTrue(all(row['heat_rank'] for row in storage.rows()))
            return {'duration': 600, 'language': 'en'}

        with patch.object(discovery, 'fetch', return_value=self.fetched), \
                patch.object(collector.notes, 'available', return_value=(True, '')), \
                patch.object(collector.media, 'probe', side_effect=probe), \
                patch.object(collector.media, 'obtain', return_value={}), \
                patch.object(collector.notes, 'build', side_effect=self.note):
            return collector.collect(FetchScope())

    def test_rank_before_media_and_count_budget_then_dedup(self):
        result = self.collect(max_videos=2)
        self.assertEqual(2, result['processed'])
        self.assertEqual(2, result['added'])
        self.assertEqual(2, len(self.calls))
        self.assertEqual(2, len([row for row in storage.rows() if row['committed']]))
        previous = set(self.calls)
        self.calls.clear()
        result = self.collect(max_videos=2)
        self.assertEqual(2, result['added'])
        self.assertFalse(previous.intersection(self.calls))

    def test_total_minutes_prevents_download_and_translation(self):
        result = self.collect(max_videos=3, max_minutes=15)
        self.assertEqual(1, result['processed'])
        self.assertEqual(10, result['video_minutes'])
        self.assertTrue(any('累计' in row['reason'] for row in storage.rows()))

    def test_failed_video_preserves_good_notes_and_retry_backoff(self):
        with patch.object(discovery, 'fetch', return_value=self.fetched), \
                patch.object(collector.notes, 'available', return_value=(True, '')), \
                patch.object(collector.media, 'probe', return_value={'duration': 600}), \
                patch.object(collector.media, 'obtain', side_effect=[RuntimeError('字幕访问失败'), {}, {}]), \
                patch.object(collector.notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope())
        self.assertEqual(2, result['added'])
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        failed = next(row for row in storage.rows() if row['state'] == 'failed')
        self.assertGreater(failed['retry_at'], time.time())
        self.assertNotIn(failed, ranking.select(storage.rows(), self.settings()['youtube']))
        self.assertIn(failed, ranking.select(storage.rows(), self.settings()['youtube'], force=True))

    def test_no_models_means_ranking_only(self):
        with patch.object(discovery, 'fetch', return_value=self.fetched), \
                patch.object(collector.notes, 'available', return_value=(False, '请配置模型')), \
                patch.object(collector.media, 'probe') as probe:
            result = collector.collect(FetchScope())
        self.assertEqual(5, result['ranked'])
        self.assertEqual(0, result['processed'])
        self.assertEqual(1, result['unconfigured'])
        probe.assert_not_called()

    def test_repointed_or_disabled_channel_cannot_commit(self):
        storage.discover(self.source, [self.fetched[0]])
        row = storage.rows()[0]
        reg.save_subscription('youtube', {'enabled': False}, self.source['id'])
        self.assertFalse(collector.commit_notes(self.source, [], {'added': 0, 'message': ''}))
        self.assertEqual([], storage.rows())
        self.assertEqual(row['source_id'], self.source['id'])

    def test_filters_and_duration_gates(self):
        self.assertIn('直播', collector.gate({'duration': 60, 'was_live': True}, self.settings()['youtube']))
        self.assertIn('Shorts', collector.gate({'duration': 60, 'media_type': 'shorts'}, self.settings()['youtube']))
        for duration in (float('nan'), -1, True, None):
            self.assertTrue(collector.gate({'duration': duration}, self.settings()['youtube']))
        entries = [self.entry(1), {**self.entry(2), 'title': 'Travel weekend', 'description': ''}]
        storage.discover(self.source, entries)
        ranked = ranking.score(storage.rows(), self.settings()['youtube'], FetchScope())
        self.assertEqual(1, len(ranked))
        self.assertEqual('filtered', next(row for row in storage.rows() if row['video_id'] == entries[1]['video_id'])['state'])

    def test_repointed_channel_rediscovers_without_accepting_old_callback(self):
        storage.discover(self.source, self.fetched[:1])
        updated = reg.save_subscription('youtube', {'url': 'https://www.youtube.com/@new-channel'},
                                        self.source['id'])
        storage.discover(self.source, self.fetched[1:2])
        self.assertEqual([], storage.rows())
        storage.discover(updated, self.fetched[:1])
        current = storage.rows()
        self.assertEqual(1, len(current))
        self.assertEqual(updated['url'], current[0]['source_url'])
        self.assertEqual(self.fetched[0]['video_id'], current[0]['video_id'])

    def test_chinese_notes_export_through_existing_pipeline(self):
        self.collect(max_videos=1)
        with patch('kb.feeds.translate.available', return_value=False):
            result = collector.YouTubeCollector('youtube').materialize()
        self.assertEqual(1, len(result))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        self.assertIn('智能体实操教程', files[0].read_text())
        self.assertIn('完整中文转录', files[0].read_text())

    def test_explicit_range_bypasses_history_floor_but_not_requested_range(self):
        entry = {**self.entry(), 'published': '2026-01-02T22:00:00+08:00'}
        storage.discover(self.source, [entry])
        row = storage.rows()[0]
        storage.update(row['video_id'], state='ready', note=json.dumps(self.note(row)))
        summary = {'added': 0, 'message': ''}
        self.assertTrue(collector.commit_notes(self.source, storage.ready(self.source), summary))
        self.assertEqual(0, summary['added'])
        with patch.object(discovery, 'fetch', return_value=[]):
            result = collector.collect(FetchScope(since='2026-01-01', until='2026-01-02'))
        self.assertEqual(1, result['added'])

    def test_preexisting_failure_not_cleared_by_budget_deferral(self):
        storage.discover(self.source, self.fetched[:1])
        storage.failed(storage.rows()[0], RuntimeError('字幕失败'))
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_error=?,failures=1 WHERE id=?',
                               ('字幕失败', self.source['id']))
        with patch.object(discovery, 'fetch', return_value=[]), \
                patch.object(collector.notes, 'available', return_value=(True, '')):
            result = collector.YouTubeCollector('youtube').fetch(FetchScope())
        self.assertFalse(result.whole)
        self.assertEqual((), result.targets)
        self.assertEqual('字幕失败', reg.subscriptions('youtube')[-1]['last_error'])
