# -*- coding: utf-8 -*-
"""正文回填脚本是可选外挂,路径不许写死在代码里。

原来是 `REFILL_SCRIPT = Path.home() / "code" / "gongzhonghao" / "gzh_refill_body.py"`
——仓库里没有这个脚本,它是维护者自己机器上的一份存量修补工具。优雅降级成 skipped
所以不崩,但每个外部用户每次点「重试失败项」都会看到一行指向别人家目录的提示,而
这个功能对他们永远不可用。

现在的口径:配了 KB_REFILL_SCRIPT 才有这一步,没配就压根不进重试计划。
"""

import os
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

    def _plan(self, *, extract_detail='跳过 3(正文空,需回填)') -> list[str]:
        """算一次重试计划。

        计划为空时 retry_failed_plan 会去库里看提取积压 —— 这里把那条支路也挡掉,
        免得测试去碰开发机上真实的 data/kb.db。
        """
        last = {"stages": {"extract": {"status": "ok", "detail": extract_detail}}}
        with patch.object(pipeline_retry, "load_last_run", return_value=last), \
             patch.object(store, "connect", return_value=_FakeConn()), \
             patch.object(extract, "pending_docs", return_value=[]):
            return pipeline_retry.retry_failed_plan()

    def test_no_hardcoded_path_anywhere_in_the_module(self):
        """源码里不许再出现写死的私人路径。"""
        source = Path(pipeline_retry.__file__).read_text(encoding="utf-8")
        for bad in ("gongzhonghao", "gzh_refill_body", "Path.home()"):
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


if __name__ == "__main__":
    unittest.main()
