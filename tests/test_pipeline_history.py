"""最近几轮同步的终态落一份盘,一轮的红不被下一轮覆盖掉。

判据一律落在**「下一轮跑完之后，上一轮那个红还查不查得到」**上,不落在
「文件写出来了没有」上:面板只读最后一轮,而一轮的红很容易在被人看见之前就被
下一轮覆盖 —— 那正是这一份历史的来由。

**隔离只 patch `_STATE_FILE`,这是有意的**:历史文件从它派生,所以这一族用例
顺带钉住了"隔离点只有一个"。另立一个模块变量的话,每个走到 `_end_run()` 的既有
测试都得记着再 patch 一次,漏掉的那个会去写**生产**那份历史。
"""
import shutil
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline, pipeline_state as state
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

    def test_no_history_yet_is_an_empty_list_not_an_error(self):
        r = self.client.get('/api/pipeline/runs')
        self.assertEqual(200, r.status_code, r.text)
        self.assertEqual([], r.json()['runs'])


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
