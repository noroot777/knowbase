# -*- coding: utf-8 -*-
"""图谱子步骤失败要一路传到管道面板上。

钉住的是一条「坏了也全绿」的链路:`wiki_daily.py` 起子进程生成三张图
(graph / graph-projects / graph-skills),原来只把 `catgraph: FAIL ...` 打进
stdout 就继续往下走,最后照样 `WIKI_DAILY_DONE`、退出码 0;而 kb 管道的
`_run_wiki()` 只看这个总退出码,于是质量验收拒绝写页面、模型配置错、写盘失败
全都显示成 `wiki: ok`——图谱长期停在旧版本,面板上一点痕迹都没有
(2026-09-22 第 8 轮审查发现)。

所以两头各钉一半:子进程非零 → wiki_daily 非零退出(且前面的步骤照常做完、
另一张图照样尝试);wiki_daily 非零 → 阶段落 error,且详情里点名是哪张图。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import wiki_daily as WD  # noqa: E402

from kb import pipeline_stages as PS  # noqa: E402

OK = "import sys\nsys.exit(0)\n"
BAD = "import sys\nsys.stderr.write('QUALITY_GATE_REJECTED\\n')\nsys.exit(1)\n"


class _FakeUniverse:
    docs: list = []
    page_keys: list = []


class WikiDailyExitTest(unittest.TestCase):
    """子进程非零 → wiki_daily 非零退出。"""

    def _run(self, graph: str, catgraph: str):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "build_graph_html.py").write_text(graph, encoding="utf-8")
        (tmp / "build_catgraph.py").write_text(catgraph, encoding="utf-8")
        vault = tmp / "vault"
        vault.mkdir()
        fake_w = SimpleNamespace(
            connect_ro=lambda: None,
            Universe=lambda con: _FakeUniverse(),
            VAULT=vault,
            render_index=lambda u, v: "# index\n",
            append_log=lambda *a: None,
        )
        out, err, code = io.StringIO(), io.StringIO(), None
        with mock.patch.object(WD, "HERE", tmp), \
                mock.patch.object(WD, "W", fake_w), \
                mock.patch.object(WD, "step_pages", lambda u: (0, 0)), \
                mock.patch.object(sys, "argv",
                                  ["wiki_daily", "--skip-linkify", "--skip-llm"]), \
                redirect_stdout(out), redirect_stderr(err):
            try:
                WD.main()
            except SystemExit as e:              # 非零退出走这儿
                code = e.code
        return out.getvalue(), err.getvalue(), code

    def test_all_ok(self):
        out, _, code = self._run(OK, OK)
        self.assertIsNone(code)
        self.assertIn("graph: ok", out)
        self.assertIn("catgraph: ok", out)
        self.assertIn("WIKI_DAILY_DONE", out)

    def test_catgraph_failure_exits_nonzero(self):
        out, err, code = self._run(OK, BAD)
        self.assertNotIn(code, (None, 0))
        self.assertIn("catgraph: FAIL QUALITY_GATE_REJECTED", out)
        self.assertIn("QUALITY_GATE_REJECTED", err)      # 完整 stderr 进日志
        self.assertNotIn("WIKI_DAILY_DONE", out)         # 没做完就别说做完了
        self.assertIn("graph: ok", out)                  # 前面的步骤照常完成

    def test_graph_failure_does_not_skip_catgraph(self):
        out, _, code = self._run(BAD, OK)
        self.assertNotIn(code, (None, 0))
        self.assertIn("graph: FAIL", out)
        self.assertIn("catgraph: ok", out)               # 一张挂了另一张照样生成

    def test_both_failures_are_reported(self):
        out, _, code = self._run(BAD, BAD)
        self.assertNotIn(code, (None, 0))
        self.assertEqual(str(code).count("退出码"), 2)     # 两张都点名,不是只报第一张
        self.assertIn("catgraph", str(code))


class RunWikiStageTest(unittest.TestCase):
    """wiki_daily 非零 → 阶段落 error,详情里点名哪张图挂了。"""

    STDOUT = ("universe: 0 篇已提取文章,0 个实体页\n"
              "linkify: 新双链化 3 篇\n"
              "digest: 生成 1 天 ['2026-09-22']\n"
              "synthesis: 更新 2 个实体 []\n"
              "index: 已重渲染\n"
              "graph: ok\n"
              "catgraph: FAIL QUALITY_GATE_REJECTED\n")

    def _stage_of(self, stdout: str, rc: int):
        tmp = Path(tempfile.mkdtemp())
        stub = tmp / "wiki_daily_stub.py"
        stub.write_text(
            f"import sys\nsys.stdout.write({stdout!r})\nsys.exit({rc})\n",
            encoding="utf-8")
        calls = []
        with mock.patch.object(PS, "WIKI_DAILY", stub), \
                mock.patch.object(PS, "_stage",
                                  lambda *a, **k: calls.append(a)):
            PS._run_wiki()
        return calls[-1]

    def test_catgraph_failure_marks_stage_error(self):
        name, status, detail = self._stage_of(self.STDOUT, 1)
        self.assertEqual((name, status), ("wiki", "error"))
        self.assertIn("catgraph: FAIL QUALITY_GATE_REJECTED", detail)

    def test_success_detail_keeps_graph_lines(self):
        ok = self.STDOUT.replace("catgraph: FAIL QUALITY_GATE_REJECTED",
                                 "catgraph: ok")
        name, status, detail = self._stage_of(ok, 0)
        self.assertEqual((name, status), ("wiki", "ok"))
        self.assertIn("双链化 3 篇", detail)
        self.assertIn("图谱 ok", detail)


if __name__ == "__main__":
    unittest.main()
