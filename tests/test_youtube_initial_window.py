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

    def test_retry_after_midnight_reuses_persisted_origin(self):
        first = self.prepare()
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
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?', (reg.now(), source['id']))
        self.assertEqual(source['initial_fetch_at'],
                         self.prepare(when=self.now + timedelta(days=5))['initial_fetch_at'])

    def test_exact_dates_use_inclusive_start_and_estimates_remain_unknown(self):
        source = self.prepare()
        floor = ranking.stamp(source['initial_fetch_at'])
        entries = [{**self.entry(1), 'published': (floor - timedelta(seconds=1)).isoformat()},
                   {**self.entry(2), 'published': floor.isoformat()},
                   {**self.entry(3), 'published': (floor - timedelta(days=10)).isoformat(), 'approximate': True},
                   {**self.entry(4), 'published': ''}]
        self.assertEqual(entries[1:], sync_window.recent(source, entries, self.scope))

    def test_estimated_dates_are_checked_again_after_real_metadata_arrives(self):
        source = self.prepare()
        floor = ranking.stamp(source['initial_fetch_at'])
        entry = {**self.entry(), 'approximate': True, 'description': '', 'views': 0}
        storage.discover(source, [entry])
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
        self.assertEqual([], ranking.score(result['rows'], settings['youtube'], self.scope))
        row = storage.rows()[0]
        self.assertNotIn('approximate', row['entry'])
        self.assertEqual('filtered', row['state'])
        self.assertIn('首次同步起点', row['reason'])
        response = self.client.get('/api/intake/youtube/ranking')
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, response.json()['counts']['filtered'])

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
        self.assertEqual([], ranking.score(storage.rows(), settings, self.scope))
        self.assertEqual(1, len(ranking.score(storage.rows(), settings, scope)))

    def test_force_retry_does_not_bypass_initial_date_window(self):
        source = self.prepare()
        storage.discover(source, [self.entry(hours=96)])
        self.assertEqual([], ranking.score(storage.rows(), self.settings()['youtube'], FetchScope(force=True)))

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
        self.assertEqual([second['id']], ranked[0]['eligible_sources'])

    def test_repointing_clears_old_origin_but_renaming_does_not(self):
        first = self.prepare()
        reg.save_subscription('youtube', {'name': '重命名频道'}, self.source['id'])
        self.assertEqual(first['initial_fetch_at'], self.prepare(when=self.now + timedelta(days=2))['initial_fetch_at'])
        source = reg.save_subscription('youtube', {'url': 'https://www.youtube.com/@changed'}, self.source['id'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])
        self.assertNotEqual(first['initial_fetch_at'],
                            self.prepare(source=source, when=self.now + timedelta(days=2))['initial_fetch_at'])

    def test_deleted_channel_does_not_leave_an_orphaned_origin(self):
        self.prepare()
        reg.delete_subscription('youtube', self.source['id'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])

    def test_round_records_origin_and_skips_known_old_entries_before_storage(self):
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
