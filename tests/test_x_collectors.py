# -*- coding: utf-8 -*-
"""两条登录态采集器自己那摊事:边界怎么定、哪些推文收进来、环境怎么清场。

和 `test_x_channels.py` 按形状分开(那边是通道的选择、水位、整批预取和它跟 HTTP
那一侧的分工,要一套 feeds.db 摆盘):这里全是纯函数,一个库、一个浏览器、一个
feedgrab 都不碰 —— 挤在一个文件里会越过仓库那条 700 行上限。

跑:  python -m unittest tests.test_x_collectors -v
"""
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from kb.feeds import x_browser, x_channels, x_feedgrab
from tests.x_page_state import page_state

# 样本时间跟着今天走,理由同 test_x_channels:新订阅只收最近几天,写死一个日期
# 的话过几天红的是一堆跟通道毫无关系的用例。
FRESH_ISO = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')


def tweet(id_='1', handle='Example', when=None, text='a thought', context=''):
    return {'id': id_, 'author': handle, 'name': 'Example Person',
            'datetime': when or FRESH_ISO, 'text': text, 'context': context,
            'url': f'https://x.com/{handle}/status/{id_}'}


def Quick():
    """只把两个**等待时长**压短,停止条件一行不动。

    `_settle` 的轮询睡眠和它那 2.5 秒上限决定的是"等多久才承认没滚出新东西",
    改小它不改变任何判定 —— 而不改的话一次 40 轮滚动的用例要跑一分多钟。
    """
    return patch.multiple(x_browser, _SETTLE_STEP=0.0, _SETTLE_SECONDS=0.01)


class Timeline:
    """一个可控的时间线:每滚一次多露出 `page` 条,顺带回答"停在底部了吗"。

    `bottom_at` 默认等于条目总数(真的能滚到底);给一个更大的值 = "页面还没到
    底却不再长出新内容"(懒加载卡住、X 限流),那是"没读完"里最容易被误当成
    "到底了"的一档。
    """

    def __init__(self, items, page=2, bottom_at=None, stall_after=None):
        self.items, self.page = items, page
        self.bottom_at = len(items) if bottom_at is None else bottom_at
        # `stall_after`:露出这么多条之后,**有一次滚动什么都没长出来、而且页面
        # 当时就停在底部**(懒加载还没吐,scrollHeight 也还没长)—— 这正是
        # "一次空窗不能当成到底"要挡的那一拍,下一次滚动照旧有新内容。
        self.stall_after, self.stalled = stall_after, False
        self.shown, self.scrolls = page, 0

    def goto(self, url):
        pass

    def eval(self, js):
        if js is x_browser._STATE_JS:
            return json.dumps(page_state(articles=self.shown))
        if js is x_browser._COLLECT_JS:
            shown = self.items[:self.shown]
            return json.dumps({'articles': len(shown), 'parsed': len(shown),
                               'items': shown})
        if js is x_browser._SCROLL_JS:
            self.scrolls += 1
            if self.shown == self.stall_after and not self.stalled:
                self.stalled = True                 # 这一拍:没长高,而且报在底部
                return json.dumps({'bottom': True})
            self.shown = min(self.shown + self.page, len(self.items))
            return json.dumps({'bottom': self.shown >= self.bottom_at})
        return 'true'


