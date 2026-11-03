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
                        'The uploader has not made this video available in your country',
                        'Video unavailable', 'This video has been removed by the uploader',
                        'This video is available to Music Premium members only',
                        'This video has been deleted'):
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
        for round_number in range(3):
            message = ('Private video', 'Video unavailable',
                       'This video is available to Music Premium members only')[round_number]
            diagnostic = f'ERROR: [youtube] 00000000999: {message}\n[download] Finished downloading playlist: fixture'
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
            self.assertNotIn('bot_wall', result['cautions'])
            self.assertIn('1 条视频无法读取', source['last_notice'])
            self.assertEqual(round_number == 0, '其他条目继续处理' in source['last_notice'])
            self.assertNotIn('会员、私有或地区限制', source['last_notice'])
        self.assertEqual('ready', storage.rows()[0]['state'])

    def test_playlist_end_does_not_mask_source_errors_or_unfinished_process(self):
        for diagnostic, returncode, delay in (
                ('ERROR: request failed\n[download] Finished downloading playlist: fixture', 1, 0),
                ('ERROR: [youtube:tab] @fixture: Sign in to confirm you are not a bot\n'
                 '[download] Finished downloading playlist: fixture', 1, 0),
                ('ERROR: [youtube:tab] @fixture: HTTP Error 429: Too Many Requests\n'
                 '[download] Finished downloading playlist: fixture', 1, 0),
                ('ERROR: failed to decrypt cookies\n'
                 '[download] Finished downloading playlist: fixture', 1, 0),
                ('ERROR: [youtube:tab] @fixture: Please sign in to view this video\n'
                 '[download] Finished downloading playlist: fixture', 1, 0),
                ('ERROR: [youtube:tab] @fixture: n challenge solving failed\n'
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
        self.assertIn('--flat-playlist', arguments)
        self.assertIn('youtubetab:approximate_date', arguments)
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

    def test_empty_archived_listing_does_not_require_a_stop_reason(self):
        diagnostic = '[download] 00000000001 has already been recorded in the archive'
        with throttle.opened(), patch.object(windowed_discovery, 'stop_reason', return_value=''), \
                patch.object(media, 'ytdlp', side_effect=self.transport(diagnostic=diagnostic)):
            self.assertEqual([], self.fetch())

    def test_complete_listing_can_contain_only_entries_without_readable_dates(self):
        undated = [{'id': '00000000991', 'title': 'missing date'},
                   {'id': '00000000992', 'upload_date': '20260230'},
                   {'id': '00000000993', 'timestamp': float('inf')},
                   {'id': '00000000994', 'timestamp': 'unreadable'}]
        archive = '[download] known has already been recorded in the archive'
        finished = '[download] Finished downloading playlist: fixture'
        for diagnostic, returncode in ((archive, 0), (finished, 0), (finished, 1), (archive + '\n' + finished, 1)):
            for usable in ([], [self.payload(1, self.now)]):
                evidence = []
                with self.subTest(diagnostic=diagnostic, code=returncode, usable=bool(usable)), throttle.opened(), \
                        patch.object(toolchain, 'command', return_value=self.command(
                            [*undated, *usable], diagnostic, returncode)), \
                        patch.object(media.login, 'arguments', return_value=[]):
                    entries = self.fetch(notes=evidence)
                self.assertEqual(len(usable), len(entries))
                self.assertEqual([], evidence)
                self.assertEqual({item['id'] for item in undated},
                                 {item['video_id'] for item in self.prepared['discovery_unavailable']})
                self.assertTrue(all(item['code'] == 'undated' for item in self.prepared['discovery_unavailable']))

    def test_undated_entries_never_override_missing_completion_or_blocking_errors(self):
        undated = {'id': '00000000999', 'title': 'missing date'}
        finished = '[download] Finished downloading playlist: fixture'
        archive = '[download] known has already been recorded in the archive'
        for diagnostic in ('', f'ERROR: request failed\n{archive}\n{finished}',
                           f'ERROR: [youtube] 00000000999: Sign in to confirm you are not a bot\n{archive}\n{finished}',
                           f'ERROR: [youtube] 00000000999: Unfamiliar failure\n{archive}\n{finished}'):
            with self.subTest(diagnostic=diagnostic), throttle.opened(), patch.object(
                    toolchain, 'command', return_value=self.command([undated], diagnostic, 1)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                with self.assertRaises(discovery.FeedError):
                    self.fetch()
        evidence = []
        with throttle.opened(), patch.object(toolchain, 'command', return_value=self.command(
                [undated], finished, 0, delay=2)), patch.object(media.login, 'arguments', return_value=[]):
            self.assertEqual([], windowed_discovery.fetch(
                self.prepared, self.settings(), time.monotonic() + .5, evidence))
        self.assertTrue(evidence)
        self.assertIn('中断', evidence[0])

    def test_missing_date_video_does_not_backoff_the_channel_or_block_later_videos(self):
        undated = {'id': '00000000999', 'title': 'missing date'}
        finished = '[download] Finished downloading playlist: fixture'
        for round_number in range(3):
            payloads = [undated]
            if round_number != 1:
                payloads.append(self.payload(round_number + 1, self.now))
            diagnostic = finished if round_number == 0 else (
                '[download] known has already been recorded in the archive\n' + finished)
            with self.subTest(round=round_number), patch.object(toolchain, 'command', return_value=self.command(
                    payloads, diagnostic, 0)), patch.object(media.login, 'arguments', return_value=[]), \
                    patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                    patch.object(notes, 'available', return_value=(True, '')), \
                    patch.object(media, 'probe', return_value={'duration': 600}), \
                    patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                    patch.object(notes, 'build', side_effect=self.note):
                result = collector.collect(self.scope)
            self.assertEqual([], result['failed_ids'])
            self.assertEqual([], result['partial'])
            self.assertIn(str(self.source['id']), result['succeeded_ids'])
            self.assertEqual(int(round_number != 1), result['discovered'])
            stored = reg.subscription('youtube', self.source['id'])
            self.assertEqual(('', 0, 0), tuple(stored[key] for key in ('last_error', 'failures', 'retry_at')))
            self.assertTrue(stored['last_success'])
            self.assertIn('1 条缺少可靠发布日期', stored['last_notice'])
            self.assertEqual('undated', result['notices'][0]['items'][0]['code'])
        self.assertEqual(['ready', 'ready'], [row['state'] for row in storage.rows()])
        self.assertEqual(2, len(collector.YouTubeCollector('youtube').materialize()))

    def test_other_dated_listing_routes_still_require_usable_entries_without_window_proof(self):
        payload = {'id': '00000000999', 'title': 'missing date'}
        with throttle.opened(), patch.object(toolchain, 'command', return_value=self.command(
                [payload], '[download] Finished downloading playlist: fixture', 0)), \
                patch.object(media.login, 'arguments', return_value=[]):
            with self.assertRaises(discovery.FeedError):
                discovery.fetch(self.source, self.settings(), estimates=False)

    def test_source_error_with_no_new_json_still_fails_after_playlist_end(self):
        for message, code in (('Sign in to confirm you are not a bot', 'bot_wall'),
                              ('HTTP Error 429: Too Many Requests', 'rate_limited'),
                              ('failed to decrypt cookies', 'cookie_unreadable'),
                              ('Please sign in to view this video', 'not_logged_in')):
            diagnostic = f'ERROR: [youtube:tab] @fixture: {message}\n[download] Finished downloading playlist: fixture'
            with self.subTest(message=message), throttle.opened(), patch.object(
                    toolchain, 'command', return_value=self.command([], diagnostic, 1)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                with self.assertRaises(discovery.FeedError) as failed:
                    self.fetch()
                self.assertEqual(code, failed.exception.code)
                self.assertEqual('youtube', failed.exception.origin)
                self.assertIn(message, failed.exception.diagnostic)

    def test_item_auth_errors_with_no_usable_entries_fail_without_advancing_watermark(self):
        for message, code in (
                ('Sign in to confirm you are not a bot', 'bot_wall'),
                ('failed to decrypt cookies', 'cookie_unreadable'),
                ('could not find chrome cookies database', 'cookie_unreadable'),
                ('Please sign in to view this video', 'not_logged_in'),
                ('n challenge solving failed', 'js_challenge'),
                ('HTTP Error 429: Too Many Requests', 'rate_limited')):
            previous = (self.now - timedelta(days=1)).isoformat()
            with reg.db() as connection:
                connection.execute('UPDATE subscriptions SET last_success=?,failures=0,retry_at=0, '
                                   "last_error='',last_notice='',last_note='' WHERE id=?", (previous, self.source['id']))
            for round_number in range(3):
                diagnostic = '\n'.join(f'ERROR: [youtube] {number:011d}: {message}' for number in range(1, 4))
                if round_number == 1:
                    diagnostic += '\n[download] known has already been recorded in the archive'
                diagnostic += '\n[download] Finished downloading playlist: fixture'
                with self.subTest(message=message, round=round_number), patch.object(
                        toolchain, 'command', return_value=self.command([], diagnostic, 1)), \
                        patch.object(media.login, 'arguments', return_value=[]), \
                        patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                        patch.object(notes, 'available', return_value=(True, '')), \
                        patch.object(media, 'probe', return_value={'duration': 600}), \
                        patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                        patch.object(notes, 'build', side_effect=self.note):
                    result = collector.collect(FetchScope(force=True))
                self.assertIn(str(self.source['id']), result['failed_ids'])
                self.assertIn(str(self.source['id']), result['walls'][code])
                self.assertNotIn(str(self.source['id']), result['succeeded_ids'])
                self.assertFalse(result['notices'])
                self.assertEqual(0, result['discovered'])
                self.assertEqual(0, result['added'])
                stored = reg.subscription('youtube', self.source['id'])
                self.assertEqual(round_number + 1, stored['failures'])
                self.assertGreater(stored['retry_at'], time.time())
                self.assertEqual(previous, stored['last_success'])
                self.assertTrue(stored['last_error'])
                self.assertFalse(stored['last_notice'])

    def test_auth_errors_keep_usable_entries_but_do_not_claim_full_coverage(self):
        for message in ('Sign in to confirm you are not a bot', 'failed to decrypt cookies',
                        'Please sign in to view this video', 'n challenge solving failed',
                        'HTTP Error 429: Too Many Requests'):
            evidence = []
            diagnostic = f'ERROR: [youtube] 00000000999: {message}\n[download] Finished downloading playlist: fixture'
            with self.subTest(message=message), throttle.opened(), patch.object(
                    toolchain, 'command', return_value=self.command([self.payload(1, self.now)], diagnostic, 1)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                entries = self.fetch(notes=evidence)
            self.assertEqual(1, len(entries))
            self.assertEqual([], self.prepared['discovery_unavailable'])
            self.assertEqual(1, len(evidence))
            self.assertIn('中断', evidence[0])

    def test_unknown_item_errors_never_become_successful_empty_scans_or_cookie_walls(self):
        for message in ("Could not find JS function 'decodeURIComponent'",
                        'Unable to download webpage: [Errno 2] No such file or directory',
                        'Unable to download webpage: HTTP Error 403: permission denied',
                        'Could not copy chapter list from the webpage',
                        "This content isn't available, try again later", 'Sign in to confirm your age',
                        'Failed to extract any player response', 'An unfamiliar error for this video'):
            for returncode in (0, 1):
                previous = (self.now - timedelta(days=1)).isoformat()
                with reg.db() as connection:
                    connection.execute('UPDATE subscriptions SET last_success=?,failures=0,retry_at=0, '
                                       "last_error='',last_notice='',last_note='' WHERE id=?", (previous, self.source['id']))
                diagnostic = (f'ERROR: [youtube] 00000000999: {message}\n'
                              '[download] known has already been recorded in the archive\n'
                              '[download] Finished downloading playlist: fixture')
                with self.subTest(message=message, exit=returncode), patch.object(
                        toolchain, 'command', return_value=self.command([], diagnostic, returncode)), \
                        patch.object(media.login, 'arguments', return_value=[]), \
                        patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                        patch.object(notes, 'available', return_value=(True, '')):
                    result = collector.collect(FetchScope(force=True))
                self.assertIn(str(self.source['id']), result['failed_ids'])
                self.assertNotIn(str(self.source['id']), result['succeeded_ids'])
                self.assertEqual({}, result['walls'])
                self.assertFalse(result['notices'])
                self.assertNotIn('cookie_unreadable', result['cautions'])
                stored = reg.subscription('youtube', self.source['id'])
                self.assertEqual(previous, stored['last_success'])
                self.assertEqual(1, stored['failures'])
                self.assertGreater(stored['retry_at'], time.time())

    def test_unknown_errors_with_entries_keep_data_but_withhold_coverage_even_on_exit_zero(self):
        for returncode in (0, 1):
            diagnostic = ('ERROR: [youtube] 00000000999: Failed to extract any player response\n'
                          '[download] Finished downloading playlist: fixture')
            evidence = []
            with self.subTest(exit=returncode), throttle.opened(), patch.object(
                    toolchain, 'command', return_value=self.command([self.payload(1, self.now)], diagnostic, returncode)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                entries = self.fetch(notes=evidence)
            self.assertEqual(1, len(entries))
            self.assertEqual([], self.prepared['discovery_unavailable'])
            self.assertTrue(evidence)

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

    def test_prior_success_keeps_overlap_and_configured_lookback_without_reinterpreting_queue(self):
        previous = self.now - timedelta(hours=3)
        with reg.db() as connection:
            connection.execute('DELETE FROM youtube_sync_origins WHERE source_id=?', (self.source['id'],))
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                               (previous.isoformat(), self.source['id']))
        prepared = sync_window.prepare(self.source, self.scope, self.now)
        self.assertNotIn('initial_fetch_at', prepared)
        expected = min(previous - timedelta(days=1), self.now.astimezone(sync_window.TZ).replace(
            hour=0, minute=0, second=0, microsecond=0) - timedelta(days=7))
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
