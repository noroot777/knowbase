# -*- coding: utf-8 -*-
"""日报交卷之后,这一批还有一轮在跑(没等的那几路):批次由那一轮收尾时关。

入库处理一走完日报就交卷(`kb.daily_sync._sync_sources`),而门打开时没跑完的那几路还在
后台跑完。日报那边在这时关批次的话,账本当场清空,那一轮收尾时找不到自己的批次,按一次
普通同步落盘 —— 日报的结论(哪怕是「生成失败」)、「日报」这个身份和类型计划一起丢了。
硬约束在 `docs/decisions/sync-schedule.md`;摆盘共用 `tests/pipeline_round_fixture.py`
(走真的 `_end_run`)。
"""
from __future__ import annotations

import unittest

from kb import pipeline
from kb import pipeline_batch as pb
from kb import pipeline_flow as flow
from kb import pipeline_state as ps
from tests.pipeline_round_fixture import RoundStore, finish_round

ROUND = ['crawl', 'export', 'index', 'extract']
VERDICT = '日报生成失败: Claude 503'


class LateCloseTest(RoundStore):
    def setUp(self):
        super().setUp()
        self.batch = pb.open_batch('daily')
        self.addCleanup(pb.close_batch)
        flow._plans.pop(self.batch, None)
        flow._plans[self.batch] = flow._new(self.batch, 'daily', flow.group_nodes(ROUND))
        self.addCleanup(flow._plans.pop, self.batch, None)

    def test_the_round_still_running_publishes_the_verdict_and_closes_the_batch(self):
        # 那一轮还在跑(没等的那几路),日报这边已经交卷:记结论、关批次
        ps._state.update(running=True, batch=self.batch, run_id='run-1')
        self.assertTrue(pb.batch_note(VERDICT, self.batch))
        self.assertEqual('', pb.close_batch())
        self.assertTrue(pb.batch_live(self.batch), '还有一轮在跑,批次不许当场关')
        # 那一轮收尾
        finish_round(ROUND, {'channel': 'rss'}, self.batch, run_id='run-1')
        self.assertFalse(pb.batch_live(self.batch), '最后一个写入者收尾时要把它关掉')
        last = pipeline.last_result()
        self.assertEqual('daily', last['run_kind'])
        self.assertTrue(any(VERDICT in line for line in last['log']), '日报的结论丢了')
        self.assertTrue(last.get('flow'), '类型计划没随这一批落盘')
        # 重启之后从磁盘读到的也是这一份
        self.fresh_process()
        saved = ps.load_last_run()
        self.assertEqual('daily', saved['run_kind'])
        self.assertTrue(any(VERDICT in line for line in saved['log']))

    def test_without_a_running_round_the_batch_closes_at_once(self):
        """反过来钉一边:这一批没有在跑的那一轮时,关就是当场关。"""
        finish_round(ROUND, {'channel': 'rss'}, self.batch, run_id='run-1')
        self.assertTrue(pb.batch_note(VERDICT, self.batch))
        pb.close_batch()
        self.assertFalse(pb.batch_live(self.batch))
        last = pipeline.last_result()
        self.assertEqual('daily', last['run_kind'])
        self.assertTrue(any(VERDICT in line for line in last['log']))

    def test_a_round_of_another_batch_does_not_hold_this_one_open(self):
        """在跑的那一轮不是这一批的:不替别人等,当场关。"""
        ps._state.update(running=True, batch='batch-other', run_id='run-9')
        pb.close_batch()
        self.assertFalse(pb.batch_live(self.batch))


if __name__ == '__main__':
    unittest.main()
