# -*- coding: utf-8 -*-
"""日报那一轮在同步中心看得见:它署自己的名,而且出报的结论留在那一轮的日志里。

2026-10-19 的形态:日报连着三轮失败(模型 503),而同步中心那张历史清单上只有几行
绿色的「同步」—— 用户看着 10:22/10:24 问「这是我点的吗」。那张清单是他排查「今天
到底发生了什么」的唯一入口,而它既分不出是谁起的、也看不到日报没出来。

判据落在**合并结果**和**那一轮的日志**上,不是"调了某个函数":

①`run_kind` 是这一批自己的身份(原来写死 `"sync"`);
②出报的结论进的是**批次账本**而不是某一轮的日志 —— 后面几轮的合并是从子轮账本
  重新拼日志的,只追加到当轮结果里的话,下一轮发布成功之后那行字就没了;
③**反过来钉一边**:没有批次时 `batch_note` 什么都不做、也不炸(手点「直接生成」
  那一档压根没有同步可看)。

跑:  uv run --offline python -m unittest tests.test_daily_visible -v
"""
from __future__ import annotations

import unittest

from kb import pipeline_batch as pb, pipeline_state as ps
from kb.pipeline_merge import _merge_runs


def _round(run_id: str, detail: str = '完成') -> dict:
    return {'run_id': run_id, 'run_seq': 1, 'seq_gen': ps._SEQ_GEN,
            'started_at': '2026-10-19 09:12:00', 'finished_at': '2026-10-19 09:40:00',
            'run_kind': 'sync', 'planned_stages': ['crawl'],
            'stages': {s: ({'status': 'ok', 'detail': detail} if s == 'crawl'
                           else {'status': 'skipped', 'detail': ''})
                       for s in ps.STAGES},
            'crawl_filter': {'channel': 'rss'}, 'log': ['09:12 开始']}


class BatchKindTests(unittest.TestCase):
    """这一批是谁起的,要写进合并结果。"""

    def test_the_batch_kind_reaches_the_merged_round(self):
        """日报那一批合出来是「日报」,不是「同步」。"""
        self.assertEqual('daily', _merge_runs([_round('r1')], None, 'daily')['run_kind'])

    def test_a_plain_batch_still_says_sync(self):
        """反过来钉一边:没署名的批次照旧是「同步」(升级前的调用方一个字不用改)。"""
        self.assertEqual('sync', _merge_runs([_round('r1')])['run_kind'])
        self.assertEqual('sync', _merge_runs([_round('r1')], None, '')['run_kind'])


class BatchKindLifecycleTests(unittest.TestCase):
    """身份只在 `open_batch` 里设、`close_batch` 里清。"""

    def setUp(self):
        self.addCleanup(pb.close_batch)

    def test_a_new_batch_does_not_inherit_the_previous_kind(self):
        """上一批是日报,下一批手点的同步**不许**跟着写成「日报」。

        不清的话就是把「分不出是谁起的」换一个方向犯一遍:历史清单上会出现一行
        用户从没起过的「日报」。
        """
        pb.open_batch('daily')
        pb.close_batch()
        pb.open_batch()
        self.assertEqual('sync', _merge_runs([_round('r1')], None,
                                             pb._batch_kind)['run_kind'])

    def test_opening_a_batch_overwrites_the_kind_even_without_a_close(self):
        """**`open_batch` 自己那一道**:上一批没关就开下一批,身份照样换过来。

        清理有两道(开和关),上面那一局**只在两道同时失守时才红** —— 单独打掉
        任何一道都被另一道兜住,于是它测不出其中任一道(定点注入实测各 0 条红)。
        这一局和下一局各打一道。

        「没关就开下一批」是真实可能的:`open_batch` 的契约是和 `close_batch`
        成对,而成对靠调用方的 `finally` —— 它不是构造上的保证。
        """
        pb.open_batch('daily')
        pb.open_batch()
        self.assertEqual('', pb._batch_kind, '新批次继承了上一批的身份')

    def test_closing_a_batch_clears_the_kind(self):
        """**`close_batch` 自己那一道**:关掉之后身份就不该留着了。"""
        pb.open_batch('daily')
        pb.close_batch()
        self.assertEqual('', pb._batch_kind, '批次关了身份还留着')


