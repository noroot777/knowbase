# -*- coding: utf-8 -*-
"""正文回填脚本是可选外挂,路径不许写死在代码里。

原来是一行 `Path.home() / ... / "gzh_refill_body.py"` ——仓库里没有这个脚本,
它是维护者自己机器上某个目录下的一份存量修补工具。优雅降级成 skipped
所以不崩,但每个外部用户每次点「重试失败项」都会看到一行指向别人家目录的提示,而
这个功能对他们永远不可用。

现在的口径:配了 KB_REFILL_SCRIPT 才有这一步,没配就压根不进重试计划。
配进来的路径一律当场变成绝对路径(`abspath`,不是 `resolve`)—— 检查和执行必须是
同一个路径,而且这一步**永远不许抛异常**:它跑在重试计划生成里,抛出去就是整条
重试链连同 index/extract 一起 500。坏配置有两种真实形态(指向自己的符号链接、
写错的用户名),它们打的是 `resolve()` 和 `expanduser()` 两处,后果一模一样,
所以下面按同一套跑。
"""

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kb import extract, pipeline_retry, store


class _FakeConn:
    """够用就行:retry_failed_plan 那条支路只 connect 再 close。"""

    def close(self):
        pass


class RefillScriptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("KB_REFILL_SCRIPT", None)
        self.stages = []
        stage = patch.object(pipeline_retry, "_stage",
                             lambda name, status, detail="", **kw:
                             self.stages.append((name, status, detail)))
        stage.start()
        self.addCleanup(stage.stop)

    def _plan(self, *, extract_detail='跳过 3(正文空,需回填)',
              stages: dict | None = None) -> list[str]:
        """算一次重试计划。

        计划为空时 retry_failed_plan 会去库里看提取积压 —— 这里把那条支路也挡掉,
        免得测试去碰开发机上真实的 data/kb.db。
        """
        last = {"stages": {"extract": {"status": "ok", "detail": extract_detail},
                           **(stages or {})}}
        with patch.object(pipeline_retry, "last_result", return_value=last), \
             patch.object(store, "connect", return_value=_FakeConn()), \
             patch.object(extract, "pending_docs", return_value=[]):
            return pipeline_retry.retry_failed_plan()

    def test_no_hardcoded_path_anywhere_in_the_module(self):
        """源码里不许再出现写死的私人路径。"""
        source = Path(pipeline_retry.__file__).read_text(encoding="utf-8")
        # `Path.home()` 是那类路径的通用形状,`gzh_refill_body` 是具体那一个
        for bad in ("gzh_refill_body", "Path.home()"):
            self.assertNotIn(bad, source)

    def test_unset_means_the_step_does_not_exist(self):
        """没配:既不进计划,跑到了也只是 skipped,提示里写的是环境变量名。"""
        self.assertIsNone(pipeline_retry.refill_script())
        self.assertNotIn("refill", self._plan())
        pipeline_retry._run_refill()
        self.assertEqual(1, len(self.stages))
        name, status, detail = self.stages[0]
        self.assertEqual(("export", "skipped"), (name, status))
        self.assertIn("KB_REFILL_SCRIPT", detail)

    def test_configured_but_missing_is_reported_not_silently_dropped(self):
        """配了却指不到是配置错误:仍然进计划,并把那条路径说出来。"""
        missing = Path(self.tmp.name) / "nope.py"
        os.environ["KB_REFILL_SCRIPT"] = str(missing)
        self.assertIn("refill", self._plan())
        pipeline_retry._run_refill()
        name, status, detail = self.stages[0]
        self.assertEqual(("export", "skipped"), (name, status))
        self.assertIn(str(missing), detail)

    def test_a_directory_is_not_a_script(self):
        """指到一个目录上同样是配置错了:别等子进程报一句看不懂的话。"""
        os.environ["KB_REFILL_SCRIPT"] = self.tmp.name
        pipeline_retry._run_refill()
        name, status, detail = self.stages[0]
        self.assertEqual(("export", "skipped"), (name, status))
        self.assertIn(self.tmp.name, detail)

    def test_configured_and_present_runs_it(self):
        """配好了就真的跑它,而且跑的是配置里那一份。"""
        script = Path(self.tmp.name) / "refill.py"
        script.write_text("print('没有找到空正文文件')\n", encoding="utf-8")
        os.environ["KB_REFILL_SCRIPT"] = str(script)
        self.assertEqual(script, pipeline_retry.refill_script())
        self.assertIn("refill", self._plan())
        pipeline_retry._run_refill()
        self.assertEqual("running", self.stages[0][1])
        self.assertEqual(("export", "ok"), self.stages[-1][:2])
        self.assertIn("没有空正文文件", self.stages[-1][2])

    def test_tilde_is_expanded(self):
        """`~/...` 这种写法得认,不然配上去永远指不到。"""
        os.environ["KB_REFILL_SCRIPT"] = "~/refill.py"
        self.assertEqual(Path.home() / "refill.py", pipeline_retry.refill_script())

    def test_relative_path_in_a_subdir_really_runs(self):
        """子目录相对路径要真的跑起来 —— 这条钉的是检查和执行用同一个路径。

        原来 `refill_script()` 只 expanduser:`tools/refill.py` 的存在性检查在当前
        目录下通过,子进程却带着 `cwd=tools/` 再找同一个相对路径,打开的是
        `tools/tools/refill.py`,阶段落 `error / 回填脚本异常`。所以这里不打桩
        subprocess,真起一个 python3 看它到底开到了哪个文件。
        """
        tools = Path(self.tmp.name) / "tools"
        tools.mkdir()
        (tools / "refill.py").write_text("print('没有找到空正文文件')\n",
                                         encoding="utf-8")
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.tmp.name)
        os.environ["KB_REFILL_SCRIPT"] = "tools/refill.py"
        self.assertTrue(pipeline_retry.refill_script().is_absolute())
        pipeline_retry._run_refill()
        self.assertEqual(("export", "ok"), self.stages[-1][:2])
        self.assertIn("没有空正文文件", self.stages[-1][2])

    # ——— 坏配置:两种形态,一条口径 ———

    def _bad_configs(self) -> dict[str, str]:
        """两种真实会遇到的坏配置。幂等,可以在同一个用例里反复取。

        循环链接打的是 `resolve()`(`RuntimeError: Symlink loop`),写错的用户名打的是
        `expanduser()`(`RuntimeError: Could not determine home directory.`)—— 两处都在
        `refill_script()` 里,抛出去就是整条重试链连同 index / extract 一起 500。
        """
        loop = Path(self.tmp.name) / "loop.py"
        if not loop.is_symlink():
            loop.symlink_to(loop)
        return {"指向自己的符号链接": str(loop),
                "写错的用户名": "~knowbase_nonexistent_user_87921/refill.py"}

    def test_a_bad_path_is_a_config_error_not_an_exception(self):
        """取路径不许抛;阶段落 skipped,提示里是用户自己配的那一行。

        而且**不许连坐**:index / extract 照样进计划 —— 异常从计划生成冒出去的话,
        这一轮什么都重试不了,而这只是一个可选外挂配错了。
        """
        for label, raw in self._bad_configs().items():
            with self.subTest(label):
                self.stages.clear()
                os.environ["KB_REFILL_SCRIPT"] = raw
                got = pipeline_retry.refill_script()   # 不抛
                self.assertIsNotNone(got)
                pipeline_retry._run_refill()
                name, status, detail = self.stages[0]
                self.assertEqual(("export", "skipped"), (name, status))
                self.assertIn(raw, detail)
                self.assertEqual(
                    ["refill", "index", "extract"],
                    self._plan(stages={"index": {"status": "error", "detail": "挂了"}}))

    def test_no_configured_string_can_make_it_raise(self):
        """契约就一句话:配进来什么字符串,它只返回路径、永不抛。

        连着两轮审查栽的都是这一条(`resolve()` 的循环链接、`expanduser()` 的用户名),
        每次只补一种形态等于等下一种。所以这里直接按契约摆一桌怪字符串。
        """
        loop = Path(self.tmp.name) / "loop.py"
        if not loop.is_symlink():
            loop.symlink_to(loop)
        # 带 NUL 的字符串不在表里:`os.environ.__setitem__` 自己就拒绝它,
        # 这个值压根到不了我们手上。超长名在:`is_file()` 对 ENAMETOOLONG 照抛。
        hostile = ["", "   ", "~", "~/refill.py", "~nouser87921/refill.py",
                   "~nouser87921", str(loop), ".", "..", "/", "-",
                   "/nonexistent/refill.py", "tools/refill.py", "x" * 4096]
        for raw in hostile:
            with self.subTest(raw=raw[:40]):
                os.environ["KB_REFILL_SCRIPT"] = raw
                got = pipeline_retry.refill_script()   # 不抛就是全部要求
                self.assertTrue(got is None or isinstance(got, Path))
                self.stages.clear()
                pipeline_retry._run_refill()           # 这一步也不许抛
                self.assertEqual("skipped", self.stages[0][1])

    def _retry_over_http(self, *, running: bool):
        """起一个只挂了重试路由的 app,真发一次 `POST /api/pipeline/retry-failed`。

        回填那一步是**真的**(就是要看它在坏配置下落 skipped);index / extract 换成
        观察器 —— 它们要碰库和模型,而这里要证明的只有一件事:它们到底有没有被跑到。
        """
        from copy import deepcopy

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from kb import pipeline_state as pstate
        from kb.web import pipeline_api

        saved = deepcopy(pstate._state)
        self.addCleanup(lambda: (pstate._state.clear(), pstate._state.update(saved)))
        ran: list[str] = []
        last = {"stages": {"extract": {"status": "ok",
                                       "detail": "跳过 3(正文空,需回填)"},
                           "index": {"status": "error", "detail": "挂了"}}}
        app = FastAPI()
        app.include_router(pipeline_api.router)
        # **起跑核对的两边都要 patch**:`last_result()` 和锁内那道核对走同一份口径
        # (`_latest_locked`),只换其中一个的话手工摆的这份快照跟另一边岔开,核对会
        # 如实报「这期间又跑完了一轮」。这一局测的是坏配置下别的阶段还能不能起。
        with patch.object(pstate, "_STATE_FILE", Path(self.tmp.name) / "state.json"), \
             patch.object(pipeline_retry, "last_result", return_value=last), \
             patch.object(pipeline_retry, "_latest_locked", return_value=last), \
             patch.object(pipeline_retry, "_run_index", lambda: ran.append("index")), \
             patch.object(pipeline_retry, "_run_extract", lambda: ran.append("extract")):
            # 每次调用是独立的一轮:身份也要一起清掉。留着上一轮的收尾时间的话,
            # patch 出来的那份快照(没有身份)会被起跑那道并发核对认成「这期间又跑
            # 完了一轮」—— 那道核对是对的,是这里的摆局该干净。
            pstate._state.update(running=running, run_id="", finished_at="")
            client = TestClient(app, raise_server_exceptions=False)
            r = client.post("/api/pipeline/retry-failed")
            if r.status_code == 200:   # 真起了一条线程,等它收尾
                deadline = time.monotonic() + 10
                while pstate._state["running"] and time.monotonic() < deadline:
                    time.sleep(0.02)
        return r, ran

    def test_a_bad_config_still_starts_the_other_retries(self):
        """管道空闲时真点一次:200,而且 index / extract 真的跑到了。

        这条才是用户按下「重试失败项」时走的那条路 —— 只断言 409 的话,证明的仅是
        「计划算得出来」,证明不了坏配置下别的阶段还能正常启动。
        """
        for label, raw in self._bad_configs().items():
            with self.subTest(label):
                self.stages.clear()
                os.environ["KB_REFILL_SCRIPT"] = raw
                r, ran = self._retry_over_http(running=False)
                self.assertEqual(200, r.status_code, r.text)
                self.assertEqual("refill → index → extract", r.json()["plan"])
                self.assertEqual(["index", "extract"], ran)
                self.assertIn(("export", "skipped"),
                              [(n, st) for n, st, _ in self.stages])

    def test_the_retry_endpoint_never_500s_on_a_bad_refill_config(self):
        """管道忙着时同样不许 500:计划照样算得出来,按 409 如实收尾。

        这一条守的是计划生成那一段 —— 它在取 `_lock` **之前**就真跑过了,抛就是 500。
        """
        for label, raw in self._bad_configs().items():
            with self.subTest(label):
                os.environ["KB_REFILL_SCRIPT"] = raw
                r, ran = self._retry_over_http(running=True)
                self.assertEqual(409, r.status_code, r.text)
                self.assertIn("已在运行", r.json().get("detail", ""))
                self.assertEqual([], ran)


if __name__ == "__main__":
    unittest.main()
