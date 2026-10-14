"""最近几轮同步的终态落一份盘,一轮的红不被下一轮覆盖掉。

判据一律落在**「下一轮跑完之后，上一轮那个红还查不查得到」**上,不落在
「文件写出来了没有」上:面板只读最后一轮,而一轮的红很容易在被人看见之前就被
下一轮覆盖 —— 那正是这一份历史的来由。

**隔离只 patch `_STATE_FILE`,这是有意的**:历史文件从它派生,所以这一族用例
顺带钉住了"隔离点只有一个"。另立一个模块变量的话,每个走到 `_end_run()` 的既有
测试都得记着再 patch 一次,漏掉的那个会去写**生产**那份历史。
"""
import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline, pipeline_retry, pipeline_state as state
from kb.web import pipeline_api
from tests.headless import probe_env, requires_headless, run_probe

_PROBE = Path(__file__).resolve().parent / "pipe_history_probe.mjs"


class HistoryCase(TestCase):
    def setUp(self):
        self.saved = deepcopy(state._state)
        self.temp = TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.file_patch = patch.object(state, '_STATE_FILE', self.dir / 'state.json')
        self.file_patch.start()
        self.reset()

    def tearDown(self):
        self.file_patch.stop()
        self.temp.cleanup()
        state._state.clear()
        state._state.update(self.saved)

    def reset(self, run_id=''):
        # 「刚启动,还没从磁盘恢复过」。`running=True` 让恢复短路,所以这一句对自己
        # 摆 `_state` 的用例没有影响;要试重启那一档的用例把 running 改成 False。
        state._restored = False
        state._state.update(
            running=True, stop_requested=False, stopped=False, run_kind='',
            planned_stages=None, current_stage='', run_id=run_id,
            started_at='2026-10-14 09:00:00', finished_at='', crawl_filter={}, log=[],
            stages={name: {'status': 'pending', 'detail': ''} for name in state.STAGES})

    def run_round(self, run_id, export_status, detail, failures=None):
        """跑一轮 crawl + export,export 按给的档位收尾。"""
        self.reset(run_id)

        def crawl(_):
            state._stage('crawl', 'ok', '检查 51/51 个来源，新增 105 条')

        def export(_):
            state._stage('export', export_status, detail, failures=failures)

        with patch.object(pipeline, '_run_crawl', side_effect=crawl), \
             patch.object(pipeline, '_run_export', side_effect=export):
            pipeline._run_pipeline(['crawl', 'export'])


