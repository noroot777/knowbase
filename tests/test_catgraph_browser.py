# -*- coding: utf-8 -*-
"""筛选着的时候点「常一起出现」,不许跳到一个被筛掉的节点上。

面板里那排关联按钮是按共现关系列出来的,跟搜索词/时间窗/热度/「只看有仓库」
没有半点关系,点进去的目标十有八九不在当前结果里。原来直接 `select()`:
`body.hasSel` 把所有可见 pill 压到 opacity .2,而目标 pill 还挂着 `.hide`——
面板展示着一个不在结果里的条目,结果区一片灰、没有一个亮的,连线也画不出来
(2026-09-23 第 1 轮审查发现,线上 graph-projects.html 实测
`visibleSelected=0 visibleDimmed=152 edgePaths=0`)。

这里拿真页面在**无头 Chrome** 里跑一遍:真的筛选、真的点关联按钮、再读 DOM。
驱动是 CDP 直连 playwright 缓存里的 chrome-headless-shell(不装 playwright、
不连网、不开有头窗口);没有那个二进制就跳过。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import build_catgraph as B  # noqa: E402
import taxonomy as T  # noqa: E402

from tests.test_catgraph import _group  # noqa: E402

PROBE = Path(__file__).resolve().parent / "catgraph_probe.mjs"
SHELL = (Path.home() / "Library/Caches/ms-playwright/chromium_headless_shell-1243"
         / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell")

# A 热、近、有仓库;B 冷、旧、没仓库——四个筛选各能把 B 单独挡住一次
TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=400)).isoformat()
SHARED = "https://x/same-article"        # 同一篇文章 → 两者之间有一条共现边


@unittest.skipUnless(shutil.which("node"), "需要 node 驱动无头浏览器")
@unittest.skipUnless(SHELL.exists(), f"需要 chrome-headless-shell({SHELL})")
class RelatedJumpTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        old_cache = T.CACHE_DIR
        T.CACHE_DIR = root / "data"
        out_dir = root / "vault"
        out_dir.mkdir()

        def fake(prompt, model, **kw):
            import re
            rows = re.findall(r"^(\d+)\. (\S+) ::", prompt, re.M)
            return json.dumps([{"id": int(i), "c": "D1"} for i, _n in rows])

        old_llm, T._llm = T._llm, fake
        try:
            B.build_one("projects", [
                _group("aaa", "AAA工具", url="https://github.com/x/aaa",
                       latest=TODAY, summary="甲",
                       arts=[(SHARED, "同一篇", TODAY),
                             ("https://x/a2", "另一篇", TODAY),
                             ("https://x/a3", "又一篇", TODAY)]),
                _group("bbb", "BBB工具", url="", latest=OLD, summary="乙",
                       arts=[(SHARED, "同一篇", OLD)]),
            ], use_llm=True, max_new=None, model="stub", workers=1, out_dir=out_dir)
        finally:
            T._llm = old_llm
            T.CACHE_DIR = old_cache
        cls.page = out_dir / B.VIEWS["projects"]["out"]
        cls.probed = cls._probe(["search", "win", "heat", "repo", "none"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _probe(cls, filters: list[str]) -> dict:
        """一次浏览器启动跑完所有筛选(每个之间重新加载页面)。"""
        r = subprocess.run(
            [shutil.which("node"), str(PROBE), str(cls.page), ",".join(filters),
             "AAA工具", "BBB工具"],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "HEADLESS_SHELL": str(SHELL)})
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def test_jumping_to_a_filtered_out_neighbour_opens_it_for_real(self):
        for filt in ("search", "win", "heat", "repo"):
            with self.subTest(filter=filt):
                s = self.probed[filt]
                self.assertTrue(s["hiddenFirst"], "这一轮的前提没了:B 没被挡住")
                self.assertTrue(s["hasButton"], "面板里没有指向 B 的关联按钮")
                self.assertEqual(s["panel"], "BBB工具")
                # 原来的坏状态:面板是 B,可见 pill 全灰,一个选中的都没有
                self.assertEqual(s["visibleSelected"], 1,
                                 f"跳到了一个被筛掉的节点上({s})")
                self.assertGreater(s["edgePaths"], 0, "没有连线(选中项不在页面上)")
                self.assertLess(s["visibleDimmed"], s["visiblePills"],
                                "所有可见 pill 都被压暗了")
                # 挡路的那一项被放开了,而且控件上看得见
                self.assertEqual(s["query"], "")
                self.assertEqual(s["winOn"], "0")
                self.assertEqual(s["heatOn"], "1")
                self.assertFalse(s["repoOn"])

    def test_an_unfiltered_jump_still_works(self):
        """没有任何筛选时照旧:这道保护不能顺手改掉正常路径。"""
        s = self.probed["none"]
        self.assertFalse(s["hiddenFirst"], "没筛选却把 B 藏了")
        self.assertEqual(s["panel"], "BBB工具")
        self.assertEqual(s["visibleSelected"], 1)
        self.assertGreater(s["edgePaths"], 0)


if __name__ == "__main__":
    unittest.main()
