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
import shutil
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
        # 建完立刻登记删除:下面每一步都可能抛,而这个目录归这条用例
        self.addCleanup(shutil.rmtree, tmp, True)
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
    """wiki_daily 非零 → 阶段落 error,详情里点名哪张图挂了、根因是什么。"""

    STDOUT = ("universe: 0 篇已提取文章,0 个实体页\n"
              "linkify: 新双链化 3 篇\n"
              "digest: 生成 1 天 ['2026-09-22']\n"
              "synthesis: 更新 2 个实体 []\n"
              "index: 已重渲染\n"
              "graph: ok\n"
              "catgraph: FAIL QUALITY_GATE_REJECTED\n")

    TRACEBACK = ("Traceback (most recent call last):\n"
                 '  File "scripts/wiki/build_catgraph.py", line 182, in main\n'
                 "    build_one(k, groups, ...)\n"
                 '  File "scripts/wiki/graph_data.py", line 61, in load_groups\n'
                 "    conn.execute(sql)\n"
                 "RuntimeError: ROOT_CAUSE_DATABASE_READ_ONLY\n"
                 "wiki_daily: catgraph 退出码 1(图谱未更新)\n")

    def _run(self, stdout: str, rc: int, stderr: str = ""):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        stub = tmp / "wiki_daily_stub.py"
        stub.write_text(
            f"import sys\nsys.stdout.write({stdout!r})\n"
            f"sys.stderr.write({stderr!r})\nsys.exit({rc})\n",
            encoding="utf-8")
        calls, logs = [], []
        with mock.patch.object(PS, "WIKI_DAILY", stub), \
                mock.patch.object(PS, "_log", logs.append), \
                mock.patch.object(PS, "_stage",
                                  lambda *a, **k: calls.append((a, k))):
            PS._run_wiki()
        args, kw = calls[-1]
        return SimpleNamespace(name=args[0], status=args[1], detail=args[2],
                               failures=kw.get("failures") or [],
                               logs="\n".join(logs))

    def test_catgraph_failure_marks_stage_error(self):
        r = self._run(self.STDOUT, 1)
        self.assertEqual((r.name, r.status), ("wiki", "error"))
        self.assertIn("catgraph", r.detail)
        self.assertIn("QUALITY_GATE_REJECTED", r.detail)

    def test_success_detail_keeps_graph_lines(self):
        ok = self.STDOUT.replace("catgraph: FAIL QUALITY_GATE_REJECTED",
                                 "catgraph: ok")
        r = self._run(ok, 0)
        self.assertEqual((r.name, r.status), ("wiki", "ok"))
        self.assertIn("双链化 3 篇", r.detail)
        self.assertIn("图谱 ok", r.detail)

    def test_traceback_root_cause_survives(self):
        """多行 traceback:stdout 那边只剩标题,根因得从 stderr 末行捞出来。"""
        out = self.STDOUT.replace("catgraph: FAIL QUALITY_GATE_REJECTED",
                                  "catgraph: FAIL Traceback (most recent call last):")
        r = self._run(out, 1, self.TRACEBACK)
        self.assertEqual(r.status, "error")
        self.assertIn("catgraph", r.detail)                       # 哪张图
        self.assertIn("ROOT_CAUSE_DATABASE_READ_ONLY", r.detail)  # 根因
        # wiki_daily 自己那句汇总不是根因,不能拿它当摘要
        self.assertNotIn("退出码", r.detail)
        # 完整 stderr 既进日志,也进面板的失败明细
        self.assertIn("ROOT_CAUSE_DATABASE_READ_ONLY", r.logs)
        self.assertIn('File "scripts/wiki/graph_data.py"', r.logs)
        self.assertEqual(len(r.failures), 1)
        self.assertIn("catgraph", r.failures[0]["title"])
        self.assertIn("ROOT_CAUSE_DATABASE_READ_ONLY", r.failures[0]["reason"])

    def test_long_error_keeps_the_label(self):
        """报错正文超长时,截的是正文,不能把开头的 catgraph 标签截掉。"""
        out = self.STDOUT.replace("QUALITY_GATE_REJECTED", "X" * 400)
        r = self._run(out, 1)
        self.assertEqual(r.status, "error")
        self.assertIn("catgraph", r.detail)
        self.assertTrue(r.detail.startswith("catgraph 失败:"), r.detail[:40])

    def test_both_graphs_failed_are_both_named(self):
        out = (self.STDOUT.replace("graph: ok", "graph: FAIL BOOM")
               .replace("QUALITY_GATE_REJECTED", "X" * 400))
        r = self._run(out, 1)
        self.assertIn("graph/catgraph 失败", r.detail)


if __name__ == "__main__":
    unittest.main()
