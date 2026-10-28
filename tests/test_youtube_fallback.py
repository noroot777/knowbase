"""发现的两条路(全程登录态)、估算数据的写入保护、窗口收口,以及列频道的解析。

**为什么写入保护要有「反过来」那一条**:两条发现路给的东西不一样,逐条解析带真实播放量和
发布时间,flat 列举只有列表顺序估出来的时间和 `views=0`。只钉「精确能覆盖估算」是不够的
—— 真正会静默丢数据的是另一个方向,而那个方向只在主路某一轮取不到时才走到。

**为什么窗口判断要钉「非估算条目不走它」**:逐条解析来的条目在 `ranking.rejection` 已经过了
同一道窗口。两处各判一遍的话,口径就有两份,改一处另一处不跟着走。

工具链(版本比较、装新版怎么不碰正在用的那份)在 `tests/test_youtube_toolchain.py`。
"""
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.feeds.fetching import FeedError
from kb.youtube.ranking import TZ
from kb.youtube import (admission, collector, discovery, login, media, notes, products,
                        ranking, storage)
from kb.youtube.settings import of
from tests.youtube_fixture import YouTubeFixture

#: 一份最小的频道 Atom:只要够 feedparser 认出版本、视频 ID 和归属。
FEED_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015"
      xmlns:media="http://search.yahoo.com/mrss/" xmlns="http://www.w3.org/2005/Atom">
 <entry><id>yt:video:00000000001</id><yt:videoId>00000000001</yt:videoId>
  <yt:channelId>UC0000000000000000000001</yt:channelId><title>AI agent tutorial</title>
  <published>2026-10-26T01:02:03+00:00</published>
  <media:group><media:description>An AI agent writes code.</media:description>
   <media:community><media:statistics views="4321"/></media:community></media:group>
 </entry>
