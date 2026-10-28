"""发现阶段的请求节奏:**一个频道一个频道地查,两个之间歇一拍。**

**为什么红要落在「两次请求有没有重叠」上,而不是「代码里有没有 sleep」**:YouTube 限的是
出口的瞬时请求速率,所以撞不撞 429 只由「同一时刻有几个 yt-dlp 在跑、相邻两次隔多久」决定。
4 路并发那一版每个频道都带回了数据、面板上一切正常,直到某一轮整批 429 —— 而 429 的原话
落在登录态检查上(`login.classify` 的 `rate_limited`),显示成「看不出登录态」,从面板上
根本追不到并发这一头。所以这里断言的是**调用区间本身**:重叠一次就算红。

参数不是我们自己试出来的,是借 daily-report 的:它那份 `fetch_yt_channels.py` 起先 8 路
并发,后来整个换成 `fetch_yt_channels_v2.py` 的串行 + `time.sleep(1)  # Rate limit`,
自此每天 48 个频道、每个逐条解析 20 条都跑得通。

反过来的三边同样要钉,少一边就各错一头:只有一个频道的那一轮不许白等一拍;人点了停止
之后不许接着查下一个频道(**两个窗口都算** —— 查完那一刻点的,和歇着那一秒里点的);额度
要把歇的那几拍算进去,否则最后几个频道每一轮都被预算闸拦掉(而面板上只说「部分频道未轮到」)。

两处写法上的坑都是反向验证逼出来的,别照直觉改回去:假替身**不许瞬时返回**(否则并发版
的区间只有几微秒、照样不重叠),「歇了几拍」**数次数不量总耗时**(毫秒级预算遇上机器忙
就是随机红)。
"""
import time
from contextlib import ExitStack
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.youtube import collector, discovery, storage
from tests.youtube_fixture import YouTubeFixture

#: 回归用的间隔:生产里是 1 秒,这里缩短只为跑得快 —— 判据用的是它本身,不是写死的数。
SPACING = 0.25
#: 假的一次查询占多久。**不许瞬时返回**:并发那一版的区间只有几微秒,瞬时假替身下
#: 「两次不重叠」恒绿(反向验证实测:改回 4 路并发,这条用例一声不响)。
DWELL = 0.1
#: 真 sleep 的原始引用。假替身占时要用它 —— 直接 `time.sleep` 会被「歇一拍」那个桩收走,
#: 于是占时本身被数成了一拍(实测:单频道那一轮的 beats 里冒出一个 DWELL)。
_DWELL_SLEEP = time.sleep


