# -*- coding: utf-8 -*-
"""「☆ 收藏此段」在四处正文上都收得下、点得回去。

这个功能原来只长在 `/doc/…` 阅读页里。日报正文、问答回答、订阅阅读器、图鉴条目
各渲染一份正文,却都没有这颗按钮 —— 而前三者**在 documents 表里没有行**(日报
写在 vault 的 `daily/` 下,那个目录不在任何 `SOURCES.root` 底下;回答压根不是
文件),所以这件事不是前端加个按钮:`favorites.path` 原来是
`NOT NULL REFERENCES documents(path)`,没有 path 的片段一条都存不下去。

判据一律落在**后果**上:库里那条收藏是什么(origin / origin_ref / path / quote)、
点回去之后屏幕上是哪一份正文、那段话有没有被高亮 —— 不落在「按钮出现了没有」
这一步。按钮浮出来而存不进去是这条链最容易留下的那种半成品,而那一步**自测过**
(选中空态建议按钮和页头标题时按钮照旧浮出来、一点就报错)。

库那一头(可空 path 的迁移、四类来源、LEFT JOIN 与计数一致、行动卡与分诊)在
`tests/test_fav_snippet_store.py`,纯 Python、不要浏览器。两份合起来才覆盖完
这条链;硬约束和事故形态在 `docs/decisions/fav-snippets.md`。

驱动是 CDP 直连 chrome-headless-shell(不装 playwright、不连网、始终无头、临时
profile),口径在 `tests/headless.py`,几个浏览器回归共用。数据由那个沙箱脚本真走
`put_items → export → index` 摆出来,另外真写一份 `vault/daily/<日期>.md` 和一轮
问答;沙箱连同它的临时库由 `fixture_server` 起和收(所有权见 `tests/fixture_root.py`)。
"""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).resolve().parent / "snip_probe.mjs"
# 第二份探针验的是另一类事:**收下来那条收藏后来还作不作数**(多行代码存成了
# 一行、一字不差重复两次的整段高亮落在第一处、订阅那篇 md 还是上一版、问答这
# 一轮其实没落库)。分两份跑是因为**顺序有意义**:第一份里有几条判据数的是
# 收藏面板上有几张卡、几个角标,第二份会往同一个库里再加好几条收藏 —— 反过来
# 跑的话那几条数量判据就不是在数它们要数的东西了。两份共用同一个沙箱服务
# (摆盘贵),共用外壳 `snip_probe_lib.mjs`。
EDGES = Path(__file__).resolve().parent / "snip_probe_edges.mjs"
# 第三份只管一件事:**存下来的到底是不是屏幕上那份文字**(`<br>` 和段落边界在
# DOM 文本节点里一个字符都不占,两行会接成一行),外带「选完了」那三种输入方式。
# 它和第二份分开是因为那两件事的形态不一样:第二份是"当场成功、后来不作数",
# 这一份是"当场看着全对、粘出来才发现不是那段话"。**排在第二份之前**:第二份
# 最后一节会把那个对话真删掉,而这一份要在问答正文上选字。
SHOWN = Path(__file__).resolve().parent / "snip_probe_shown.mjs"
# 第三份管**绑得对不对**:位置绑在哪一处(两侧的位置证据只听了一半 → 高亮落在一处
# 另一侧已经排除掉的地方)、版本绑在哪一版(核对和落库之间正文换了一版 → 收藏固化
# 在一篇再也找不到那句话的文档上)。**排在最后**:它会真改掉盘上那两篇 md
# (沙箱独有的 `/__snip__/touch`),前两份有判据按"盘上那篇就是屏幕上这份"摆盘。
BIND = Path(__file__).resolve().parent / "snip_probe_bind.mjs"

sys.path.insert(0, str(REPO))

from tests.headless import (  # noqa: E402
    fixture_server, probe_env, requires_headless, run_probe)


