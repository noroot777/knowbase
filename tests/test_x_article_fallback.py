# -*- coding: utf-8 -*-
"""**后路**:长文接口关了,就从手里那个已经登录的浏览器里把文章页读出来。

**按形状单独一个文件**:`test_x_article.py` 已经贴着 700 行硬上限,而这一组的
摆盘是「假 CDP 会话按脚本交出 DOM 快照」、判据是「页面落在哪一篇上」—— 和那边
那几组(渲染、接口解析、水位)形状本来就不同。摆盘仍共用那一份,各写一份的话
两边的前提会各漂各的。
"""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from kb.feeds import x_article, x_browser, x_feedgrab
from kb.feeds.fetching import FeedError
from kb.feeds.x_cdp import CdpError
from kb.feeds import registry as reg
from tests.test_x_article import (BLANK, OLD_WATER, _FakeChrome, answering,
                                  article_payload, entry, harvest_round, snapshot)
from tests.test_x_channels import ChannelBase


ART = ['整理书签、做学习计划,我以前都是一个人对着 AI 来回问。',
       '## 先说结论',
       '![](https://pbs.twimg.com/media/Fx.jpg)',
       '书签保存开源项目:https://github.com/example/x-bookmarks-archive']


class BrowserFallbackTests(unittest.TestCase):
    """**后路**:长文接口(FxTwitter)是个第三方服务,它关了这一步就没了 ——
    而 `browser` / `attach` 两条通道手里正好有一个**已经登录**的浏览器,直接
    把文章页读出来即可(`/<handle>/article/<tweet_id>`)。

    这一组的重点是**判据的方向性**。把「有长文」说成「没有」会让一条空正文
    落库,而 `put_items` 是 `DO NOTHING` —— 那一篇从此永久是空的、下一轮重试
    也救不回来;反过来把「没有长文」说成「问不到」只是让水位留在原处、下一轮
    重叠重抓一次。所以只有 X 自己把我们弹回推文页那一种才算「没有」,登录墙、
    空壳、渲染超时、CDP 报错一律当成「问不到」。
    """

    def test_a_tweet_url_becomes_an_article_url(self):
        """`/status/` 换成 `/article/`。**这是 X 自己的规范形式**,所以后路
        不需要 `article_id` —— 那个 id 只出现在接口回包里,而后路恰恰要在
        接口已经不可用的时候跑。"""
        self.assertEqual(
            'https://x.com/Pluvio9yte/article/2108346962986909740',
            x_article.article_page(
                'https://x.com/Pluvio9yte/status/2108346962986909740'))
        for bad in ('https://x.com/i/article/123', 'https://x.com/someone',
                    'https://example.org/a/status/1', '', 'not a url'):
            self.assertEqual('', x_article.article_page(bad), bad)

    def test_the_fallback_fills_the_body_when_the_endpoint_is_down(self):
        """**后果先断言**:接口 500 而后路读到了 —— 这一轮就是读全了,水位
        照常推进,正文真的落在条目上。"""
        got, _ = harvest_round([FeedError('HTTP 500')],
                               snaps=[snapshot(blocks=ART, title='X书签保存Skill')])
        self.assertEqual('', got['incomplete'], '后路补上了,这一轮不算没读完')
        self.assertEqual(datetime(2026, 10, 9, 0, 1, tzinfo=timezone.utc),
                         x_browser._utc(got['watermark']))
        article = [e for e in got['entries'] if e['url'].endswith('/9')][0]
        self.assertIn('整理书签', article['body'])
        self.assertIn('## 先说结论', article['body'], '块级结构照搬')
        self.assertIn('![](https://pbs.twimg.com/media/Fx.jpg)', article['body'])
        self.assertIn('\n\n', article['body'], '段落边界是按块拿的')
        self.assertEqual('X书签保存Skill', article['title'])

    def test_the_fallback_opens_the_article_page_of_that_tweet(self):
        harvest_round([FeedError('x')], snaps=[snapshot(blocks=ART)])
        self.assertEqual(['https://x.com/example/article/9'], _FakeChrome.visited)

    def test_x_bouncing_us_back_means_this_tweet_has_no_article(self):
        """纯图推文:实测 X 自己把 `/article/<id>` 302 回 `/status/<id>`。
        那是一个确定的结论 —— 照旧入库、**不冻水位**,不然一条纯图推文能把
        这个号的水位永久冻住。"""
        got, _ = harvest_round([FeedError('x')],
                               snaps=[snapshot(at=False, back=True)])
        self.assertEqual('', got['incomplete'])
        self.assertEqual(datetime(2026, 10, 9, 0, 1, tzinfo=timezone.utc),
                         x_browser._utc(got['watermark']))
        self.assertEqual(['https://x.com/example/status/9',
                          'https://x.com/example/status/8'],
                         [e['url'] for e in got['entries']], '照旧入库')

    def test_a_login_wall_is_not_a_tweet_without_an_article(self):
        """**方向性那条**:既不在文章页、又没有正文 —— 这是「这个浏览器没登录
        X」的形态,不是「这条没有长文」。当成后者就是拿一次故障换一条永久
        空正文。"""
        got, _ = harvest_round([FeedError('x')],
                               snaps=[snapshot(at=False, back=False)])
        self.assertEqual(OLD_WATER, got['watermark'], '水位一步都不许动')
        self.assertIn('长文正文没取到', got['incomplete'])
        self.assertEqual(['https://x.com/example/status/8'],
                         [e['url'] for e in got['entries']], '本轮不入库')
        self.assertEqual('', got['error'], '这不是一次失败,只是没读完')

    def test_a_blank_article_page_freezes_the_watermark_too(self):
        got, _ = harvest_round([FeedError('x')], snaps=[snapshot(blocks=())])
        self.assertEqual(OLD_WATER, got['watermark'])
        self.assertIn('长文正文没取到', got['incomplete'])

    def test_a_cdp_error_does_not_take_the_whole_round_down(self):
        """单篇读不动只是这一条没取到,剩下的账号照旧跑完 —— 别让它冒到
        `harvest` 外面那道「浏览器会话中断」上去。"""
        got, _ = harvest_round([FeedError('x')], snaps=[CdpError('eval 超时')])
        self.assertEqual(OLD_WATER, got['watermark'])
        self.assertIn('长文正文没取到', got['incomplete'])
        self.assertEqual('', got['error'])

    def test_the_endpoint_saying_no_article_never_opens_a_browser(self):
        """接口答「这条没有长文」是确定结论,不该再开一张标签页白等。"""
        got, _ = harvest_round([{'tweet': {'text': 'hi'}}])
        self.assertEqual([], _FakeChrome.visited)
        self.assertEqual('', got['incomplete'])

    def test_a_working_endpoint_never_opens_a_browser(self):
        harvest_round([article_payload()])
        self.assertEqual([], _FakeChrome.visited)

    def test_turning_the_fallback_off_leaves_the_old_behaviour(self):
        """`KB_X_ARTICLE_BROWSER=0`:接口失败就按没读完办,浏览器一次都不碰。"""
        got, _ = harvest_round([FeedError('x')], snaps=[snapshot(blocks=ART)],
                               fallback=False)
        self.assertEqual([], _FakeChrome.visited)
        self.assertEqual(OLD_WATER, got['watermark'])
        self.assertIn('长文正文没取到', got['incomplete'])

    def test_the_endpoint_off_but_a_browser_in_hand_still_fills(self):
        """`KB_X_ARTICLE_API=''`(不想用那个第三方)+ 手里有浏览器:只走后路,
        一次 HTTP 都不发,长文照旧补得到。"""
        got, calls = harvest_round([], api='',
                                   snaps=[snapshot(blocks=ART, title='标题')])
        self.assertEqual([], calls.urls, '接口关掉了就一次都不该问')
        self.assertEqual(['https://x.com/example/article/9'], _FakeChrome.visited)
        article = [e for e in got['entries'] if e['url'].endswith('/9')][0]
        self.assertIn('整理书签', article['body'])
        self.assertEqual('', got['incomplete'])

    def test_both_switches_off_is_a_full_stand_down(self):
        got, calls = harvest_round([], api='', fallback=False,
                                   snaps=[snapshot(blocks=ART)])
        self.assertEqual(([], []), (calls.urls, _FakeChrome.visited))
        self.assertEqual('', got['incomplete'])
        self.assertEqual(2, len(got['entries']), '一条都不拦,逐字退回从前')

    def test_the_fallback_resets_the_give_up_counter(self):
        """接口整个挂了而后路都好时,**不许在第 3 条上放弃** —— `_GIVE_UP_AFTER`
        防的是"每条各等满一次超时",而后路正常答得上来。"""
        sess = _FakeChrome()
        _FakeChrome.reset(snapshot(blocks=ART))
        req, calls = answering(*[FeedError('down')] * 10)
        with req, patch.object(x_article, 'PAGE_WAIT', 0.0):
            kept, note = x_article.fill(
                [entry(url=f'https://x.com/a/status/{i}') for i in range(10)],
                session=sess)
        self.assertEqual('', note)
        self.assertEqual(10, len(kept))
        self.assertTrue(all('整理书签' in e['body'] for e in kept))

    def test_the_fallback_gives_up_when_it_is_broken_too(self):
        """两条路都坏了时照旧要放弃,否则 10 条各等一次渲染超时。"""
        sess = _FakeChrome()
        _FakeChrome.reset(BLANK)
        req, _ = answering(*[FeedError('down')] * 10)
        with req, patch.object(x_article, 'PAGE_WAIT', 0.0):
            kept, note = x_article.fill(
                [entry(url=f'https://x.com/a/status/{i}') for i in range(10)],
                session=sess)
        self.assertEqual([], kept)
        self.assertEqual(x_article._GIVE_UP_AFTER, len(_FakeChrome.visited))
        self.assertIn('10', note)

    def test_an_empty_title_falls_back_to_the_tweets_own(self):
        sess = _FakeChrome()
        _FakeChrome.reset(snapshot(blocks=ART, title=''))
        req, _ = answering(FeedError('down'))
        with req, patch.object(x_article, 'PAGE_WAIT', 0.0):
            kept, _ = x_article.fill([entry(title='某人 的动态')], session=sess)
        self.assertEqual('某人 的动态', kept[0]['title'])

    def test_feedgrab_has_no_browser_so_nothing_changes(self):
        """**反过来钉一边**:feedgrab 那条路压根没有浏览器,后路不许悄悄对它
        生效 —— 它是子进程调用,手里没有任何 CDP 会话。"""
        import inspect
        src = inspect.getsource(x_feedgrab)
        call = src.split('x_article.fill')[1]
        self.assertNotIn('session=', call[:call.index(')')],
                         'feedgrab 手里没有 CDP 会话,不许把 session 传下去')


