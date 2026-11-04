import json
import sys
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, discovery_pages, media, notes, storage, toolchain, windowed_discovery
from tests.youtube_fixture import YouTubeFixture

#: 普通一轮只走带登录态的轻量列表(`windowed_discovery`),条目是 `--flat-playlist` 的形状:
#: `_type: url`,粗日期在 `timestamp` 上,没有播放量和简介 —— 那两样等元数据阶段 `media.probe`
#: 补。写成逐条解析那种完整条目的话,本地去重(只认 flat 条目)和翻页判据验的都不是真路径。


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
        self.settings(rank_limit=50)
        self.now = datetime.now(timezone.utc)
        self.budgets = []
        self.argv = self.root / 'argv.jsonl'

    def payloads(self, count):
        return [{'_type': 'url', 'id': f'{number:011d}', 'title': 'Practical AI agent coding tutorial',
                 'channel_id': self.source['url'].rsplit('/', 1)[-1],
                 'timestamp': (self.now - timedelta(minutes=number)).timestamp(),
                 'duration': 600} for number in range(1, count + 1)]

    @staticmethod
    def probe(row, *arguments, **keywords):
        """元数据阶段补回真实日期和播放量(轻量列表给不出)。"""
        return {'id': row['video_id'], 'title': row['entry']['title'], 'view_count': 1000,
                'timestamp': datetime.fromisoformat(row['entry']['published']).timestamp(),
                'description': 'AI workflow implementation and limitations', 'duration': 600}

    def collect_using(self, *, command=None, listing=None, clock=None, configured=True):
        with ExitStack() as stack:
            stack.enter_context(patch.object(collector.login, 'readiness',
                                             return_value={'ready': True, 'browser': 'chrome'}))
            stack.enter_context(patch.object(notes, 'available', return_value=(configured, '隔离验证')))
            stack.enter_context(patch.object(media, 'probe', side_effect=self.probe))
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
                stack.enter_context(patch.object(windowed_discovery, 'time', clock))
            return collector.collect(self.scope)

    def command(self, payloads):
        """真子进程:照真 yt-dlp 那样按 `--playlist-end` 截断,截断了也照旧印出列尽那一行
        (所以「列尽」要靠条数少于上限来认,见 `discovery_pages.inspect`)。argv 逐次记下。"""
        script = f'''
import json, sys
arguments = sys.argv[1:]
with open({str(self.argv)!r}, 'a', encoding='utf-8') as handle:
    handle.write(json.dumps(arguments) + '\\n')
payloads = {payloads!r}
if '--playlist-end' in arguments:
    payloads = payloads[:int(arguments[arguments.index('--playlist-end') + 1])]
for payload in payloads:
    print(json.dumps(payload))
print('[download] Finished downloading playlist: fixture')
'''
        return [sys.executable, '-u', '-c', script]

    def calls(self):
        return [json.loads(line) for line in self.argv.read_text(encoding='utf-8').splitlines()]

    def listing(self, payloads, clock, seconds_per_item=4.3, empty_seconds=0):
        def listed(arguments, path, deadline, stopped, **options):
            # 时钟从真实的 monotonic 起跳,量级每次不同,相减的末位舍入跟着变(基线上就随机
            # 差出 1e-13)。比到微秒足够说清「拿到的是不是那一整段预算」。
            self.budgets.append(round(deadline - clock.monotonic(), 6))
            if '--playlist-end' in arguments:
                payloads_now = payloads[:int(arguments[arguments.index('--playlist-end') + 1])]
            else:
                payloads_now = payloads
            output = media.unique(path)
            emitted = []
            clock.sleep(empty_seconds)
            for payload in payloads_now:
                if clock.monotonic() + seconds_per_item > deadline:
                    output.write_text('\n'.join(emitted))
                    cause = media.Interrupted('本轮频道时间预算用尽')
                    raise media.PartialOutput(output, str(cause), cause)
                clock.sleep(seconds_per_item)
                emitted.append(json.dumps(payload))
            emitted.append('[download] Finished downloading playlist: fixture')
            output.write_text('\n'.join(emitted))
            return output
        return listed

    def origin(self):
        with reg.db() as connection:
            return connection.execute('SELECT initial_fetch_at FROM youtube_sync_origins WHERE source_id=?',
                                      (self.source['id'],)).fetchone()['initial_fetch_at']

    def test_real_process_pages_past_the_first_batch_and_known_ids_do_not_hide_the_tail(self):
        """近期条目一直延伸到批尾就按 10、20、40 往下翻,直到真正列尽;下一轮已知的那些
        **在本地去重**,不靠 `--download-archive` 让工具藏起来(藏起来就看不见连续旧页,
        翻页判据跟着失明)。"""
        payloads = self.payloads(31)
        first = self.collect_using(command=self.command(payloads), configured=False)
        self.assertEqual(31, first['discovered'])
        self.assertEqual([], first['partial'])
        self.assertEqual({payload['id'] for payload in payloads}, {row['video_id'] for row in storage.rows()})
        limits = [int(argv[argv.index('--playlist-end') + 1]) for argv in self.calls()]
        self.assertEqual([discovery_pages.FIRST_BATCH * 2 ** step for step in range(3)], limits)
        payloads = [self.payloads(32)[-1], *payloads]
        second = self.collect_using(command=self.command(payloads), configured=False)
        self.assertEqual(1, second['discovered'])
        self.assertEqual([], second['partial'])
        self.assertEqual({payload['id'] for payload in payloads}, {row['video_id'] for row in storage.rows()})
        self.assertFalse([argv for argv in self.calls() if '--download-archive' in argv])

    def test_expansion_past_the_first_batch_spends_the_same_channel_budget(self):
        """翻第二页是**同一个频道那 180 秒**里的事,不是再领一份新预算。"""
        clock = ListingClock()
        result = self.collect_using(listing=self.listing(self.payloads(12), clock), clock=clock)
        self.assertEqual(2, len(self.budgets), self.budgets)
        self.assertEqual(180, self.budgets[0])
        self.assertAlmostEqual(180 - discovery_pages.FIRST_BATCH * 4.3, self.budgets[1], places=6)
        self.assertEqual(12, result['discovered'])
        self.assertEqual(12, len(storage.rows()))
        self.assertEqual([], result['partial'])
        self.assertIn(str(self.source['id']), result['succeeded_ids'])

    def test_forty_eight_channels_rotate_within_a_bounded_discovery_round(self):
        sources = [self.source, *(self.channel(number) for number in range(2, 49))]
        clock = ListingClock()
        unseen = {str(source['id']) for source in sources}
        for round_number in range(4):
            before = {str(source['id']): source['last_attempt'] for source in reg.subscriptions('youtube')}
            result = self.collect_using(listing=self.listing([], clock, empty_seconds=52), clock=clock)
            received = set(result['succeeded_ids'])
            with self.subTest(round=round_number):
                self.assertGreater(result['checked'], 0)
                self.assertLess(result['checked'], 48)
                self.assertEqual(48 - result['checked'], result['skipped'])
                self.assertLessEqual(result['timings']['discovery_seconds'], collector.DISCOVERY_CEILING)
                self.assertFalse(result['partial'])
                self.assertFalse(result['failed_ids'])
                self.assertGreater(len(received & unseen), 0)
                for source in reg.subscriptions('youtube'):
                    if str(source['id']) in unseen - received:
                        self.assertEqual(before[str(source['id'])], source['last_attempt'])
            unseen -= received
        self.assertFalse(unseen)
        self.assertEqual({180}, set(self.budgets))

    def test_spacing_is_reserved_inside_the_aggregate_discovery_ceiling(self):
        for number in range(2, 6):
            self.channel(number)
        clock = ListingClock()
        result = self.collect_using(listing=self.listing([], clock, empty_seconds=179.25), clock=clock)
        self.assertEqual(4, result['checked'])
        self.assertEqual(1, result['skipped'])
        self.assertEqual(720, result['timings']['discovery_seconds'])
        self.assertEqual([180] * 4, self.budgets)

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
        # 下一轮按正常速度列(轻量列表逐条很快;上一轮的 20 秒一条只是为了把它卡在半路),
        # 从头重查同一个窗口,已收下的 9 条本地去重,只报队尾那 3 条。
        second = self.collect_using(listing=self.listing(payloads, clock, seconds_per_item=0.1), clock=clock)
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
