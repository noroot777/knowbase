import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage
from tests.youtube_fixture import YouTubeFixture


class YouTubeAdmission(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()

    def collect(self, metadata, force=False):
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=metadata) as probe, \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope(force=force))
        return result, probe

    def test_all_subscriptions_admit_before_video_dedup_and_commit_separately(self):
        alias = self.channel(2)
        self.entry_value = self.entry(hours=24 * 6)
        now = datetime.now(timezone.utc)
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               ((now - timedelta(days=5)).isoformat(), self.source['id']))
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               ((now - timedelta(days=30)).isoformat(), alias['id']))
        result, probe = self.collect({'duration': 600})
        self.assertEqual((1, 1, 1), (result['ranked'], result['processed'], result['added']))
        probe.assert_called_once()
        self.assertEqual(1, reg.items('youtube', source_id=alias['id'])['total'])
        self.assertEqual(0, reg.items('youtube', source_id=self.source['id'])['total'])

    def test_highest_eligible_weight_wins_not_insertion_order(self):
        alias = self.channel(2, youtube={'score_multiplier': 3})
        reg.save_subscription('youtube', {'youtube': {'score_multiplier': .1}}, self.source['id'])
        storage.discover(self.source, [self.entry_value])
        storage.discover(alias, [self.entry_value])
        policy = self.settings()['youtube']
        global_row = ranking.score(storage.rows(), policy, FetchScope())[0]
        alias_row = ranking.score(storage.rows([alias['id']]), policy, FetchScope())[0]
        self.assertEqual(alias['id'], global_row['source_id'])
        self.assertEqual((alias_row['heat'], alias_row['value']), (global_row['heat'], global_row['value']))
        reg.save_subscription('youtube', {'youtube': {'pool': 'creator_watchlist'}}, alias['id'])
        global_row = ranking.score(storage.rows(), policy, FetchScope())[0]
        self.assertEqual(self.source['id'], global_row['source_id'])

    def test_missing_duration_waits_then_force_rechecks_and_recovers(self):
        result, _ = self.collect({'duration': None})
        row = storage.rows()[0]
        self.assertEqual(('deferred', 'duration'), (row['state'], row['gate_code']))
        self.assertGreater(row['retry_at'], time.time())
        self.assertNotIn(str(self.source['id']), result['succeeded_ids'])
        result, probe = self.collect({'duration': 600})
        self.assertEqual(0, result['processed'])
        probe.assert_not_called()
        result, probe = self.collect({'duration': 600}, force=True)
        self.assertEqual(1, result['processed'])
        self.assertTrue(probe.call_args.args[-1])

    def test_premiere_and_live_reprobe_after_wait_without_force(self):
        for status in ('is_upcoming', 'is_live'):
            with self.subTest(status=status):
                result, _ = self.collect({'duration': 600, 'live_status': status})
                self.assertEqual(0, result['processed'])
                row = storage.rows()[0]
                self.assertEqual('deferred', row['state'])
                storage.update(row['video_id'], retry_at=0)
                result, probe = self.collect({'duration': 600, 'live_status': 'not_live'})
                self.assertEqual(1, result['processed'])
                self.assertTrue(probe.call_args.args[-1])
                storage.update(row['video_id'], state='candidate', note='', validation='')

    def test_permanent_exclusion_does_not_rank_but_explicit_retry_rechecks(self):
        self.collect({'duration': 60, 'media_type': 'short'})
        self.assertEqual(('excluded', 'shorts'), tuple(storage.rows()[0][field] for field in ('state', 'gate_code')))
        result, probe = self.collect({'duration': 600})
        self.assertEqual(0, result['processed'])
        probe.assert_not_called()
        result, probe = self.collect({'duration': 600}, force=True)
        self.assertEqual(1, result['processed'])
        probe.assert_called_once()

    def test_legacy_untyped_exclusion_is_reopened_on_upgrade(self):
        storage.discover(self.source, [self.entry_value])
        storage.update(self.entry_value['video_id'], state='excluded', reason='没有可靠时长')
        self.assertEqual('legacy', storage.rows()[0]['gate_code'])
        result, probe = self.collect({'duration': 600})
        self.assertEqual(1, result['processed'])
        self.assertTrue(probe.call_args.args[-1])

    def test_real_short_metadata_blocks_watch_and_shorts_urls_before_media(self):
        for number, path in enumerate(('watch?v=', 'shorts/'), 2):
            with self.subTest(path=path):
                entry = self.entry(number)
                metadata = {'id': entry['video_id'], 'duration': 60, 'media_type': 'short',
                            'webpage_url': 'https://www.youtube.com/' + path + entry['video_id']}
                with patch.object(discovery, 'fetch', return_value=[entry]), \
                        patch.object(notes, 'available', return_value=(True, '')), \
                        patch.object(media, 'probe', return_value=metadata), \
                        patch.object(media, 'obtain') as obtain, patch.object(notes, 'build') as build:
                    result = collector.collect(FetchScope(force=True))
                obtain.assert_not_called()
                build.assert_not_called()
                self.assertEqual((0, 0), (result['processed'], result['admitted']))
                row = next(row for row in storage.rows() if row['video_id'] == entry['video_id'])
                self.assertEqual(('excluded', 'shorts'), (row['state'], row['gate_code']))

    def test_short_type_normalization_does_not_exclude_ordinary_short_videos(self):
        policy = self.settings()['youtube']
        for media_type in ('short', 'shorts', ' SHORT '):
            self.assertEqual('shorts', collector.gate({'duration': 60, 'media_type': media_type}, policy).code)
        for media_type in ('video', None, ''):
            self.assertIsNone(collector.gate({'duration': 60, 'media_type': media_type,
                                            'webpage_url': self.entry_value['url']}, policy))