class DiscoveryPacing(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.spans = []
        self.beats = []

    def watched(self, count=3, stop_at=None, offset=0):
        """建 count 个频道,记下每次 `discovery.fetch` 的进出时刻。

        `offset` 只为同一条用例里跑第二轮时换一批频道(订阅表按链接唯一)。
        """
        sources = [self.channel(offset + number) for number in range(1, count + 1)]

        def fetch(source, *rest):
            span = [source['id'], time.monotonic(), None]
            self.spans.append(span)
            _DWELL_SLEEP(DWELL)
            if stop_at == 'fetch':
                collector._state['stop_requested'] = True
            span[2] = time.monotonic()
            return []

        return sources, fetch

    def collect(self, fetch, real_sleep=False, stop_at=None, scope=None):
        """跑一轮。**默认把歇那一拍打桩成「记一笔」**:次数和时机不该受机器负载影响
        (真睡的判据只留给专门量间隔那一条)。`stop_at='beat'` 模拟歇着的那一秒里人点了停止。
        """
        def beat(seconds):
            self.beats.append(seconds)
            if stop_at == 'beat':
                collector._state['stop_requested'] = True

        with ExitStack() as stack:
            stack.enter_context(patch.object(collector, 'DISCOVERY_SPACING', SPACING))
            stack.enter_context(patch.object(discovery, 'fetch', side_effect=fetch))
            if not real_sleep:
                # 打桩的是 `collector` 看见的那个 `time.sleep`;这一段里只有被测代码在跑。
                stack.enter_context(patch.object(collector.time, 'sleep', side_effect=beat))
            return collector.collect(scope or FetchScope())

    def test_two_channels_are_never_queried_at_the_same_time(self):
        """任意两个频道的查询区间不许重叠 —— 重叠就是并发,就是撞 429 那一版。

        假替身要占住 `DWELL` 那一段时间:并发时第二个频道会在第一个还没返回时开跑,
        区间当场交叠(瞬时返回的假替身看不出来,见 `DWELL` 那条注释)。
        """
        sources, fetch = self.watched(4)
        result = self.collect(fetch)
        self.assertEqual(4, result['checked'])
        self.assertEqual([source['id'] for source in sources], [span[0] for span in self.spans],
                         '顺序也要是 pending 那一份(按 last_attempt 轮转)')
        for (_, _, earlier_end), (_, later_start, _) in zip(self.spans, self.spans[1:]):
            self.assertGreaterEqual(later_start, earlier_end, '两次查询重叠了,等于又回到并发')

    def test_the_gap_between_two_channels_is_at_least_one_beat(self):
        """相邻两个频道之间真的歇了一拍(**这一条用真时钟、真 sleep**)。

        只去掉并发、不歇那一拍的话,48 个频道会变成 48 次背靠背的请求 —— 速率照旧,
        而这条用例是唯一能看出区别的地方(上面那条「不重叠」它照样全绿)。
        """
        _, fetch = self.watched(3)
        self.collect(fetch, real_sleep=True)
        self.assertEqual(3, len(self.spans))
        for (_, _, earlier_end), (_, later_start, _) in zip(self.spans, self.spans[1:]):
            self.assertGreaterEqual(later_start - earlier_end, SPACING, '两个频道之间没歇够一拍')

    def test_a_single_channel_round_does_not_wait_for_nothing(self):
        """反过来钉一边:只有一个频道的那一轮一拍都不许歇;三个频道歇两拍。

        歇在「查完之后」而不是「下一个之前」的话,每一轮末尾都多等一拍 —— 那一拍还正好
        压在后面处理视频的时间预算上。**数的是歇了几次,不是总耗时**:后者是毫秒级预算,
        机器一忙就随机红。
        """
        _, fetch = self.watched(1)
        result = self.collect(fetch)
        self.assertEqual(1, result['checked'])
        self.assertEqual([], self.beats, '一个频道的那一轮歇了不该歇的一拍')
        self.beats.clear()
        self.spans.clear()
        self.isolate()
        _, fetch = self.watched(3, offset=10)
        self.collect(fetch)
        self.assertEqual([SPACING, SPACING], self.beats, '三个频道之间该歇两拍,每拍一个间隔')

    def test_a_stop_request_lands_before_the_next_channel(self):
        """反过来钉一边:人点了停止,下一个频道一次都不许再查 —— **两个窗口都算**。

        一是查完那一刻点的(循环开头那道 `not stopped()` 管得着),二是**歇着的那一秒里**
        点的:那是歇完之后再看一眼的唯一用途。反向验证实测过 —— 只去掉第二道检查时,
        第一个窗口那半边照样全绿(循环开头先判了),所以两边都得钉,否则那道检查等于没有回归。
        """
        for window, expected in (('fetch', '查完那一刻'), ('beat', '歇着的那一秒里')):
            with self.subTest(window=window):
                self.spans.clear()
                self.beats.clear()
                collector._state['stop_requested'] = False
                # 上一轮那几个频道停用掉(`isolate`),这一轮换一批编号 —— 否则 pending
                # 里混着两轮的频道,`skipped` 那条断言就说不清是谁没轮到。
                self.isolate()
                _, fetch = self.watched(3, stop_at=window if window == 'fetch' else None,
                                        offset=0 if window == 'fetch' else 10)
                result = self.collect(fetch, stop_at=window)
                self.assertEqual(1, len(self.spans), f'{expected}点的停止之后又查了一个频道')
                self.assertEqual(1, result['checked'])
                self.assertEqual(2, result['skipped'], '没轮到的要如实报进 skipped')
                self.assertTrue(result['stopped'])

    def test_the_budget_covers_the_beats_as_well_as_the_channels(self):
        """额度要同时够「每个频道用满自己的预算」和「歇完中间那几拍」。

        只按频道数 × 单频道预算算的话,真实耗时多出 (count-1) 拍,于是每一轮固定有最后
        几个频道被预算闸拦掉、静默进 `skipped` —— 它们本该轮得到。
        """
        count = 10
        needed = count * collector.DISCOVERY_PER_SOURCE + (count - 1) * collector.DISCOVERY_SPACING
        self.assertGreaterEqual(collector.discovery_budget(count), needed)
        # 反过来钉一边:额度不许跟着频道数无限涨,上限照旧封在 `DISCOVERY_CEILING`。
        self.assertEqual(collector.DISCOVERY_CEILING, collector.discovery_budget(500))

    def test_discovered_entries_still_land_while_paced(self):
        """节流不许顺手改掉入库这件事:歇一拍之后带回来的条目照旧收下。

        改串行时「每个频道一份自己的 `told`」那一处也跟着动了(原先是四个线程共写一份
        字典),所以这里连着钉一下条目和那句「没列完」都还落得下来。
        """
        sources = [self.channel(number) for number in (1, 2)]
        entries = {sources[0]['id']: [self.entry(1)], sources[1]['id']: [self.entry(2)]}

        def fetch(source, settings, budget, approximate, told):
            if source['id'] == sources[1]['id']:
                told.append('只列到 1 条就中断了：预算到点')
            return entries[source['id']]

        # 处理那一段这一轮不参与(`available` 报不可用就只登记 deferred,不碰 `media`)。
        with patch.object(collector.notes, 'available', return_value=(False, '本轮不处理视频')):
            result = self.collect(fetch)
        self.assertEqual(2, result['discovered'])
        self.assertEqual({'00000000001', '00000000002'},
                         {row['video_id'] for row in storage.rows()})
        self.assertEqual([{'title': sources[1]['name'], 'reason': '只列到 1 条就中断了：预算到点'}],
                         result['partial'], '哪个频道没列完要说得出来')