</feed>
"""


class DiscoveryFallback(YouTubeFixture):
    """**配了浏览器就全程带登录态**:公开那份 Atom 只留给「没有登录态」那一档。

    仓库主人两次点名(「那就全程登录态吧」、「改成 daily-report 那种」),性能代价也如实
    报过、他没收回。所以这里的判据不是「哪条路先成功」,而是**配了浏览器的那一轮里,
    公开请求一次都不许发出去** —— 发现入口和归属解析两处都算。
    """

    def setUp(self):
        super().setUp()
        self.source = self.channel()
        # handle 订阅:地址里没有 UC…,库里也没缓存过 —— 归属要靠工具自己报的那一份。
        self.handle = self.channel(2, url='https://www.youtube.com/@ai-handle')
        self.listed = [{'video_id': f'{number:011d}', 'title': f'AI news {number}',
                        'duration': 600.0 + number, 'channel_id': '',
                        'url': f'https://www.youtube.com/watch?v={number:011d}'}
                       for number in range(1, 4)]
        # 主路(带登录态逐条解析)的返回样本:真实日期、真实播放量,没有估算标记。
        self.detailed = [{'video_id': f'{number:011d}', 'title': f'AI agents {number}',
                          'published': (datetime.now(TZ) - timedelta(hours=number)).isoformat(),
                          'description': 'An AI agent writes code.', 'views': 1000 * number,
                          'duration': 700.0 + number, 'channel_id': 'UC' + f'{number:022d}',
                          'url': f'https://www.youtube.com/watch?v={number:011d}'}
                         for number in range(1, 3)]

    def test_a_configured_browser_never_sends_a_public_request(self):
        """**全程登录态的判据只有一条:这一轮一个无登录态请求都没发出去。**

        原来是「先试公开订阅,失败再带登录态」—— 那每轮都先付一次 404 的风险(那批 47/48
        就是这么来的)和一次重试的时间,而用户要的是另一件事。这里连 handle 订阅一起钉:
        它的归属原先要先解析一次公开主页,**带登录态的工具因此可能还没起跑就被公开请求
        拦住**。反过来钉一边(`test_without_settings_...`):真的没有登录态时,公开那条照旧走。
        """
        for source, label in ((self.source, '/channel/UC… 订阅'), (self.handle, 'handle 订阅')):
            with self.subTest(source=label):
                asked = []
                with patch.object(discovery, 'request', side_effect=lambda url, **kwargs: asked.append(url)), \
                        patch.object(media, 'detailed_videos', return_value=self.detailed) as detailed, \
                        patch.object(media, 'channel_videos') as listing:
                    entries = discovery.fetch(source, {'youtube': {}})
                self.assertEqual([], asked, '配了浏览器的那一轮不许发无登录态请求')
                detailed.assert_called_once()
                listing.assert_not_called()
                self.assertEqual(2, len(entries))
                # 归属以工具自己报的那一份为准 —— 补它不该再去发一次请求。
                self.assertEqual(['UC' + '0' * 21 + '1', 'UC' + '0' * 21 + '2'],
                                 [entry['channel_id'] for entry in entries])

    def test_the_logged_in_path_is_the_one_that_runs_first(self):
        """**主路就是 daily-report 那条**:同一个地址交给 yt-dlp、带登录态逐条解析。

        判据落在**条目本身**:日期真实(没有 `approximate` 标记)、播放量真实、简介带回来了
        (`ai_only` 要用它);而估算那条一次都不许起。
        """
        with patch.object(media, 'detailed_videos', return_value=self.detailed) as detailed, \
                patch.object(media, 'channel_videos') as listing:
            entries = discovery.fetch(self.source, {'youtube': {}})
        listing.assert_not_called()
        self.assertEqual(2, len(entries))
        self.assertTrue(all('approximate' not in entry for entry in entries))
        self.assertEqual([1000, 2000], [entry['views'] for entry in entries])
        self.assertTrue(all(entry['description'] for entry in entries), '简介要带回来,ai_only 要用')
        self.assertLessEqual(detailed.call_args.kwargs['limit'], 20, '一轮别翻太多条,逐条解析很贵')
        self.assertIn('notes', detailed.call_args.kwargs, '「没列完」那句话要有地方落')

    def test_detail_failure_falls_back_and_marks_the_guesswork(self):
        with patch.object(media, 'detailed_videos', side_effect=RuntimeError('逐条解析也没取到')), \
                patch.object(media, 'channel_videos', return_value=self.listed):
            entries = discovery.fetch(self.source, {'youtube': {}})
        self.assertEqual(3, len(entries))
        self.assertTrue(all(entry['approximate'] for entry in entries))
        self.assertTrue(all(entry['views'] == 0 for entry in entries))
        # 频道页是时间倒序,**估算时间必须跟着位置递减** —— 同分时还要知道谁更新。
        stamps = [entry['published'] for entry in entries]
        self.assertEqual(stamps, sorted(stamps, reverse=True))
        self.assertEqual(len(stamps), len(set(stamps)), '估算时间不能挤成同一个值')
        floor = datetime.now(timezone.utc) - timedelta(days=1)
        self.assertTrue(all(datetime.fromisoformat(stamp) > floor for stamp in stamps),
                        '估算时间要落在默认窗口里,否则后路发现的条目全被 ranking 挡掉')
        self.assertTrue(all(entry['channel_id'] == self.source['url'][-24:] for entry in entries),
                        'flat 列举没报归属时退回地址里那一份,不许留空也不许去发请求')

    def test_ranged_round_refuses_guesses_but_still_takes_the_logged_in_path(self):
        """**显式日期范围那一轮只禁「估算」那一条,不是禁掉所有退路。**

        在这之前整条退路一起禁:主路取不到的那一轮,这个频道颗粒无收 —— 连库里既有候选
        都不处理(审查实测入选 0、处理 0)。逐条解析拿的是真实日期,那一轮正该走它。
        反过来钉一边:估算那条在这一轮仍然一次都不许起。
        """
        with patch.object(media, 'detailed_videos', return_value=self.detailed), \
                patch.object(media, 'channel_videos') as listing:
            entries = discovery.fetch(self.source, {'youtube': {}}, estimates=False)
        listing.assert_not_called()
        self.assertEqual(2, len(entries))
        self.assertTrue(all('approximate' not in entry for entry in entries),
                        '这条路的日期是真的,不许打估算标记')
        with patch.object(media, 'detailed_videos', side_effect=RuntimeError('没解析出带发布时间的视频')), \
                patch.object(media, 'channel_videos') as listing:
            with self.assertRaises(FeedError) as raised:
                discovery.fetch(self.source, {'youtube': {}}, estimates=False)
        listing.assert_not_called()
        self.assertIn('没解析出带发布时间的视频', str(raised.exception))

    def test_without_settings_the_public_feed_is_the_only_path_and_gets_retried(self):
        """**反过来钉一边:真的没有登录态时,公开那条照旧是唯一的办法,而且要重试。**

        那批 47/48 的 404 是间歇性的(事后同样 4 路并发复测 52 个频道全部 200),所以这条路
        上第一道防线是同一条路多试两次,代价接近零(照 daily-report 的
        `--retry 3 --retry-delay 1 --retry-all-errors`)。没有 `cookies_browser` 就没有
        登录态,带登录态那两条路一次都不许起 —— 它们起来也只会再撞同一道墙。
        """
        calls = []

        def flaky(url, deadline=None, **kwargs):
            calls.append(url)
            if len(calls) < 3:
                raise FeedError('订阅服务返回 HTTP 404')
            return SimpleNamespace(body=FEED_XML, status=200, headers={})

        with patch.object(discovery, 'RSS_BACKOFF', 0.01), \
                patch.object(discovery, 'request', side_effect=flaky), \
                patch.object(media, 'detailed_videos') as detailed, \
                patch.object(media, 'channel_videos') as listing:
            entries = discovery.fetch(self.source, settings=None)
        self.assertEqual(3, len(calls), '同一条路要再试,次数照 RSS_ATTEMPTS')
        self.assertEqual(1, len(entries))
        self.assertGreaterEqual(discovery.RSS_ATTEMPTS, 2, '只试一次等于没有重试')
        detailed.assert_not_called()
        listing.assert_not_called()

        # 次数用完还不行就是失败:没有登录态,这里没有下一条路可以让位。
        calls.clear()
        with patch.object(discovery, 'RSS_BACKOFF', 0.01), \
                patch.object(discovery, 'RSS_ATTEMPTS', 2), \
                patch.object(discovery, 'request',
                             side_effect=FeedError('订阅服务返回 HTTP 404')), \
                patch.object(media, 'detailed_videos') as detailed:
            with self.assertRaises(FeedError):
                discovery.fetch(self.source, settings=None)
        detailed.assert_not_called()

    def test_retrying_never_sleeps_past_its_own_budget(self):
        """退避放不下就别退避 —— 不许把预算睡掉之后才报失败。"""
        with patch.object(discovery, 'RSS_BACKOFF', 5.0), \
                patch.object(discovery, 'request', side_effect=FeedError('订阅服务返回 HTTP 404')):
            started = time.monotonic()
            with self.assertRaises(FeedError):
                discovery.fetch(self.source, settings=None, timeout=6)
        self.assertLess(time.monotonic() - started, 4, '退避放不下就别退避')

    def test_each_path_keeps_its_own_slice_of_the_budget(self):
        """**两条路共用一个 deadline 时,主路卡到预算用尽会让后路一个进程都起不起来。**

        主路失败有两档形态:立刻回来的(cookie 读不出来、频道不存在),和卡到预算用尽的。
        后者共用 deadline 时,`media.run` 在起子进程之前就按「已超时」拒掉 —— 报出来是
        「都没取到」,而后路其实一次都没跑。这里让主路**真的**把自己那一段耗干,
        再看后路手里还剩多少。
        """
        seen = {}

        def burning_detail(source, settings, deadline, stopped, limit=10, notes=None):
            while time.monotonic() < deadline:
                time.sleep(0.01)
            raise RuntimeError('逐条解析超时')

        def listing(source, settings, deadline, stopped, limit=20, notes=None):
            seen['left'] = deadline - time.monotonic()
            return self.listed

        # 比例调小只为把这条用例的等待压到半秒级;切分本身与比例取值无关。
        with patch.object(discovery, 'DETAIL_SHARE', 0.5), \
                patch.object(media, 'detailed_videos', side_effect=burning_detail), \
                patch.object(media, 'channel_videos', side_effect=listing):
            entries = discovery.fetch(self.source, {'youtube': {}}, timeout=6)
        self.assertEqual(3, len(entries), '主路把自己那一段耗干,第 2 条照旧要跑完')
        self.assertGreater(seen['left'], 1.0, '第 2 条起跑时手里还得有预算')
        self.assertTrue(0.5 <= discovery.DETAIL_SHARE < 1,
                        '默认比例要真的切出两段来,而且慢的那条拿大头')

    def test_every_path_failing_names_every_reason(self):
        """两条路都没取到时,两份原话都要带上 —— 面板上那一行是唯一的线索。"""
        with patch.object(media, 'detailed_videos', side_effect=RuntimeError('登录态读不出来')), \
                patch.object(media, 'channel_videos', side_effect=RuntimeError('没列出任何视频')):
            with self.assertRaises(FeedError) as raised:
                discovery.fetch(self.source, {'youtube': {}})
        for fragment in ('登录态读不出来', '没列出任何视频'):
            self.assertIn(fragment, str(raised.exception))


class DiscoveryBudget(YouTubeFixture):
    """**预算要按主路真实的速度定。**

    全程登录态之后每一轮都是逐条解析、单频道半分钟一档(实测每条约 4.3 秒)。单频道预算
    还按原来的 20 秒给的话,乘上它那一段比例只剩十几秒,`media.run` 在它解析完之前就按
    超时掐掉 —— 面板上看是「都没取到」,而它其实差的只是时间。
    """

    def checked_channels(self, total, offset=0):
        """造 `total` 个启用频道(别的全停用),跑一轮,回报每个频道拿到的发现预算。

        `offset` 让同一个用例里多跑几轮时频道地址不撞 —— `isolate()` 只是停用,
        地址仍然占着,重名会被 `save_subscription` 直接拒掉。
        """
        self.isolate()
        sources = [self.channel(offset + number) for number in range(1, total + 1)]
        seen = []

        def record(source, settings, timeout, estimates, notes=None):
            seen.append(timeout)
            return []

        with patch.object(collector.discovery, 'fetch', side_effect=record), \
                patch.object(login, 'readiness', return_value={'ready': True, 'message': ''}), \
                patch.object(collector.notes, 'available', return_value=(True, '')):
            summary = collector.collect(FetchScope())
        return sources, seen, summary

    def test_a_single_channel_round_still_checks_that_channel(self):
        """**额度正好够的那一轮,第一批不许被自己的闸拦住。**

        待查频道数 ≤ 并发数时额度正好是一批的量,「剩下的时间够不够再跑一整批」这道闸
        在第一批就差那几微秒 —— 结果是一轮一个频道都没查、全进 `skipped`,而面板上只说
        「部分频道未轮到」。判据落在**后果**上:查了几个、漏了几个。
        """
        for total in (1, 4):
            with self.subTest(total=total):
                sources, seen, summary = self.checked_channels(total, offset=100 * total)
                self.assertEqual(total, len(seen))
                self.assertEqual(total, summary['checked'])
                self.assertEqual(0, summary['skipped'], '一个都不许静默留到下一轮')

    def test_each_channel_gets_enough_budget_for_the_logged_in_path(self):
        sources, seen, summary = self.checked_channels(5, offset=900)
        self.assertEqual(len(sources), len(seen), '每个频道都该被查一次')
        self.assertEqual({collector.DISCOVERY_PER_SOURCE}, set(seen), '单频道预算只有一份口径')
        slice_for_detail = collector.DISCOVERY_PER_SOURCE * discovery.DETAIL_SHARE
        self.assertGreaterEqual(slice_for_detail, 30,
                                '主路那一段至少要够一个频道跑完(实测半分钟一档)')
        # 反过来钉一边:整轮还是有上限的,不许一轮发现无限期跑下去。
        self.assertLessEqual(collector.DISCOVERY_CEILING, 1800)
        self.assertGreaterEqual(collector.DISCOVERY_CEILING,
                                collector.DISCOVERY_PER_SOURCE * 4,
                                '上限连几批都放不下的话,后面的频道永远轮不到')


class PartialListing(YouTubeFixture):
    """**一条视频受限、或者预算在第 7 条上到点,前面列到的那几条照旧要收下。**

    审查用两个真实子进程复现过:第一条正常视频整行印了出来,第二条报私有并以 1 退出 ——
    整份输出被删掉,显式日期那一轮这个频道颗粒无收。这里钉的是它在**整轮层面**的后果:
    列到的入库、这条订阅不算失败、而「没列完」这句话要留在面板上(口径同 `feeds.sync`
    的 `partial`:记成 error 会显示「连接失败」并让它进退避,而它明明带回了东西)。
    条目层面那一半在 `tests/test_youtube_media.py`。
    """

    def one_round(self, note=''):
        self.isolate()
        source = self.channel(300 if note else 301)

        def fetch(selected, settings, timeout, estimates, notes=None):
            if note:
                notes.append(note)
            # 刻意给一条窗口外的:这一轮不必真处理它,判据落在「入库了没有」上。
            return [self.entry(1, hours=24 * 60)]

        with patch.object(collector.discovery, 'fetch', side_effect=fetch), \
                patch.object(login, 'readiness',
                             return_value={'ready': True, 'message': '', 'browser': 'chrome'}), \
                patch.object(collector.notes, 'available', return_value=(True, '')):
            summary = collector.collect(FetchScope())
        return source, summary

    def test_a_truncated_listing_is_stored_and_said_out_loud_without_a_failure(self):
        note = '只列到 1 条就中断了：yt-dlp 执行失败：ERROR: Private video'
        source, summary = self.one_round(note)
        self.assertEqual([note], [item['reason'] for item in summary['partial']])
        self.assertEqual(['频道 300'], [item['title'] for item in summary['partial']])
        self.assertEqual([], summary['errors'], '没列完不是失败,不许让它进退避重试')
        self.assertEqual([str(source['id'])], summary['succeeded_ids'])
        self.assertIn('没列完', summary['message'], '面板上要看得出这个频道停在半路')
        self.assertIn('Private video', summary['message'], '原话要带上,那是唯一的线索')
        self.assertEqual(1, len(storage.rows([source['id']])), '列到的那条照旧入库')

    def test_a_complete_listing_says_nothing(self):
        """反过来钉一边:正常列完的那一轮面板上一个字都不许多。"""
        source, summary = self.one_round()
        self.assertEqual([], summary['partial'])
        self.assertNotIn('没列完', summary['message'])
        self.assertEqual(1, len(storage.rows([source['id']])))


class EstimateNeverOverwritesFacts(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.source = self.channel()
        self.precise = {**self.entry(1, hours=30, views=5000)}
        self.guess = {'video_id': self.precise['video_id'], 'url': self.precise['url'],
                      'title': self.precise['title'], 'channel_id': '', 'description': '',
                      'published': datetime.now(timezone.utc).isoformat(),
                      'views': 0, 'approximate': True, 'duration': 600.0}

    def stored(self):
        row = storage.rows()[0]
        return row['views'], row['entry'].get('approximate'), row['entry']['published']

    def test_guess_arriving_second_keeps_the_real_numbers(self):
        storage.discover(self.source, [self.precise])
        storage.discover(self.source, [self.guess])
        views, approximate, published = self.stored()
        self.assertEqual(5000, views, '估算把真实播放量清成 0 了,候选榜下一轮就找不到它')
        self.assertIsNone(approximate)
        self.assertEqual(self.precise['published'], published)

    def test_the_listing_says_the_view_count_is_unknown_instead_of_zero(self):
        """**估出来的 `views=0` 是「读不到」,面板不能把它显示成「没人看」。**

        后路是从频道列表页发现的,那上面没有播放量。榜单那一行带上这面旗才分得清
        「0 次观看」和「播放量未知」—— 不带的话等于替 YouTube 说一句它没说过的话。
        反过来钉一边:精确那条不许带旗,否则真实的 0 播放量也被说成未知。
        """
        storage.discover(self.source, [self.guess])
        self.assertTrue(storage.listing()['items'][0]['approximate'])
        storage.discover(self.source, [self.precise])
        self.assertFalse(storage.listing()['items'][0]['approximate'])

    def test_facts_arriving_second_do_overwrite_the_guess(self):
        """反过来钉一边:精确数据照旧覆盖估算 —— 否则 RSS 恢复之后那条永远停在估算值上。"""
        storage.discover(self.source, [self.guess])
        self.assertEqual((0, True, self.guess['published']), self.stored())
        storage.discover(self.source, [self.precise])
        self.assertEqual((5000, None, self.precise['published']), self.stored())


class FactsReplaceEstimates(YouTubeFixture):
    """事实日期一到手就要换掉估算值,**并且重判一次资格**。

    审查定点冒烟复现过:库里一条估 2026-10-27、实际 2026-10-25 的候选,在
    `since=until=2026-10-27` 的补抓里一路走到成品,日期还是 10-27 —— 既收进了别的
    日期的视频,又把估出来的日子当成发布日期落了库。
    **`fallback=False` 挡不住这一类**:它只拦住这一轮新增估算候选,库里上一轮留下的
    那些照旧会被挑中。所以闭环必须在「第一次见到真实时间」那一刻,而不是在发现那一头。
    """

    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()

    def guessed(self, published):
        entry = {**self.entry(), 'published': published, 'views': 0, 'approximate': True}
        storage.discover(self.source, [entry])
        return entry

    def stored(self):
        row = storage.rows([self.source['id']])[0]
        return row['entry']['published'][:10], row['entry'].get('approximate'), row['views']

    def test_calibrate_replaces_the_date_the_flag_and_the_view_count(self):
        entry = self.guessed('2026-10-27T10:00:00+08:00')
        calibrated = admission.calibrate({'entry': entry},
                                         {'upload_date': '20261025', 'view_count': 4200})
        self.assertEqual('2026-10-25', calibrated['published'][:10])
        self.assertNotIn('approximate', calibrated, '标记留着的话下一轮还会被当成估算条目')
        self.assertEqual(4200, calibrated['views'], '后路给的 0 不是「没人看」,是「不知道」')
        self.assertEqual(entry['title'], calibrated['title'], '其余字段照旧')

    def test_unreadable_date_defers_and_stays_reprobeable(self):
        """**读不出事实时间既不放行也不永久排除。** 放行会让一个估出来的日子落库,
        永久排除会把一条好视频钉死在错判上。"""
        for metadata in ({}, {'timestamp': 0}, {'timestamp': True},
                         {'upload_date': '2026-10-26'}, {'upload_date': '不是日期'}):
            with self.subTest(metadata=metadata):
                self.assertIsNone(admission.calibrate({'entry': {}}, metadata))
        self.assertEqual(('window', 'deferred'), (admission.UNDATED.code, admission.UNDATED.state))
        self.assertGreater(admission.UNDATED.retry_seconds, 0)
        self.assertIn('window', admission.TEMPORARY, 'deferred 的 code 不在重探清单里就再也不会复查')

    def test_recalibrate_only_touches_rows_still_marked_as_estimates(self):
        """判据待在语句自己的 WHERE 里。**0 行不是失败** —— 那说明别人已经校准过了。"""
        entry = self.guessed('2026-10-27T10:00:00+08:00')
        fact = {**entry, 'published': '2026-10-25T09:00:00+08:00', 'views': 77}
        fact.pop('approximate')
        storage.recalibrate(entry['video_id'], fact)
        self.assertEqual(('2026-10-25', None, 77), self.stored())
        # 再来一次:库里那条已经不是估算了,这一下什么都不该改。
        storage.recalibrate(entry['video_id'], {**fact, 'published': '2020-01-01T00:00:00+08:00', 'views': 1})
        self.assertEqual(('2026-10-25', None, 77), self.stored())

    def test_ranged_round_rejects_a_video_whose_real_date_is_another_day(self):
        """端到端:指定 10-27 补抓,库里那条估 10-27 而实际 10-25 —— 不许产出成品,
        库里的日期也要换成事实值。**这是审查冒烟的那一例。**"""
        entry = self.guessed('2026-10-27T10:00:00+08:00')
        metadata = {'id': entry['video_id'], 'duration': 600, 'language': 'en',
                    'upload_date': '20261025', 'view_count': 4200}
        summary = {'added': 0, 'processed': 0, 'errors': [], 'message': '', 'stopped': False}
        settings = reg.get_settings('youtube')
        scope = FetchScope(since='2026-10-27', until='2026-10-27')
        ranking.score(storage.rows(), of(settings), scope)
        with patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=metadata), \
                patch.object(media, 'obtain') as obtain, patch.object(notes, 'build') as build:
            collector.process(storage.rows(), settings, scope, summary, time.monotonic() + 30)
        obtain.assert_not_called()
        build.assert_not_called()
        self.assertEqual(0, summary['processed'])
        row = storage.rows([self.source['id']])[0]
        self.assertEqual('2026-10-25', row['entry']['published'][:10], '事实日期必须落库')
        self.assertNotIn('approximate', row['entry'])
        # **「这一轮不要」不是「永久不要」**:写成 excluded 的话,下一轮普通同步里
        # `rejection` 那道 `state=='excluded' and not force` 会直接拒掉它,它再也回不来。
        # 落的状态还必须和 `score()` 对同一类拒绝写的那个**一致**(`filtered`),
        # 否则「这一轮没进」在两处是两个状态,恢复路径也就只有一条能走通。
        self.assertEqual('filtered', row['state'])
        self.assertIn('指定日期范围', row['reason'])
        # **接着钉住它真能回来**:换成普通一轮(事实日期 10-25 落在默认 7 天窗口里),
        # 同一条候选重新进榜 —— 这是「放宽窗口/换一天还能收」那半边。
        revived = ranking.score(storage.rows(), of(settings), FetchScope())
        self.assertEqual([entry['video_id']], [item['video_id'] for item in revived])
        self.assertEqual('candidate', storage.rows([self.source['id']])[0]['state'])

    def test_one_owner_losing_on_dates_does_not_speak_for_the_others(self):
        """**同一条视频挂在两条订阅下时,一个归属失格不等于这条视频失格。**

        新加的那条订阅历史下界是一天前,老订阅是六天前;视频真实发布时间三天前。代表
        归属(权重高的那条新订阅)按日期该拒,老订阅本来收得到 —— 只重判代表那一个
        归属的话,这条视频会被**全局**判掉:下一轮只同步老订阅时入选 0、处理 0,而且
        状态写成 excluded 之后连放宽窗口都救不回来(审查冒烟的那一例)。
        """
        real = datetime.now(timezone.utc) - timedelta(days=3)
        newer = self.channel(2, youtube={'score_multiplier': 2.0})
        # 下界是 `YYYY-MM-DD`(`reg.history_floor` 会自己再往前推 HISTORY_DAYS 天,
        # 这里要的是最终那个日子,所以直接写)。
        def floor_at(days):
            return (datetime.now(TZ) - timedelta(days=days)).date().isoformat()
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               (floor_at(1), newer['id']))
            connection.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                               (floor_at(6), self.source['id']))
        entry = {**self.entry(), 'published': datetime.now(timezone.utc).isoformat(),
                 'views': 0, 'approximate': True}
        for source in (self.source, newer):
            storage.discover(source, [entry])
        metadata = {'id': entry['video_id'], 'duration': 600, 'language': 'en',
                    'timestamp': int(real.timestamp()), 'view_count': 4200}
        settings = reg.get_settings('youtube')
        ranked = ranking.score(storage.rows(), of(settings), FetchScope())
        self.assertEqual(newer['id'], ranked[0]['source_id'], '代表该是权重高的那条新订阅')
        summary = {'added': 0, 'processed': 0, 'errors': [], 'message': '', 'stopped': False}
        with patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                    'segments': [{'start': 0, 'end': 600, 'text': 'An AI agent writes and verifies code.'}]}), \
                patch.object(notes, 'build', return_value={'url': entry['url'], 'title': '中文标题',
                    'summary': '摘要', 'body': '正文', 'published': real.isoformat()}), \
                patch.object(products, 'certify', return_value='ok'):
            # **喂进去的必须是排过名那一份**(生产里就是 `collector.collect` 的做法):
            # 那里头每条已经换成了代表归属,拿 `storage.rows()` 顶替等于把这条用例
            # 退化成「判了老订阅一次」,代表是谁压根没进执行路径。
            collector.process(ranked, settings, FetchScope(), summary, time.monotonic() + 30)
        self.assertEqual(1, summary['processed'], '老订阅合格,这条视频就该继续处理')
        row = storage.rows([self.source['id']])[0]
        self.assertNotEqual('excluded', row['state'], '日期类拒绝不许写成永久排除')
        self.assertEqual(self.source['id'], row['source_id'])

    def test_plain_round_keeps_a_video_whose_real_date_is_inside_the_window(self):
        """反过来钉一边:事实日期落在窗口里的那条照旧往下走,别把闭环写成一道死闸。"""
        entry = self.guessed('2026-10-27T10:00:00+08:00')
        recent = datetime.now(timezone.utc) - timedelta(days=1)
        metadata = {'id': entry['video_id'], 'duration': 600, 'language': 'en',
                    'timestamp': int(recent.timestamp()), 'view_count': 99}
        summary = {'added': 0, 'processed': 0, 'errors': [], 'message': '', 'stopped': False}
        settings = reg.get_settings('youtube')
        ranking.score(storage.rows(), of(settings), FetchScope())
        with patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                    'segments': [{'start': 0, 'end': 600, 'text': 'An AI agent writes and verifies code.'}]}), \
                patch.object(notes, 'build', return_value={'url': entry['url'], 'title': '中文标题',
                    'summary': '摘要', 'body': '正文', 'published': entry['published']}), \
                patch.object(products, 'certify', return_value='ok'):
            collector.process(storage.rows(), settings, FetchScope(), summary, time.monotonic() + 30)
        self.assertEqual(1, summary['processed'])
        row = storage.rows([self.source['id']])[0]
        self.assertEqual(recent.astimezone(admission.TZ).date().isoformat(), row['entry']['published'][:10])
        self.assertEqual(99, row['views'])


class ChannelListing(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.source = self.channel()

    def listing(self, payload):
        def fake(arguments, output, *rest, **kwargs):
            self.arguments, self.kwargs = arguments, kwargs
            # 真实现返回「这次调用自己的」那一份(`media.unique`),消费方认返回值。
            actual = media.unique(output)
            actual.write_text(payload, encoding='utf-8')
            return actual
        return fake

    def test_parses_json_lines_and_asks_for_a_flat_playlist(self):
        lines = '\n'.join([
            'WARNING: 这一行不是 JSON,要跳过',
            json.dumps({'id': '12345678901', 'title': '一', 'duration': 600.0}),
            json.dumps({'id': '12345678901', 'title': '重复的,只算一条'}),
            json.dumps({'id': 'too-short', 'title': '坏 id'}),
            json.dumps({'id': '12345678902', 'title': '二', 'duration': 'bad'}),
            '{坏 JSON',
        ])
        with patch.object(media, 'ytdlp', side_effect=self.listing(lines)):
            entries = media.channel_videos(self.source, {'youtube': {}}, time.monotonic() + 5,
                                           lambda: False, limit=20)
        self.assertEqual(['12345678901', '12345678902'], [item['video_id'] for item in entries])
        self.assertEqual([600.0, None], [item['duration'] for item in entries])
        self.assertIn('--flat-playlist', self.arguments)
        self.assertEqual(['--playlist-end', '20'],
                         self.arguments[self.arguments.index('--playlist-end'):][:2])
        self.assertTrue(self.kwargs['playlist'], '不展开播放列表就列不出频道')
        self.assertEqual('chrome', self.kwargs['spec'])
        self.assertTrue(self.arguments[-1].endswith('/videos'))

    def test_listing_nothing_raises_instead_of_reporting_an_empty_channel(self):
        """**真空频道和列举失败在这条路上分不开。** 抛的代价是重列一次,漏报的代价是这个
        频道从此「今天没有新内容」,而面板上一切正常。"""
        with patch.object(media, 'ytdlp',
                          side_effect=self.listing('WARNING: cannot decrypt v10 cookies: no key found')):
            with self.assertRaises(RuntimeError) as caught:
                media.channel_videos(self.source, {'youtube': {}}, time.monotonic() + 5, lambda: False)
        # **工具原话要跟着异常走。** 只报「没有列出任何视频」的话,`login.classify`
        # 就看不出这其实是 cookie 读不出来 —— 用户会被送去反复登录,而钥匙串那一头没人碰。
        self.assertIn('没有从', str(caught.exception))
        self.assertIn('no key found', str(caught.exception))
        self.assertEqual('cookie_unreadable', login.classify(str(caught.exception)))