ART_B = ['这是第二篇文章自己的正文。', '## 第二篇的小节']


def two_articles(*, stuck='', snaps=(), fallback=True):
    """两条都是长文(正文空、带长文卡片)的一轮,接口整个挂掉所以必走后路。

    返回 `(kept, note, visited)`。`fill` / `browser_fetch` 一行不打桩 —— 这一组
    要验的正是「页面没跳过去时它拿没拿别人的正文」,打桩成功正好绕开它。
    """
    _FakeChrome.reset(*(snaps or [snapshot(blocks=ART_B)]), stuck=stuck)
    sess = _FakeChrome()
    req, _ = answering(FeedError('down'), FeedError('down'))
    with req, patch.object(x_article, 'PAGE_WAIT', 0.0), \
            patch.object(x_article, 'BROWSER_FALLBACK', fallback):
        kept, note = x_article.fill(
            [entry(url='https://x.com/Review/status/100', body=''),
             entry(url='https://x.com/Review/status/200', body='')],
            session=sess)
    return kept, note, list(_FakeChrome.visited)


class PageIdentityTests(unittest.TestCase):
    """**这一页到底是不是我们要的那一篇。**

    导航失败时 Chrome 的 `Page.navigate` 回包带 `errorText`,而**页面停在原处**
    —— 上一条刚读过的那篇文章。实测一个 HTTP 204 的文章页就足够(2026-10-09
    第 1 轮审查用真无头浏览器复现)。不核对身份的后果是**不可恢复的**:第二条
    被写进第一篇的标题和正文,`fill` 报成功、水位照常推进,而 `put_items` 是
    `ON CONFLICT(key) DO NOTHING` —— 拿正确正文重新入库返回新增 0 条。

    **每条都先断言后果**(那一条扣留了没有、它身上是谁的正文),只验「有没有
    发请求」的话,一个认出来却照样收下正文的实现也能全绿。
    """

    def test_a_failed_navigation_never_hands_over_the_previous_article(self):
        """**后果**:第二条导航失败 → 它被扣留,**而且绝不带着第一篇的正文**。"""
        kept, note, _ = two_articles(
            stuck=['', 'abort'],
            snaps=[snapshot(blocks=['第一篇的正文,只属于 /status/100。'])])
        self.assertEqual(['https://x.com/Review/status/100'],
                         [e['url'] for e in kept], '第二条必须被扣留')
        self.assertIn('第一篇的正文', kept[0]['body'])
        self.assertNotIn('/status/200', [e['url'] for e in kept])
        self.assertIn('没取到', note)

    def test_a_silent_failed_navigation_is_caught_too(self):
        """**身份核对必须自己承重**:会话什么都不回时 `errorText` 那道是瞎的,
        而页面照样停在上一篇上 —— 老的假会话、换一种传输都是这个形态。"""
        kept, note, _ = two_articles(
            stuck=['', 'silent'],
            snaps=[snapshot(blocks=['第一篇的正文,只属于 /status/100。'])])
        self.assertEqual(['https://x.com/Review/status/100'],
                         [e['url'] for e in kept], '第二条必须被扣留')
        self.assertIn('没取到', note)

    def test_a_failed_navigation_says_so_without_reading_the_page(self):
        """**`errorText` 那道早一道的独立射程**:导航明确失败时一次 eval 都不发,
        既不去读页面、也不等满那一页的渲染上限。"""
        _FakeChrome.reset(snapshot(blocks=ART_B), stuck='abort')
        sess = _FakeChrome()
        before = len(_FakeChrome.visited)
        with patch.object(x_article, 'PAGE_WAIT', 30.0):
            with self.assertRaises(x_article.ArticleUnavailable) as got:
                x_article.browser_fetch(sess, 'https://x.com/Review/status/200')
        self.assertIn('没打开', str(got.exception))
        self.assertIn('ERR_ABORTED', str(got.exception))
        self.assertEqual(before + 1, len(_FakeChrome.visited))

    def test_being_bounced_to_someone_elses_tweet_is_not_an_answer(self):
        """停在**别的一篇**的推文页上不算「这条没有长文」。按 `back` 一个字段
        判的话,别人那篇的结论会被记成这一条的 —— 空正文落库之后改不回来。"""
        _FakeChrome.reset(snapshot(at=False, back=True,
                                   path='/Review/status/100'))
        with patch.object(x_article, 'PAGE_WAIT', 0.0):
            with self.assertRaises(x_article.ArticleUnavailable) as got:
                x_article.browser_fetch(_FakeChrome(),
                                        'https://x.com/Review/status/200')
        self.assertIn('/status/100', str(got.exception))

    def test_two_good_navigations_still_fill_both(self):
        """**反过来钉一边**:导航都正常时两条照旧各拿自己那一篇的正文。"""
        kept, note, visited = two_articles(
            snaps=[snapshot(blocks=['第一篇。']), snapshot(blocks=['第二篇。'])])
        self.assertEqual('', note)
        self.assertEqual(['第一篇。', '第二篇。'], [e['body'] for e in kept])
        self.assertEqual(['https://x.com/Review/article/100',
                          'https://x.com/Review/article/200'], visited)


