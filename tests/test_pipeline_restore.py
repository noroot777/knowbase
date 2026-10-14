"""「最新那一轮」只有一份口径,重启之后也得成立。

两件事:**可信终态**(活进程里内存就是真相,磁盘那份写失败了不该改变「最新是哪一
轮」)和**恢复**(新进程启动时把磁盘终态读回内存,两份落盘都要核对)。它们共用
`test_pipeline_history.py` 那套摆盘(`HistoryCase`),但判据完全不同 —— 那边钉的是
「落了盘没有、翻不翻得到」,这边钉的是「拿哪一份当最新、拿不准时说不说出来」。
"""
import json
from pathlib import Path
from unittest import main
from unittest.mock import patch

from kb import pipeline_retry, pipeline_state as state
from tests.test_pipeline_history import HistoryCase


class LatestResultTests(HistoryCase):
    """「最新那一轮」只有一份口径,展示和重试都从它取。

    磁盘那份是给重启做恢复的 —— **它写失败了不该改变活进程里「最新是哪轮」这个
    事实**。原来展示和重试各自直读磁盘,于是最近状态写失败那一轮静默落后一整轮。
    """

    def test_the_latest_result_comes_from_memory_not_the_stale_disk_copy(self):
        """写失败那一轮,`last_result()` 仍然认内存那一份。

        实测形态:新一轮翻译报 503、内存终态是 error,而面板显示上一轮的「已保存
        3 条 · 同步已完成」、连重试按钮都没有;方向反过来就是显示着上一轮的失败、
        而「重试失败项」去抓**上一轮那条订阅**。
        """
        self.run_round('run-old', 'ok', '已保存 3 条到知识库')
        with patch.object(state, '_write_atomic', side_effect=PermissionError('read-only')):
            self.run_round('run-new', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')

        self.assertEqual('run-old', (state.load_last_run() or {}).get('run_id'),
                         '这一局的前提:磁盘那份确实落后了')
        got = state.last_result()
        self.assertEqual('run-new', got.get('run_id'), '最新那一轮要认内存那一份')
        self.assertEqual('error', got['stages']['export']['status'], '它那个红要还在')
        self.assertIn('503', got['stages']['export']['detail'])

    def test_a_running_round_falls_back_to_the_disk_copy(self):
        """**跑着的时候要回退磁盘。** 那时内存里躺的是正在跑的这一轮(阶段还是
        pending),拿它派生重试计划会错 —— 这一档要的恰恰是「上一轮」。
        """
        self.run_round('run-old', 'error', '3 条翻译失败（服务返回 503）')
        self.reset('run-now')                      # 又跑起来了
        got = state.last_result()
        self.assertEqual('run-old', got.get('run_id'), '跑着的时候要给上一轮')
        self.assertEqual('error', got['stages']['export']['status'])

    def test_a_failed_state_write_does_not_skip_the_history_write(self):
        """状态文件写失败,不许把本来写得成的那份历史一起跳过。

        串在一个 try 里的话那一轮在历史里直接不存在,而「历史里没有这一轮」和
        「这一轮还没跑过」在面板上长得一模一样。
        """
        real = state._write_atomic

        def only_state_fails(path, data):
            if path == state._STATE_FILE:
                raise PermissionError('read-only')
            return real(path, data)

        with patch.object(state, '_write_atomic', side_effect=only_state_fails):
            self.run_round('run-new', 'error', '3 条翻译失败（服务返回 503）')

        self.assertEqual(['run-new'], [r['run_id'] for r in state.load_history()],
                         '历史那一份照旧要写上')
        self.assertIn('PermissionError', state.persist_error(), '失败要如实记一笔')
        self.assertTrue(any('没能留档' in line for line in state.get_state()['log']),
                        '那一轮自己的日志里也要留一行(重启之后内存那笔诊断就没了)')

    def test_the_retry_derives_everything_from_one_snapshot(self):
        """范围、计划、导出范围必须从**同一份**快照派生。判据是**读了几次**。

        原来各读一次「最近一次」:两次之间别的窗口或调度器恰好跑完一轮的话,计划
        来自新那一轮的失败、而导出范围还是旧那一轮的来源 —— 实测里重试去抓了
        订阅 A,而导致这个计划的失败发生在订阅 B。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        snap = state.last_result()
        with patch.object(pipeline_retry, 'last_result', return_value=snap) as reads, \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, msg)
        self.assertEqual(1, reads.call_count, f'整次重试只许取一份快照,取了 {reads.call_count} 次')
        self.assertTrue(thread.called, '这一局的前提:它真走到了起跑那一步')

    def test_a_round_finishing_in_between_blocks_the_retry(self):
        """取快照到起跑这段时间里又跑完一轮 —— 一律拒,让用户再点一次。

        不拒的话派生出来的计划和范围全是**上一轮**的,拿它起跑等于按一份跟当前
        失败毫无关系的范围去抓。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        stale = state.last_result()
        self.run_round('run-b', 'ok', '已保存 95 条到知识库')      # 这期间又跑完一轮

        with patch.object(pipeline_retry, 'last_result', return_value=stale), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '身份变了就不许起跑')
        self.assertIn('又跑完', msg)
        self.assertFalse(thread.called, '一个线程都不许起')

    def test_a_restart_can_still_retry_from_the_disk_copy(self):
        """**反过来钉一边**:重启之后内存里没跑过任何一轮,照旧要能重试。

        那一档的终态只在磁盘上,而起跑核对是无条件做的 —— 靠的是启动后第一次读
        状态时把磁盘终态恢复进内存(`_restore_once`),不是靠「拿不准就放行」。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        self.reset()                                # 模拟重启:内存清空、没跑过
        state._state.update(running=False, run_id='', finished_at='', stages={
            name: {'status': '', 'detail': ''} for name in state.STAGES})

        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, f'重启之后还要能重试:{msg}')
        self.assertTrue(thread.called)


class RestoreTests(HistoryCase):
    """重启之后「最新那一轮」照旧有版本,起跑核对也照旧做得了。

    新进程的 `_state` 里身份、起止时间全是空的,而「最新那一轮的终态在内存里」这个
    契约 `last_result()` 和起跑核对都建在它上面 —— 重启那一档原来是个洞,前后端各用
    一条特例绕过去(前端把空身份当「已追平」、后端「拿不准一律放行」),而那两条特例
    各自又带一个事故:一条待处理数查询刚回来就作废、紧接着再查,实测 1.3 秒两千多次;
    另一条让旧快照绕过并发核对,去抓了跟当前失败无关的来源。
    """

    def restart(self):
        """模拟重启:内存清空、没跑过任何一轮,磁盘那份还在。"""
        self.reset()
        state._state.update(
            running=False, run_id='', started_at='', finished_at='', crawl_filter={},
            export_filter={}, crawl_retry={}, log=[],
            stages={name: {'status': '', 'detail': ''} for name in state.STAGES})

    def test_a_restart_reads_the_last_round_back_into_memory(self):
        """启动后第一次读状态,把磁盘终态恢复进来 —— 身份、阶段结果一起。"""
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        disk = state.load_last_run() or {}
        self.restart()
        self.assertEqual('', state._state['run_id'], '这一局的前提:内存里确实什么都没有')

        got = state.get_state()
        self.assertEqual('run-a', got['run_id'], '重启之后身份要从磁盘恢复')
        self.assertEqual(disk['finished_at'], got['finished_at'])
        self.assertEqual('error', got['stages']['export']['status'], '阶段结果也要跟着回来')
        self.assertEqual('run-a', state.result_version(), '于是这一屏有版本可报')

    def test_a_live_round_is_not_overwritten_by_the_disk_copy(self):
        """**反过来钉一边**:已经跑过、或正在跑,磁盘那份都不许盖掉内存。"""
        self.run_round('run-a', 'ok', '已保存 3 条到知识库')      # 磁盘 = run-a
        self.reset('run-b')                                        # 内存 = 正在跑 run-b
        self.assertEqual('run-b', state.get_state()['run_id'],
                         '正在跑的那一轮不许被磁盘那份换掉')
        state._state.update(running=False, finished_at='2026-10-14 10:00:00')
        state._restored = ''                                       # 装成「还没恢复过」
        self.assertEqual('run-b', state.get_state()['run_id'],
                         '已经收尾的那一轮也不许回头被磁盘盖掉')

    def only_state_write_fails(self):
        """状态文件写不上、历史写得上 —— 磁盘上最新那一轮**只在历史里**。"""
        return self._write_fails_on(lambda path: path == state._STATE_FILE)

    def only_history_write_fails(self):
        """历史写不上、状态写得上 —— 那一轮**不在历史里**(位置证据就此缺失)。"""
        return self._write_fails_on(lambda path: path != state._STATE_FILE)

    def _write_fails_on(self, hit):
        real = state._write_atomic

        def pick(path, data):
            if hit(path):
                raise PermissionError('read-only')
            return real(path, data)

        return patch.object(state, '_write_atomic', side_effect=pick)

    def read_fails_on(self, hit):
        """**注入打在真实的那次 IO 上**(`Path.read_bytes`)。打在
        `_load_json_strict` 上的话「读不出来一律抛」那一道就被 patch 盖掉了。
        """
        real = Path.read_bytes

        def pick(path, *args, **kwargs):
            if hit(path):
                raise PermissionError('read-only')
            return real(path, *args, **kwargs)

        return patch.object(Path, 'read_bytes', pick)

    def test_the_restore_prefers_the_newer_round_in_the_history(self):
        """恢复要核对两份落盘,不能只信状态文件。

        实测形态:上一轮成功、这一轮翻译 503(状态文件写失败、历史写成功),重启
        之后页头写着「同步已完成」,而历史第一条就是那个失败 —— 旧那份被当成
        「最新」固化下来,展示、完成版本和重试计划一起退回上一轮。
        """
        self.run_round('run-a', 'ok', '已保存 3 条到知识库')
        with self.only_state_write_fails():
            self.run_round('run-b', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        self.assertEqual('run-a', (state.load_last_run() or {}).get('run_id'),
                         '这一局的前提:磁盘状态文件确实落后了')
        self.assertEqual('run-b', state.load_history()[0]['run_id'],
                         '这一局的前提:最新那一轮只在历史里')

        self.restart()
        got = state.get_state()
        self.assertEqual('run-b', got['run_id'], '恢复要认历史里那一轮')
        self.assertEqual('error', got['stages']['export']['status'])
        self.assertIn('503', got['stages']['export']['detail'])
        self.assertEqual('run-b', state.result_version(), '完成版本跟着走')
        self.assertTrue(state.restore_gap(),
                        '而且要如实记一笔:这一轮的重试范围没能留档')

    def test_an_incomplete_restore_refuses_to_retry(self):
        """**恢复不完整时拒绝起跑,不许静默退回上一轮的范围。**

        历史那份记录没有导出范围和失败名单(有意的),拿上一轮的去抓会把失败那个
        来源摘掉:面板报「导出完成 · 已保存 0 条」,而它那几篇一个都没落盘。实测
        反向摆局时导出阶段收到的正是**上一轮那个来源**。
        """
        self.run_round('run-a', 'ok', '已保存 3 条到知识库')
        with self.only_state_write_fails():
            self.run_round('run-b', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        self.restart()
        self.assertEqual({}, state.get_state()['export_filter'],
                         '这一局的前提:导出范围确实没恢复出来')

        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '范围没留下就不许起跑')
        self.assertIn('没能留档', msg)
        self.assertFalse(thread.called, '一个线程都不许起')

    def test_a_complete_restore_still_retries(self):
        """**反过来钉一边**:状态文件就是最新那一轮时,恢复是完整的、重试照旧能起。"""
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        self.restart()
        self.assertEqual('run-a', state.get_state()['run_id'])
        self.assertEqual('', state.restore_gap(), '状态文件最新,恢复就是完整的')

        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, f'完整恢复之后照旧要能重试:{msg}')
        self.assertTrue(thread.called)

    def test_the_restore_keeps_the_state_file_when_the_history_write_failed(self):
        """**反过来钉一边**:历史那一份写失败时,最新那一轮只在状态文件里。

        这一档是另一个方向(上一条是状态文件写失败)。两轮**落在同一秒**里 ——
        那时两个收尾时间串相等,拿时间比大小判不出谁新,而这正是真实形态
        (几百毫秒就结束的那种运行)。恢复要认状态文件那一份,而且它是完整的。
        """
        real = state._write_atomic

        def only_history_fails(path, data):
            if path != state._STATE_FILE:
                raise PermissionError('read-only')
            return real(path, data)

        with patch.object(state, '_now', return_value='2026-10-14 09:00:00'):
            self.run_round('run-a', 'ok', '已保存 3 条到知识库')
            with patch.object(state, '_write_atomic', side_effect=only_history_fails):
                self.run_round('run-b', 'error',
                               '3 条翻译失败（服务返回 503），库里仍是原文')
        self.assertEqual(['run-a'], [r['run_id'] for r in state.load_history()],
                         '这一局的前提:新那一轮没写进历史')
        self.assertEqual('run-b', (state.load_last_run() or {}).get('run_id'),
                         '这一局的前提:状态文件是最新那一轮')

        self.restart()
        self.assertEqual('run-b', state.get_state()['run_id'],
                         '同一秒里的两轮也要认出状态文件那一份更新')
        self.assertEqual('', state.restore_gap(), '那一份是完整的,没有欠账')

    def test_a_transient_read_failure_does_not_freeze_the_restore(self):
        """**一次读不通不许固化成「从没跑过」**,下一次读状态还要再试。

        实测形态:只对状态文件的第一次读注入 `PermissionError`、之后全部正常 ——
        页面照旧显示 503 和重试按钮(展示那条路直读磁盘),而内存里那份终态永远是
        空的。

        **注入打在真实的那次 IO 上**(`Path.read_bytes`),不是打在
        `_load_json_strict` 上 —— 打在它身上的话「读不出来一律抛」那一道就被
        patch 盖掉了,这条用例对它不再敏感(真这么写过,红 0 条)。顺带也钉住了
        宽口径那一份照旧吞异常:展示那条路不受影响。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        self.restart()

        real, seen = Path.read_bytes, []

        def flaky(path, *args, **kwargs):
            seen.append(path)
            if len(seen) == 1:
                raise PermissionError('read-only')
            return real(path, *args, **kwargs)

        with patch.object(Path, 'read_bytes', flaky):
            self.assertEqual('', state.get_state()['run_id'],
                             '这一局的前提:第一次确实没读到')
            self.assertEqual('run-a', state.get_state()['run_id'],
                             '故障过去了,下一次读状态就该恢复')

    def test_a_transient_read_failure_does_not_refuse_the_retry(self):
        """短暂读失败之后恢复正常,重试照旧能起 —— 展示和核对**同源**。

        实测形态(上一版):连点两次都是 409「这期间又跑完了一轮，请重新点一次」,
        而其实什么都没跑过 —— 展示从磁盘读到了真实那一轮,核对却拿空内存去比。
        **和下面那条的分界是「这一刻读不读得通」**:读通了就有可信的最新终态,
        没有任何理由拦;读不通才核对不了哪一轮更新。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        self.restart()

        real, seen = Path.read_bytes, []

        def flaky(path, *args, **kwargs):
            seen.append(path)
            if len(seen) == 1:
                raise PermissionError('read-only')
            return real(path, *args, **kwargs)

        with patch.object(Path, 'read_bytes', flaky), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            self.assertEqual('', state.get_state()['run_id'],
                             '这一局的前提:第一次确实没读到')
            self.assertEqual('unknown', state.restore_gap(),
                             '那一刻要如实报「核对不了」')
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertTrue(ok, f'读通之后就该放行:{msg}')
        self.assertTrue(thread.called)
        self.assertEqual('', state.restore_gap(), '欠账跟着清掉')

    def test_a_restore_that_cannot_prove_latest_refuses_the_retry(self):
        """**读不通那一刻,不许按最后能读到的那一份自动重试。**

        实测形态:磁盘状态记着 A、历史记着更新的 B(B 那一轮的状态写失败),只对
        历史文件注入读失败 —— 展示 A、按钮可用,点一次重试导出阶段真去抓了 A 那个
        来源,而失败其实发生在另一个来源上。**同源不等于最新。**
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        with self.only_state_write_fails():
            self.run_round('run-b', 'ok', '已保存 95 条到知识库')
        self.restart()

        with self.read_fails_on(lambda path: path == state._history_file()), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            self.assertEqual('run-a', (state.last_result() or {}).get('run_id'),
                             '这一局的前提:能读到的那一份就是旧那一轮')
            self.assertEqual('unknown', state.restore_gap())
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '核对不了哪一轮最新就不许起跑')
        self.assertIn('核对不了', msg)
        self.assertFalse(thread.called, '一个线程都不许起')

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
            'run_id': 'run-old', 'run_seq': 3,
            'finished_at': at_old, 'started_at': '2026-10-14 08:50:00',
            'stages': {name: {'status': 'error' if name == 'export' else 'ok',
                              'detail': '3 条翻译失败（服务返回 503）'}
                       for name in state.STAGES},
            'export_filter': {'accounts': ['A']},
            'crawl_filter': {'channel': 'rss'}, 'log': ['旧那一轮'],
        }
        new_round = {
            'run_id': 'run-new', 'run_seq': 1,
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

    def test_a_sequence_that_contradicts_the_clock_is_reported_as_unprovable(self):
        """序号和收尾时间**指向不同的一轮** = 发号一度回退过,这种时候不许挑一个。

        存量记录里就可能有这种组合(老代码在发号读失证时从 1 重新开始)。两样凭据
        互相矛盾时报「证明不了」、拦住重试,**跑完一轮自己就好了**。
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

    def test_a_snapshot_without_an_id_still_has_a_version(self):
        """升级前落的记录没有 `run_id` —— 版本降级到收尾时间,不是「没有版本」。"""
        self.assertEqual('2026-10-14 09:05:00',
                         state.result_version({'finished_at': '2026-10-14 09:05:00'}))
        self.assertEqual('run-x', state.result_version(
            {'run_id': 'run-x', 'finished_at': '2026-10-14 09:05:00'}), '有身份优先用身份')
        self.assertEqual('none', state.result_version({}),
                         '从没跑过也是一个**确定**的版本,不是「未知」')

    def test_an_old_record_without_an_id_cannot_skip_the_start_check(self):
        """**旧格式快照不许绕过起跑核对。**

        实测:升级后重启、点一次「重试失败项」,期间另一轮刚成功结束 —— 旧快照没有
        身份,「两边都有身份才比」那道核对整条跳过,上一轮失败的那几个订阅又被抓了
        一遍,而失败其实发生在另一个来源上。
        """
        with patch.object(state, '_now', return_value='2026-10-14 09:05:00'):
            self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        stale = state.last_result()
        stale.pop('run_id')                        # 升级前落的那种记录
        with patch.object(state, '_now', return_value='2026-10-14 09:30:00'):
            self.run_round('run-b', 'ok', '已保存 95 条到知识库')

        with patch.object(pipeline_retry, 'last_result', return_value=stale), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '没有身份也要按收尾时间比得出来')
        self.assertIn('又跑完', msg)
        self.assertFalse(thread.called, '一个线程都不许起')

if __name__ == '__main__':
    main()