class BrowserBoundaryTests(unittest.TestCase):
    """浏览器通道的边界判定:靠推文时间决定翻到哪儿为止、哪些条目收进来。"""

    def floor(self, hours):
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    def at(self, hours):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')

    def test_published_is_formatted_the_same_way_as_a_feed_entry(self):
        """日报按 `published` 字符串比窗口,两处格式不一样的话同一天的内容会被
        算进不同的窗口(见 kb/daily.py 里那条字符串比较)。"""
        self.assertEqual('2026-10-07 20:30',
                         x_browser._as_published('2026-10-07T12:30:00Z'))
        self.assertEqual('2026-10-07 20:30',
                         x_browser._as_published('2026-10-07T12:30:00.000Z'))
        self.assertEqual('', x_browser._as_published('not a time'))
        self.assertEqual('', x_browser._as_published(None))

    def test_one_old_tweet_is_not_enough_to_declare_the_bottom(self):
        """时间线上混着置顶和偶发的乱序,看一条就收手会把下面的新推漏掉。"""
        floor = self.floor(24)
        items = [tweet('9', when=self.at(1)), tweet('8', when=self.at(30))]
        self.assertFalse(x_browser._old_enough(items, floor))

    def test_a_streak_of_old_tweets_ends_the_scroll(self):
        floor = self.floor(24)
        items = [tweet(str(i), when=self.at(30 + i)) for i in range(3)]
        self.assertTrue(x_browser._old_enough(items, floor))

    def test_a_pinned_tweet_does_not_count_towards_the_streak(self):
        """置顶那条又老又永远在最上面,算进去会让每一轮都在第一屏就收手。"""
        floor = self.floor(24)
        items = [tweet('p', when=self.at(9000), context='Pinned'),
                 tweet('a', when=self.at(30)), tweet('b', when=self.at(31))]
        self.assertFalse(x_browser._old_enough(items, floor))
        items.append(tweet('c', when=self.at(32)))
        self.assertTrue(x_browser._old_enough(items, floor))

    def test_selection_drops_anything_at_or_below_the_floor(self):
        floor = self.floor(24)
        picked = x_browser._select([
            tweet('old', when=self.at(48)),
            tweet('new', when=self.at(2), text='fresh thought'),
        ], floor)
        self.assertEqual(['https://x.com/Example/status/new'], [e['url'] for e in picked])
        self.assertEqual('fresh thought', picked[0]['title'])

    def test_a_freshly_pinned_tweet_is_collected(self):
        """**刚发出来就被钉上去的推文照收。** 原来按"置顶每天都在"一律丢掉,
        那是在拿一个不存在的问题换一个真的:重复入库压根不会发生(`put_items`
        按 `sha256(kind:url)` 去重,而且旧置顶的时间低于水位、这里自己就筛掉),
        而宣布新版本、活动预告这类"发出来就钉上"的推文被永久丢掉 —— 下一轮它
        已经低于水位,再同步也补不回来(2026-10-08 审查实测)。"""
        floor = self.floor(24)
        picked = x_browser._select([
            tweet('p', when=self.at(1), context='置顶', text='new release'),
            tweet('old-p', when=self.at(9000), context='Pinned'),   # 旧置顶照旧被时间闸门筛掉
            tweet('new', when=self.at(2)),
        ], floor, 'example')
        self.assertEqual({'https://x.com/Example/status/p',
                          'https://x.com/Example/status/new'},
                         {e['url'] for e in picked})

    def test_without_a_floor_the_pinned_one_comes_in_too(self):
        picked = x_browser._select([tweet('p', context='Pinned'), tweet('a')], None)
        self.assertEqual(2, len(picked))

    def test_a_pinned_tweet_keeps_its_own_text_not_a_quote_line(self):
        """「置顶」不是转推上下文,别把它当引用行贴进正文。"""
        entry = x_browser._entry(tweet('p', text='new release', context='Pinned'))
        self.assertEqual('new release', entry['body'])

    def test_a_promoted_tweet_is_not_this_persons_opinion(self):
        picked = x_browser._select([tweet('ad', handle='Example', context='Promoted'),
                                    tweet('ad2', handle='Example', context='推广'),
                                    tweet('own')], None, 'example')
        self.assertEqual(['https://x.com/Example/status/own'], [e['url'] for e in picked])

    def test_someone_elses_tweet_with_no_context_is_not_credited_here(self):
        """推荐位(「你可能喜欢」)里别人的推文:按这个订阅记进去就是把别人的
        推文当成这个大V的观点。转推一定带着一行上下文,所以这么判不丢转推 ——
        而转推正是"他此刻在想什么"的一部分。"""
        picked = x_browser._select([
            tweet('rec', handle='Stranger'),                       # 推荐位,无上下文
            tweet('rt', handle='Stranger', context='Example 转推了'),  # 转推,留着
            tweet('own', handle='Example'),
        ], None, 'example')
        self.assertEqual(['https://x.com/Stranger/status/rt', 'https://x.com/Example/status/own'],
                         sorted((e['url'] for e in picked), reverse=True))

    def test_a_handle_in_a_different_case_is_still_this_account(self):
        picked = x_browser._select([tweet('own', handle='ExAmPlE')], None, 'example')
        self.assertEqual(1, len(picked))

    def test_without_a_handle_nothing_is_dropped_for_authorship(self):
        picked = x_browser._select([tweet('rec', handle='Stranger')], None, '')
        self.assertEqual(1, len(picked))

    def test_promoted_and_recommended_tweets_do_not_count_towards_the_streak(self):
        """它们的时间跟这条时间线的进度没有关系,算进去会让翻页提前收手。"""
        floor = self.floor(24)
        noise = [tweet('ad', handle='Example', when=self.at(9000), context='Promoted'),
                 tweet('rec', handle='Stranger', when=self.at(9001)),
                 tweet('rec2', handle='Stranger', when=self.at(9002))]
        self.assertFalse(x_browser._old_enough(noise + [tweet('a', when=self.at(1))],
                                               floor, 'example'))

    def test_a_retweet_keeps_its_context_line_in_the_body(self):
        entry = x_browser._entry(tweet('1', text='worth reading', context='Example 转推了'))
        self.assertTrue(entry['body'].startswith('> Example 转推了'))
        self.assertIn('worth reading', entry['body'])
        self.assertEqual('Example Person', entry['author'])
        self.assertEqual('post', entry['content_kind'])

    def test_a_tweet_with_neither_text_nor_time_is_dropped(self):
        self.assertIsNone(x_browser._entry({'url': 'https://x.com/a/status/1'}))
        self.assertIsNone(x_browser._entry({'text': 'orphan'}))

    def test_an_image_only_tweet_still_becomes_an_entry(self):
        entry = x_browser._entry(tweet('1', text=''))
        self.assertEqual('Example Person 的动态', entry['title'])

    def test_the_watermark_never_advances_on_an_empty_round(self):
        """一条都没抓到可能只是没滚到(预算用完、X 限流)。改成"现在"等于把这段
        时间宣布为已抓过,那几条推文从此再也进不来。"""
        self.assertEqual('2026-10-01T00:00:00+00:00',
                         x_browser._newest([], '2026-10-01T00:00:00+00:00'))
        self.assertEqual('', x_browser._newest([{'published': ''}], ''))

    def test_the_watermark_round_trips_through_the_local_format(self):
        entries = [{'published': '2026-10-07 20:30'}, {'published': '2026-10-06 08:00'}]
        water = x_browser._newest(entries, '')
        self.assertEqual(datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc),
                         x_browser._utc(water))

    def test_cards_that_parse_to_nothing_are_reported_as_a_layout_change(self):
        """"X 改了 DOM"和"这个号今天没发推"必须分开:后者安静跳过是对的,
        前者安静跳过会让这条通道从某天起永远返回空,而面板上一切正常。"""
        class Page:
            def goto(self, url): pass

            def eval(self, js):
                if js is x_browser._STATE_JS:
                    return json.dumps(page_state(articles=7))
                if js is x_browser._COLLECT_JS:
                    return json.dumps({'articles': 7, 'parsed': 0, 'items': []})
                if js is x_browser._SCROLL_JS:
                    return json.dumps({'bottom': True})
                return 'true'

        with Quick():
            with self.assertRaises(x_browser.XBrowserError) as caught:
                x_browser._harvest_one(Page(), 'Example', None)
        self.assertIn('改版', str(caught.exception))

    def test_a_login_page_is_not_mistaken_for_an_empty_timeline(self):
        class Page:
            def eval(self, js):
                return json.dumps(page_state(url='https://x.com/i/flow/login',
                                             login=True))

        with self.assertRaises(x_browser.XBrowserError) as caught:
            x_browser._wait_state(Page(), 'Example', time.monotonic() + 1)
        self.assertIn('登录态', str(caught.exception))


