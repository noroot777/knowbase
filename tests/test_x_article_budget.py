# -*- coding: utf-8 -*-
"""长文补全这一步有没有整轮的时间预算和停止开关。

**它跑在账号的时间线已经读完之后**,所以外面那两道闸都够不着它:`_harvest_one`
那个 `ACCOUNT_SECONDS` 只约束时间线本身,整轮那道「进下一个账号之前检查预算和
停止」只在**账号之间**发生。一个发了几十篇长文的账号会在这两道闸中间串行启动
几十次新请求,而 `_GIVE_UP_AFTER` 只数**连续失败** —— 每一次成功都把它清零,
所以它限不住「大量慢但成功」那一种。

2026-10-09 第 1 轮审查实测:`budget=0.020` 秒、停止请求在第一个响应完成时就置位,
这一步照样新发 7 次请求、总耗时 0.323 秒,8 条全部留下、`incomplete` 是空的、
水位照常前进到本轮最新。阴性对照把两个开关关掉:0 次请求、0.00005 秒。

**每条都先断言后果**(还发了几次请求、那几条扣留了没有、水位动没动),只验
「有没有把 deadline 传下去」的话,一个传了而没人认它的实现也能全绿。

**按形状单独一个文件**:这一组的摆盘是「一个账号带 8 篇长文 + 一个极小预算」,
和后路那几组(假 DOM 快照)、接口那几组形状都不同;摆盘仍共用那一份。

跑:  python -m unittest tests.test_x_article_budget -v
"""
import contextlib
import time
import unittest
from unittest.mock import patch

from kb.feeds import x_article, x_browser, x_feedgrab
from tests.test_x_article import (OLD_WATER, _FakeChrome, answering,
                                  article_payload, entry, snapshot)

MANY = 8        # 一个账号发了 8 篇长文:报告那一局的条数


def long_round(*, budget=0.0, spend=0.0, stop_after=None, fallback=False):
    """真跑一轮 `x_browser.harvest`:一个账号 + `MANY` 篇长文(正文都空)。

    长文接口**每次都成功**(这正是 `_GIVE_UP_AFTER` 限不住的那一种),所以
    「还发了几次请求」就是这一步有没有预算的全部依据。`stop_after` = 第几次
    接口调用之后置位停止(报告那一局是 1)。

    返回 `(那个账号的结果, 接口调用记录)`。`fill` 一行不打桩。
    """
    urls = [f'https://x.com/example/status/{i}' for i in range(MANY)]

    def fake_one(page, handle, floor):
        # 读时间线本身花掉的时间。**预算必须在这之后才过期**:在它之前过期的话
        # 整个账号会被外面那道闸(进下一个账号之前的检查)落成 `skipped`,而那
        # 正是这一组要绕开的 —— 要验的是「时间线读完了、预算也用完了」那一档。
        time.sleep(spend)
        return [entry(url=u) for u in urls], ''

    _FakeChrome.reset(snapshot(blocks=['后路的正文。']))
    req, calls = answering(*[article_payload() for _ in range(MANY * 2)])
    flag = {'on': False}

    def should_stop():
        return flag['on']

    real = x_article.fetch

    def counting_fetch(url, *a, **kw):
        got = real(url, *a, **kw)
        if stop_after is not None and len(calls.urls) >= stop_after:
            flag['on'] = True
        return got

    stack = [req, patch.object(x_browser, '_harvest_one', fake_one),
             patch.object(x_browser, 'Chrome', _FakeChrome),
             patch.object(x_article, 'PAGE_WAIT', 0.0),
             patch.object(x_article, 'fetch', counting_fetch),
             patch.object(x_article, 'BROWSER_FALLBACK', fallback)]
    with contextlib.ExitStack() as es:
        for cm in stack:
            es.enter_context(cm)
        got = x_browser.harvest([('example', OLD_WATER)], profile='/tmp/p',
                                budget=budget, should_stop=should_stop)
    return got['example'], calls