@requires_headless
class SnippetFavoriteEverywhereTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # 沙箱服务、端口、那份临时库全部交给 `fixture_server`。**建完立刻登记
        # `addClassCleanup`**:`setUpClass` 抛异常时 unittest 不调 `tearDownClass`,
        # 只跑 `doClassCleanups` —— 于是"起服务 → 跑探针 → 解析回包"整段都在同一个
        # 清理域里,包括"回包非空但不是合法 JSON"那一支。
        cls.stack = contextlib.ExitStack()
        cls.addClassCleanup(cls.stack.close)
        # manifest 不放沙箱根里:探针读它,它的生死该跟这条用例一致
        tmp = cls.stack.enter_context(
            tempfile.TemporaryDirectory(prefix="kb-snip-manifest-"))
        manifest = Path(tmp) / "snip.json"
        # 摆盘(导出 + 建索引 + 写日报 + 落一轮问答)在起服务之前跑完,等得比
        # 别的沙箱久
        base = cls.stack.enter_context(fixture_server(
            "--manifest", str(manifest), script="serve_snippets.py", timeout=180))
        node = shutil.which("node")
        run = run_probe([node, str(PROBE), base, str(manifest)],
                        timeout=420, env=probe_env())
        if not run.stdout.strip():
            raise AssertionError(run.stderr)
        # 探针往 stdout 只写一次(整份 JSON,缩进 1),所以整段解析而不是取末行
        cls.out = json.loads(run.stdout)
        shown = run_probe([node, str(SHOWN), base, str(manifest)],
                          timeout=420, env=probe_env())
        if not shown.stdout.strip():
            raise AssertionError(shown.stderr)
        cls.shown = json.loads(shown.stdout)
        # 第二份接着跑(顺序有意义,见上面 EDGES 那段注释)
        edges = run_probe([node, str(EDGES), base, str(manifest)],
                          timeout=420, env=probe_env())
        if not edges.stdout.strip():
            raise AssertionError(edges.stderr)
        cls.edges = json.loads(edges.stdout)
        bind = run_probe([node, str(BIND), base, str(manifest)],
                         timeout=420, env=probe_env())
        if not bind.stdout.strip():
            raise AssertionError(bind.stderr)
        cls.bind = json.loads(bind.stdout)

    def test_snippets_can_be_taken_and_jumped_back_to_everywhere(self):
        results = self.out["results"]
        self.assertTrue(results, "探针一条都没跑")
        for r in results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])

    def test_a_saved_snippet_still_holds_up_afterwards(self):
        """收下来那条收藏后来还作不作数(原文保真、落点、绑定、真落库)。

        和上面那条分开记:这几条红的时候,功能在屏幕上是**好的** —— 按钮弹了、
        收藏成功了、面板上那条也在 —— 不好的是库里存的那几个字、高亮落在第几
        处、刷新之后还在不在。混进上面那条的话,报告里看不出是"收不下"还是
        "收下来了但不作数"。
        """
        results = self.edges["results"]
        self.assertTrue(results, "反例探针一条都没跑")
        for r in results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])

    def test_the_quote_is_what_was_on_the_screen(self):
        """库里那条引文,逐字就是屏幕上选中的那份文字。

        和上面两条分开记:这几条红的时候,收藏**当场是成功的**、面板上那条也在、
        点回去还落得对 —— 不好的是那几个字本身(屏幕上分了两行,库里是接成一行
        的那一份)。混进别条的话,报告里看不出是"收不下"还是"收下来的不是那段话"。
        """
        results = self.shown["results"]
        self.assertTrue(results, "显示文本探针一条都没跑")
        for r in results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])

    def test_a_snippet_binds_to_the_right_place_and_version(self):
        """位置绑在哪一处、版本绑在哪一版。

        和上面两条分开记:这几条红的时候,收藏**当场是成功的**、库里那条也在 ——
        不好的是点回去高亮落在另一处(而三处的文字一模一样,页面上看不出来),
        或者那条收藏绑的那一篇在落库那一刻就已经是另一版正文了(回看 0 个高亮)。
        混进上面两条的话,报告里看不出是"收不下"、"收下来不作数"还是"绑错了"。
        """
        results = self.bind["results"]
        self.assertTrue(results, "绑定探针一条都没跑")
        for r in results:
            with self.subTest(check=r["name"]):
                self.assertTrue(r["ok"], r["detail"])

    def test_no_page_error_along_the_way(self):
        """页面里抛出来的异常单独报一条。

        混进上面那条的话,一个「某处 undefined」会被当成某个判据不成立,而真因在
        别的地方 —— 收藏链上多数半成品的形态恰恰是"按钮响应了、脚本抛了、库里没
        写进去",屏幕上看不出来。
        """
        self.assertEqual([], self.out["pageErrors"])
        self.assertEqual([], self.shown["pageErrors"])
        self.assertEqual([], self.edges["pageErrors"])
        self.assertEqual([], self.bind["pageErrors"])


if __name__ == "__main__":
    unittest.main()