class ReadToTheWatermarkTests(unittest.TestCase):
    """「这一轮读到上次的位置了吗」—— 没读到就不许推进水位。"""

    def at(self, hours):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')

    def floor(self, hours):
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    def fresh(self, count):
        """全都比水位新的一串推文:无论滚多少次都翻不到底。"""
        return [tweet(str(i), when=self.at(1 + i * 0.01)) for i in range(count)]

    def test_running_out_of_scrolls_is_reported_as_incomplete(self):
        """生产的 40 次上限 + 42 条都比水位新的推文:滚完 40 次仍在时间线中间。

        原来 `_harvest_one` 对这一档什么都不说,`harvest` 无条件按"这一轮见过的
        最新"推进水位 —— 没读到的那几条比旧水位新、又比新水位老,从此任何一轮
        增量都选不到(审查实测 `retry_urls=[]`)。
        """
        page = Timeline(self.fresh(42), page=1)
        with Quick():
            entries, incomplete = x_browser._harvest_one(page, 'Example', self.floor(24))
        self.assertEqual(x_browser.MAX_SCROLLS, page.scrolls)
        self.assertIn('滚动次数', incomplete)
        self.assertTrue(entries)                       # 抓到的照旧要入库

    def test_a_timeline_that_stops_growing_short_of_the_bottom_is_incomplete(self):
        """懒加载卡住 / 被限流:页面不长高,而 scrollHeight 说还没到底。

        这一档和"到底了"长得一模一样,把它当成完整成功就是给一次没读完的抓取
        盖章 —— 所以判据是「停在底部」而且「连着 `_BOTTOM_TRIES` 次」。
        """
        page = Timeline(self.fresh(4), page=2, bottom_at=99)
        with Quick():
            _, incomplete = x_browser._harvest_one(page, 'Example', self.floor(24))
        self.assertIn('底部', incomplete)

    def test_reaching_the_real_bottom_counts_as_complete(self):
        page = Timeline(self.fresh(4), page=2)
        with Quick():
            entries, incomplete = x_browser._harvest_one(page, 'Example', self.floor(24))
        self.assertEqual('', incomplete)
        self.assertEqual(4, len(entries))

    def test_one_empty_lazy_load_window_is_not_the_bottom(self):
        """**一次空窗不算到底**,哪怕那一刻页面确实停在底部。

        慢网络下的一拍懒加载空窗和"时间线到底了"在页面上完全同形(没长高、
        scrollHeight 也没变)。认一次就收手的话:这一轮停在半中间、还以"完整"
        收场 —— 水位跟着推进,下面那一段从此谁也补不回来。
        所以断言的是**后果**(读全了没有),不是滚了几次 —— 拿 `_BOTTOM_TRIES`
        自己去算次数的话,把它改成 1 这条用例照旧全绿。
        """
        page = Timeline(self.fresh(6), page=2, stall_after=4)
        with Quick():
            entries, incomplete = x_browser._harvest_one(page, 'Example', self.floor(24))
        self.assertEqual(6, len(entries), '停在那一拍空窗上,后面的推文没读到')
        self.assertEqual('', incomplete)

    def test_reaching_the_streak_counts_as_complete(self):
        items = self.fresh(2) + [tweet(f'o{i}', when=self.at(30 + i)) for i in range(3)]
        page = Timeline(items, page=5)
        with Quick():
            _, incomplete = x_browser._harvest_one(page, 'Example', self.floor(24))
        self.assertEqual('', incomplete)

    def test_a_first_round_without_a_floor_is_never_called_incomplete(self):
        """新订阅没有水位,"读到水位了吗"无从谈起 —— 按读到的为准。"""
        page = Timeline(self.fresh(42), page=1)
        with Quick():
            _, incomplete = x_browser._harvest_one(page, 'Example', None)
        self.assertEqual('', incomplete)

    def test_an_incomplete_round_keeps_the_old_watermark(self):
        """整条链上的那一半:`harvest` 看到未完成就留着旧水位。

        下一轮重叠重抓,`put_items` 按 URL 去重,一条都不会重复入库。
        """
        old = '2026-10-01T00:00:00+00:00'

        def fake_harvest_one(page, handle, floor):
            return [{'published': '2026-10-07 20:30'}], '滚动次数用完'

        with patch.object(x_browser, '_harvest_one', fake_harvest_one), \
                patch.object(x_browser, 'Chrome', _FakeChrome):
            got = x_browser.harvest([('example', old)], profile='/tmp/p')
        self.assertEqual(old, got['example']['watermark'])
        self.assertEqual('滚动次数用完', got['example']['incomplete'])
        self.assertEqual('', got['example']['error'])

    def test_a_first_incomplete_round_saves_the_floor_it_actually_used(self):
        """**新订阅那一轮尤其要存**:它的 `water` 是空的,「留着旧水位」在它身上
        什么都没留下 —— 空值压根不产生 config 补丁(`x_channels.watermark_patch`),
        下一轮又按「当天往回 FIRST_DAYS 天」现算一个下界,那段没读完的日期
        **随时间往前滑走**,而面板上写的是「已同步」(2026-10-09 第 4 轮审查在
        feedgrab 那条通道上实测,两条通道是同一个缺口)。

        断言的是「存下来的那个值解析回去,正是这一轮真用过的下界」—— 只验"非空"
        的话,存一个"现在"上去也能过,而那恰好是最坏的一种。
        """
        used = []

        def fake_harvest_one(page, handle, floor):
            used.append(floor)
            return [{'published': '2026-10-07 20:30'}], '滚动次数用完'

        with patch.object(x_browser, '_harvest_one', fake_harvest_one), \
                patch.object(x_browser, 'Chrome', _FakeChrome):
            got = x_browser.harvest([('example', '')], profile='/tmp/p')
        self.assertNotEqual('', got['example']['watermark'], '首轮未完成,下界没存下来')
        self.assertEqual(used[0], x_browser._utc(got['example']['watermark']))

    def test_a_complete_round_does_advance_the_watermark(self):
        def fake_harvest_one(page, handle, floor):
            return [{'published': '2026-10-07 20:30'}], ''

        with patch.object(x_browser, '_harvest_one', fake_harvest_one), \
                patch.object(x_browser, 'Chrome', _FakeChrome):
            got = x_browser.harvest([('example', '2026-10-01T00:00:00+00:00')],
                                    profile='/tmp/p')
        self.assertEqual(datetime(2026, 10, 7, 12, 30, tzinfo=timezone.utc),
                         x_browser._utc(got['example']['watermark']))
        self.assertEqual('', got['example']['incomplete'])