class HeldBackStaysRecoverableTests(ChannelBase):
    """**扣留换来的那个第二次机会,真走一遍库。**

    报告那份证据的完整形状是:第二条被写进第一篇的正文、真 `put_items` 之后
    库里存错,**拿正确正文重新入库返回新增 0 条** —— `ON CONFLICT(key) DO
    NOTHING`。所以在 `fill` 这一步拦住不是"提示更准",是这一条推文唯一的出路。
    """

    def test_a_held_back_tweet_can_still_be_filled_next_round(self):
        s = self.sub('Review')
        kept, note, _ = two_articles(
            stuck=['', 'abort'],
            snaps=[snapshot(blocks=['第一篇的正文,只属于 /status/100。'])])
        reg.put_items(s, kept)
        urls = [r['url'] for r in reg.items('social', s['id'])['items']]
        self.assertNotIn('https://x.com/Review/status/200', urls,
                         '扣留的那条不许带着别人的正文落库')
        self.assertIn('没取到', note)

        # 下一轮补到了:这条 key 库里还没有,所以它真的进得去。
        added = reg.put_items(s, [{**entry(url='https://x.com/Review/status/200'),
                                   'body': '第二篇自己的正文。'}])
        self.assertEqual(1, added, '扣留之后下一轮必须补得回来')
        row = [r for r in reg.items('social', s['id'])['items']
               if r['url'].endswith('/status/200')][0]
        self.assertIn('第二篇自己的正文', row['body'])


if __name__ == '__main__':
    unittest.main()
