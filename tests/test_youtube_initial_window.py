import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import (candidate_metadata, collector, discovery, media, notes, ranking,
                        storage, sync_window, throttle)
from tests.youtube_fixture import YouTubeFixture


class YouTubeInitialWindow(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.scope = FetchScope()
        self.now = datetime.now(timezone.utc)

    def prepare(self, source=None, scope=None, when=None):
        return sync_window.prepare(source or self.source, scope or self.scope, when or self.now)

    def test_missing_fetch_time_starts_three_shanghai_calendar_days_ago(self):
        when = datetime(2026, 10, 30, 18, tzinfo=timezone.utc)
        source = self.prepare(when=when)
        self.assertEqual('2026-10-28T00:00:00+08:00', source['initial_fetch_at'])
        stored = reg.subscription('youtube', source['id'])
        for key in ('last_success', 'last_attempt', 'last_error', 'failures', 'retry_at'):
            self.assertEqual(self.source[key], stored[key])
        with reg.db() as connection:
            self.assertEqual(1, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])

    def test_all_channels_use_the_same_round_start_even_when_requests_cross_midnight(self):
        when = datetime(2026, 10, 30, 15, 59, 59, tzinfo=timezone.utc)
        first = self.prepare(when=when)
        second = self.prepare(source=self.channel(2), when=when)
        self.assertEqual(first['initial_fetch_at'], second['initial_fetch_at'])
        self.assertEqual('2026-10-27T00:00:00+08:00', first['initial_fetch_at'])

    def test_retry_after_midnight_reuses_persisted_origin(self):
        first = self.prepare()
        storage.discover(first, [])
        with reg.db() as connection:
            connection.execute('''UPDATE subscriptions SET last_attempt=?,last_error='网络错误',
                failures=3,retry_at=999 WHERE id=?''', (reg.now(), self.source['id']))
        again = self.prepare(when=self.now + timedelta(days=5))
        self.assertEqual(first['initial_fetch_at'], again['initial_fetch_at'])
        self.assertEqual(('网络错误', 3, 999),
                         tuple(again[key] for key in ('last_error', 'failures', 'retry_at')))
        self.assertFalse(again['last_success'])

    def test_existing_success_is_not_replaced_by_a_three_day_default(self):
        previous = (self.now - timedelta(days=6)).isoformat()
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                               (previous, self.source['id']))
        source = self.prepare()
        self.assertNotIn('initial_fetch_at', source)
        self.assertEqual(previous, source['last_success'])
        entries = [self.entry(hours=96)]
        self.assertEqual(entries, sync_window.recent(source, entries, self.scope))

    def test_invalid_previous_timestamp_uses_the_safe_initial_default(self):
        with reg.db() as connection:
            connection.execute("UPDATE subscriptions SET last_success='invalid' WHERE id=?", (self.source['id'],))
        self.assertIsNotNone(ranking.stamp(self.prepare()['initial_fetch_at']))

    def test_later_success_does_not_move_the_original_fallback(self):
        source = self.prepare()
        storage.discover(source, [])
        succeeded = reg.now()
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?', (succeeded, source['id']))
        refreshed = self.prepare(when=self.now + timedelta(days=5))
        lookback = (self.now + timedelta(days=5)).astimezone(sync_window.TZ).replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7)
        self.assertEqual(min(ranking.stamp(succeeded) - timedelta(days=1), lookback),
                         ranking.stamp(refreshed['discovery_after']))
        with reg.db() as connection:
            self.assertEqual(source['initial_fetch_at'], connection.execute(
                'SELECT initial_fetch_at FROM youtube_sync_origins WHERE source_id=?', (source['id'],)).fetchone()[0])

    def test_exact_dates_use_inclusive_start_and_estimates_remain_unknown(self):
        source = self.prepare()
        floor = ranking.stamp(source['initial_fetch_at'])
        entries = [{**self.entry(1), 'published': (floor - timedelta(seconds=1)).isoformat()},
                   {**self.entry(2), 'published': floor.isoformat()},
                   {**self.entry(3), 'published': (floor - timedelta(days=10)).isoformat(), 'approximate': True},
                   {**self.entry(4), 'published': ''}]
        self.assertEqual(entries[1:], sync_window.recent(source, entries, self.scope))

    def test_existing_estimated_candidate_survives_new_origin_and_metadata_calibration(self):
        entry = {**self.entry(), 'approximate': True, 'description': '', 'views': 0}
        storage.discover(self.source, [entry])
        source = self.prepare()
        floor = ranking.stamp(source['initial_fetch_at'])
        storage.discover(source, [])
        settings = self.settings()
        self.assertEqual(1, len(ranking.score(storage.rows(), settings['youtube'], self.scope)))
        metadata = {'timestamp': (floor - timedelta(hours=1)).timestamp(),
                    'view_count': 1234, 'description': 'AI agent coding tutorial', 'duration': 600}
        with throttle.opened(), patch.object(media, 'probe', return_value=metadata) as probe, \
                patch.object(media, 'obtain') as obtain:
            result = candidate_metadata.refresh(storage.rows(), settings, self.scope,
                                                time.monotonic() + 10, lambda: False)
        probe.assert_called_once()
        obtain.assert_not_called()
        self.assertEqual(1, len(ranking.score(result['rows'], settings['youtube'], self.scope)))
        row = storage.rows()[0]
        self.assertNotIn('approximate', row['entry'])
        self.assertEqual('candidate', row['state'])
        self.assertNotIn('首次同步起点', row['reason'])
        response = self.client.get('/api/intake/youtube/ranking')
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()['counts']['candidate'])

    def test_explicit_date_backfill_neither_initializes_nor_applies_default(self):
        entry = self.entry(hours=96)
        day = ranking.stamp(entry['published']).astimezone(sync_window.TZ).date().isoformat()
        scope = FetchScope(since=day, until=day, force=True)
        self.assertNotIn('initial_fetch_at', self.prepare(scope=scope))
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])
        source = self.prepare()
        self.assertEqual([entry], sync_window.recent(source, [entry], scope))
        storage.discover(source, [entry])
        settings = self.settings()['youtube']
        self.assertEqual(1, len(ranking.score(storage.rows(), settings, self.scope)))
        self.assertEqual(1, len(ranking.score(storage.rows(), settings, scope)))

    def test_force_retry_keeps_previously_queued_candidates(self):
        source = self.prepare()
        storage.discover(source, [self.entry(hours=96)])
        self.assertEqual(1, len(ranking.score(storage.rows(), self.settings()['youtube'], FetchScope(force=True))))

    def test_shared_video_qualifies_for_an_owner_with_existing_history(self):
        source = self.prepare()
        second = self.channel(2)
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?', (reg.now(), second['id']))
        second = self.prepare(source=second)
        entry = self.entry(hours=96)
        storage.discover(source, [entry])
        storage.discover(second, [entry])
        ranked = ranking.score(storage.rows(), self.settings()['youtube'], self.scope)
        self.assertEqual(1, len(ranked))
        self.assertEqual({source['id'], second['id']}, set(ranked[0]['eligible_sources']))

    def test_repointing_clears_old_origin_but_renaming_does_not(self):
        first = self.prepare()
        storage.discover(first, [])
        reg.save_subscription('youtube', {'name': '重命名频道'}, self.source['id'])
        self.assertEqual(first['initial_fetch_at'], self.prepare(when=self.now + timedelta(days=2))['initial_fetch_at'])
        source = reg.save_subscription('youtube', {'url': 'https://www.youtube.com/@changed'}, self.source['id'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])
        self.assertNotEqual(first['initial_fetch_at'],
                            self.prepare(source=source, when=self.now + timedelta(days=2))['initial_fetch_at'])

    def test_deleted_channel_does_not_leave_an_orphaned_origin(self):
        storage.discover(self.prepare(), [])
        reg.delete_subscription('youtube', self.source['id'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])

    def test_old_scan_progress_is_source_scoped_resettable_and_not_a_candidate(self):
        source = self.prepare()
        old = self.entry(hours=96)
        self.assertEqual([], sync_window.recent(source, [old], self.scope))
        storage.discover(source, [])
        again = self.prepare()
        self.assertEqual((old['video_id'],), again['checked_old_ids'])
        self.assertEqual((), again['known_video_ids'])
        self.assertEqual([], storage.rows())
        other = self.prepare(source=self.channel(2))
        self.assertEqual((), other['checked_old_ids'])
        reg.save_subscription('youtube', {'name': '只改名字'}, source['id'])
        self.assertEqual((old['video_id'],), self.prepare()['checked_old_ids'])
        reg.save_subscription('youtube', {'url': 'https://www.youtube.com/channel/UC' + 'Z' * 22}, source['id'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_discovery_history').fetchone()[0])

    def test_widened_window_can_reconsider_old_scan_progress(self):
        source = self.prepare()
        old = self.entry(hours=96)
        sync_window.recent(source, [old], self.scope)
        storage.discover(source, [])
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                               ((self.now - timedelta(days=6)).isoformat(), source['id']))
        widened = self.prepare()
        self.assertEqual((), widened['checked_old_ids'])
        self.assertEqual([old], sync_window.recent(widened, [old], self.scope))
        reg.delete_subscription('youtube', source['id'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_discovery_history').fetchone()[0])

    def test_window_setting_widens_discovery_and_releases_matching_history_ids(self):
        self.settings(window_days=7)
        first = self.prepare()
        original = first['initial_fetch_at']
        entries = [self.entry(1, hours=5 * 24), self.entry(2, hours=20 * 24), self.entry(3, hours=40 * 24)]
        self.assertEqual([], sync_window.recent(first, entries, self.scope))
        storage.discover(first, [])
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                               (self.now.isoformat(), self.source['id']))
        ordinary = self.prepare()
        self.assertEqual({entries[1]['video_id'], entries[2]['video_id']}, set(ordinary['checked_old_ids']))
        self.assertEqual([entries[0]], sync_window.recent(ordinary, entries, self.scope))
        self.settings(window_days=30)
        widened = self.prepare()
        self.assertEqual((entries[2]['video_id'],), widened['checked_old_ids'])
        self.assertEqual(entries[:2], sync_window.recent(widened, entries, self.scope))
        storage.discover(widened, entries[:2])
        self.assertEqual({entry['video_id'] for entry in entries[:2]}, {row['video_id'] for row in storage.rows()})
        with reg.db() as connection:
            self.assertEqual(original, connection.execute(
                'SELECT initial_fetch_at FROM youtube_sync_origins WHERE source_id=?', (self.source['id'],)).fetchone()[0])

    def test_existing_lagging_watermark_is_never_shortened_by_a_small_lookback(self):
        self.settings(window_days=1)
        previous = self.now - timedelta(days=25)
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                               (previous.isoformat(), self.source['id']))
        self.assertEqual(previous - timedelta(days=1), ranking.stamp(self.prepare()['discovery_after']))

    def test_round_records_origin_and_skips_new_old_entries_before_storage(self):
        fetched = [self.entry(1, hours=96), self.entry(2)]
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(discovery, 'fetch', return_value=fetched), \
                patch.object(notes, 'available', return_value=(False, '测试不调用模型')), \
                patch.object(media, 'probe') as probe:
            result = collector.collect(self.scope)
            repeated = collector.collect(self.scope)
        probe.assert_not_called()
        self.assertEqual(1, result['discovered'])
        self.assertEqual(1, result['outside_initial_window'])
        self.assertEqual(1, result['ranked'])
        self.assertEqual(result['initial_fetch_at'], repeated['initial_fetch_at'])
        self.assertEqual([fetched[1]['video_id']], [row['video_id'] for row in storage.rows()])
        self.assertFalse(reg.subscription('youtube', self.source['id'])['last_success'])

    def test_unconfigured_gateway_does_not_claim_a_fetch_origin(self):
        with patch.object(collector.login, 'readiness', return_value={'ready': False, 'message': '未配置登录态'}), \
                patch.object(discovery, 'fetch') as fetch:
            collector.collect(self.scope)
        fetch.assert_not_called()
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])

    def test_failed_first_discovery_freezes_origin_across_days_without_faking_success(self):
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(discovery, 'fetch', side_effect=RuntimeError('临时网络错误')):
            result = collector.collect(self.scope)
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        with reg.db() as connection:
            origin = connection.execute('SELECT initial_fetch_at FROM youtube_sync_origins').fetchone()[0]
        retried = self.prepare(when=self.now + timedelta(days=5))
        self.assertEqual(origin, retried['initial_fetch_at'])
        self.assertFalse(retried['last_success'])
        self.assertEqual('临时网络错误', retried['last_error'])
        self.assertEqual(1, retried['failures'])
        floor = ranking.stamp(origin)
        missed = {**self.entry(), 'published': (floor + timedelta(hours=1)).isoformat()}
        self.assertEqual([missed], sync_window.recent(retried, [missed], self.scope))

    def test_previous_initial_window_filter_is_repaired_without_clearing_error_history(self):
        storage.discover(self.source, [self.entry(hours=96), self.entry(2, hours=96)])
        for row in storage.rows():
            storage.update(row['video_id'], state='filtered', reason='早于首次同步起点（默认从今天回看 3 天）',
                           last_error='旧模型错误' if row['video_id'].endswith('2') else '', attempts=2, retry_at=99)
        storage.discover(self.source, [self.entry(3)])
        storage.update('00000000003', state='filtered', reason='其他日期限制')
        with reg.db() as connection:
            connection.execute("DELETE FROM feed_meta WHERE key='youtube-discovery-window-v2'")
        rows = {row['video_id']: row for row in storage.rows()}
        self.assertEqual('candidate', rows['00000000001']['state'])
        failed = rows['00000000002']
        self.assertEqual(('failed', '旧模型错误', 2, 99),
                         tuple(failed[key] for key in ('state', 'last_error', 'attempts', 'retry_at')))
        self.assertEqual('filtered', rows['00000000003']['state'])
        self.assertEqual('其他日期限制', rows['00000000003']['reason'])

    def test_old_queued_dates_are_not_reclassified_by_new_discovery_origin(self):
        entries = [self.entry(number, hours=hours) for number, hours in enumerate((80, 96, 110), 1)]
        storage.discover(self.source, entries)
        source = self.prepare()
        storage.discover(source, [])
        ranked = ranking.score(storage.rows(), self.settings()['youtube'], self.scope)
        self.assertEqual(3, len(ranked))
        self.assertEqual(entries, sync_window.recent(source, entries, self.scope))
