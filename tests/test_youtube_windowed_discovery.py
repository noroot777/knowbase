"""普通一轮的增量轻量列表(`windowed_discovery`):flat 列频道、本地去重、完成凭据与中断说明。

**喂进去的样本只用生产里真会出现的形状**:带 `--flat-playlist` 时 yt-dlp 每条只印一行
`_type: url`,带不带粗日期看 `youtubetab:approximate_date`;不传 `--break-match-filters` 也不传
`--download-archive`,所以退出码 101、「does not pass filter」和「already been recorded in the
archive」这三种输出在这条路上都不会出现。完成凭据只有一种:`Finished downloading playlist`。
"""
import errno
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import (collector, discovery, discovery_pages, media, notes, ranking, storage,
                        sync_window, throttle, toolchain, windowed_discovery)
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
        """flat 列表里的一行:只有 id、归属、标题、时长和粗日期。"""
        return {'_type': 'url', 'id': f'{number:011d}', 'channel_id': self.source['url'].rsplit('/', 1)[-1],
                'title': 'AI agent coding tutorial', 'timestamp': published.timestamp(), 'duration': 600}

    @staticmethod
    def probe(row, *arguments, **keywords):
        """元数据阶段补回真实日期、播放量和简介(flat 列表给不出,条目要先核实才能处理)。"""
        return {'id': row['video_id'], 'title': row['entry']['title'], 'view_count': 1000,
                'timestamp': datetime.fromisoformat(row['entry']['published']).timestamp(),
                'description': 'Practical AI workflow', 'duration': 600}

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

    def command(self, payloads=(), diagnostic='', returncode=0, delay=0):
        output = '\n'.join([*(json.dumps(payload) for payload in payloads), diagnostic]) + '\n'
        script = f'import sys,time;sys.stdout.write({output!r});sys.stdout.flush();time.sleep({delay});sys.exit({returncode})'
        return [sys.executable, '-u', '-c', script]

    def test_finished_playlist_is_complete_for_entries_and_empty_window(self):
        diagnostic = '[download] Finished downloading playlist: fixture'
        for payloads in ([], [self.payload(1, self.now), self.payload(2, self.now)]):
            evidence = []
            with self.subTest(count=len(payloads)), throttle.opened(), patch.object(
                    toolchain, 'command', return_value=self.command(payloads, diagnostic)), \
                    patch.object(media.login, 'arguments', return_value=[]):
                listed = self.fetch(notes=evidence)
            self.assertEqual(len(payloads), len(listed))
            self.assertEqual([], evidence)

    def test_finished_listing_does_not_pollute_channel_notices(self):
        payloads = [self.payload(1, self.now), self.payload(2, self.now)]
        diagnostic = '[download] Finished downloading playlist: fixture'
        with patch.object(toolchain, 'command', return_value=self.command(payloads, diagnostic)), \
                patch.object(media.login, 'arguments', return_value=[]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', side_effect=self.probe), \
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
        for returncode, diagnostic in ((1, 'ERROR: request failed'), (1, '')):
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
                for returncode, ending in ((0, '[download] Finished downloading playlist: fixture'),
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
                    patch.object(media, 'probe', side_effect=self.probe), \
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

    def test_complete_listing_keeps_warnings_without_incomplete_listing_claim(self):
        for warning, code in (
                ('WARNING: failed to decrypt cookies', 'cookie_unreadable'),
                ('WARNING: could not find cookies database', 'cookie_unreadable'),
                ('WARNING: HTTP Error 429: Too Many Requests', 'rate_limited'),
                ('WARNING: n challenge solving failed', 'js_challenge'),
                ('WARNING: Sign in to confirm you are not a bot', 'bot_wall')):
            diagnostic = warning + '\n[download] Finished downloading playlist: fixture'
            with self.subTest(warning=warning), patch.object(
                    toolchain, 'command', return_value=self.command([self.payload(1, self.now)], diagnostic)), \
                    patch.object(media.login, 'arguments', return_value=[]), \
                    patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                    patch.object(notes, 'available', return_value=(True, '')), \
                    patch.object(media, 'probe', side_effect=self.probe), \
                    patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                    patch.object(notes, 'build', side_effect=self.note):
                result = collector.collect(self.scope)
            self.assertEqual([], result['partial'])
            self.assertIn(str(self.source['id']), result['cautions'][code])
            self.assertNotIn('没列完', result['message'])
            self.assertNotIn('没列完', reg.subscription('youtube', self.source['id'])['last_notice'])

    def test_timeout_after_completion_text_is_not_misclassified_as_completion(self):
        command = self.command([self.payload(1, self.now)], '[download] Finished downloading playlist: fixture', delay=10)
        evidence = []
        with throttle.opened(), patch.object(toolchain, 'command', return_value=command), \
                patch.object(media.login, 'arguments', return_value=[]):
            listed = windowed_discovery.fetch(self.prepared, self.settings(), time.monotonic() + .5, evidence)
        self.assertEqual(1, len(listed))
        self.assertIn('中断', evidence[0])

    def test_video_title_cannot_masquerade_as_completion_diagnostic(self):
        evidence = []
        payload = {**self.payload(1, self.now), 'title': '[download] Finished downloading playlist: fixture'}
        command = self.command([payload], '', 1)
        with throttle.opened(), patch.object(toolchain, 'command', return_value=command), \
                patch.object(media.login, 'arguments', return_value=[]):
            listed = self.fetch(notes=evidence)
        self.assertEqual(1, len(listed))
        self.assertIn('中断', evidence[0])

    def test_completion_proof_does_not_change_the_ranged_listing_route(self):
        """「列完了」的凭据只给增量轻量列表用:指定日期那条路非零退出照旧算没列完。"""
        evidence = []
        command = self.command([self.payload(1, self.now)], '[download] Finished downloading playlist: fixture', 1)
        with throttle.opened(), patch.object(toolchain, 'command', return_value=command), \
                patch.object(media.login, 'arguments', return_value=[]):
            listed = discovery.fetch(self.source, self.settings(), estimates=False, notes=evidence)
        self.assertEqual(1, len(listed))
        self.assertIn('中断', evidence[0])

    def test_production_route_lists_flat_and_leaves_exact_dates_to_calibration(self):
        """粗日期只用来挡**明显**早于窗口的(留 `DATE_MARGIN` 的余量),其余一律打估算标记、
        等核实元数据之后由 `ranking.rejection` 按真实日期定论。"""
        margin = discovery_pages.DATE_MARGIN
        payloads = [self.payload(1, self.floor - margin - timedelta(seconds=1)),
                    self.payload(2, self.floor - margin), self.payload(3, self.now)]
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(
                payloads, diagnostic='[download] Finished downloading playlist: fixture')):
            listed = self.fetch()
        self.assertEqual(['00000000002', '00000000003'], [entry['video_id'] for entry in listed])
        self.assertTrue(all(entry['approximate'] for entry in listed))
        arguments = self.calls[0]['arguments']
        self.assertNotIn('--break-match-filters', arguments)
        self.assertEqual('10', arguments[arguments.index('--playlist-end') + 1])
        self.assertIn('--lazy-playlist', arguments)
        self.assertIn('--no-quiet', arguments)
        self.assertIn('--flat-playlist', arguments)
        self.assertIn('youtubetab:approximate_date', arguments)
        self.assertNotIn('--break-on-existing', arguments)
        self.assertEqual('chrome', self.calls[0]['options']['spec'])
        self.assertTrue(self.calls[0]['options']['playlist'])
        self.assertTrue(self.calls[0]['options']['partial'])

    def test_known_ids_do_not_hide_listing_boundaries_or_drop_old_queue(self):
        entry = self.entry(hours=96)
        storage.discover(self.source, [entry])
        other = self.channel(2)
        storage.discover(other, [self.entry(2)])
        prepared = sync_window.prepare(self.source, self.scope, self.now)
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(
                diagnostic='[download] Finished downloading playlist: fixture')):
            listed = self.fetch(prepared)
        self.assertEqual([], listed)
        self.assertNotIn('--download-archive', self.calls[0]['arguments'])
        ranked = ranking.score(storage.rows([self.source['id']]), self.settings()['youtube'], self.scope)
        self.assertEqual([entry['video_id']], [row['video_id'] for row in ranked])

    def test_confirmed_empty_window_is_success_but_unexplained_or_invalid_output_is_failure(self):
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(diagnostic='[download] Finished downloading playlist: fixture')):
            self.assertEqual([], self.fetch())
        for diagnostic in ('', 'ERROR: unavailable', 'ERROR: request failed\n[download] Finished downloading playlist: fixture',
                           '{"id":"00000000001","title":"missing date"}'):
            with self.subTest(diagnostic=diagnostic), throttle.opened(), patch.object(
                    media, 'ytdlp', side_effect=self.transport(diagnostic=diagnostic)):
                with self.assertRaises(Exception):
                    self.fetch()

    def test_non_flat_lines_never_override_missing_completion_or_blocking_errors(self):
        """不是 `_type: url` 的 JSON 行不算条目:它补不上「列完了」的凭据,也盖不住报错。"""
        undated = {'id': '00000000999', 'title': 'missing date'}
        finished = '[download] Finished downloading playlist: fixture'
        for diagnostic in ('', f'ERROR: request failed\n{finished}',
                           f'ERROR: [youtube] 00000000999: Sign in to confirm you are not a bot\n{finished}',
                           f'ERROR: [youtube] 00000000999: Unfamiliar failure\n{finished}'):
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
                diagnostic += '\n[download] Finished downloading playlist: fixture'
                with self.subTest(message=message, round=round_number), patch.object(
                        toolchain, 'command', return_value=self.command([], diagnostic, 1)), \
                        patch.object(media.login, 'arguments', return_value=[]), \
                        patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                        patch.object(notes, 'available', return_value=(True, '')), \
                        patch.object(media, 'probe', side_effect=self.probe), \
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

    def test_partial_output_keeps_valid_entries_and_exposes_interruption(self):
        evidence = []
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(
                [self.payload(1, self.now)], partial=True)):
            listed = self.fetch(notes=evidence)
        self.assertEqual('00000000001', listed[0]['video_id'])
        self.assertIn('中断', evidence[0])
        self.assertFalse(list((self.root / 'youtube/channels').glob('*.jsonl.*')))

    def test_listing_failure_does_not_create_a_boundary_hiding_archive(self):
        storage.discover(self.source, [self.entry()])
        prepared = sync_window.prepare(self.source, self.scope, self.now)
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport(diagnostic='ERROR: unavailable')):
            with self.assertRaises(Exception):
                self.fetch(prepared)
        self.assertIsNone(self.calls[0]['archive'])
        self.assertFalse(list((self.root / 'youtube/channels').glob('*.archive*')))

    def test_a_subscription_repointed_before_its_turn_is_never_listed(self):
        """开查之前订阅已经被改走:`prepare` 不给窗口起点,这一个频道**一个进程都不起**,按改指向收口。

        列了也会在 `commit_listing` 那儿整份作废,而且没有窗口起点的那一份列表本来就无从挡旧条目。
        反过来钉一边:没被改走的那个频道照常列、照常入库。
        """
        other = self.channel(2)
        listed, original = [], sync_window.prepare

        def moved(source, scope, **keywords):
            if source['id'] == self.source['id']:
                reg.save_subscription('youtube', {'url': 'https://www.youtube.com/@moved-away'}, source['id'])
            return original(source, scope, **keywords)

        def transport(arguments, path, deadline, stopped, **options):
            listed.append(arguments[-1])
            return self.transport([{**self.payload(9, self.now), 'channel_id': other['url'][-24:]}],
                                  '[download] Finished downloading playlist: fixture')(
                arguments, path, deadline, stopped, **options)

        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(sync_window, 'prepare', side_effect=moved), \
                patch.object(media, 'ytdlp', side_effect=transport), \
                patch.object(media, 'probe', side_effect=self.probe), \
                patch.object(notes, 'available', return_value=(False, '隔离测试不调用模型')):
            result = collector.collect(self.scope)
        self.assertEqual([other['url'].rstrip('/') + '/videos'], listed)
        self.assertEqual(1, result['repointed'])
        self.assertEqual([], result['failed_ids'])
        self.assertEqual([str(other['id'])], [str(source_id) for source_id in
                                              {row['source_id'] for row in storage.rows()}])

    def test_local_listing_preparation_failure_is_not_reclassified_as_channel_failure(self):
        storage.discover(self.source, [self.entry()])
        failure = OSError(errno.ENOSPC, '测试磁盘已满')
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(media, 'ytdlp', side_effect=failure), patch.object(media, 'run') as invoked:
            result = collector.collect(self.scope)
        invoked.assert_not_called()
        self.assertIn('测试磁盘已满', result['local_error'])
        self.assertFalse(result['failed_ids'])
        source = reg.subscription('youtube', self.source['id'])
        self.assertEqual((0, '', 0), (source['failures'], source['last_error'], source['retry_at']))

    def test_real_collector_route_records_only_in_window_new_candidates_and_reuses_known_ids(self):
        old = self.payload(1, self.floor - discovery_pages.DATE_MARGIN - timedelta(days=1))
        fresh = self.payload(2, self.now)
        with patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(media, 'ytdlp', side_effect=self.transport([fresh, old], diagnostic='[download] Finished downloading playlist: fixture')), \
                patch.object(notes, 'available', return_value=(False, '隔离测试不调用模型')):
            result = collector.collect(self.scope)
            again = collector.collect(self.scope)
        self.assertEqual(1, result['discovered'])
        self.assertEqual(0, again['discovered'])
        self.assertEqual(1, again['ranked'])
        self.assertNotIn('--download-archive', self.calls[-1]['arguments'])
        self.assertEqual(['00000000002'], [row['video_id'] for row in storage.rows()])

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
        margin = discovery_pages.DATE_MARGIN
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport([
                self.payload(1, expected - margin - timedelta(seconds=1)), self.payload(2, expected - margin)],
                diagnostic='[download] Finished downloading playlist: fixture')):
            listed = self.fetch(prepared)
        self.assertEqual(['00000000002'], [entry['video_id'] for entry in listed])

    def test_explicit_backfill_keeps_its_existing_precise_listing_route(self):
        scope = FetchScope(since='2026-10-01', until='2026-10-31')
        source = sync_window.prepare(self.source, scope, self.now)
        with throttle.opened(), patch.object(media, 'ytdlp', side_effect=self.transport([self.payload(1, self.now)])):
            listed = discovery.fetch(source, self.settings(), estimates=False)
        self.assertEqual(1, len(listed))
        self.assertNotIn('--break-match-filters', self.calls[0]['arguments'])