class BrowserBudgetTests(unittest.TestCase):
    """默认那条浏览器通道(`x_channels.prefetch → x_browser.harvest`)。"""

    def test_an_exhausted_budget_stops_asking(self):
        """**后果**:预算用完之后不再为长文发新请求,没取到的那几条扣留下来、
        水位一步不动、面板上说得出为什么。"""
        got, calls = long_round(budget=0.05, spend=0.08)
        self.assertEqual([], calls.urls,
                         f'预算已经用完,却发了 {len(calls.urls)} 次')
        self.assertIn('没取到', got['incomplete'])
        self.assertIn('预算', got['incomplete'])
        self.assertEqual(OLD_WATER, got['watermark'], '没读完,水位一步不许动')
        self.assertEqual([], got['entries'], '一条都没补到,全部扣留')

    def test_a_stop_request_lands_mid_batch(self):
        """**后果**:第一条答完就按下停止 —— 后面那几条一次请求都不许再发。"""
        got, calls = long_round(stop_after=1)
        self.assertEqual(1, len(calls.urls),
                         f'停止已经置位,却发了 {len(calls.urls)} 次')
        self.assertIn('停止', got['incomplete'])
        self.assertEqual(OLD_WATER, got['watermark'])
        self.assertEqual(1, len(got['entries']), '答到的那一条照旧入库')

    def test_a_roomy_budget_still_fills_every_one(self):
        """**反过来钉一边**:预算充足时 8 条照旧全部补到,提示是空的、水位前进。"""
        got, calls = long_round(budget=600.0)
        self.assertEqual(MANY, len(calls.urls))
        self.assertEqual('', got['incomplete'])
        self.assertEqual(MANY, len(got['entries']))
        self.assertNotEqual(OLD_WATER, got['watermark'], '读全了就该推进')

    def test_the_fallback_is_asked_before_opening_the_page(self):
        """**接口切后路之前那道的独立射程**:预算是在**接口那次往返里**用完的。

        循环顶上那道在发请求**之前**问,那一刻预算还在、它放行了;而一次接口
        往返本身就能把预算耗光(默认上限 20 秒)。所以随后那次开页面只有
        `_one` 里那道拦得住 —— 这一条照旧扣留,**而且一次 goto 都不发**
        (开一张标签页、等一次渲染,比发一次 HTTP 还贵)。

        **把 deadline 直接给成过期的测不出这一道**:那样第一条在循环顶上就被
        拦住了,压根走不到接口,更走不到这道闸(自查发现,注入它时 0 红)。
        """
        _FakeChrome.reset(snapshot(blocks=['后路的正文。']))
        sess = _FakeChrome()

        def slow_and_broken(url, headers, deadline=None):
            time.sleep(0.05)           # 这一次往返把预算耗掉
            raise x_article.FeedError('down')

        with patch.object(x_article, 'request', slow_and_broken), \
                patch.object(x_article, 'PAGE_WAIT', 0.0):
            kept, note = x_article.fill(
                [entry(url='https://x.com/a/status/1')],
                session=sess, deadline=time.monotonic() + 0.02)
        self.assertEqual([], kept, '扣留下来')
        self.assertIn('没取到', note)
        self.assertEqual([], _FakeChrome.visited, '一次 goto 都不许发')


class FillBudgetTests(unittest.TestCase):
    """直测 `fill` 自己那两道。"""

    def test_a_past_deadline_asks_nothing_at_all(self):
        req, calls = answering(*[article_payload() for _ in range(4)])
        with req:
            kept, note = x_article.fill(
                [entry(url=f'https://x.com/a/status/{i}') for i in range(4)],
                deadline=0.0)
        self.assertEqual([], calls.urls, '预算早过了,一次都不许发')
        self.assertEqual([], kept)
        self.assertIn('预算', note)

    def test_a_stop_flag_asks_nothing_at_all(self):
        req, calls = answering(*[article_payload() for _ in range(4)])
        with req:
            kept, note = x_article.fill(
                [entry(url=f'https://x.com/a/status/{i}') for i in range(4)],
                should_stop=lambda: True)
        self.assertEqual([], calls.urls)
        self.assertIn('停止', note)

    def test_no_budget_at_all_behaves_exactly_as_before(self):
        """**反过来钉一边**:两个都不给时行为和这一档出现之前逐字相同。"""
        req, calls = answering(*[article_payload() for _ in range(4)])
        with req:
            kept, note = x_article.fill(
                [entry(url=f'https://x.com/a/status/{i}') for i in range(4)])
        self.assertEqual(4, len(calls.urls))
        self.assertEqual(4, len(kept))
        self.assertEqual('', note)


class FeedgrabBudgetTests(unittest.TestCase):
    """**另一条通道的独立射程。** 两条通道各挂一次,漏哪一条就是那条通道上的
    长文把整轮拖垮 —— 口径同 `docs/decisions/x-article.md` 里「两条通道各挂一次」那条。"""

    def test_feedgrab_passes_the_budget_down_too(self):
        import inspect
        call = inspect.getsource(x_feedgrab).split('x_article.fill')[1]
        call = call[:call.index(')')]
        self.assertIn('deadline=', call)
        self.assertIn('should_stop=', call)


if __name__ == '__main__':
    unittest.main()
