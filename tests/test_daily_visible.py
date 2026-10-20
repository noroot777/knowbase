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
  那一档压根没有同步可看);
④那行字的身份是**调用方自己手里那个批次**,而且**记完立刻发布** —— 出报是这一批的
  最后一件事,靠"后面恰好还会跑一轮子轮"把它捎带出去的话,库整理被关掉/设置读不出/
  用户按了停止那几档根本没有后续子轮,`close_batch` 紧接着就把账本清了(审查 R6)。

跑:  uv run --offline python -m unittest tests.test_daily_visible -v
"""
from __future__ import annotations

import unittest

from unittest.mock import Mock, patch

from kb.daily_state import fresh_round
from kb import pipeline_batch as pb, pipeline_state as ps
from kb.pipeline_merge import _merge_runs
from tests.pipeline_round_fixture import finish_round, isolate_pipeline_state


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
        self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
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
        pb.batch_note('日报生成失败: Claude 503', batch)
        merged = _merge_runs([_round('r1'), _round('r2'), _round('r3')],
                             pb._batch_notes, pb._batch_kind)
        self.assertTrue(any('Claude 503' in line for line in merged['log']))

    def test_without_a_batch_it_does_nothing_and_does_not_blow_up(self):
        """反过来钉一边:手点「直接生成」那一档没有批次 —— 什么都不做,也不炸。

        抛出去的话会把 `_run` 的 `finally` 打断,后面的库整理段和 `_release_round`
        一起不跑,而「可以受理下一轮」正是在那里发布的 —— 日报从此再也起不来。
        """
        ps._state['batch'] = ''
        self.assertFalse(pb.batch_note('日报已生成:12 条素材', ''))

    def test_a_note_from_another_batch_is_not_recorded(self):
        """事件属于**别的**批次时不记:它不该被念进这一批的日志。"""
        pb.open_batch('daily')
        self.assertFalse(pb.batch_note('不属于这一批', 'batch-999'))
        self.assertEqual([], pb._batch_notes)

    def test_the_global_pipeline_moving_on_does_not_drop_our_note(self):
        """**身份是调用方手里那个批次,不是全局管道当前那一批。**

        出报期间用户自己点了一轮同步,`ps._state['batch']` 就换成了别人的 —— 照它
        判的话日报「生成失败: Claude 503」那行字被静默丢掉,同步中心看不到这次失败
        (审查 R6)。这两样答的是两个不同的问题,见 `batch_note` 的 docstring。
        """
        batch = pb.open_batch('daily')
        ps._state['batch'] = 'batch-999'          # 全局管道已经是别人的了
        self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
        self.assertTrue(any('Claude 503' in line for line in pb._batch_notes))


class BatchNotePublishTests(unittest.TestCase):
    """那行字要**立刻落到盘上**,不等下一个子轮捎带(审查 R6)。

    判据落在**磁盘上那份「最近一次运行」**里:出报是这一批的最后一件事,库整理被
    关掉、设置读不出、用户按了停止、管道被占住起不来 —— 这几档后面压根没有子轮
    会收尾,而 `close_batch` 紧接着就把账本清了。只验"内存里追加上了"的实现照样
    全绿,而重启之后那行字一个字都不在。

    **而且两份持久化副本都要收这一版**(审查 R3):「最近一次运行」只有一格,下一次
    普通同步一接替它,这条结论就只剩历史那张清单上还查得到 —— 历史那一份原本是靠
    另一个子轮在 `_end_run` 里顺手写的,而这条路后面没有子轮。
    """

    def setUp(self):
        isolate_pipeline_state(self)

    def _two_rounds(self) -> str:
        """摆一批里的两轮(抓取 + 入库),都走真的收尾。"""
        batch = pb.open_batch('daily')
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, None, 'run-1')
        finish_round(['index', 'extract'], None, batch, None, 'run-2')
        return batch

    def test_the_verdict_is_on_disk_before_the_batch_closes(self):
        batch = self._two_rounds()
        pb.batch_note('日报生成失败: Claude 503', batch)
        pb.close_batch()
        saved = ps._saved_round_strict() or {}
        self.assertTrue(any('Claude 503' in line for line in saved.get('log') or []),
                        f'盘上那份里没有出报的结论:{saved.get("log")}')
        self.assertEqual('daily', saved.get('run_kind'))

    def test_it_is_also_the_in_memory_authority(self):
        """内存那份权威同样要带上它:面板读的是它(磁盘只是重启后的后路)。"""
        batch = self._two_rounds()
        pb.batch_note('日报生成失败: Claude 503', batch)
        from kb import pipeline
        self.assertTrue(any('Claude 503' in line
                            for line in pipeline.last_result().get('log') or []))

    def test_a_failed_write_does_not_lose_the_note_or_throw(self):
        """反过来钉一边:写盘炸了**不许抛**(它在 `_run` 的 `finally` 里跑,抛出去
        就把收尾挡住了),那行字照旧在账本和内存权威里,原因另有人说。"""
        batch = self._two_rounds()
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
        self.assertTrue(any('Claude 503' in line for line in pb._batch_notes))
        self.assertIn('磁盘满', pb.close_batch())

    def test_the_verdict_is_also_in_the_history_copy(self):
        """**审查 R3**:翻历史那张清单也要看得到日报的结论。

        不写的话历史里同一个身份那条只有「管道结束」 —— 而「最近一次运行」那一格
        被下一次普通同步接替之后,这就是唯一还查得到的地方了。
        """
        batch = self._two_rounds()
        pb.batch_note('日报生成失败: Claude 503', batch)
        rows = [r for r in ps.load_history() if r.get('run_id') == 'run-2']
        self.assertEqual(1, len(rows), f'历史里这一轮不止一条或压根没有:{rows}')
        self.assertTrue(any('Claude 503' in line for line in rows[0].get('log') or []),
                        f'历史副本里没有出报的结论:{rows[0].get("log")}')

    def test_a_successful_verdict_reaches_history_too(self):
        """反过来钉一边:成了也要进历史 —— 只给失败字符串补写的实现照样漏一半。"""
        batch = self._two_rounds()
        pb.batch_note('日报已生成:12 条素材', batch)
        rows = [r for r in ps.load_history() if r.get('run_id') == 'run-2']
        self.assertTrue(any('12 条素材' in line for line in rows[0].get('log') or []),
                        f'历史副本里没有出报成功那句:{rows[0].get("log")}')

    def test_history_write_failure_is_diagnosed_not_swallowed(self):
        """反过来钉一边:历史那一份写不上**不许抛、也不许不出声**。

        它在 `_run` 的 `finally` 里跑(抛出去就挡住收尾),而默不作声的后果是
        「翻历史看不到今天的日报」没人解释得了。
        """
        batch = self._two_rounds()
        with patch.object(ps, '_save_history', Mock(side_effect=OSError('历史文件没了'))):
            self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
        self.assertIn('历史文件没了', pb.close_batch())

    def test_a_refused_publish_does_not_reshuffle_history(self):
        """**没资格发布**那一档:历史里最新那条仍是更新的那一轮,**而这一批自己那条
        要补上这次的结论**。

        期间用户自己点的那一轮已经收尾、它才是最新 —— 把这一批插到清单最前面就等于
        让一批更早的运行看着像是最后跑的那一轮(那张清单是倒序的)。可「不许当最新」
        和「不许补全自己那条」是两种权限:后者一起扣下来的话,翻那一轮的同步历史永远
        查不到日报成没成、为什么没成(审查 R4)—— 而「最近一次运行」那一格已经被
        别人接替了,日报自己那份留档下一轮又被下一份日报换掉。
        """
        batch = self._two_rounds()
        finish_round(['index'], None, '', None, 'run-别人')   # 用户自己点的那一轮
        self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
        order = [r.get('run_id') for r in ps.load_history()]
        self.assertEqual('run-别人', order[0],
                         f'历史里最新那条被这一批顶掉了:{order}')
        rows = [r for r in ps.load_history() if r.get('run_id') == 'run-2']
        self.assertEqual(1, len(rows), f'这一批自己那条历史不止一条或压根没有:{order}')
        self.assertTrue(any('Claude 503' in line for line in rows[0].get('log') or []),
                        f'这一批自己那条历史里没有出报的结论:{rows[0].get("log")}')
        self.assertIn('run-别人', pb.close_batch())

    def test_a_refused_publish_does_not_invent_a_history_row(self):
        """反过来钉一边:**这一批在历史里压根没有自己那一条时不许新增一行。**

        新增就是插队(那张清单是倒序的),而且那一行声称的是一批更早的运行刚跑完。
        这一档要说出来 —— 默不作声的话「翻历史看不到今天的日报」没人解释得了。
        """
        batch = self._two_rounds()
        finish_round(['index'], None, '', None, 'run-别人')
        with patch.object(ps, 'update_history_round', Mock(return_value=False)):
            self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
        self.assertEqual(['run-别人', 'run-2'],
                         [r.get('run_id') for r in ps.load_history()],
                         '历史里多出来一行或者少了一行')
        self.assertIn('没能补进去', pb.close_batch())

    def test_updating_a_round_that_is_not_in_history_writes_nothing(self):
        """**原位更新那一侧自己的红**:要更新的那条身份不在清单里时,返回 False
        而且**文件一个字节都没动**。

        上面那条钉的是调用方(把 `update_history_round` 整个打桩成 False,验它会
        说出来),所以「找不到就不新增」这条判据在函数自己这一侧没有红 —— 真去
        插一行的实现在那条用例里照样全绿。而插进去的那一行是**倒序清单的第一
        行**,等于让一批更早的运行看着像是刚跑完的那一轮。
        """
        self._two_rounds()
        before = ps._history_file().read_bytes()
        self.assertFalse(ps.update_history_round(dict(_round('run-没见过'))))
        self.assertEqual(before, ps._history_file().read_bytes(),
                         '没命中也动了历史文件')
        self.assertEqual(['run-2'], [r.get('run_id') for r in ps.load_history()])

    def test_an_empty_run_id_never_matches_anything(self):
        """**反过来钉一边**:空身份一律不认 —— 拿空串去匹配会把另一轮没有身份的
        运行原地改写成我们这一批(口径同 `_save_history`)。"""
        self._two_rounds()
        before = ps._history_file().read_bytes()
        self.assertFalse(ps.update_history_round(dict(_round(''), run_id='')))
        self.assertEqual(before, ps._history_file().read_bytes(),
                         '空身份也动了历史文件')

    def test_a_failed_history_update_is_diagnosed_not_swallowed(self):
        """反过来钉一边:原位更新那一下炸了**不许抛、也不许不出声**。

        它在 `_run` 的 `finally` 里跑(抛出去就挡住收尾)。
        """
        batch = self._two_rounds()
        finish_round(['index'], None, '', None, 'run-别人')
        with patch.object(ps, 'update_history_round',
                          Mock(side_effect=OSError('历史文件没了'))):
            self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
        self.assertIn('历史文件没了', pb.close_batch())

    def test_nothing_to_publish_yet_is_not_a_dropped_note(self):
        """反过来钉一边:一轮子轮都还没收尾(全被占住挡掉了)时没有可发布的汇总
        —— 这不算丢,那行字留在账本里,真有子轮收尾时跟着一起出去。"""
        batch = pb.open_batch('daily')
        self.assertTrue(pb.batch_note('日报生成失败: Claude 503', batch))
        finish_round(['crawl'], {'channel': 'rss'}, batch, None, 'run-1')
        finish_round(['index'], None, batch, None, 'run-2')
        saved = ps._saved_round_strict() or {}
        self.assertTrue(any('Claude 503' in line for line in saved.get('log') or []),
                        f'留在账本里的那行字没跟着出去:{saved.get("log")}')


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
                   lambda note, batch: (seen['notes'].append(note), True)[1]), \
             patch('kb.pipeline.close_batch', lambda: ''), \
             patch('kb.daily.generate', lambda **kw: generate()), \
             patch('kb.daily_library.library_round', lambda *a: None), \
             patch('kb.daily_run._sync_round', Mock(return_value={
                 'channel': '', 'label': 'x', 'status': 'ok', 'detail': ''})), \
             patch('kb.daily_run._channel_labels', lambda: {'rss': 'RSS'}), \
             patch('kb.daily_run._release_round', lambda *a: None):
            _state.update(**fresh_round(), sched_day='')
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