class HistoryTests(HistoryCase):
    def test_a_red_round_is_still_there_after_the_next_round_goes_green(self):
        """**这一族的来由。** 上午那轮导出判了红,晚上一次重试跑完全绿 ——
        面板那份当前状态只留最后一轮,于是那个红一个地方都查不到。
        """
        self.run_round('run-1', 'error', '3 条翻译失败（服务返回 503），库里仍是原文',
                       failures=[{'title': 'A new agent runtime', 'reason': 'HTTP 503'}])
        self.run_round('run-2', 'ok', '已保存 95 条到知识库')

        last = state.load_last_run()
        self.assertEqual('ok', last['stages']['export']['status'],
                         '前提:面板读的那一份确实已经被最新这轮覆盖了')

        runs = state.load_history()
        self.assertEqual(['run-2', 'run-1'], [r['run_id'] for r in runs], '最新在最前')
        red = runs[1]['stages']['export']
        self.assertEqual('error', red['status'])
        self.assertIn('503', red['detail'], '当时为什么红,要还能读到')
        self.assertEqual([{'title': 'A new agent runtime', 'reason': 'HTTP 503'}],
                         red['failures'], '哪几条失败的明细也要留住')

    def test_each_round_keeps_its_own_log(self):
        """翻到某一轮时要看得到**那一轮**的日志,不是最新那轮的。"""
        self.run_round('run-1', 'error', '3 条翻译失败（服务返回 503）')
        self.run_round('run-2', 'ok', '已保存 95 条到知识库')
        runs = state.load_history()
        self.assertTrue(any('503' in line for line in runs[1]['log']))
        self.assertFalse(any('503' in line for line in runs[0]['log']),
                         '最新那轮的日志里不该混进上一轮的行')

    def test_the_round_that_was_stopped_says_so(self):
        self.reset('run-1')

        def crawl(_):
            state._stage('crawl', 'ok', '新增 3 篇')
            state.request_stop()

        with patch.object(pipeline, '_run_crawl', side_effect=crawl), \
             patch.object(pipeline, '_run_export'):
            pipeline._run_pipeline(['crawl', 'export'])
        got = state.load_history()[0]
        self.assertTrue(got['stopped'])
        self.assertEqual('stopped', got['stages']['export']['status'])

    def test_the_last_run_file_keeps_its_shape(self):
        """**反过来钉一边:承重的那一份一个字段都没少。**

        `pipeline_retry` 从 `load_last_run()` 恢复抓取范围、导出范围和失败名单,
        读错一个字段的后果是静默不抓(见 `docs/decisions/retry-scope.md`)。加一份
        历史不许动它。
        """
        self.reset('run-1')
        state._state.update(crawl_filter={'channel': 'rss'},
                            export_filter={'channel': 'rss'}, run_kind='retry',
                            planned_stages=['crawl', 'export'])
        state.note_retry_targets('rss', [7], '2026-10-14T01:00:00Z')
        with patch.object(pipeline, '_run_crawl'), patch.object(pipeline, '_run_export'):
            pipeline._run_pipeline(['crawl', 'export'])
        last = state.load_last_run()
        for key in ('started_at', 'finished_at', 'stopped', 'run_kind', 'planned_stages',
                    'stages', 'crawl_filter', 'export_filter', 'run_id', 'crawl_retry', 'log'):
            self.assertIn(key, last, f'{key} 不见了')
        self.assertEqual({'rss': {'targets': ['7'], 'at': '2026-10-14T01:00:00Z'}},
                         state.retry_notes(last), '重试名单照旧派生得出来')

    def test_writing_the_same_round_twice_leaves_one_entry(self):
        self.run_round('run-1', 'ok', '已保存 95 条')
        state._save_history()
        state._save_history()
        self.assertEqual(['run-1'], [r['run_id'] for r in state.load_history()])

    def test_rounds_without_an_identity_are_not_folded_together(self):
        """**空串不是身份。** 拿它去重会把两轮各自跑过的运行合成一条 ——
        历史里少一轮是静默丢数据,多留一条只是多一行。
        """
        self.run_round('', 'error', '第一轮红了')
        self.run_round('', 'ok', '第二轮好了')
        runs = state.load_history()
        self.assertEqual(2, len(runs), '两轮都要在')
        self.assertEqual(['ok', 'error'], [r['stages']['export']['status'] for r in runs])

    def test_only_the_most_recent_rounds_are_kept(self):
        with patch.object(state, '_HISTORY_KEEP', 3):
            for i in range(5):
                self.run_round(f'run-{i}', 'ok', f'第 {i} 轮')
            self.assertEqual(['run-4', 'run-3', 'run-2'],
                             [r['run_id'] for r in state.load_history()])

    def test_a_missing_or_broken_file_is_just_an_empty_list(self):
        """只读展示:读不出来不如少显示几行,不许把一轮同步的收尾带崩。"""
        self.assertEqual([], state.load_history(), '文件还没有')
        for junk in ('{', '[]', '{"runs": "nope"}', 'null'):
            state._history_file().write_text(junk, encoding='utf-8')
            self.assertEqual([], state.load_history(), junk)
        state._history_file().write_text('{"runs": [{"run_id": "a"}, 7, null]}',
                                         encoding='utf-8')
        self.assertEqual([{'run_id': 'a'}], state.load_history(), '夹着垃圾的只摘好的')

    def test_the_history_file_follows_the_state_file(self):
        """隔离点只有一个 —— 见本模块 docstring。"""
        self.assertEqual(self.dir, state._history_file().parent)
        self.run_round('run-1', 'ok', '已保存 95 条')
        self.assertTrue(state._history_file().exists(), '真写进了被隔离的那个目录')
        self.assertFalse(list(self.dir.glob('*.tmp')), '临时文件不留下')


