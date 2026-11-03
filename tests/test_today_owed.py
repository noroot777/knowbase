# -*- coding: utf-8 -*-
"""今天还欠的那几步**各欠在哪个来源上**(`pipeline_retry.today_owed`)。

实页形态:早上 YouTube 抓取、下午 RSS 导出各红了一笔,晚上一轮大V观点全绿收尾。同步中心
在大V那一轮的卡片底下写「今天还有 2 个步骤没补上 · 抓取内容、导出文章」,人只会当成刚跑完
的大V欠的 —— 而大V什么都不欠。所以面板要念出来源,来源由后端从**同一次派生**里给
(和 `today_failed`、和那颗按钮真会跑的范围是同一份)。

判据落在「念出来的是哪个来源」上,并反过来钉一边:刚跑完那个全绿的来源一个字都不许出现;
真补上之后整份是空的。

跑:  uv run --offline python -m unittest tests.test_today_owed -v
"""
from __future__ import annotations

import unittest

from kb import pipeline_retry
from tests.daybook_case import DaybookCase


class TodayOwedSourcesTests(DaybookCase):
    def test_owed_steps_name_the_source_that_owes_them(self):
        self.crawl({'channel': 'rss'}, broken=[self.a], also_red=['export'])
        self.wechat_round()

        owed = pipeline_retry.today_owed()

        self.assertEqual({'crawl': ['rss'], 'export': ['rss']}, owed)
        self.assertNotIn('wechat_local', [c for where in owed.values() for c in where],
                         '刚跑完、全绿的那个来源不许被念成欠账的')
        self.assertEqual(list(owed), pipeline_retry.today_failed(),
                         '步骤清单和来源必须出自同一次派生')

    def test_nothing_is_named_once_the_retry_settles(self):
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.wechat_round()
        self.assertEqual(['rss'], pipeline_retry.today_owed().get('crawl'), '这一局的前提')

        self.retry_settled()

        self.assertEqual({}, pipeline_retry.today_owed())
        self.assertEqual([], pipeline_retry.today_failed())

    def test_library_wide_steps_owe_no_named_source(self):
        self.crawl({'channel': 'rss'}, also_red=['index'])
        self.wechat_round()

        owed = pipeline_retry.today_owed()

        self.assertIn('index', owed)
        self.assertEqual([], [c for c in owed['index'] if c],
                         '全库那一步不挂在任何具名来源上,面板只念步骤名')


if __name__ == '__main__':
    unittest.main()
