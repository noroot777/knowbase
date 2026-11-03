import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import candidate_metadata, discovery, media, notes, ranking, storage, sync_window, throttle
from tests.youtube_fixture import YouTubeFixture


class YouTubeSyncRepair(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.now = datetime.now(timezone.utc)
        self.scope = FetchScope()
        self.settings()
        self.prepared = sync_window.prepare(self.source, self.scope, self.now)
        self.calls = []

    def payload(self, number, published=None):
        return {'_type': 'url', 'id': f'{number:011d}', 'title': 'AI agent implementation',
                'timestamp': published.timestamp() if published else None, 'duration': 600}

    def listing(self, payloads, partial=False):
        def run(arguments, path, deadline, stopped, **options):
            self.calls.append(arguments)
            output = media.unique(path)
            output.write_text('\n'.join([*(json.dumps(value) for value in payloads),
                                           '[download] Finished downloading playlist: fixture']), encoding='utf-8')
            if partial:
                raise media.PartialOutput(output, 'fixture interrupted', RuntimeError('fixture'))
            return output
        return run

    def fetch(self, payloads, source=None, partial=False, evidence=None):
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.listing(payloads, partial)):
            return discovery.fetch(source or self.prepared, self.settings(), notes=evidence)

    def test_flat_scan_never_expands_old_videos_and_does_not_stop_before_a_late_new_video(self):
        old = self.now - timedelta(days=1000)
        payloads = [self.payload(1, self.now), self.payload(2, old), self.payload(3, self.now)]
        with patch.object(media, 'probe') as probe:
            entries = self.fetch(payloads)
        self.assertEqual(['00000000001', '00000000003'], [entry['video_id'] for entry in entries])
        self.assertTrue(all(entry['approximate'] for entry in entries))
        self.assertTrue(all(entry['channel_id'] for entry in entries))
        probe.assert_not_called()
        self.assertIn('--flat-playlist', self.calls[0])
        self.assertNotIn('--playlist-end', self.calls[0])
        self.assertNotIn('--break-match-filters', self.calls[0])
        self.assertFalse(self.prepared['discovery_unavailable'])

    def test_all_old_flat_entries_are_an_empty_window_not_precise_history(self):
        entries = self.fetch([self.payload(1, self.now - timedelta(days=300))])
        self.assertEqual([], entries)
        self.assertFalse(self.prepared['discovery_unavailable'])

    def test_missing_flat_dates_remain_unknown_and_preserve_partial_evidence(self):
        evidence = []
        entries = self.fetch([self.payload(1)], partial=True, evidence=evidence)
        self.assertTrue(entries[0]['approximate'])
        self.assertIn('fixture interrupted', evidence[0])
        self.assertFalse(self.prepared['discovery_unavailable'])

    def test_rounded_boundary_hint_is_not_used_as_the_final_publication_date(self):
        floor = ranking.stamp(self.prepared['discovery_after'])
        entry = self.fetch([self.payload(1, floor - timedelta(days=1))])[0]
        storage.discover(self.prepared, [entry])
        actual = floor + timedelta(minutes=5)
        metadata = {'timestamp': actual.timestamp(), 'view_count': 10, 'duration': 600,
                    'description': 'AI agent implementation'}
        with throttle.opened(), patch.object(media, 'probe', return_value=metadata):
            candidate_metadata.refresh(storage.rows(), self.settings(), self.scope,
                                       time.monotonic() + 10, lambda: False)
        row = storage.rows()[0]
        self.assertEqual(actual, ranking.stamp(row['entry']['published']))
        self.assertFalse(row['entry'].get('approximate'))
        self.assertEqual(1, len(ranking.score([row], self.settings()['youtube'], self.scope)))

    def test_actual_old_unknown_video_cannot_enter_the_initial_five_day_window(self):
        entry = self.fetch([self.payload(1)])[0]
        storage.discover(self.prepared, [entry])
        actual = ranking.stamp(self.prepared['discovery_after']) - timedelta(hours=1)
        metadata = {'timestamp': actual.timestamp(), 'view_count': 10, 'duration': 600,
                    'description': 'AI agent implementation'}
        with throttle.opened(), patch.object(media, 'probe', return_value=metadata):
            candidate_metadata.refresh(storage.rows(), self.settings(), self.scope,
                                       time.monotonic() + 10, lambda: False)
        self.assertEqual([], ranking.score(storage.rows(), self.settings()['youtube'], self.scope))
        self.assertEqual('filtered', storage.rows()[0]['state'])

    def test_failed_first_sync_uses_today_five_days_not_an_old_frozen_request_floor(self):
        later = self.now + timedelta(days=30)
        retried = sync_window.prepare(self.source, self.scope, later)
        expected = later.astimezone(sync_window.TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(expected - timedelta(days=5), ranking.stamp(retried['discovery_after']))
        self.assertEqual(self.prepared['initial_fetch_at'], retried['initial_fetch_at'])

    def test_discovery_floor_is_owned_by_the_subscription_not_the_shared_video(self):
        other = self.channel(2)
        with reg.db() as connection:
            connection.execute("UPDATE subscriptions SET history_floor='' WHERE id=?", (other['id'],))
        entry = self.fetch([self.payload(1)])[0]
        storage.discover(self.prepared, [entry])
        storage.discover(other, [entry])
        row = storage.rows()[0]
        entry = {**row['entry'], 'published': (ranking.stamp(self.prepared['discovery_after']) - timedelta(hours=1)).isoformat()}
        entry.pop('approximate')
        storage.refresh_metadata(row, entry)
        rows = ranking.score(storage.rows(), self.settings()['youtube'], self.scope)
        self.assertEqual([other['id']], rows[0]['eligible_sources'])

    def test_long_but_usable_chinese_facts_are_not_a_false_compression_failure(self):
        value = '这段内容介绍智能体的实现步骤和适用限制。' * 100
        self.assertGreater(len(value), 1000)
        self.assertEqual(value, notes.summary_output(value, '字幕中文资料。', chinese_source=True))
        with self.assertRaisesRegex(RuntimeError, '压缩'):
            notes.summary_output('中' * (notes.CONTEXT_LIMIT + 1), chinese_source=True)

    def test_old_owner_schema_is_migrated_without_loosening_existing_links(self):
        with reg.db() as connection:
            connection.execute('ALTER TABLE youtube_video_sources DROP COLUMN discovery_after')
            storage.initialize(connection)
            self.assertIn('discovery_after', {row['name'] for row in connection.execute(
                'PRAGMA table_info(youtube_video_sources)')})
