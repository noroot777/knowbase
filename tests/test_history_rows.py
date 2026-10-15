# -*- coding: utf-8 -*-
"""一条业务结果占几行历史:谁有资格顶掉谁。

从 `test_batch_results.py` 拆出来 —— 那份加上这一组会超过单文件 700 行上限。发布
与保全(权威位、降级文案、发布资格)仍在那一份里;摆盘共用
`tests/pipeline_round_fixture.py`。

这一组钉的全是同一个判据:`pipeline_state._save_history` 删掉哪几行。它只认「这一份
记录实际合并了哪几轮」(`pipeline_batch._aggregate_covers`),**不许据「内存权威位上
有没有东西」反推** —— 权威位上也可能躺着一轮普通运行自己的快照(落盘失败那一档),
它一轮都没合并。两边都要钉:该顶的顶掉(不顶的话同一个 run id 下存着两种含义的
结果),不该顶的一行都不许少(那是那几轮唯一的副本,删掉不可恢复)。
"""
import json
import unittest
from unittest.mock import Mock, patch

from kb import pipeline_batch as pb
from kb import pipeline_state as ps
from tests.pipeline_round_fixture import RoundStore, finish_round


class HistoryRowTests(RoundStore):

    def test_both_recoverable_copies_carry_the_same_business_result(self):
        """两份可恢复副本必须是**同一个业务结果**的两个投影。

        状态文件存整批、历史存末个子轮,而两者**共用同一个 run id** —— 于是同一个
        身份下躺着两种含义的结果:一份抓取 error、一份抓取「未选择」。后果有两层:
        正常保存时面板说"失败"、点开同身份那条历史却写着"已完成";汇总那次写入
        失败再重启,恢复按完成序号挑中历史那条,日报里那条 RSS 抓取失败当场变成
        「同步已完成」、连「重试失败项」都没有(那一档钉在 `test_batch_results.py`
        的 `test_a_transient_write_failure_cannot_demote_the_batch` 的重启那一段)。
        所以这里钉的是**正常保存**这条路:两份的计划、阶段结论必须一致,而且
        **一轮日报只占一条历史** —— 整批那一份顶掉这一批前几轮各自留下的那条
        (顶掉不丢内容:整批是子轮的并集)。
        """
        from kb import pipeline
        batch = pb.open_batch()
        self.addCleanup(pb.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {
                         'status': 'error', 'detail': '失败 1 个',
                         'failures': [{'title': '某订阅', 'reason': '连不上'}]}})
        self.assertEqual(['run-1'], [r['run_id'] for r in pipeline.load_history()])
        finish_round(['index', 'extract'], None, batch, run_id='run-2',
                     started='2026-10-14 09:30:00')
        disk = json.loads(ps._STATE_FILE.read_text(encoding='utf-8'))
        rows = pipeline.load_history()
        self.assertEqual(['run-2'], [r['run_id'] for r in rows], '一轮日报占了两条历史')
        for name, got in (('盘上那份', disk), ('历史那条', rows[0])):
            with self.subTest(name):
                self.assertEqual(['crawl', 'export', 'index', 'extract'],
                                 got['planned_stages'])
                self.assertEqual('error', got['stages']['crawl']['status'])
                self.assertEqual(['某订阅'],
                                 [f['title'] for f in got['stages']['crawl']['failures']])

    def test_an_unsaved_aggregate_still_supersedes_the_rounds_it_merged(self):
        """**真的汇总即使状态写失败,历史存的也还是完整汇总、顶掉的也还是那几轮。**

        这一档和下面两档的区别全在"这份记录包含了谁":这里权威位上躺的是整批那份
        并集,它确实把 `run-1` 合并进去了,所以 `run-1` 那行该顶 —— 退回去存局部
        结果就是把这一批的失败连同重试名单一起抹掉(重启形态钉在
        `test_batch_results.py` 的 `test_a_transient_write_failure_cannot_demote_the_batch`)。
        """
        from kb import pipeline
        batch = pb.open_batch()
        self.addCleanup(pb.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {
                         'status': 'error', 'detail': '失败 1 个',
                         'failures': [{'title': '某订阅', 'reason': '连不上'}]}})
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            finish_round(['index', 'extract'], None, batch, run_id='run-2',
                         started='2026-10-14 09:30:00')
        rows = pipeline.load_history()
        self.assertEqual(['run-2'], [r['run_id'] for r in rows], '整批那条没顶掉子轮那条')
        self.assertEqual(['crawl', 'export', 'index', 'extract'], rows[0]['planned_stages'])
        self.assertEqual('error', rows[0]['stages']['crawl']['status'])
        self.assertEqual(['某订阅'],
                         [f['title'] for f in rows[0]['stages']['crawl']['failures']],
                         '历史里那条退回成了局部结果')

    def test_a_refused_batch_leaves_the_earlier_rounds_their_own_history_row(self):
        """反过来钉一边:**没资格发布**那一档写的是子轮自己那份,前几轮那几条不许顶。

        顶掉的前提是"整批那一份已经存进去了、它是子轮的并集"。被更新的一轮接替时
        盘上和历史里存的都只是当前子轮,这时把前几轮那条删掉就是**真的丢数据** ——
        那几条是它们唯一的副本。
        """
        from kb import pipeline
        batch = pb.open_batch()
        self.addCleanup(pb.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        # 期间用户自己点了一轮同步(不带批次身份),它成了最新那一轮
        finish_round(['index'], None, '', run_id='run-manual',
                     started='2026-10-14 10:00:00')
        finish_round(['index', 'extract'], None, batch, run_id='run-2',
                     started='2026-10-14 10:10:00')
        self.assertIn('run-manual', pb.close_batch())
        rows = pipeline.load_history()
        self.assertEqual(['run-2', 'run-manual', 'run-1'], [r['run_id'] for r in rows])
        self.assertEqual('error', rows[-1]['stages']['crawl']['status'],
                         '第一轮那条抓取失败被顶掉了')

    def test_an_interleaved_run_that_cannot_be_saved_keeps_the_batch_rows(self):
        """**权威位上有结果 ≠ 这份结果覆盖了整批。**

        期间那一轮独立同步落盘失败时,权威位上躺的是它自己那份快照(`_save_own` 的
        降级路径要让面板看得出这一轮是降级完成的),而它一轮都没合并。按"有缓存就是
        整批"去删,这一批前几轮各自唯一那条历史当场消失。

        实测形态:先一轮 RSS 抓取失败留下它那条,用户自己点的那轮同步赶在这一批
        中间收尾、只给它的状态写入注入一个 `OSError` —— 面板上那条抓取失败的历史
        没了,而两边都没出声(状态文件里那份是上一次写成功的,于是看着一切正常)。
        """
        from kb import pipeline
        batch = pb.open_batch()
        self.addCleanup(pb.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            finish_round(['index'], None, '', run_id='run-manual',
                         started='2026-10-14 10:00:00')
        rows = pipeline.load_history()
        self.assertEqual(['run-manual', 'run-1'], [r['run_id'] for r in rows])
        self.assertEqual('error', rows[-1]['stages']['crawl']['status'],
                         '第一轮那条抓取失败被顶掉了')
        # 那一轮自己写不上盘是真的,如实留在它那条记录的日志里(`_end_run` 那句
        # 「没能留档」在 `_save_history` 之后才写,所以记录里认的是降级那一句)
        self.assertTrue(any('没能写进磁盘' in line for line in rows[0]['log']),
                        rows[0]['log'])

    def test_a_refused_round_that_cannot_be_saved_keeps_the_batch_rows(self):
        """同一类的第二条触发路径:**发布被拒**之后那份局部结果也走 `_save_own`,
        也会在落盘失败时占住权威位。资格判定和写盘各失败一次,合起来不许凑出
        "整批已经存进去了" —— 这一批前几轮那几条仍是它们唯一的副本。
        """
        from kb import pipeline
        batch = pb.open_batch()
        self.addCleanup(pb.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        finish_round(['index'], None, '', run_id='run-manual',
                     started='2026-10-14 10:00:00')
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            finish_round(['index', 'extract'], None, batch, run_id='run-2',
                         started='2026-10-14 10:10:00')
        self.assertIn('run-manual', pb.close_batch())
        rows = pipeline.load_history()
        self.assertEqual(['run-2', 'run-manual', 'run-1'], [r['run_id'] for r in rows])
        self.assertEqual('error', rows[-1]['stages']['crawl']['status'],
                         '第一轮那条抓取失败被顶掉了')


if __name__ == '__main__':
    unittest.main()
