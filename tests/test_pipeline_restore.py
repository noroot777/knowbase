"""「最新那一轮」只有一份口径,重启之后也得成立。

两件事:**可信终态**(活进程里内存就是真相,磁盘那份写失败了不该改变「最新是哪一
轮」)和**恢复**(新进程启动时把磁盘终态读回内存,两份落盘都要核对)。它们共用
`test_pipeline_history.py` 那套摆盘(`HistoryCase`),但判据完全不同 —— 那边钉的是
「落了盘没有、翻不翻得到」,这边钉的是「恢复这件事做对了没有」。

**「两份各记着不同一轮时谁更新」那一族在 `test_pipeline_order.py`**:凭据够不够是
另一个问题,这边只问恢复本身(内存读回来了没、活进程被盖没被盖、读失败之后还能不能
重试)。
"""
import json
from pathlib import Path
from unittest import main
from unittest.mock import patch

from kb import pipeline_persist as persist, pipeline_retry, pipeline_state as state
from tests.pipeline_case import HistoryCase


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
        with self.all_writes_fail():
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
        with self.only_state_write_fails():
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

    def test_an_aliased_round_finishing_in_between_still_blocks_the_retry(self):
        """期间又跑完一轮,而那两轮的**展示版本撞上了** —— 这道核对照旧要拦住。

        上一条靠身份变了判出来;这一条摆的是旧格式:两轮都没有身份、收尾时间又相同,
        展示版本于是相等。核对比版本的话整条放行,而手上那份快照其实已经不是最新那
        一轮了 —— 按它的范围去抓就抓错了来源。所以判据是 `same_round`
        (「这两份是不是同一轮」),和恢复那边同一份口径。
        """
        self.restart()                         # 开局得干净:上一条可能还留着 running
        at = '2026-10-14 19:30:00'
        common = {'finished_at': at, 'run_kind': 'sync', 'stopped': False,
                  'planned_stages': list(state.STAGES), 'crawl_filter': {'channel': 'rss'}}
        stale = dict(common, started_at='2026-10-14 19:20:00',
                     export_filter={'accounts': ['A-failed']},
                     stages={name: {'status': 'error' if name == 'export' else 'ok',
                                    'detail': '3 条翻译失败（服务返回 503）'}
                             for name in state.STAGES},
                     log=['[10-14 19:30] 管道结束'])
        fresh = dict(common, started_at='2026-10-14 19:25:00',
                     stages={name: {'status': 'ok', 'detail': '完成'} for name in state.STAGES},
                     log=['[10-14 19:30] 管道结束'])
        self.assertEqual(state.result_version(stale), state.result_version(fresh),
                         '摆局前提:两轮的展示版本确实撞上了')

        with patch.object(pipeline_retry, 'last_result', return_value=stale), \
             patch.object(pipeline_retry, '_latest_locked', return_value=fresh), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '版本撞上不算「还是同一轮」')
        self.assertIn('又跑完', msg)
        self.assertFalse(thread.called)

    def test_an_identical_looking_round_finishing_in_between_blocks_the_retry(self):
        """期间又跑完一轮,而那两轮**内容逐字相同** —— 身份不同就得拦住。

        上一条摆的是旧格式(没身份,只能靠内容判);这一条两轮都带身份,内容却一字
        不差:同一秒收尾、阶段结果和日志相同。内容那一级比的字段里没有
        `export_filter` —— 而两轮的导出范围恰恰是不同的订阅。让内容兜底盖过身份的
        话整条放行,手上那份快照其实已经落后一轮:实测 `/api/pipeline/retry-failed`
        返回 200(该是 409),重试线程收到的是上一轮那个订阅。
        """
        self.restart()                         # 开局得干净:上一条可能还留着 running
        at = '2026-10-14 21:04:00'
        common = {'finished_at': at, 'started_at': '2026-10-14 21:00:00',
                  'run_kind': 'sync', 'stopped': False, 'seq_gen': state._SEQ_GEN,
                  'planned_stages': list(state.STAGES), 'crawl_filter': {'channel': 'rss'},
                  'stages': {name: {'status': 'error' if name == 'export' else 'ok',
                                    'detail': '3 条翻译失败（服务返回 503）'}
                             for name in state.STAGES},
                  'log': ['[10-14 21:04] 管道结束']}
        stale = dict(common, run_id='run-6acf76695d2e-1', run_seq=1,
                     export_filter={'accounts': ['A订阅']})
        fresh = dict(common, run_id='run-6acf76695d2e-2', run_seq=2,
                     export_filter={'accounts': ['B订阅']})
        self.assertTrue(all(stale.get(k) == fresh.get(k) for k in persist._ROUND_FIELDS),
                        '摆局前提:内容那一级比的每个字段都逐字相同')

        with patch.object(pipeline_retry, 'last_result', return_value=stale), \
             patch.object(pipeline_retry, '_latest_locked', return_value=fresh), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '身份不同就是另一轮,内容再像也不算')
        self.assertIn('又跑完', msg)
        self.assertFalse(thread.called, '更不许起线程去抓 A订阅')

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
        with patch.object(state, '_now', return_value='2026-10-14 09:00:00'):
            self.run_round('run-a', 'ok', '已保存 3 条到知识库')
            with self.only_history_write_fails():
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

        实测:升级后重启、点一次「重试失败项」,期间另一轮刚结束 —— 旧快照没有身份,
        「两边都有身份才比」那道核对整条跳过,上一轮失败的那几个订阅又被抓了一遍,
        而失败其实发生在另一个来源上。

        **期间那一轮也留着红**:计划现在从当日账本派生,期间那轮全绿的话今天压根
        没有欠账,这颗按钮会早一步回「没有失败项」—— 那也是对的答案,但核对那道闸
        一个字都没验到。要验它就得让计划非空。
        """
        with patch.object(state, '_now', return_value='2026-10-14 09:05:00'):
            self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        stale = state.last_result()
        stale.pop('run_id')                        # 升级前落的那种记录
        with patch.object(state, '_now', return_value='2026-10-14 09:30:00'):
            self.run_round('run-b', 'error', '1 条导出失败（磁盘只读）')

        with patch.object(pipeline_retry, 'last_result', return_value=stale), \
             patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '没有身份也要按收尾时间比得出来')
        self.assertIn('又跑完', msg)
        self.assertFalse(thread.called, '一个线程都不许起')

    def test_a_record_that_would_break_the_screen_is_not_the_latest_either(self):
        """恢复拒掉的那种记录,**不许从磁盘回落那条路原样进来**。

        回落走的是「恢复还没成功 / 恢复拒了磁盘那份」这一档,而拒的理由多半正是
        形状不对。`load_last_run()` 是宽口径的「磁盘那份原样」(还有只取失败名单的
        消费者在用它),回落时不过一遍形状判据的话,被挡在门外的那份记录从这里进来,
        照旧交给面板整屏渲染和重试派生 —— 日志那一屏 `join()` 当场抛,整屏变成
        「状态暂时无法更新」。**宁可报「尚无运行记录」**:那是实话,下一轮就补回来。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        raw = json.loads(state._STATE_FILE.read_text())
        state._STATE_FILE.write_text(json.dumps(dict(raw, log='管道结束'),
                                                ensure_ascii=False))
        state._history_file().write_text(json.dumps({'runs': []}, ensure_ascii=False))
        self.assertEqual('管道结束', (state.load_last_run() or {}).get('log'),
                         '摆局前提:磁盘那份原样读出来确实是那个字符串')

        self.restart()
        self.assertEqual({}, state.last_result(),
                         '渲染不了的记录不算「最新那一轮」')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, _ = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '更不许按它派生一次重试')
        self.assertFalse(thread.called)

    def test_a_restored_legacy_round_does_not_borrow_the_current_generation(self):
        """恢复回来的旧记录**不许借用内存里那份默认的世代标识**。

        `_state` 初始带的是当前世代,恢复只覆盖它列出来的那几个字段 —— 漏掉出处的
        话,升级前那一轮的 `run-1` / 回退过的 `run_seq` 在内存里就成了「当前这一代
        writer 发的号」,而它们跨重启会重复。**出处是所有身份判据的输入**,改掉它
        等于让 `event_id` / `_seq_of` 那两道自己的门槛失效。

        反过来钉一边:恢复过一次之后**再跑一轮,出处要回到当前世代** —— 不然内存
        那份终态永久失证,起跑核对就只剩内容兜底那一级(`_end_run` 发号时署名)。
        """
        at = '2026-10-14 09:16:00'
        one = {'finished_at': at, 'started_at': '2026-10-14 09:00:00',
               'run_kind': 'sync', 'stopped': False, 'run_id': 'run-1', 'run_seq': 3,
               'planned_stages': list(state.STAGES), 'crawl_filter': {'channel': 'rss'},
               'stages': {name: {'status': 'ok', 'detail': '完成'} for name in state.STAGES},
               'log': ['[10-14 09:16] 管道结束']}
        state._STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        state._STATE_FILE.write_text(json.dumps(dict(one, export_filter={}),
                                                ensure_ascii=False))
        state._history_file().write_text(json.dumps({'runs': [dict(one)]},
                                                    ensure_ascii=False))

        self.restart()
        got = state.get_state()
        self.assertEqual('run-1', got['run_id'], '摆局前提:那个旧身份确实恢复进来了')
        self.assertEqual('', persist.event_id(got), '旧记录拿不出可信的事件身份')
        self.assertEqual(0, persist._seq_of(got), '它那个号也不算凭据')

        self.run_round('run-after-restore', 'ok', '完成')
        self.assertTrue(persist.event_id(state.get_state()),
                        '这一代 writer 定稿时要署名 —— 不然内存那份永久失证')


    def test_two_broken_copies_are_not_reported_as_never_having_run(self):
        """两份都渲染不了时,**「一份都挑不出来」不等于「确定从没跑过」**。

        那一轮确实发生过(时间、身份都在),只是两份副本都读不出来。把这一档固化成
        「没跑过」会连同那道拦闸一起吞掉:面板报「尚无运行记录」,而「重试失败项」
        照旧可点 —— 它会按磁盘上那份读得出来的旧范围去抓。所以这一档要:**什么都不
        展示,但欠账如实记着、按钮按不下去。**
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        for path in (state._STATE_FILE, state._history_file()):
            raw = json.loads(path.read_text())
            if 'runs' in raw:
                raw['runs'] = [dict(one, log='管道结束') for one in raw['runs']]
            else:
                raw['log'] = '管道结束'
            path.write_text(json.dumps(raw, ensure_ascii=False))

        self.restart()
        self.assertEqual('', state.get_state()['finished_at'],
                         '两份都渲染不了 —— 什么也不往屏幕上放')
        self.assertEqual('broken', state.restore_gap(),
                         '可那一轮发生过:不许说「从没跑过」')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok)
        self.assertIn('留档损坏', msg,
                      '拒绝理由要说准 —— 不是「上次运行没有失败项」')
        self.assertFalse(thread.called)


    def test_a_round_whose_result_never_reached_the_disk_is_not_forgotten(self):
        """两份终态都没写上时,**那一轮不许就此查无此事**。

        两份都是收尾时才写的:都失败那一档,磁盘上一个字都没留下这一轮 —— 新身份、
        完成序号、终态和那笔留档诊断全在内存里,而重启把内存清空。于是恢复看到的两份
        都是上一轮,`same_event` 正确地证明「这两份是同一次事件」,可它证明不了「没有
        更新的一轮」:实测 B 轮(成功、范围是 B订阅)两份写入都报满盘,重启后页头又显示
        A 的 503、**空欠账、没有任何过期提示**,点下去导出阶段收到 A订阅。

        所以开工标记要落在**起跑之前**,收尾两份都写上了才清掉。重启后它还在、而磁盘上
        最新那一份不是它,就说明有一轮跑过而结果没落下来。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        self.assertFalse(state._open_file().exists(), '摆局前提:上一轮正常收尾,标记清掉了')
        with self.all_writes_fail():
            self.run_round('run-b', 'ok', '已保存 95 条')
        self.assertTrue(state._open_file().exists(), 'B 轮两份都没写上 —— 标记留着')
        self.assertEqual('run-a', (state.load_last_run() or {}).get('run_id'),
                         '摆局前提:磁盘上最新那一份还是 A')

        self.restart()
        state.get_state()                               # 恢复是懒做的
        self.assertEqual('lost', state.restore_gap(),
                         '有一轮跑过而结果没落下来 —— 屏幕上这一轮已经过期')
        with patch.object(pipeline_retry.threading, 'Thread') as thread:
            ok, msg = pipeline_retry.start_retry_failed()
        self.assertFalse(ok, '不许按一份已经过期的范围去抓')
        self.assertIn('没能留档', msg)
        self.assertFalse(thread.called)

    def test_a_round_that_did_land_is_not_reported_as_lost(self):
        """**反过来钉一边**:标记没清掉不等于失证。

        只有一份写失败那一档(`scope`)标记也留着,可那一轮确实在历史里 —— 判据是
        「标记不是磁盘上最新那一轮」,不是「标记还在」。按「标记还在就报失证」写的话,
        每一次单写失败都被多报一档,而那一屏其实是最新的。
        """
        self.run_round('run-a', 'error', '3 条翻译失败（服务返回 503）')
        with self.only_state_write_fails():
            self.run_round('run-b', 'ok', '已保存 95 条')
        self.assertTrue(state._open_file().exists(), '摆局前提:有一份写失败,标记留着')

        self.restart()
        self.assertEqual('run-b', state.get_state()['run_id'],
                         'B 轮在历史里 —— 展示的就是它')
        self.assertEqual('scope', state.restore_gap(),
                         '标记指着的就是这一轮:不是失证,只是重试范围没留档')

    def test_a_readable_copy_of_the_same_round_is_not_called_an_earlier_one(self):
        """退回到的是**同一轮的另一个副本**时,不许说「下面显示的是更早一轮」。

        状态文件那份正文坏了、而历史里那份同一轮的副本正常:屏幕上显示的就是最新那
        一轮自己,只是少了重试派生字段 —— 那恰恰是 `scope` 的处境。说成「更早一轮」
        是假话,而人会据这句话去决定下一步。
        """
        self.run_round('run-a', 'ok', '已保存 95 条')
        raw = json.loads(state._STATE_FILE.read_text())
        state._STATE_FILE.write_text(json.dumps(dict(raw, log='管道结束'),
                                                ensure_ascii=False))

        self.restart()
        self.assertEqual('run-a', state.get_state()['run_id'],
                         '展示的是同一轮的历史副本')
        self.assertEqual('scope', state.restore_gap(),
                         '同一轮的副本 —— 报「范围没留档」,不是「更早一轮」')

if __name__ == '__main__':
    main()
