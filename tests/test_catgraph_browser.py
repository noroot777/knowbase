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

# A 热、近、有仓库;B 冷、旧、没仓库——四个筛选各能把 B 单独挡住一次。
# A、B 的简介里都有 SHARED,C 没有:搜 SHARED 时 B 不会被搜索挡住,用来搭
# 「目标仍命中搜索词、只被另一项挡着」的组合;C 则用来看放宽有没有放过头。
TODAY = date.today().isoformat()
OLD = (date.today() - timedelta(days=400)).isoformat()
SHARED = "共同词"
SHARED_ART = "https://x/same-article"    # 同一篇文章 → A、B 之间有一条共现边


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
                       latest=TODAY, summary=f"{SHARED} 甲",
                       arts=[(SHARED_ART, "同一篇", TODAY),
                             ("https://x/a2", "另一篇", TODAY),
                             ("https://x/a3", "又一篇", TODAY)]),
                _group("bbb", "BBB工具", url="", latest=OLD,
                       summary=f"{SHARED} 乙",
                       arts=[(SHARED_ART, "同一篇", OLD)]),
                # 不含公共词、也不和谁共现:放宽放过头了它就会冒出来
                _group("ccc", "CCC工具", url="https://github.com/x/ccc",
                       latest=TODAY, summary="丙",
                       arts=[("https://x/c1", "丙一", TODAY),
                             ("https://x/c2", "丙二", TODAY)]),
            ], use_llm=True, max_new=None, model="stub", workers=1, out_dir=out_dir)
        finally:
            T._llm = old_llm
            T.CACHE_DIR = old_cache
        cls.page = out_dir / B.VIEWS["projects"]["out"]
        cls.probed = cls._probe(["search", "win", "heat", "repo", "none",
                                 "q+win", "q+heat", "q+repo", "enter", "ime"])

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    @classmethod
    def _probe(cls, filters: list[str]) -> dict:
        """一次浏览器启动跑完所有筛选(每个之间重新加载页面)。"""
        r = subprocess.run(
            [shutil.which("node"), str(PROBE), str(cls.page), ",".join(filters),
             "AAA工具", "BBB工具", SHARED],
            capture_output=True, text=True, timeout=300,
            env={**os.environ, "HEADLESS_SHELL": str(SHELL)})
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        return json.loads(r.stdout.strip().splitlines()[-1])

    def _landed(self, s: dict) -> None:
        """不管哪种筛选,跳过去之后都得真的落在 B 上。"""
        self.assertTrue(s["hiddenFirst"], "这一轮的前提没了:B 没被挡住")
        self.assertTrue(s["hasButton"], "面板里没有指向 B 的关联按钮")
        self.assertEqual(s["panel"], "BBB工具")
        # 原来的坏状态:面板是 B,可见 pill 全灰,一个选中的都没有
        self.assertEqual(s["visibleSelected"], 1, f"跳到了一个被筛掉的节点上({s})")
        self.assertGreater(s["edgePaths"], 0, "没有连线(选中项不在页面上)")
        self.assertLess(s["visibleDimmed"], s["visiblePills"], "所有可见 pill 都被压暗了")

    def test_jumping_to_a_filtered_out_neighbour_opens_it_for_real(self):
        for filt in ("search", "win", "heat", "repo"):
            with self.subTest(filter=filt):
                s = self.probed[filt]
                self._landed(s)
                # 挡路的那一项被放开了,而且控件上看得见
                self.assertEqual(s["query"], "")
                self.assertEqual(s["winOn"], "0")
                self.assertEqual(s["heatOn"], "1")
                self.assertFalse(s["repoOn"])

    def test_a_search_that_still_matches_the_target_is_left_alone(self):
        """组合筛选下只解除真正挡住目标的那一项:搜索词仍命中就别动它。

        复审第 2 轮:`relax()` 的搜索分支原来只看 `F.q` 空不空,不看目标是不是
        还命中它。于是「搜 Claude + 近 30 天 → 点 Bun」(Bun 的简介里就有
        Claude,挡住它的只有时间窗)会把搜索词一起清掉,结果从 18 个当场炸回
        3187 个,和同文件里那句「只放开挡路的那几项」正相反。
        """
        for filt, seg in (("q+win", "winOn"), ("q+heat", "heatOn"),
                          ("q+repo", "repoOn")):
            with self.subTest(filter=filt):
                s = self.probed[filt]
                self._landed(s)
                self.assertEqual(s["query"], SHARED,
                                 f"目标仍命中搜索词,搜索却被清掉了({s})")
                # 只有挡路的那一项回位,另外两项本来就没开
                self.assertEqual(s["winOn"], "0")
                self.assertEqual(s["heatOn"], "1")
                self.assertFalse(s["repoOn"])
                # 搜索还在 → 不含公共词的 C 不该冒出来(A + B 两个)
                self.assertEqual(s["shown"], "2",
                                 f"放宽放过头了,筛选结果从 {s['shownFirst']} 涨到 "
                                 f"{s['shown']}({s})")

    def test_an_unfiltered_jump_still_works(self):
        """没有任何筛选时照旧:这道保护不能顺手改掉正常路径。"""
        s = self.probed["none"]
        self.assertFalse(s["hiddenFirst"], "没筛选却把 B 藏了")
        self.assertEqual(s["panel"], "BBB工具")
        self.assertEqual(s["visibleSelected"], 1)
        self.assertGreater(s["edgePaths"], 0)

    def test_replacing_the_query_and_hitting_enter_right_away_opens_the_new_hit(self):
        """换个搜索词立刻回车:回车得按**刚敲的那个词**找,不是上一个。

        复审第 3 轮:`F.q` 是防抖 120ms 之后才更新的,而回车分支里的 `match()`
        读的就是它。已经搜着一个词、换成另一个再立刻回车时,目标被上一个搜索
        条件排除,回车什么也没打开;120ms 后防抖把列表换成新词,还顺手把旧选中
        项 reset 掉——最后停在「列表是新词、面板关着、一个选中的都没有」。
        """
        s = self.probed["enter"]
        self.assertTrue(s["hasButton"], "这一轮的前提没了:第一次回车没打开 A")
        self.assertTrue(s["hiddenFirst"], "这一轮的前提没了:A 的搜索词没挡住 B")
        self.assertEqual(s["query"], "BBB工具")
        self.assertTrue(s["panelShown"], f"回车没打开新结果({s})")
        self.assertEqual(s["panel"], "BBB工具", f"回车按旧搜索词找的({s})")
        self.assertEqual(s["visibleSelected"], 1)

    def test_an_ime_enter_that_only_confirms_a_candidate_opens_nothing(self):
        """输入法用 Enter 确认候选的那一下,不算「搜这个词」。

        复审第 4 轮:中日韩输入法在组合阶段就用 Enter 选词,浏览器给页面的事件
        带 `isComposing=true`。不排掉的话,选第一个词时按的回车就把详情面板打开
        了——词还没敲完。组合结束(compositionend)之后的普通回车才该生效。
        """
        s = self.probed["ime"]
        self.assertTrue(s["eventWasComposing"], "这一轮的前提没了:事件不在组合态")
        self.assertFalse(s["midPanelShown"], f"确认候选的回车把详情打开了({s})")
        self.assertTrue(s["panelShown"], f"组合结束后的回车反倒不管用了({s})")
        self.assertEqual(s["panel"], "BBB工具")
        self.assertEqual(s["visibleSelected"], 1)


if __name__ == "__main__":
    unittest.main()
