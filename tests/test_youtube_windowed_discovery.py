import errno
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import (collector, discovery, media, notes, ranking, storage, sync_window,
                        throttle, toolchain, windowed_discovery)
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

    def command(self, payloads=(), diagnostic='', returncode=101, delay=0):
        output = '\n'.join([*(json.dumps(payload) for payload in payloads), diagnostic]) + '\n'
        script = f'import sys,time;sys.stdout.write({output!r});sys.stdout.flush();time.sleep({delay});sys.exit({returncode})'
        return [sys.executable, '-u', '-c', script]

    def test_real_date_boundary_exit_101_is_complete_for_entries_and_empty_window(self):
        diagnostic = '[download] old does not pass filter (upload_date>=?20261028), skipping ..\nAborting remaining downloads'
        for payloads in ([], [self.payload(1, self.now), self.payload(2, self.now)]):
            evidence = []
            with self.subTest(count=len(payloads)), throttle.opened(), patch.object(
                    toolchain, 'command', return_value=self.command(payloads, diagnostic)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                listed = self.fetch(notes=evidence)
            self.assertEqual(len(payloads), len(listed))
            self.assertEqual([], evidence)

    def test_real_boundary_completion_does_not_pollute_channel_notices(self):
        payloads = [self.payload(1, self.now), self.payload(2, self.now)]
        diagnostic = '[download] old does not pass filter (upload_date>=?20261028), skipping ..\nAborting remaining downloads'
        with patch.object(toolchain, 'command', return_value=self.command(payloads, diagnostic)), \
                patch.object(media.login, 'arguments', return_value=[]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(self.scope)
        self.assertEqual(2, result['discovered'])
        self.assertEqual([], result['partial'])
        self.assertEqual([], result['failed_ids'])
        self.assertNotIn('没列完', result['message'])
        stored = reg.subscription('youtube', self.source['id'])
        self.assertTrue(stored['last_success'])
        self.assertFalse(stored['last_notice'])

    def test_real_unexpected_nonzero_exit_still_reports_partial_output(self):
        for returncode, diagnostic in (
                (1, 'ERROR: request failed'),
                (101, 'Aborting remaining downloads'),
                (101, '[download] old does not pass filter\nERROR: request failed'),
                (1, '[download] old does not pass filter')):
            evidence = []
            with self.subTest(code=returncode, diagnostic=diagnostic), throttle.opened(), \
                    patch.object(toolchain, 'command', return_value=self.command(
                        [self.payload(1, self.now)], diagnostic, returncode)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                listed = self.fetch(notes=evidence)
            self.assertEqual(1, len(listed))
            self.assertIn('中断', evidence[0])

    def test_item_unavailability_does_not_make_a_proven_complete_scan_partial(self):
        for message in ('Private video', 'Join this channel to get access to members-only content',
                        'The uploader has not made this video available in your country'):
            for payloads in ([], [self.payload(1, self.now)]):
                for returncode, ending in (
                        (101, '[download] old does not pass filter\nAborting remaining downloads'),
                        (1, '[download] Finished downloading playlist: fixture')):
                    diagnostic = f'ERROR: [youtube] 00000000999: {message}\n{ending}'
                    evidence = []
                    with self.subTest(message=message, count=len(payloads), code=returncode), \
                            throttle.opened(), patch.object(toolchain, 'command', return_value=self.command(
                                payloads, diagnostic, returncode)), \
                            patch.object(media.login, 'arguments', return_value=[]):
                        listed = self.fetch(notes=evidence)
                    self.assertEqual(len(payloads), len(listed))
                    self.assertEqual([], evidence)
                    self.assertEqual('00000000999', self.prepared['discovery_unavailable'][0]['video_id'])

    def test_unavailable_video_converges_and_remains_a_notice_over_repeated_rounds(self):
        diagnostic = ('ERROR: [youtube] 00000000999: Join this channel to get access to members-only content\n'
                      '[download] Finished downloading playlist: fixture')
        for round_number in range(3):
            payloads = [self.payload(1, self.now)] if round_number == 0 else []
            with self.subTest(round=round_number), patch.object(toolchain, 'command', return_value=self.command(
                    payloads, diagnostic, 1)), patch.object(media.login, 'arguments', return_value=[]), \
                    patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                    patch.object(notes, 'available', return_value=(True, '')), \
                    patch.object(media, 'probe', return_value={'duration': 600}), \
                    patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                    patch.object(notes, 'build', side_effect=self.note):
                result = collector.collect(self.scope)
            self.assertEqual([], result['partial'])
            self.assertEqual([], result['failed_ids'])
            self.assertIn(str(self.source['id']), result['succeeded_ids'])
            self.assertEqual(1, result['notices'][0]['count'])
            source = reg.subscription('youtube', self.source['id'])
            self.assertTrue(source['last_success'])
            self.assertFalse(source['last_error'])
            self.assertEqual(0, source['failures'])
            self.assertIn('1 条视频无法读取', source['last_notice'])
        self.assertEqual('ready', storage.rows()[0]['state'])

    def test_playlist_end_does_not_mask_source_errors_or_unfinished_process(self):
        for diagnostic, returncode, delay in (
                ('ERROR: request failed\n[download] Finished downloading playlist: fixture', 1, 0),
                ('ERROR: [youtube] 00000000999: Video unavailable. Sign in to confirm you are not a bot\n'
                 '[download] Finished downloading playlist: fixture', 1, 0),
                ('ERROR: [youtube] 00000000999: Private video', 1, 0),
                ('ERROR: [youtube] 00000000999: Private video\n'
                 '[download] Finished downloading playlist: fixture', 1, 10)):
            evidence = []
            with self.subTest(diagnostic=diagnostic, delay=delay), throttle.opened(), patch.object(
                    toolchain, 'command', return_value=self.command([self.payload(1, self.now)], diagnostic,
                                                                 returncode, delay)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                listed = windowed_discovery.fetch(self.prepared, self.settings(), time.monotonic() + .5, evidence)
            self.assertEqual(1, len(listed))
            self.assertIn('中断', evidence[0])

    def test_normal_date_completion_keeps_warnings_without_incomplete_listing_claim(self):
        for warning, code in (
                ('WARNING: failed to decrypt cookies', 'cookie_unreadable'),
                ('WARNING: could not find cookies database', 'cookie_unreadable'),
                ('WARNING: HTTP Error 429: Too Many Requests', 'rate_limited'),
                ('WARNING: n challenge solving failed', 'js_challenge'),
                ('WARNING: Sign in to confirm you are not a bot', 'bot_wall')):
            diagnostic = warning + '\n[download] old does not pass filter\nAborting remaining downloads'
            with self.subTest(warning=warning), patch.object(
                    toolchain, 'command', return_value=self.command([self.payload(1, self.now)], diagnostic)), \
                    patch.object(media.login, 'arguments', return_value=[]), \
                    patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                    patch.object(notes, 'available', return_value=(True, '')), \
                    patch.object(media, 'probe', return_value={'duration': 600}), \
                    patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                    patch.object(notes, 'build', side_effect=self.note):
                result = collector.collect(self.scope)
            self.assertEqual([], result['partial'])
            self.assertIn(str(self.source['id']), result['cautions'][code])
            self.assertNotIn('没列完', result['message'])
            self.assertNotIn('没列完', reg.subscription('youtube', self.source['id'])['last_notice'])

    def test_real_timeout_after_boundary_text_is_not_misclassified_as_completion(self):
        command = self.command([self.payload(1, self.now)], '[download] old does not pass filter', delay=10)
        evidence = []
        with throttle.opened(), patch.object(toolchain, 'command', return_value=command), \
                patch.object(media.login, 'arguments', return_value=[]):
            listed = windowed_discovery.fetch(self.prepared, self.settings(), time.monotonic() + .5, evidence)
        self.assertEqual(1, len(listed))
        self.assertIn('中断', evidence[0])

    def test_video_title_cannot_masquerade_as_date_boundary_diagnostic(self):
        evidence = []
        payload = {**self.payload(1, self.now), 'title': 'AI: what does not pass filter mean?'}
        command = self.command([payload], 'Aborting remaining downloads')
        with throttle.opened(), patch.object(toolchain, 'command', return_value=command), \
                patch.object(media.login, 'arguments', return_value=[]):
            listed = self.fetch(notes=evidence)
        self.assertEqual(1, len(listed))
        self.assertIn('中断', evidence[0])

    def test_date_boundary_cancellation_does_not_change_other_listing_routes(self):
        evidence = []
        command = self.command([self.payload(1, self.now)], '[download] old does not pass filter')
        with throttle.opened(), patch.object(toolchain, 'command', return_value=command), \
                patch.object(media.login, 'arguments', return_value=[]):
            listed = discovery.fetch(self.source, self.settings(), estimates=False, notes=evidence)
        self.assertEqual(1, len(listed))
        self.assertIn('中断', evidence[0])

    def test_production_route_reads_real_dates_and_enforces_exact_new_entry_boundary(self):
        payloads = [self.payload(1, self.floor - timedelta(seconds=1)),
                    self.payload(2, self.floor), self.payload(3, self.now)]
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(payloads)):
            listed = self.fetch()
        self.assertFalse(any(entry.get('approximate') for entry in listed))
        accepted = sync_window.recent(self.prepared, listed, self.scope)
        self.assertEqual(['00000000002', '00000000003'], [entry['video_id'] for entry in accepted])
        arguments = self.calls[0]['arguments']
        self.assertNotIn('--break-match-filters', arguments)
        self.assertNotIn('--playlist-end', arguments)
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
                                  '[download] 00000000001 has already been recorded in the archive\n'
                                  '[download] Finished downloading playlist: fixture')
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
        self.assertEqual('youtube 00000000001\nyoutube 00000000002\n', self.calls[-1]['known'])
        self.assertEqual(['00000000002'], [row['video_id'] for row in storage.rows()])
        with reg.db() as connection:
            self.assertEqual(['00000000001'], [row['video_id'] for row in connection.execute(
                'SELECT video_id FROM youtube_discovery_history WHERE source_id=?', (self.source['id'],))])

    def test_listing_failure_retains_initial_origin_without_claiming_success(self):
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(media, 'ytdlp', side_effect=ToolFailure('模拟网络失败')):
            result = collector.collect(self.scope)
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        with reg.db() as connection:
            origin = connection.execute('SELECT initial_fetch_at FROM youtube_sync_origins').fetchone()[0]
        self.assertEqual(self.prepared['initial_fetch_at'], origin)
        source = reg.subscription('youtube', self.source['id'])
        self.assertFalse(source['last_success'])
        self.assertIn('模拟网络失败', source['last_error'])
        self.assertEqual(origin, sync_window.prepare(source, self.scope,
                         self.now + timedelta(days=5))['initial_fetch_at'])

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
