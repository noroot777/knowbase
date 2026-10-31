import json
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, storage, toolchain
from tests.youtube_fixture import YouTubeFixture


class ListingClock:
    def __init__(self):
        self.elapsed = time.monotonic()

    def monotonic(self):
        return self.elapsed

    def sleep(self, seconds):
        self.elapsed += seconds

    def time(self):
        return time.time()


class YouTubeWindowCoverage(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.scope = FetchScope()
        self.settings(max_videos=20, max_minutes=240, rank_limit=50)
        self.now = datetime.now(timezone.utc)
        self.budgets = []

    def payloads(self, count):
        return [{'id': f'{number:011d}', 'title': 'Practical AI agent coding tutorial',
                 'description': 'AI workflow implementation and limitations',
                 'channel_id': self.source['url'].rsplit('/', 1)[-1],
                 'timestamp': (self.now - timedelta(minutes=number)).timestamp(),
                 'view_count': 1000, 'duration': 600} for number in range(1, count + 1)]

    def collect_using(self, *, command=None, listing=None, clock=None, configured=True):
        with ExitStack() as stack:
            stack.enter_context(patch.object(collector.login, 'readiness',
                                             return_value={'ready': True, 'browser': 'chrome'}))
            stack.enter_context(patch.object(notes, 'available', return_value=(configured, '隔离验证')))
            stack.enter_context(patch.object(media, 'probe', return_value={'duration': 600}))
            stack.enter_context(patch.object(media, 'obtain', return_value=self.complete_transcript()))
            stack.enter_context(patch.object(notes, 'build', side_effect=self.note))
            if command is not None:
                stack.enter_context(patch.object(toolchain, 'command', return_value=command))
                stack.enter_context(patch.object(media.login, 'arguments', return_value=[]))
            if listing is not None:
                stack.enter_context(patch.object(media, 'ytdlp', side_effect=listing))
            if clock is not None:
                stack.enter_context(patch.object(collector, 'time', clock))
                stack.enter_context(patch.object(discovery, 'time', clock))
            return collector.collect(self.scope)

    def command(self, payloads):
        script = f'''
import json, sys
from pathlib import Path
arguments = sys.argv[1:]
payloads = {payloads!r}
if '--playlist-end' in arguments:
    payloads = payloads[:int(arguments[arguments.index('--playlist-end') + 1])]
known = set()
if '--download-archive' in arguments:
    known = set(Path(arguments[arguments.index('--download-archive') + 1]).read_text().splitlines())
for payload in payloads:
    if 'youtube ' + payload['id'] in known:
        print('[download] ' + payload['id'] + ' has already been recorded in the archive')
    else:
        print(json.dumps(payload))
print('[download] old does not pass filter, skipping ..')
print('Aborting remaining downloads')
sys.exit(101)
'''
        return [sys.executable, '-u', '-c', script]

    def listing(self, payloads, clock, seconds_per_item=4.3, empty_seconds=0):
        def listed(arguments, path, deadline, stopped, **options):
            self.budgets.append(deadline - clock.monotonic())
            known = set()
            if '--download-archive' in arguments:
                known = set(Path(arguments[arguments.index('--download-archive') + 1]).read_text().splitlines())
            output = media.unique(path)
            emitted = []
            clock.sleep(empty_seconds)
            for payload in payloads:
                if 'youtube ' + payload['id'] in known:
                    continue
                if clock.monotonic() + seconds_per_item > deadline:
                    output.write_text('\n'.join(emitted))
                    cause = media.Interrupted('本轮频道时间预算用尽')
                    raise media.PartialOutput(output, str(cause), cause)
                clock.sleep(seconds_per_item)
                emitted.append(json.dumps(payload))
            emitted.append('[download] old does not pass filter, skipping ..')
            output.write_text('\n'.join(emitted))
            cause = media.ToolFailure('日期边界取消', returncode=101)
            raise media.PartialOutput(output, str(cause), cause)
        return listed

    def origin(self):
        with reg.db() as connection:
            return connection.execute('SELECT initial_fetch_at FROM youtube_sync_origins WHERE source_id=?',
                                      (self.source['id'],)).fetchone()['initial_fetch_at']

    def test_real_process_collects_beyond_twenty_and_archive_does_not_hide_the_tail(self):
        payloads = self.payloads(31)
        first = self.collect_using(command=self.command(payloads), configured=False)
        self.assertEqual(31, first['discovered'])
        self.assertEqual([], first['partial'])
        self.assertEqual({payload['id'] for payload in payloads}, {row['video_id'] for row in storage.rows()})
        payloads = [self.payloads(32)[-1], *payloads]
        second = self.collect_using(command=self.command(payloads), configured=False)
        self.assertEqual(1, second['discovered'])
        self.assertEqual([], second['partial'])
        self.assertEqual({payload['id'] for payload in payloads}, {row['video_id'] for row in storage.rows()})

    def test_twelve_precise_entries_fit_the_window_budget_at_four_point_three_seconds_each(self):
        clock = ListingClock()
        result = self.collect_using(listing=self.listing(self.payloads(12), clock), clock=clock)
        self.assertEqual([180], self.budgets)
        self.assertEqual(12, result['discovered'])
        self.assertEqual(12, len(storage.rows()))
        self.assertEqual([], result['partial'])
        self.assertIn(str(self.source['id']), result['succeeded_ids'])

    def test_forty_eight_channels_each_receive_their_full_budget(self):
        sources = [self.source, *(self.channel(number) for number in range(2, 49))]
        clock = ListingClock()
        result = self.collect_using(listing=self.listing([], clock, empty_seconds=52), clock=clock)
        self.assertEqual(48, result['checked'])
        self.assertEqual(0, result['skipped'])
        self.assertEqual([180] * 48, self.budgets)
        self.assertEqual({str(source['id']) for source in sources}, set(result['succeeded_ids']))
        self.assertGreater(result['timings']['discovery_seconds'], 900)
        self.assertEqual([], result['partial'])

    def assert_partial_catchup(self, previous):
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_success=? WHERE id=?',
                               (previous, self.source['id']))
        payloads = self.payloads(12)
        clock = ListingClock()
        first = self.collect_using(listing=self.listing(payloads, clock, seconds_per_item=20), clock=clock)
        received = {row['video_id'] for row in storage.rows()}
        self.assertEqual(9, len(received))
        self.assertTrue(all(row['state'] == 'ready' for row in storage.rows()))
        self.assertEqual(9, first['processed'])
        self.assertEqual(1, len(first['partial']))
        self.assertIn('没列完', first['message'])
        self.assertNotIn(str(self.source['id']), first['succeeded_ids'])
        self.assertEqual([], first['failed_ids'])
        self.assertEqual(previous, reg.subscription('youtube', self.source['id'])['last_success'])
        origin = self.origin() if not previous else None
        second = self.collect_using(listing=self.listing(payloads, clock, seconds_per_item=20), clock=clock)
        self.assertEqual(3, second['discovered'])
        self.assertEqual([], second['partial'])
        self.assertEqual({payload['id'] for payload in payloads}, {row['video_id'] for row in storage.rows()})
        self.assertIn(str(self.source['id']), second['succeeded_ids'])
        self.assertNotEqual(previous, reg.subscription('youtube', self.source['id'])['last_success'])
        if origin is not None:
            self.assertEqual(origin, self.origin())

    def test_partial_first_window_freezes_origin_and_catches_up_without_losing_entries(self):
        self.assert_partial_catchup('')

    def test_partial_existing_window_does_not_advance_success_until_the_tail_is_discovered(self):
        self.assert_partial_catchup((self.now - timedelta(days=2)).isoformat())