class _FakeChrome:
    """只替掉"起一个浏览器"这一下,`harvest` 自己那段编排一行不打桩。"""

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def open_page(self, url):
        pass


class PageOrderStreakTests(unittest.TestCase):
    """「连续几条老推」问的是**页面出现顺序**,不是时间排序。"""

    def at(self, hours):
        return (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
            '%Y-%m-%dT%H:%M:%SZ')

    def floor(self, hours):
        return datetime.now(timezone.utc) - timedelta(hours=hours)

    def test_old_retweets_scattered_among_new_tweets_are_not_the_bottom(self):
        """转推显示的是**原推**时间,所以一条很老的转推可能就排在新推中间。

        按时间排序的话这三条不相邻的旧转推会被拼成一条"连续的老尾巴",第一屏
        就判翻到底、下面的新推全漏(审查实测:穿插转发旧内容会漏掉后面的新推)。
        """
        rt = lambda i: tweet(f'rt{i}', handle='Someone',
                             when=self.at(9000 + i), context='Example 转推了')
        items = [tweet('n1', when=self.at(1)), rt(1),
                 tweet('n2', when=self.at(2)), rt(2),
                 tweet('n3', when=self.at(3)), rt(3)]
        self.assertFalse(x_browser._old_enough(items, self.floor(24), 'example'))

    def test_a_fresh_tweet_resets_the_streak(self):
        items = [tweet('o1', when=self.at(30)), tweet('o2', when=self.at(31)),
                 tweet('n', when=self.at(1)),
                 tweet('o3', when=self.at(32)), tweet('o4', when=self.at(33))]
        self.assertFalse(x_browser._old_enough(items, self.floor(24), 'example'))
        items.append(tweet('o5', when=self.at(34)))
        self.assertTrue(x_browser._old_enough(items, self.floor(24), 'example'))

    def test_a_consecutive_run_of_the_accounts_own_old_tweets_is_the_bottom(self):
        items = [tweet('n', when=self.at(1))] + \
                [tweet(f'o{i}', when=self.at(30 + i)) for i in range(3)]
        self.assertTrue(x_browser._old_enough(items, self.floor(24), 'example'))

    def test_an_old_retweet_does_not_add_to_the_streak_but_a_fresh_one_resets_it(self):
        """两头各管一边:转推"很老"证明不了位置(可能是刚转的一条老推),
        而它"很新"倒是证明得了 —— 活动时间比它显示的那个时间更新。"""
        old_rt = tweet('rt', handle='Someone', when=self.at(9000),
                       context='Example 转推了')
        own_old = [tweet(f'o{i}', when=self.at(30 + i)) for i in range(2)]
        self.assertFalse(x_browser._old_enough(own_old + [old_rt], self.floor(24), 'example'))
        fresh_rt = tweet('rt2', handle='Someone', when=self.at(1),
                         context='Example 转推了')
        self.assertFalse(x_browser._old_enough(
            own_old + [fresh_rt] + [tweet('o9', when=self.at(40))],
            self.floor(24), 'example'))

    def test_the_streak_ignores_items_that_are_not_on_this_timeline(self):
        """推广位和推荐位既不入库、也不参与这个证明(两处共用 `_in_timeline`)。"""
        noise = [tweet('ad', when=self.at(9000), context='Promoted'),
                 tweet('rec', handle='Stranger', when=self.at(9001)),
                 tweet('rec2', handle='Stranger', when=self.at(9002))]
        self.assertFalse(x_browser._old_enough(noise, self.floor(24), 'example'))


