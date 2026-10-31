import errno
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage, sync_window, throttle
from kb.youtube.diagnosis import ToolFailure
from tests.youtube_fixture import YouTubeFixture


class YouTubeWindowedDiscovery(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.scope = FetchScope()
        self.settings()
        self.now = datetime.now(timezone.utc)
        self.prepared = sync_window.prepare(self.source, self.scope, self.now)
        self.floor = ranking.stamp(self.prepared['discovery_after'])
        self.calls = []

    def payload(self, number, published):
        return {'id': f'{number:011d}', 'channel_id': self.source['url'].rsplit('/', 1)[-1],
                'title': 'AI agent coding tutorial', 'description': 'Practical AI workflow',
                'timestamp': published.timestamp(), 'view_count': 1000, 'duration': 600}

    def transport(self, payloads=(), diagnostic='', partial=False):
        def listed(arguments, path, deadline, stopped, **options):
            archive = Path(arguments[arguments.index('--download-archive') + 1]) if '--download-archive' in arguments else None
            self.calls.append({'arguments': arguments, 'deadline': deadline, 'options': options,
                               'archive': archive, 'known': archive.read_text() if archive else ''})
            output = media.unique(path)
            output.write_text('\n'.join([*(json.dumps(payload) for payload in payloads), diagnostic]))
            if partial:
                raise media.PartialOutput(output, '本轮频道预算已到，保留已返回的条目', RuntimeError('timeout'))
            return output
        return listed

    def fetch(self, source=None, timeout=30, notes=None):
        return discovery.fetch(source or self.prepared, self.settings(), timeout, estimates=True, notes=notes)

    def test_production_route_reads_real_dates_and_enforces_exact_new_entry_boundary(self):
        payloads = [self.payload(1, self.floor - timedelta(seconds=1)),
                    self.payload(2, self.floor), self.payload(3, self.now)]
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(payloads)):
            listed = self.fetch()
        self.assertFalse(any(entry.get('approximate') for entry in listed))
        accepted = sync_window.recent(self.prepared, listed, self.scope)
        self.assertEqual(['00000000002', '00000000003'], [entry['video_id'] for entry in accepted])
        arguments = self.calls[0]['arguments']
        day = self.floor.astimezone(timezone.utc).strftime('%Y%m%d')
        self.assertEqual(f'upload_date>=?{day}', arguments[arguments.index('--break-match-filters') + 1])
        self.assertEqual('20', arguments[arguments.index('--playlist-end') + 1])
        self.assertIn('--no-quiet', arguments)
        self.assertNotIn('--flat-playlist', arguments)
        self.assertNotIn('--break-on-existing', arguments)
        self.assertEqual('chrome', self.calls[0]['options']['spec'])
        self.assertTrue(self.calls[0]['options']['playlist'])
        self.assertTrue(self.calls[0]['options']['partial'])

    def test_known_id_archive_is_source_scoped_temporary_and_does_not_drop_old_queue(self):
        entry = self.entry(hours=96)
        storage.discover(self.source, [entry])
        other = self.channel(2)
        storage.discover(other, [self.entry(2)])
        prepared = sync_window.prepare(self.source, self.scope, self.now)
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(
                diagnostic=f'[download] {entry["video_id"]} has already been recorded in the archive')):
            listed = self.fetch(prepared)
        self.assertEqual([], listed)
        self.assertEqual(f'youtube {entry["video_id"]}\n', self.calls[0]['known'])
        self.assertFalse(self.calls[0]['archive'].exists())
        ranked = ranking.score(storage.rows([self.source['id']]), self.settings()['youtube'], self.scope)
        self.assertEqual([entry['video_id']], [row['video_id'] for row in ranked])

    def test_confirmed_empty_window_is_success_but_unexplained_or_invalid_output_is_failure(self):
        for message in ('[download] Old video does not pass filter (upload_date>=?20261027), skipping ..',
                        '[download] 00000000001 has already been recorded in the archive'):
            with self.subTest(message=message), throttle.opened(), patch.object(
                    media, 'ytdlp', side_effect=self.transport(diagnostic=message)):
                self.assertEqual([], self.fetch())
        for diagnostic in ('', 'ERROR: unavailable',
                           '[download] old does not pass filter\nERROR: request failed',
                           '{"id":"00000000001","title":"missing date"}'):
            with self.subTest(diagnostic=diagnostic), throttle.opened(), patch.object(
                    media, 'ytdlp', side_effect=self.transport(diagnostic=diagnostic)):
                with self.assertRaises(Exception):
                    self.fetch()

    def test_partial_precise_output_keeps_valid_entries_and_exposes_interruption(self):
        evidence = []
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(
                [self.payload(1, self.now)], partial=True)):
            listed = self.fetch(notes=evidence)
        self.assertEqual('00000000001', listed[0]['video_id'])
        self.assertIn('中断', evidence[0])
        self.assertFalse(list((self.root / 'youtube/channels').glob('*.jsonl.*')))

    def test_archive_is_cleaned_when_listing_fails(self):
        storage.discover(self.source, [self.entry()])
        prepared = sync_window.prepare(self.source, self.scope, self.now)
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(diagnostic='ERROR: unavailable')):
            with self.assertRaises(Exception):
                self.fetch(prepared)
        self.assertFalse(self.calls[0]['archive'].exists())

    def test_local_archive_failure_is_not_reclassified_as_channel_failure(self):
        storage.discover(self.source, [self.entry()])
        failure = OSError(errno.ENOSPC, '测试磁盘已满')
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(Path, 'write_text', side_effect=failure), patch.object(media, 'ytdlp') as invoked:
            result = collector.collect(self.scope)
        invoked.assert_not_called()
        self.assertIn('测试磁盘已满', result['local_error'])
        self.assertFalse(result['failed_ids'])
        source = reg.subscription('youtube', self.source['id'])
        self.assertEqual((0, '', 0), (source['failures'], source['last_error'], source['retry_at']))

    def test_real_collector_route_records_only_in_window_new_candidates_and_reuses_known_ids(self):
        old = self.payload(1, self.floor - timedelta(days=2))
        fresh = self.payload(2, self.now)
        first = self.transport([fresh, old])
        repeated = self.transport(diagnostic='[download] 00000000002 has already been recorded in the archive\n'
                                  '[download] Old video does not pass filter (upload_date>=?20261027), skipping ..')
        call_count = 0

        def boundary(*arguments, **options):
            nonlocal call_count
            call_count += 1
            return (first if call_count == 1 else repeated)(*arguments, **options)

        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(media, 'ytdlp', side_effect=boundary), \
                patch.object(notes, 'available', return_value=(False, '隔离测试不调用模型')):
            result = collector.collect(self.scope)
            again = collector.collect(self.scope)
        self.assertEqual(1, result['discovered'])
        self.assertEqual(1, result['outside_initial_window'])
        self.assertEqual(0, again['discovered'])
        self.assertEqual(1, again['ranked'])
        self.assertEqual('youtube 00000000002\n', self.calls[-1]['known'])
        self.assertEqual(['00000000002'], [row['video_id'] for row in storage.rows()])

    def test_listing_failure_does_not_persist_initial_origin(self):
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(media, 'ytdlp', side_effect=ToolFailure('模拟网络失败')):
            result = collector.collect(self.scope)
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        with reg.db() as connection:
            self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_sync_origins').fetchone()[0])

    def test_prior_success_uses_one_day_overlap_without_reinterpreting_queued_entries(self):
        previous = self.now - timedelta(hours=3)
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                               (previous.isoformat(), self.source['id']))
        prepared = sync_window.prepare(self.source, self.scope, self.now)
        expected = previous - timedelta(days=1)
        self.assertEqual(expected, ranking.stamp(prepared['discovery_after']))
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport([
                self.payload(1, expected - timedelta(seconds=1)), self.payload(2, expected)])):
            listed = self.fetch(prepared)
        self.assertEqual(['00000000002'], [entry['video_id'] for entry in sync_window.recent(prepared, listed, self.scope)])

    def test_explicit_backfill_keeps_its_existing_precise_listing_route(self):
        scope = FetchScope(since='2026-10-01', until='2026-10-31')
        source = sync_window.prepare(self.source, scope, self.now)
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport([self.payload(1, self.now)])):
            listed = discovery.fetch(source, self.settings(), estimates=False)
        self.assertEqual(1, len(listed))
        self.assertNotIn('--break-match-filters', self.calls[0]['arguments'])