class PersistFailureTests(HistoryCase):
    """留档失败不许把一轮**已经做完**的同步变成「永远进行中」。

    判据一律落在**「这一轮收没收尾、下一轮开不开得起来」**上,不落在「异常捕获里
    列了哪几个类型」上 —— 后者按坏法一种一种往里补,下一种坏法照旧重现。
    """

    def start_again(self):
        """真走一次 `start_pipeline`(身份发放 + running 检查),正文换掉不跑。"""
        with patch.object(pipeline, '_run_pipeline', lambda *a, **k: None):
            got = pipeline.start_pipeline(['export'])
        state._state['running'] = False      # 那个空线程不会自己收尾
        return got

    def test_a_corrupt_history_file_does_not_escape_the_persist_layer(self):
        """三种实测过的坏法,**在留档这一层就要隔离掉**,不靠收尾那道兜底。

        非 UTF-8 字节抛 `UnicodeDecodeError`、深嵌套 JSON 抛 `RecursionError`、
        孤立代理字符在**写**那一步抛 `UnicodeEncodeError` —— 三个都不属于
        `OSError` 和 `JSONDecodeError`。

        **两道是冗余保护,各有自己的回归**(「两份实现各有自己的回归」):这一条
        钉第一道,所以**直接调这两个函数、不经过 `_end_run`* ——走 `_end_run`
        的话第二道会把异常吃掉,这一条在任何单道注入下都不红。第二道由
        `test_the_invariant_is_the_finally_not_the_exception_list` 钉,那一条同时
        断言用户遇到的后果。
        """
        for name, raw in (
            ('非 UTF-8 字节', b'{"runs": [{"run_id": "\xff\xfe"}]}'),
            ('深嵌套 JSON', b'[' * 2000 + b']' * 2000),
            ('孤立代理字符', '{"runs": [{"run_id": "old", "log": ["\\ud800"]}]}'.encode()),
        ):
            with self.subTest(name):
                state._history_file().write_bytes(raw)
                self.reset('run-bad')
                state._state.update(finished_at='2026-10-14 10:05:00', run_kind='sync',
                                    planned_stages=['export'])
                try:
                    state.load_history()
                    state._save_history()
                except Exception as exc:        # noqa: BLE001 - 这正是要钉的
                    self.fail(f'{name}:{type(exc).__name__} 从留档层逃出来了')
                # 读出来的旧记录(坏字节换成 U+FFFD 之后)也还在,所以只认最前那一条
                self.assertEqual('run-bad', (state.load_history() or [{}])[0].get('run_id'),
                                 f'{name}:隔离掉之后这一轮还是要留下来、而且在最前')

    def test_the_invariant_is_the_finally_not_the_exception_list(self):
        """注入一个谁都没列进捕获的异常,收尾照旧,诊断如实记着是什么坏了。

        这一条钉的是**第二道**(`_end_run` 那个 `finally`),判据落在用户遇到的
        后果上:阶段卡片和日志已经写着「管道结束」,而标题是「同步正在启动」、按钮
        禁用,新同步和失败重试一概被拒,点停止也救不回来(线程早退了)。
        **用一个没人想过的异常类型**(`MemoryError`),正是因为不变量该钉在 finally
        上、不该钉在「捕获里列了哪几个类型」上。

        **反过来钉一边**:下一轮留档成功,那条诊断就撤掉 —— 不然面板会一直挂着
        一句早就不成立的话。
        """
        with patch.object(state, '_write_atomic', side_effect=MemoryError('boom')):
            self.run_round('run-x', 'error', '3 条翻译失败（服务返回 503），库里仍是原文')
        self.assertFalse(state.get_state()['running'], '留档炸了,收尾也得完成')
        self.assertTrue(state.run_result('run-x'), '等结果的人还要拿得到终态')
        self.assertTrue(self.start_again(), '下一轮还要起得来')
        # 两份分别尝试、分别诊断,所以两个都炸时记的是两条
        self.assertIn('MemoryError', state.persist_error(), '是什么坏了要如实记')

        self.run_round('run-y', 'ok', '已保存 95 条到知识库')
        self.assertEqual('', state.persist_error(), '留档又成功了,诊断要撤')
        self.assertEqual(['run-y'], [r['run_id'] for r in state.load_history()])

    def test_a_bad_byte_costs_one_character_not_the_whole_file(self):
        """坏字节换成 U+FFFD,**其余几轮一个字节不差**。

        「漏报是静默丢数据」:整份当读不出来扔掉的代价,是把本来好好的那几轮一起
        扔了 —— 而历史文件里少一轮,在面板上和「还没跑过」长得一模一样。
        """
        good = {"run_id": "run-keep", "started_at": "2026-10-13 09:00:00",
                "finished_at": "2026-10-13 09:10:00", "stopped": False,
                "run_kind": "sync", "planned_stages": ["export"],
                "stages": {"export": {"status": "error", "detail": "翻译 503"}},
                "crawl_filter": {}, "log": ["[10-13 09:10:00] 管道结束"]}
        raw = json.dumps({"runs": [good]}, ensure_ascii=False).encode()
        state._history_file().write_bytes(raw.replace(b'503', b'5\xff3'))

        kept = state.load_history()
        self.assertEqual(['run-keep'], [r['run_id'] for r in kept], '那一轮要还在')
        self.assertIn('翻译', kept[0]['stages']['export']['detail'], '坏的只有那一个字符')

        self.run_round('run-new', 'ok', '已保存 95 条到知识库')
        self.assertEqual(['run-new', 'run-keep'],
                         [r['run_id'] for r in state.load_history()], '合并也不许丢它')

    def test_a_failed_replace_leaves_no_temp_file(self):
        """原子换文件那一步失败,不许把临时文件留在数据目录里。

        单进程里重名覆盖,但每次重启换一个 pid —— 留下来的那些慢慢堆着,没人收。
        """
        def nope(_self, _target):
            raise PermissionError('read-only')

        with patch.object(Path, 'replace', nope):
            self.run_round('run-z', 'ok', '已保存 95 条到知识库')
        self.assertFalse(state.get_state()['running'], '收尾照旧要完成')
        self.assertEqual([], sorted(p.name for p in self.dir.glob('*.tmp')),
                         '临时文件要收掉')


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
        state._restored = False                                    # 装成「还没恢复过」
        self.assertEqual('run-b', state.get_state()['run_id'],
                         '已经收尾的那一轮也不许回头被磁盘盖掉')

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