@contextmanager
def host_tz(name):
    """把**宿主机器**的时区换成 name:日期边界那几条要证明的正是"它不影响结果"。"""
    old = os.environ.get('TZ')
    os.environ['TZ'] = name
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = old
        time.tzset()


class DateBoundaryTests(unittest.TestCase):
    """区间补抓那一天的边界:时区按项目的、起点算在里面。

    两条独立的错各有一条回归(审查第 2、3 条):只修时区的话零点那条照样漏,
    而零点那条在上海时区的机器上也成立。
    """

    SHANGHAI = ZoneInfo('Asia/Shanghai')

    def day(self, hour, minute=0, day=5):
        return datetime(2026, 10, day, hour, minute, tzinfo=self.SHANGHAI)

    def dated(self):
        """上海时间 10-05 那天的四条(含零点整),外加前一天三条原创老推 ——
        后者让"连着三条自己的老推"成立,这一轮才算真读完。"""
        inside = [self.day(8, 1), self.day(7, 59), self.day(1, 0), self.day(0, 0)]
        before = [self.day(23, day=4), self.day(22, day=4), self.day(21, day=4)]
        return [tweet(str(100 + i), when=w.isoformat())
                for i, w in enumerate(inside + before)]

    def test_a_naive_timestamp_means_the_projects_timezone_not_the_machines(self):
        """`astimezone` 对不带时区的时间拿**宿主**时区补齐 —— UTC 机器上
        `2026-10-05` 会被理解成上海时间 08:00(审查实测漏掉凌晨 8 小时)。"""
        for name in ('UTC', 'Asia/Shanghai', 'America/Los_Angeles'):
            with host_tz(name):
                self.assertEqual(self.day(0), x_browser._utc('2026-10-05'), name)

    def test_the_whole_requested_day_comes_in_on_a_utc_machine(self):
        """审查实测 `TZ=UTC`:四条里只收进 08:01,00:00 / 01:00 / 07:59 全缺,
        而 `errors=[]` —— 没有任何提示。"""
        with host_tz('UTC'), Quick():
            entries, incomplete = x_browser._harvest_one(
                Timeline(self.dated(), page=3), 'Example',
                x_browser._utc(x_channels.since_floor('2026-10-05')))
        self.assertEqual('', incomplete)
        self.assertEqual(['2026-10-05 00:00', '2026-10-05 01:00',
                          '2026-10-05 07:59', '2026-10-05 08:01'],
                         [e['published'] for e in entries])

    def test_midnight_itself_is_inside_the_requested_range(self):
        """下游那条选择规则是严格大于(日常水位靠它避免重抓),而"从 10-05 开始"
        包含这一天 00:00:00 发的那条 —— 在上海时区的机器上也一样漏
        (审查实测 `missing_times=['2026-10-05 00:00']`)。"""
        with host_tz('Asia/Shanghai'), Quick():
            entries, _ = x_browser._harvest_one(
                Timeline(self.dated(), page=3), 'Example',
                x_browser._utc(x_channels.since_floor('2026-10-05')))
        self.assertIn('2026-10-05 00:00', [e['published'] for e in entries])

    def test_the_daily_watermark_still_excludes_the_tweet_it_stopped_at(self):
        """包含性只属于区间那条路:日常水位照旧**排他**,否则每一轮都把上一轮
        最后那条重抓一遍(`put_items` 会去重,但那是白跑一趟)。"""
        water = self.day(1, 0)
        picked = x_browser._select(self.dated(), x_browser._utc(water.isoformat()), 'Example')
        self.assertEqual(['2026-10-05 07:59', '2026-10-05 08:01'],
                         [e['published'] for e in picked])


