"""清单上「哪一行是此刻屏幕上那一轮」:**位置不是证据,身份才是。**

从 `test_pipeline_history.py` 里分出来的(那边验留了什么、`test_pipeline_retention.py`
验留多久),三边共用同一个 `HistoryCase`。分开为了行数,也因为这一条的性质不同:它问
的是**两份独立可失败的状态之间能不能互相冒充**。

事故形态:历史落盘失败那一轮根本没写进历史文件,于是清单首条是上午那个红,而状态端点
报的是下午那一轮的成功。前端原来按「排第一而且没在跑」把首行当成最新、点它等于「回到
最新」—— 实点上午那个红,屏幕换成下午的「同步已完成」,那一轮的失败明细再也打不开。

**判据落在回包那个字段上**(`is_current`),页面那一侧由 `tests/pipe_latest_probe.mjs`
实点。两边各钉一份红:后端标错了前端还有一道(版本对照),前端松了后端这一道也得拦住。
"""
import json
import shutil
from pathlib import Path
from unittest import TestCase, main

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline_state as state
from kb.web import pipeline_api
from tests.headless import probe_env, requires_headless, run_probe
from tests.pipeline_case import HistoryCase

_PROBE = Path(__file__).resolve().parent / "pipe_latest_probe.mjs"


class CurrentRowTests(HistoryCase):
    def setUp(self):
        super().setUp()
        app = FastAPI()
        app.include_router(pipeline_api.router)
        self.client = TestClient(app)

    def rows(self):
        return self.client.get('/api/pipeline/runs').json()['runs']

    def test_the_newest_round_is_marked_and_the_older_one_is_not(self):
        """正常那一档:最新那一轮标出来,上一轮不标。

        **反过来钉一边** —— 判据写成「一律不标」也能让下面那几条全绿,而那样面板上
        「最新」那一枚就永远不出现、点首行永远进只读态(一个屏幕一个字不变的末态)。
        """
        self.run_round('run-1', 'error', '3 条翻译失败（服务返回 503）')
        self.run_round('run-2', 'ok', '已保存 95 条')
        got = {r['run_id']: r['is_current'] for r in self.rows()}
        self.assertEqual({'run-2': True, 'run-1': False}, got)

    def test_no_row_is_marked_when_the_newest_round_never_reached_the_history(self):
        """**留档失败那一档:一行都不许标。**

        这一轮的结果在内存里(`last_result()` 报的就是它),历史文件里最新那条却是上一
        轮 —— 两份允许不同步,而「清单首条」和「屏幕上那一轮」此刻**不是同一轮**。
        标了的话点它就等于「回到最新」:用户点的是上午那个红,屏幕给的是下午那份成功。
        """
        self.run_round('run-1', 'error', '3 条翻译失败（服务返回 503）')
        with self.only_history_write_fails():
            self.run_round('run-2', 'ok', '已保存 95 条')
        rows = self.rows()
        self.assertEqual(['run-1'], [r['run_id'] for r in rows], '历史里只有上午那一轮')
        self.assertEqual('run-2', state.last_result()['run_id'], '屏幕上那一轮是后来成功的')
        self.assertFalse(rows[0]['is_current'], '首行不是此刻那一轮,不许标')

    def test_a_legacy_record_without_a_trustworthy_identity_is_not_marked(self):
        """旧格式那几条**没有可信身份**,一律不标(`same_event` 只认带出处的身份)。

        它们的 `run_id` 只有进程内自增、重启之后会重复,拿它认「同一轮」会在恢复过一次
        之后把另一轮认成这一轮。不标的代价只是「回到最新」要走概况卡上那颗按钮。
        """
        self.run_round('run-1', 'ok', '已保存 95 条')
        runs = state.load_history()
        for run in runs:
            run.pop('seq_gen', None)
        state._history_file().write_text(json.dumps({'runs': runs}, ensure_ascii=False),
                                         encoding='utf-8')
        self.assertFalse(self.rows()[0]['is_current'], '证明不了就不标')


@requires_headless
class BrowserTests(TestCase):
    """页面那一侧实点:判据和理由在探针自己的抬头里。

    这里只负责**经 `run_probe` 起它** —— 探针不许自己 spawn 浏览器、测试不许裸
    `subprocess.run` 跑探针(见 `docs/decisions/probe-shell.md`)。

    **能力门不许漏**(`@requires_headless`):CI 刻意不装无头壳,`probe_env()` 进去
    就断言失败 —— 同一份 discovery 里别的浏览器类都如实 skip,只这一个报红,而红的
    原因和被测的那件事毫无关系。同模块那三条后端用例照旧跑(门只扣在这个类上)。
    """

    def test_a_row_that_cannot_prove_it_is_current_opens_its_own_round(self):
        r = run_probe([shutil.which("node"), str(_PROBE)], timeout=300, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == '__main__':
    main()
