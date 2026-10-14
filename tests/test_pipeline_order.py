# -*- coding: utf-8 -*-
"""「哪一轮更新」只认两样凭据:同一份历史里的位置、同一代发出来的完成序号。

两份落盘是分别写、分别可失败的,所以磁盘上它们记着的可能是不同轮次 —— 这一族钉的
就是**那时候怎么判**,以及**判不出来时说没说**。和 `test_pipeline_restore.py` 分开
是因为判据不同:那边问「恢复这件事做对了没有」(内存读回来了没、活进程被盖没被盖、
读失败之后还能不能重试),这边问「凭据够不够」。摆盘共用 `HistoryCase`。

**墙上时间不在凭据之内**,所以这一族里有两条时钟回拨:一条钉「没凭据时不许拿时间
凑」,一条反过来钉「有号的时候时间往回跳不许动摇它」。
"""

import json
from unittest import main
from unittest.mock import patch

from kb import pipeline_retry, pipeline_state as state
from tests.test_pipeline_history import HistoryCase


class OrderTests(HistoryCase):
    """两份落盘各记着不同一轮时,谁更新 —— 以及证明不了时拦没拦住重试。"""

    def test_two_rounds_in_the_same_second_are_told_apart_by_the_sequence(self):
        """**两份独立可失败的副本加墙上时钟构不成总序。**

        A 轮的历史没写上、紧接着 B 轮的状态没写上:磁盘状态记着 A、历史只有 B,
        而 A **不在历史里**(位置证据缺失)、两轮又落在同一秒(自然时钟实测两个
        收尾时间都是 16:08:29)。原来这一档默认旧那一份完整:页头显示 A 的 503,
        点「重试失败项」导出阶段收到的是 A 那个来源。
        """
        with patch.object(state, '_now', return_value='2026-10-14 16:08:29'):
            with self.only_history_write_fails():
                self.run_round('run-a', 'error',
                               '3 条翻译失败（服务返回 503），库里仍是原文')
            with self.only_state_write_fails():
                self.run_round('run-b', 'ok', '已保存 95 条到知识库')
        self.assertEqual('run-a', (state.load_last_run() or {}).get('run_id'),
                         '这一局的前提:状态文件记着 A')
        self.assertEqual(['run-b'], [r['run_id'] for r in state.load_history()],
                         '这一局的前提:历史里只有 B,A 不在里面')

        self.restart()
        self.assertEqual('run-b', state.get_state()['run_id'],
                         '完成序号要分出 B 更新 —— 时间和位置都分不出来')
        self.assertEqual('scope', state.restore_gap(),
                         '挑中的是历史那一份,它没有重试范围')

    def test_an_invalid_first_row_does_not_hide_the_newer_rounds(self):
        """历史首条是个空对象时,不许把后面那些仍能证明更新的记录整段遮住。

        坏文件、半截写入都会落出这种形状。原来只过滤非字典,于是拿那个空对象当
        「最新」比一轮 —— 恢复退回状态文件那一份,而它可能是旧的。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        with self.only_state_write_fails():
            self.run_round('run-b', 'ok', '已保存 95 条到知识库')
        path = state._history_file()
        raw = json.loads(path.read_text())
        raw['runs'].insert(0, {})
        path.write_text(json.dumps(raw, ensure_ascii=False))

        self.restart()
        self.assertEqual('run-b', state.get_state()['run_id'],
                         '那个空对象不许遮住后面仍能证明更新的 B')
        self.assertEqual('scope', state.restore_gap())

    def test_an_unprovable_order_is_reported_instead_of_trusting_the_old_copy(self):
        """两份都没有完成序号(升级前的记录)、收尾时间又相同 —— 先后**证明不了**。

        展示最后已知的那一份(它完整、带日志),但要如实说出来并拦住重试,
        **不许默认它可信**。
        """
        at = '2026-10-14 16:08:29'
        state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state._STATE_FILE.write_text(json.dumps({
            'run_id': 'run-old-a', 'finished_at': at, 'started_at': at,
            'stages': {name: {'status': 'error' if name == 'export' else 'ok',
                              'detail': '3 条翻译失败（服务返回 503）'}
                       for name in state.STAGES},
            'crawl_filter': {'channel': 'rss'}, 'log': [],
        }, ensure_ascii=False))
        state._history_file().write_text(json.dumps({'runs': [{
            'run_id': 'run-old-b', 'finished_at': at, 'started_at': at,
            'stages': {name: {'status': 'ok', 'detail': '完成'} for name in state.STAGES},
            'crawl_filter': {'channel': 'rss'}, 'log': [],
        }]}, ensure_ascii=False))

        self.restart()
        self.assertEqual('run-old-a', state.get_state()['run_id'],
                         '展示最后已知的那一份')
        self.assertEqual('order', state.restore_gap(), '但要如实说先后分不出来')

        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '证明不了最新就不许起跑')
        self.assertIn('先后分不出来', msg)
        self.assertFalse(thread.called)

    def _legacy(self, rid, at, *, failed, started):
        """一份**旧格式**记录:没有世代标识,身份可有可无。

        这是被兼容的格式,不是坏文件 —— 字段、阶段、日志都齐,只是升级前的 writer
        没写身份/序号/世代。
        """
        run = {
            'finished_at': at, 'started_at': started,
            'run_kind': 'sync', 'stopped': False, 'planned_stages': list(state.STAGES),
            'stages': {name: {'status': 'error' if (failed and name == 'export') else 'ok',
                              'detail': '3 条翻译失败（服务返回 503）' if failed else '完成'}
                       for name in state.STAGES},
            'crawl_filter': {'channel': 'rss'},
            'log': [f'[{at[5:16]}] 管道结束'],
        }
        if rid:
            run['run_id'] = rid
        return run

    def _put(self, saved, runs):
        state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state._STATE_FILE.write_text(json.dumps(saved, ensure_ascii=False))
        state._history_file().write_text(json.dumps({'runs': runs}, ensure_ascii=False))

    def test_an_aliased_display_version_does_not_prove_the_same_round(self):
        """**展示版本相等不等于同一轮。**

        `result_version` 保证非空(身份没有就降级到收尾时间),**不保证唯一** ——
        两条都没有身份而收尾时间又相同的旧记录版本就会撞。拿它当「同一轮」的证明,
        恢复就直接返回状态文件那一份并报空欠账:实测两轮都是 19:30:00,状态记着 A
        的翻译失败、历史唯一那行是后来 B 的成功 —— 页头显示 A 的 503、重试可点,
        点下去导出阶段收到的是 A 那个来源。

        这条捷径排在顺序判据**前面**,所以位置和序号那两道连被问到的机会都没有。
        """
        at = '2026-10-14 19:30:00'
        a = self._legacy('', at, failed=True, started='2026-10-14 19:20:00')
        a['export_filter'] = {'accounts': ['A-failed']}
        b = self._legacy('', at, failed=False, started='2026-10-14 19:25:00')
        self._put(a, [b])
        self.assertEqual(state.result_version(a), state.result_version(b),
                         '摆局前提:两条的展示版本确实撞上了')

        self.restart()
        state.get_state()
        self.assertEqual('order', state.restore_gap(),
                         '版本撞上不算证明 —— 先后其实分不出来')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '证明不了最新就不许按它的范围去抓')
        self.assertIn('先后分不出来', msg)
        self.assertFalse(thread.called, '更不许真起线程去抓 A')

    def test_two_copies_of_one_round_are_still_recognised_without_an_id(self):
        """**反过来钉一边**:升级前那最后一轮(两份都写成功)照旧认得出是同一轮。

        它没有身份也没有序号,证明只能靠**两份副本的内容逐字相同** —— 同一轮的两份
        是从同一份 `_state` 写出去的。少了这一级就是误报:升级后第一次打开面板,
        重试被拦住说「先后分不出来」,而两份其实是同一轮。
        """
        at = '2026-10-14 19:30:00'
        one = self._legacy('', at, failed=True, started='2026-10-14 19:20:00')
        saved = dict(one, export_filter={'accounts': ['A-failed']})
        self._put(saved, [dict(one)])            # 历史那份没有重试派生字段,其余一字不差

        self.restart()
        self.assertEqual('', state.restore_gap(),
                         '内容逐字相同 —— 这就是同一轮的两份副本')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, _ = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, '同一轮、而且状态文件那份完整 —— 重试照旧放行')
        self.assertTrue(thread.called)

    def test_a_legacy_round_still_gets_its_position_proof(self):
        """**反过来钉一边**:旧格式照旧用得上位置证据。

        捷径砍掉之后那一道才有机会被问到 —— 历史里留着状态文件那一轮(内容逐字
        相同,所以认得出),它出现在首条后面就证明首条更新。原来版本一撞就直接返回
        状态文件那一份,**行次序连被看一眼的机会都没有**。
        """
        at = '2026-10-14 19:30:00'
        saved = self._legacy('', at, failed=True, started='2026-10-14 19:20:00')
        saved['export_filter'] = {'accounts': ['A-failed']}
        newest = self._legacy('', at, failed=False, started='2026-10-14 19:25:00')
        self._put(saved, [newest, dict(saved)])   # 历史倒序:新那轮在前,旧那轮还留着

        self.restart()
        self.assertEqual('2026-10-14 19:25:00', state.get_state()['started_at'],
                         '位置证据证明了首条更新 —— 展示它')
        self.assertEqual('scope', state.restore_gap(),
                         '挑中历史那一份,照旧要说「重试范围没留档」')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, _ = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '历史那份没有重试派生字段 —— 不许按上一轮的范围抓')
        self.assertFalse(thread.called)

    def test_a_legacy_id_that_repeats_across_restarts_is_not_an_event_id(self):
        """旧格式那些 `run-1` / `run-2` **重启之后会重复**,不算事件身份。

        它们只有进程内自增,没有跨进程唯一的那一截(当前格式的 `new_run_id()` 才
        有)。按身份信它就判出「同一轮」,而两条的收尾时间、阶段、日志都不一样。
        """
        a = self._legacy('run-1', '2026-10-14 09:16:00', failed=True,
                         started='2026-10-14 09:00:00')
        a['export_filter'] = {'accounts': ['A-failed']}
        b = self._legacy('run-1', '2026-10-14 21:04:00', failed=False,
                         started='2026-10-14 21:00:00')
        self._put(a, [b])

        self.restart()
        state.get_state()
        self.assertEqual('order', state.restore_gap(),
                         '旧身份撞上不算证明 —— 它跨重启会重复')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, _ = pipeline_retry.start_retry_failed()
        self.assertFalse(ok)
        self.assertFalse(thread.called)

    def test_the_position_anchor_does_not_settle_for_an_aliased_version(self):
        """位置锚点也要过「同一轮」那道,**不许拿展示版本去找**。

        两条旧记录版本撞上时,拿版本在历史后续行里找状态那一轮会**找到另一轮** ——
        「首条更新」这个结论就建在一个假锚点上。这一局里历史后续行那条和状态文件
        那条版本相同、内容不同:锚点不成立,所以先后仍然证明不了。
        """
        at = '2026-10-14 19:30:00'
        saved = self._legacy('', at, failed=True, started='2026-10-14 19:20:00')
        saved['export_filter'] = {'accounts': ['A-failed']}
        newest = self._legacy('', '2026-10-14 21:04:00', failed=False,
                              started='2026-10-14 21:00:00')
        # 后续行:版本和状态那条一样(同秒、都没身份),但**是另一轮**
        other = self._legacy('', at, failed=False, started='2026-10-14 19:00:00')
        self._put(saved, [newest, other])
        self.assertEqual(state.result_version(saved), state.result_version(other),
                         '摆局前提:后续行那条的版本和状态那条撞上了')

        self.restart()
        state.get_state()
        self.assertEqual('order', state.restore_gap(),
                         '假锚点不算位置证据 —— 先后还是证明不了')

    def test_a_sequence_that_cannot_be_read_is_not_handed_out_as_a_small_number(self):
        """发号那一步读失证时**发 0,不许从 1 重新开始**。

        0 在 `_seq_of` 那边表示「这一轮没有顺序凭据」,恢复时序号那一档直接走不通、
        退到位置和时间去证明。发一个看着合法的小整数是最坏的那一档:它和磁盘上那些
        号**不相等**,于是恢复层拿它当成「谁更晚」的证明用 —— 后果见下面那一条。
        """
        for rid in ('run-1', 'run-2', 'run-3'):
            self.run_round(rid, 'ok', '已保存 3 条到知识库')
        self.assertEqual(3, (state.load_last_run() or {}).get('run_seq'),
                         '这一局的前提:磁盘上已经发到第 3 号')

        with self.read_fails_on(lambda path: True):
            self.assertEqual(0, state._next_seq(),
                             '读不出来就是「没有凭据」,不是「一个小凭据」')

    def test_a_round_whose_sequence_failed_does_not_lose_to_an_older_one(self):
        """发号读失证的那一轮,不许**输给**磁盘上那个更大的旧序号。

        实测形态:磁盘上已经三轮、序号 1/2/3,第三轮 A 的导出 503;第四轮 B 成功,
        发号那次读失证(所以 B 的序号是 0)、而 B 的状态文件又没写上 —— 于是磁盘上
        状态记着 A/3、历史首条是 B/0、历史里还留着 A。序号那一档排在位置证据前面
        的话,它当场判出「A 更新」:页头显示 A 的 503,点「重试失败项」导出阶段收到
        的是 A 那个来源,而 B 明明已经成功。

        **位置证据不依赖发号做对** —— A 出现在历史首条后面,这就够证明首条更新了。
        """
        for rid in ('run-1', 'run-2'):
            self.run_round(rid, 'ok', '已保存 3 条到知识库')
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        self.assertEqual(3, (state.load_last_run() or {}).get('run_seq'))

        # **注入只罩住发号那一步**(里面照旧打在真实的 `Path.read_bytes` 上):罩住
        # 整轮的话 `_save_history` 的读也失败,那一份就按「存量没读出来」抛了 ——
        # 历史里压根没有 B,摆不出这一局
        real_next = state._next_seq

        def seq_without_disk():
            with self.read_fails_on(lambda path: True):
                return real_next()

        with patch.object(state, '_next_seq', seq_without_disk), \
                self.only_state_write_fails():
            self.run_round('run-b', 'ok', '已保存 95 条到知识库')
        runs = state.load_history()
        self.assertEqual(['run-b', 'run-a'], [r.get('run_id') for r in runs[:2]],
                         '摆局前提:历史首条是 B,而 A 还留在它后面')
        self.assertEqual(0, runs[0].get('run_seq'), '摆局前提:B 没拿到序号')
        self.assertEqual('run-a', (state.load_last_run() or {}).get('run_id'),
                         '摆局前提:状态文件还记着 A')

        self.restart()
        self.assertEqual('run-b', state.get_state()['run_id'],
                         '位置证据已经证明 B 更新,不许挑那个序号更大的旧轮')
        self.assertEqual('scope', state.restore_gap(),
                         '挑中的是历史那一份 —— 没有重试派生字段,要说出来')

        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '恢复不完整就不许按上一轮的范围去抓')
        self.assertFalse(thread.called)

    def test_the_history_position_outranks_a_rolled_back_sequence_on_disk(self):
        """**存量里已经落盘的回退号**:位置证据照旧要盖过它。

        老代码在发号读失证时从 1 重新开始,那些号已经写进磁盘了 —— 修了发号也追不
        回来。这一局摆的就是它:历史首条是真正最新的那一轮却只有 1 号,状态文件记着
        的上一轮有 3 号,而那一轮**还留在历史里**(位置已经证明它是过去的)。

        序号档排在位置档前面的话,它当场判出「3 号那一轮更新」——而历史自己的次序
        就在反驳它。这一条和上面那条的区别是:上面那条靠发号发 0 走不通序号档,这
        一条的两个号都是正整数、都非 0,**只有判据顺序能救**。
        """
        at_new, at_old = '2026-10-14 21:00:00', '2026-10-14 09:00:00'
        old_round = {
            'run_id': 'run-old', 'run_seq': 3, 'seq_gen': state._SEQ_GEN,
            'finished_at': at_old, 'started_at': '2026-10-14 08:50:00',
            'stages': {name: {'status': 'error' if name == 'export' else 'ok',
                              'detail': '3 条翻译失败（服务返回 503）'}
                       for name in state.STAGES},
            'export_filter': {'accounts': ['A']},
            'crawl_filter': {'channel': 'rss'}, 'log': ['旧那一轮'],
        }
        new_round = {
            'run_id': 'run-new', 'run_seq': 1, 'seq_gen': state._SEQ_GEN,
            'finished_at': at_new, 'started_at': '2026-10-14 20:50:00',
            'stages': {name: {'status': 'ok', 'detail': '完成'} for name in state.STAGES},
            'crawl_filter': {'channel': 'rss'}, 'log': ['新那一轮'],
        }
        state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state._STATE_FILE.write_text(json.dumps(old_round, ensure_ascii=False))
        # 历史倒序:新那一轮在前,旧那一轮**还留着** —— 这就是位置证据
        state._history_file().write_text(
            json.dumps({'runs': [new_round, old_round]}, ensure_ascii=False))

        self.restart()
        self.assertEqual('run-new', state.get_state()['run_id'],
                         '历史自己的次序已经证明旧那一轮是过去的')
        self.assertEqual('scope', state.restore_gap())

        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, _ = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '挑中历史那一份就不许按它派生重试范围')
        self.assertFalse(thread.called)

    def test_a_clock_that_went_backwards_cannot_make_an_old_round_latest(self):
        """**墙上时间不是顺序凭据** —— 两样真凭据都不成立时不许拿它凑。

        实测形态:A 轮导出失败(状态写上了、历史没写上)、B 轮成功(发号那次读失证
        所以没有号、历史写上了、状态没写上),这期间时钟往回拨过一次(手动改、NTP
        校正、夏令时都会)—— 两轮的收尾时间于是成了 18:30:05 和 18:30:00。磁盘上
        状态只记 A、历史只记 B,A 不在历史后续行**所以位置证据不存在**,B 又没有号。
        比收尾时间的话判据反过来指向 A,**而且报「完整」**:页头显示 A 的 503、重试
        可点,点下去导出阶段收到的是 A 那个来源,而历史里唯一那行写着 B 已完成。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        # 摆出「位置证据不存在」:把 A 那一条从历史里摘掉(真实形态是它的历史写失败了)
        state._history_file().write_text(json.dumps({'runs': []}, ensure_ascii=False))

        real_next = state._next_seq

        def seq_without_disk():
            with self.read_fails_on(lambda path: True):
                return real_next()

        with patch.object(state, '_next_seq', seq_without_disk), \
                self.only_state_write_fails():
            self.run_round('run-b', 'ok', '已保存 95 条到知识库')

        # **时钟回拨**:B 后跑完,收尾时间却更早
        saved = json.loads(state._STATE_FILE.read_text())
        runs = json.loads(state._history_file().read_text())
        saved['finished_at'] = '2026-10-14 18:30:05'
        runs['runs'][0]['finished_at'] = '2026-10-14 18:30:00'
        state._STATE_FILE.write_text(json.dumps(saved, ensure_ascii=False))
        state._history_file().write_text(json.dumps(runs, ensure_ascii=False))
        self.assertEqual('run-a', saved.get('run_id'), '摆局前提:状态文件记着 A')
        self.assertEqual(['run-b'], [r.get('run_id') for r in runs['runs']],
                         '摆局前提:历史只有 B,A 不在它后面(位置证据不存在)')
        self.assertEqual(0, runs['runs'][0].get('run_seq'), '摆局前提:B 没拿到号')

        self.restart()
        state.get_state()
        self.assertEqual('order', state.restore_gap(),
                         '两样凭据都不成立 —— 不许拿收尾时间凑一个先后出来')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '证明不了最新就不许按它的范围去抓')
        self.assertIn('先后分不出来', msg)
        self.assertFalse(thread.called)

    def test_a_trusted_sequence_outranks_a_clock_that_went_backwards(self):
        """**反过来钉一边**:同一代发出来的号在手上时,时钟回拨不许动摇它。

        号才是对的那一个 —— 它单调,而收尾时间会往回跳。所以序号那一档**不拿时间
        去反驳**:B 的号更大就是 B 更晚,哪怕它的收尾时间更早。
        """
        at_old, at_new = '2026-10-14 18:30:05', '2026-10-14 18:30:00'
        old_round = {
            'run_id': 'run-old', 'run_seq': 1, 'seq_gen': state._SEQ_GEN,
            'finished_at': at_old, 'started_at': '2026-10-14 18:20:00',
            'stages': {name: {'status': 'error' if name == 'export' else 'ok',
                              'detail': '3 条翻译失败（服务返回 503）'}
                       for name in state.STAGES},
            'export_filter': {'accounts': ['A']},
            'crawl_filter': {'channel': 'rss'}, 'log': ['旧那一轮'],
        }
        state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state._STATE_FILE.write_text(json.dumps(old_round, ensure_ascii=False))
        state._history_file().write_text(json.dumps({'runs': [{
            'run_id': 'run-new', 'run_seq': 2, 'seq_gen': state._SEQ_GEN,
            'finished_at': at_new, 'started_at': '2026-10-14 18:25:00',
            'stages': {name: {'status': 'ok', 'detail': '完成'} for name in state.STAGES},
            'crawl_filter': {'channel': 'rss'}, 'log': ['新那一轮'],
        }]}, ensure_ascii=False))

        self.restart()
        self.assertEqual('run-new', state.get_state()['run_id'],
                         '号大的那一轮就是更晚,时间往回跳不改变这件事')
        self.assertEqual('scope', state.restore_gap(),
                         '挑中的是历史那一份,照旧要说「重试范围没留档」')

    def test_a_sequence_without_a_generation_stamp_is_not_trusted(self):
        """**存量里那些号没有出处,一个都不信。**

        老代码在发号读失证时从 1 重新开始,那些回退号已经落在盘上了 —— 单看一个
        正整数分不出它是哪一代发的。这一局摆的就是存量:状态文件 7 号、历史首条
        2 号,位置证据也不成立(那一轮不在历史里)。按数值信它就判出「7 号那一轮
        更新」,而它其实是旧的。**没出处就当没有凭据** —— 报「证明不了」、拦住重试,
        跑完一轮自己就好了。
        """
        state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        # 状态文件:序号大、时间**早**
        state._STATE_FILE.write_text(json.dumps({
            'run_id': 'run-old', 'run_seq': 7,
            'finished_at': '2026-10-14 09:00:00', 'started_at': '2026-10-14 08:50:00',
            # 带失败项:否则「上次运行没有失败项」那道先拒,这一局就验不到欠账那一闸
            'stages': {name: {'status': 'error' if name == 'export' else 'ok',
                              'detail': '3 条翻译失败（服务返回 503）'}
                       for name in state.STAGES},
            'export_filter': {'accounts': ['A']},
            'crawl_filter': {'channel': 'rss'}, 'log': [],
        }, ensure_ascii=False))
        # 历史首条:序号小、时间**晚**。位置证据也不成立(那一轮不在历史里)
        state._history_file().write_text(json.dumps({'runs': [{
            'run_id': 'run-new', 'run_seq': 2,
            'finished_at': '2026-10-14 21:00:00', 'started_at': '2026-10-14 20:50:00',
            'stages': {name: {'status': 'ok', 'detail': '完成'} for name in state.STAGES},
            'crawl_filter': {'channel': 'rss'}, 'log': [],
        }]}, ensure_ascii=False))

        self.restart()
        state.get_state()                      # 恢复在第一次读状态时懒做
        self.assertEqual('order', state.restore_gap(),
                         '两样凭据互相矛盾 —— 如实说证明不了')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok)
        self.assertIn('先后分不出来', msg)
        self.assertFalse(thread.called)

    def test_a_history_whose_shape_is_wrong_is_not_treated_as_absent(self):
        """「可解析但形状不对」和「读不出来」同一档,**不许当成「这份副本没有」**。

        当成没有的话,恢复就静默认可了状态文件那一份 —— 而它可能正是旧的那一轮。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        state._history_file().write_bytes(b'{"runs": 3}')

        self.restart()
        state.get_state()                      # 同上:先触发恢复
        self.assertEqual('unknown', state.restore_gap(),
                         '形状不对就是核对不了,不是「历史里没有」')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok)
        self.assertIn('核对不了', msg)
        self.assertFalse(thread.called)


if __name__ == '__main__':
    main()
