# -*- coding: utf-8 -*-
"""两条登录态采集器自己那摊事:边界怎么定、哪些推文收进来、环境怎么清场。

和 `test_x_channels.py` 按形状分开(那边是通道的选择、水位、整批预取和它跟 HTTP
那一侧的分工,要一套 feeds.db 摆盘):这里全是纯函数,一个库、一个浏览器、一个
feedgrab 都不碰 —— 挤在一个文件里会越过仓库那条 700 行上限。

跑:  python -m unittest tests.test_x_collectors -v
"""
import json
import os
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kb.feeds import x_browser, x_feedgrab

# 样本时间跟着今天走,理由同 test_x_channels:新订阅只收最近几天,写死一个日期
# 的话过几天红的是一堆跟通道毫无关系的用例。
FRESH_ISO = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime('%Y-%m-%dT%H:%M:%SZ')


def tweet(id_='1', handle='Example', when=None, text='a thought', context=''):
    return {'id': id_, 'author': handle, 'name': 'Example Person',
            'datetime': when or FRESH_ISO, 'text': text, 'context': context,
            'url': f'https://x.com/{handle}/status/{id_}'}


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

    def test_selection_drops_pinned_and_anything_at_or_below_the_floor(self):
        floor = self.floor(24)
        picked = x_browser._select([
            # 置顶那条故意是**新的**:只有置顶这条规则能把它丢掉,时间闸门不会。
            # 写成一条老置顶的话,这个断言对"置顶照收"压根不敏感。
            tweet('p', when=self.at(1), context='置顶'),
            tweet('old', when=self.at(48)),
            tweet('new', when=self.at(2), text='fresh thought'),
        ], floor)
        self.assertEqual(['https://x.com/Example/status/new'], [e['url'] for e in picked])
        self.assertEqual('fresh thought', picked[0]['title'])

    def test_without_a_floor_everything_except_the_pinned_one_comes_in(self):
        picked = x_browser._select([tweet('p', context='Pinned'), tweet('a')], None)
        self.assertEqual(1, len(picked))

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
                    return json.dumps({'url': 'https://x.com/Example', 'ready': 'complete',
                                       'articles': 7, 'login': False, 'empty': False,
                                       'limited': False})
                if js is x_browser._COLLECT_JS:
                    return json.dumps({'articles': 7, 'parsed': 0, 'items': []})
                return 'true'

        with self.assertRaises(x_browser.XBrowserError) as caught:
            x_browser._harvest_one(Page(), 'Example', None)
        self.assertIn('改版', str(caught.exception))

    def test_a_login_page_is_not_mistaken_for_an_empty_timeline(self):
        class Page:
            def eval(self, js):
                return json.dumps({'url': 'https://x.com/i/flow/login', 'ready': 'complete',
                                   'articles': 0, 'login': True, 'empty': False,
                                   'limited': False})

        with self.assertRaises(x_browser.XBrowserError) as caught:
            x_browser._wait_state(Page(), 'Example', time.monotonic() + 1)
        self.assertIn('登录态', str(caught.exception))


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

    def test_no_since_key_is_set_when_there_is_no_watermark(self):
        env = x_feedgrab._env(Path('/tmp/run-x'), '/tmp/sessions', '')
        self.assertNotIn('X_USER_TWEETS_SINCE', env)

    def test_another_handles_tweet_is_not_credited_to_this_subscription(self):
        """引用/转推会在产物里留下别人的 status 链接;按它入库就是把别人的推文
        记成这个订阅的内容。"""
        meta = {'source': 'https://x.com/Someone/status/9', 'published': '2026-10-07',
                'author_name': 'Someone'}
        self.assertIsNone(x_feedgrab._entry(meta, 'quoted text', 'Example'))

    def test_a_pinned_tweet_is_dropped_here_too(self):
        meta = {'source': 'https://x.com/Example/status/1', 'published': '2026-10-07',
                'is_pinned': 'true'}
        self.assertIsNone(x_feedgrab._entry(meta, 'pinned text', 'Example'))

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
