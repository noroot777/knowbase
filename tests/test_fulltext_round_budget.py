# -*- coding: utf-8 -*-
"""正文补抓一轮抓多少、失败明细能不能直接去浏览器补(kb/feeds/fulltext.py + exclusion.py)。

①**一轮只由墙钟预算封顶,不设条数上限。** 原来还有一道「一轮最多 20 条」:生产库
  里 HN Buzzing 一次同步进来 42 条壳,20 条半分钟就抓完、预算还剩九成,余下 22 条
  一次都没试过就挂成「待补抓原文正文」,等下一次同步 —— 面板上只报了一条真失败,
  人看着一屏「今天的都没抓到」。判据落在**库里那几行有没有补上正文**,不是计数。
  反过来钉一边:预算用完照旧停手,没轮到的留给下一轮、不算失败;显式 `limit`
  照旧只取那几条。
②**补抓失败的明细带着条目身份(`capture`),翻译失败的不带。** 面板上那颗「从浏览器
  采集」按它打开采集抽屉;翻译失败缺的不是正文,带了就是指着一条正文齐全的内容让人
  去补正文。

脚手架复用 `tests/test_feed_fulltext.py` 的 `FullTextCase`(传输层打桩,其余真跑)。
"""
import hashlib
import unittest
from unittest.mock import Mock, patch

from kb import llm, pipeline_state
from kb.feeds import fulltext, registry as reg, translate
from kb.feeds.fetching import FeedError
from tests.test_feed_fulltext import PAGE, SHELL, FullTextCase, page

# 比原来那道条数上限多出一截:上限还在的话这一轮必然留下 10 条。
MANY = 30


def key_of(url: str) -> str:
    return hashlib.sha256(b'rss:' + url.encode()).hexdigest()[:32]


class RoundBudgetTests(FullTextCase):

    def _fetch(self, result=None, side_effect=None, **kw):
        req = Mock(side_effect=side_effect) if side_effect else Mock(return_value=result)
        with patch.object(fulltext, 'request', req), \
                patch.dict(pipeline_state._state, stop_requested=False):
            return fulltext.fetch_pending('rss', **kw), req

    def _shells(self, n):
        return [self.put(url=f'https://e.org/p{i}', title=f'Post {i}') for i in range(n)]

    def test_one_round_fills_every_queued_shell(self):
        keys = self._shells(MANY)
        summary, req = self._fetch(result=page(PAGE))
        self.assertEqual(MANY, req.call_count)
        for key in keys:
            with self.subTest(key=key):
                row = self.row(key)
                self.assertNotEqual('', row['fulltext_at'])
                self.assertNotEqual(SHELL, row['body'])
        self.assertEqual(0, fulltext.pending('rss'))
        self.assertEqual((MANY, 0), (summary['done'], summary['left']))

    def test_the_wall_clock_budget_still_stops_the_round(self):
        """反过来钉一边:预算用完就停手,一条都不联网,也不算失败、不扣额度。"""
        keys = self._shells(3)
        with patch.object(fulltext, 'ROUND_SECONDS', 0):
            summary, req = self._fetch(result=page(PAGE))
        self.assertEqual(0, req.call_count)
        self.assertEqual((0, 0, 3), (summary['done'], summary['failed'], summary['left']))
        for key in keys:
            row = self.row(key)
            self.assertEqual(('', 0, SHELL), (row['fulltext_at'], row['fulltext_tries'], row['body']))
        self.assertEqual(3, fulltext.pending('rss'))

    def test_an_explicit_limit_is_still_honoured(self):
        self._shells(5)
        summary, req = self._fetch(result=page(PAGE), limit=2)
        self.assertEqual(2, req.call_count)
        self.assertEqual((2, 3), (summary['done'], summary['left']))
        self.assertEqual(3, fulltext.pending('rss'))

    def test_the_old_item_count_knob_is_gone(self):
        """旧环境里留着的 `KB_FULLTEXT_MAX_ITEMS` 不许再把一轮卡在 20 条。"""
        self.assertFalse(hasattr(fulltext, 'MAX_ITEMS'))
        import inspect
        self.assertNotIn('KB_FULLTEXT_MAX_ITEMS', inspect.getsource(fulltext))


class CaptureIdentityTests(FullTextCase):

    def _fetch(self, **kw):
        req = Mock(**kw)
        with patch.object(fulltext, 'request', req), \
                patch.dict(pipeline_state._state, stop_requested=False):
            return fulltext.fetch_pending('rss')

    def test_a_request_failure_carries_the_item_key(self):
        key = self.put()
        summary = self._fetch(side_effect=FeedError('这一页要求先完成验证或登录'))
        self.assertEqual([key], [f.get('capture') for f in summary['errors']])
        # 「不再抓取」那份身份照旧在,两颗按钮各认各的。
        self.assertEqual(key, summary['errors'][0]['drop']['id'])

    def test_an_unreadable_page_carries_it_too(self):
        key = self.put()
        summary = self._fetch(return_value=page('<html><body><p>hi</p></body></html>'))
        self.assertEqual(1, summary['empty'])
        self.assertEqual([key], [f.get('capture') for f in summary['errors']])

    def test_a_translation_failure_does_not(self):
        """反过来钉一边:翻译失败缺的不是正文,不许长出「从浏览器采集」。"""
        reg.put_items(self.plain, [{'url': 'https://solo.example.org/a', 'title': 'Plain English',
                                    'body': 'The runtime schedules tools for coding agents. ' * 4,
                                    'published': '2026-10-08 09:00', 'content_kind': 'summary'}])
        with patch.object(llm, 'chat', Mock(side_effect=RuntimeError('上游 500'))):
            summary = translate.translate_pending('rss')
        self.assertEqual(1, summary['failed'])
        self.assertEqual([None], [f.get('capture') for f in summary['errors']])
        self.assertTrue(summary['errors'][0].get('drop'))


if __name__ == '__main__':
    unittest.main()