class FeedgrabChannelTests(unittest.TestCase):
    def test_the_vault_variable_is_cleared_so_tweets_never_land_in_the_vault(self):
        """feedgrab 的输出目录优先认 OBSIDIAN_VAULT,而这台机器上它指的正是知识库
        那个 vault —— 不清掉,推文会被直接写进 vault/X/,绕过 feed_items、翻译和
        导出整条链(索引照收,清理找不到)。"""
        with patch.dict(os.environ, {'OBSIDIAN_VAULT': '/Users/someone/vault',
                                     'OUTPUT_DIR': '/elsewhere',
                                     'HTTPS_PROXY': 'http://127.0.0.1:7890',
                                     'SOME_SECRET': 'leak-me'}, clear=False):
            env = x_feedgrab._env(Path('/tmp/run-x'), '/tmp/sessions', '2026-10-01')
        self.assertEqual('', env['OBSIDIAN_VAULT'])
        self.assertEqual('/tmp/run-x', env['OUTPUT_DIR'])
        self.assertEqual('2026-10-01', env['X_USER_TWEETS_SINCE'])
        self.assertEqual('true', env['X_USER_TWEETS_ENABLED'])
        self.assertEqual('http://127.0.0.1:7890', env['HTTPS_PROXY'])
        self.assertNotIn('SOME_SECRET', env)

    def test_a_ranged_floor_is_handed_over_as_the_day_before(self):
        """这条通道的下界是 `X_USER_TWEETS_SINCE`,只认日期而且按 **UTC 的日**算
        —— 把 `2026-10-05` 原样交给它,上海时间那天 0 点到 7 点 59 一条也拿不到。
        换算成前一天的最后一瞬之后 `[:10]` 得到 10-04,那段窗口才回来;多出来的
        前一天由 `sync._save_result` 的日期筛选收口。

        这也是为什么换算放在 `x_channels.since_floor` 而不是给浏览器那条选择规则
        加一个"包含起点"的开关:后者修不到这条通道。
        """
        seen = {}

        def run(exe, handle, env, base):
            seen.update(env)
            # `_run` 的契约是把 CLI 的输出交回来(完整性判据要读它),所以这个桩
            # 也得给一句真实形状的收尾摘要 —— 返回 None 的桩测的就不是同一条路。
            return '总数: 0, 成功: 0, 跳过: 0, 失败: 0'

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(x_feedgrab, 'find_binary', lambda b: '/usr/bin/true'), \
                    patch.object(x_feedgrab, '_run', run), \
                    patch.object(x_feedgrab, '_read_output', lambda *a: []):
                x_feedgrab.harvest([('example', x_channels.since_floor('2026-10-05'))],
                                   data_dir=f'{tmp}/sessions', workdir=f'{tmp}/work')
        self.assertEqual('2026-10-04', seen['X_USER_TWEETS_SINCE'])

    def test_a_first_incomplete_round_saves_the_boundary_it_handed_over(self):
        """同浏览器通道那条:首轮没抓全时存回**这一轮真正交给上游的那个下界**。

        这条通道的下界就是 `X_USER_TWEETS_SINCE`,所以断言直接拿它比 —— 存回它
        不会漏掉边界那一天:上游的日期过滤是 `tweet_date < since_date` 才丢
        (含下界当天),下一轮的窗口和这一轮逐字相同。
        """
        seen = {}

        def run(exe, handle, env, base):
            seen.update(env)
            # 翻页半途中断:上游这一行之后 `break`,而批量记录和摘要里一个字
            # 都看不出来(见 `x_feedgrab._incomplete`)。
            return ('[UserTweets] >>> 第 2 页所有 1 个账号均失败 <<< 已抓取 1 条，停止分页\n'
                    '总数: 1, 成功: 1, 跳过: 0, 失败: 0')

        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(x_feedgrab, 'find_binary', lambda b: '/usr/bin/true'), \
                    patch.object(x_feedgrab, '_run', run), \
                    patch.object(x_feedgrab, '_read_output', lambda *a: []):
                got = x_feedgrab.harvest([('example', '')], data_dir=f'{tmp}/sessions',
                                         workdir=f'{tmp}/work')
        self.assertIn('翻页', got['example']['incomplete'])
        self.assertEqual(seen['X_USER_TWEETS_SINCE'], got['example']['watermark'],
                         '首轮未完成,交给上游的那个下界没存下来')

    def test_no_since_key_is_set_when_there_is_no_watermark(self):
        env = x_feedgrab._env(Path('/tmp/run-x'), '/tmp/sessions', '')
        self.assertNotIn('X_USER_TWEETS_SINCE', env)

    def test_another_handles_tweet_is_not_credited_to_this_subscription(self):
        """引用/转推会在产物里留下别人的 status 链接;按它入库就是把别人的推文
        记成这个订阅的内容。"""
        meta = {'source': 'https://x.com/Someone/status/9', 'published': '2026-10-07',
                'author_name': 'Someone'}
        self.assertIsNone(x_feedgrab._entry(meta, 'quoted text', 'Example'))

    def test_a_pinned_tweet_is_collected_here_too(self):
        """口径和浏览器通道同一条:刚发出来就被钉上去的推文不许丢(审查实测,
        原来这里按 `is_pinned` 一律返回 None)。重复入库由 `put_items` 去重兜住。"""
        meta = {'source': 'https://x.com/Example/status/1', 'published': '2026-10-07',
                'is_pinned': 'true'}
        entry = x_feedgrab._entry(meta, 'pinned text', 'Example')
        self.assertIsNotNone(entry, '置顶推文被丢掉了')
        self.assertEqual('https://x.com/Example/status/1', entry['url'])

    def test_a_tweet_becomes_an_entry_with_day_precision(self):
        meta = {'source': 'https://x.com/Example/status/1', 'published': '2026-10-07',
                'author': ['Example Person'], 'title': 'a thought'}
        entry = x_feedgrab._entry(meta, 'a thought body', 'Example')
        self.assertEqual('2026-10-07', entry['published'])
        self.assertEqual('Example Person', entry['author'])
        self.assertEqual('post', entry['content_kind'])

    def test_the_watermark_keeps_day_precision_and_never_jumps_on_empty(self):
        self.assertEqual('2026-10-07', x_feedgrab._newest(
            [{'published': '2026-10-06'}, {'published': '2026-10-07'}], ''))
        self.assertEqual('2026-10-01', x_feedgrab._newest([], '2026-10-01'))

    def test_a_missing_binary_is_reported_as_something_to_install(self):
        with patch('shutil.which', return_value=None):
            with self.assertRaises(x_feedgrab.FeedgrabError) as caught:
                x_feedgrab.find_binary('')
        self.assertIn('feedgrab', str(caught.exception))
        with self.assertRaises(x_feedgrab.FeedgrabError):
            x_feedgrab.find_binary('/nonexistent/feedgrab')


if __name__ == '__main__':
    unittest.main()