class HistoryEndpointTests(HistoryCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(pipeline_api.router)
        self.client = TestClient(app)

    def test_the_endpoint_hands_over_the_rounds_newest_first(self):
        self.run_round('run-1', 'error', '3 条翻译失败（服务返回 503）')
        self.run_round('run-2', 'ok', '已保存 95 条')
        r = self.client.get('/api/pipeline/runs')
        self.assertEqual(200, r.status_code, r.text)
        runs = r.json()['runs']
        self.assertEqual(['run-2', 'run-1'], [item['run_id'] for item in runs])
        self.assertIn('503', runs[1]['stages']['export']['detail'])

    def test_both_endpoints_report_the_same_version(self):
        """两个端点的版本是**同一份口径**,而且任何一次回包都有。

        前端拿它判「手上这份待处理数 / 这份历史清单是不是这一轮的」。两边各算一遍
        就会在某一档上岔开,而岔开的后果是静默的:对不上就一直补读(忙循环),或者
        被当成已追平(面板永久停在旧数字)。
        """
        fresh = self.client.get('/api/pipeline/status?source=false')
        self.assertEqual('none', fresh.json()['result_version'],
                         '从没跑过也要有版本')
        self.assertEqual('none', self.client.get('/api/pipeline/runs').json()['version'])

        self.run_round('run-1', 'ok', '已保存 95 条')
        got = self.client.get('/api/pipeline/status?source=false').json()
        self.assertEqual('run-1', got['result_version'])
        self.assertEqual(got['result_version'],
                         self.client.get('/api/pipeline/runs').json()['version'],
                         '两个端点必须报同一个版本')

    def test_no_history_yet_is_an_empty_list_not_an_error(self):
        r = self.client.get('/api/pipeline/runs')
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([], r.json()['runs'])


# 一个独立进程:走**生产那条启动路径**(`start_pipeline` 发身份、起线程、`_end_run`
# 落盘)跑一轮,把这一轮的身份打在最后一行。导出阶段的成败按参数给,不发任何外部请求。
_ONE_ROUND = """
import sys
from unittest.mock import patch
from kb import pipeline
from kb.feeds import sync as feed_sync      # noqa: F401 - 让延迟 import 先落位

status, detail = sys.argv[1], sys.argv[2]

def export(*a, **kw):
    pipeline._stage('export', status, detail,
                    failures=[{'title': 'A new agent runtime', 'reason': 'HTTP 503'}]
                    if status == 'error' else None)

with patch.object(pipeline, '_run_export', side_effect=export):
    run_id = pipeline.start_pipeline(['export'])
    assert run_id, '没拿到身份'
    for _ in range(600):
        if not pipeline.get_state()['running']:
            break
        time.sleep(0.05)
    else:
        raise SystemExit('那一轮没有收尾')
print(run_id)
"""


class RestartTests(TestCase):
    """**身份要跨进程重启仍然唯一** —— 它同时是历史落盘的去重键。

    这一族非得真起两个进程不可:`_run_seq` 归零**模拟不了重启**(真重启会连
    `_RUN_EPOCH` 一起重新生成),而手工给两轮不同的身份恰恰把要验的那件事假设掉了。
    判据落在**「第一轮那个红还在不在历史里」**上,不落在「两个身份长得一样吗」。
    """

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.dir = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def one_round(self, status, detail):
        env = {**os.environ, 'KB_DATA_DIR': str(self.dir),
               'PYTHONPATH': str(Path(__file__).resolve().parents[1])}
        r = subprocess.run([sys.executable, '-c', 'import time\n' + _ONE_ROUND, status, detail],
                           capture_output=True, text=True, timeout=240, env=env,
                           cwd=str(Path(__file__).resolve().parents[1]))
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def history(self):
        path = self.dir / 'pipeline_state_runs.json'
        return json.loads(path.read_text(encoding='utf-8'))['runs'] if path.exists() else []

    def test_a_restart_does_not_overwrite_the_round_before_it(self):
        """两个独立进程共享一个数据目录:第一轮的失败原因、明细和日志都要留住。

        原来身份是进程内自增的 `run-{序号}`,重启后新的 `run-1` 把上个进程那个
        `run-1` 从历史里替换掉 —— 远在保留上限之前就开始丢,而丢没丢在面板上一个字
        都看不见。
        """
        first = self.one_round('error', '3 条翻译失败（服务返回 503），库里仍是原文')
        second = self.one_round('ok', '已保存 95 条到知识库')

        # **后果先断言。** 身份撞不撞是机制,「那个红还查不查得到」才是用户遇到的事
        runs = self.history()
        self.assertEqual(2, len(runs), '重启前那一轮不许被覆盖')
        red = next((r for r in runs if r['stages']['export']['status'] == 'error'), None)
        self.assertIsNotNone(red, '第一轮那个红还要查得到')
        self.assertIn('503', red['stages']['export']['detail'], '当时为什么红,要还能读到')
        self.assertEqual('HTTP 503', red['stages']['export']['failures'][0]['reason'])
        self.assertTrue(any('503' in line for line in red['log']),
                        '那一轮自己的日志也要留住')
        # 机制排在后面
        self.assertNotEqual(first, second, '两个进程发出来的身份不许相同')
        self.assertEqual([second, first], [r['run_id'] for r in runs], '最新在最前')


@requires_headless
class BrowserTests(TestCase):
    """面板上翻得到那一轮,而且翻历史时「重试失败项」按不下去。

    判据和理由都在探针自己的抬头里。这里只负责**经 `run_probe` 起它** ——
    探针不许自己 spawn 浏览器、测试不许裸 `subprocess.run` 跑探针
    (见 `docs/decisions/probe-shell.md`)。
    """

    def test_a_past_round_can_be_opened_on_the_real_panel(self):
        r = run_probe([shutil.which("node"), str(_PROBE)],
                      timeout=300, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == '__main__':
    main()