class BatchNoteTests(unittest.TestCase):
    """出报的结论怎么进那一轮的日志。"""

    def setUp(self):
        self.saved = dict(ps._state)
        self.addCleanup(lambda: ps._state.update(self.saved))
        self.addCleanup(pb.close_batch)

    def test_the_report_failure_lands_in_the_merged_log(self):
        """那行字要出现在**合并结果**的日志里 —— 面板上看的就是这一份。"""
        batch = pb.open_batch('daily')
        ps._state['batch'] = batch
        self.assertTrue(pb.batch_note('日报生成失败: Claude 503'))
        merged = _merge_runs([_round('r1')], pb._batch_notes, pb._batch_kind)
        self.assertTrue(any('Claude 503' in line for line in merged['log']),
                        '出报失败那一行没进那一轮的日志')
        self.assertEqual('daily', merged['run_kind'])

    def test_it_survives_a_later_round_rebuilding_the_log(self):
        """**后面还跑了几轮**:合并是从子轮账本重新拼日志的,那行字照旧在。

        这是「必须进批次账本」的全部理由 —— 只追加到当轮合并结果里的话,下一轮
        发布成功之后它就没了,而页面上写着原因留在运行日志里。
        """
        batch = pb.open_batch('daily')
        ps._state['batch'] = batch
        pb.batch_note('日报生成失败: Claude 503')
        merged = _merge_runs([_round('r1'), _round('r2'), _round('r3')],
                             pb._batch_notes, pb._batch_kind)
        self.assertTrue(any('Claude 503' in line for line in merged['log']))

    def test_without_a_batch_it_does_nothing_and_does_not_blow_up(self):
        """反过来钉一边:手点「直接生成」那一档没有批次 —— 什么都不做,也不炸。

        抛出去的话会把 `_run` 的 `finally` 打断,后面的库整理段和 `_release_round`
        一起不跑,而「可以受理下一轮」正是在那里发布的 —— 日报从此再也起不来。
        """
        ps._state['batch'] = ''
        self.assertFalse(pb.batch_note('日报已生成:12 条素材'))

    def test_a_note_from_another_batch_is_not_recorded(self):
        """这一轮属于**别的**批次时不记:它的结论不该被念进这一批的日志。"""
        pb.open_batch('daily')
        ps._state['batch'] = 'batch-999'
        self.assertFalse(pb.batch_note('不属于这一批'))
        self.assertEqual([], pb._batch_notes)


class OrchestrationTests(unittest.TestCase):
    """在日报那一轮的真实编排里:这一批署的是「日报」,出报的结论真被记下来。

    上面几组验的是零件自己。这一组验**接上没接上** —— 零件都对、而编排里压根
    没传 `'daily'`、或者压根没调 `batch_note`,那两档在零件那几组里一条都不红
    (定点注入实测各 0 条红,因为打桩把参数吞掉了)。所以这里的替身**记下参数**。
    """

    def _run_once(self, generate):
        from unittest.mock import Mock, patch

        from kb import daily_run
        from kb.daily_state import _state
        seen: dict = {'kind': '未调用', 'notes': []}
        with patch('kb.pipeline.open_batch',
                   lambda kind='': (seen.update(kind=kind), 'b-1')[1]), \
             patch('kb.pipeline.batch_note',
                   lambda note: (seen['notes'].append(note), True)[1]), \
             patch('kb.pipeline.close_batch', lambda: ''), \
             patch('kb.daily.generate', lambda **kw: generate()), \
             patch('kb.daily_run.library_round', lambda *a: None), \
             patch('kb.daily_run._sync_round', Mock(return_value={
                 'channel': '', 'label': 'x', 'status': 'ok', 'detail': ''})), \
             patch('kb.daily_run._channel_labels', lambda: {'rss': 'RSS'}), \
             patch('kb.daily_run._release_round', lambda *a: None):
            _state.update(stop_requested=False, over_budget=False, channels=[],
                          sched_day='', phase='idle', detail='', log=[])
            daily_run._run(None, None, True, ['rss'], 1)
        return seen

    def test_the_daily_round_signs_its_batch(self):
        """日报那一批传的是 `'daily'` —— 不传的话历史清单上又是一行「同步」。"""
        seen = self._run_once(lambda: {'date': '2026-10-19', 'since': '', 'until': '',
                                       'docs': 3, 'late': 0})
        self.assertEqual('daily', seen['kind'])

    def test_a_failed_report_leaves_its_reason_in_the_batch(self):
        """**2026-10-19 那一档**:出报失败的原因要进这一批的日志。

        不记的话同步中心那边只有几行绿色的「日报」,而日报压根没出来。
        """
        def boom():
            raise RuntimeError('Claude 调用失败: Error code: 503')
        seen = self._run_once(boom)
        self.assertEqual('daily', seen['kind'])
        self.assertTrue(any('503' in n for n in seen['notes']),
                        f'出报失败的原因没进批次日志:{seen["notes"]}')

    def test_a_successful_report_also_says_so(self):
        """反过来钉一边:成了也要记一句 —— 同步中心那边才看得出日报出来了没有。"""
        seen = self._run_once(lambda: {'date': '2026-10-19', 'since': '', 'until': '',
                                       'docs': 12, 'late': 0})
        self.assertTrue(any('日报已生成' in n for n in seen['notes']),
                        f'出报成功也该留一句:{seen["notes"]}')


if __name__ == '__main__':
    unittest.main()
