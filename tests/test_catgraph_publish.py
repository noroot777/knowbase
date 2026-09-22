# -*- coding: utf-8 -*-
"""发布这一步:不作数的判定不许发出去,页面落盘必须是原子的。

两条都是「没有旧页面可保」时暴露出来的(2026-09-22 第 1 轮审查发现):

1. 拦截原来写成 `not res.complete and out.exists()`——首次生成、页面被人删了或
   产物丢了的时候,一份已经判定「不作数」的结果照样会被写成 0 节点的新页面,
   还以退出码 0 收场,`wiki_daily.run_step()` 看到的是成功,管道面板全绿。
   没有旧页面可保 ≠ 坏结果可以发。
2. 页面是直接 `write_text` 到正式产物上的。磁盘满、进程被杀、写到一半抛异常,
   都会先把好页面截成半截再报错——缓存那头的两阶段提交完好,kb-web 却已经在
   拿一份坏 HTML 对外服务。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts" / "wiki"))

import build_catgraph as B  # noqa: E402
import taxonomy as T  # noqa: E402

from tests.test_catgraph import _group  # noqa: E402


class PublishGateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        old = T.CACHE_DIR
        T.CACHE_DIR = Path(self.tmp.name)
        self.addCleanup(lambda: setattr(T, "CACHE_DIR", old))
        self.out_dir = Path(self.tmp.name) / "vault"
        self.out_dir.mkdir()
        self.page = self.out_dir / B.VIEWS["skills"]["out"]
        self.groups = [_group(f"n{i}", f"名{i}", topics=["skill工具"])
                       for i in range(3)]

    def _stub(self, code: str):
        """模型桩:每条都回同一个 code。"""
        def fake(prompt, model, **kw):
            rows = re.findall(r"^(\d+)\. (\S+) ::", prompt, re.M)
            return json.dumps([{"id": int(i), "c": code} for i, _nm in rows])

        T._llm, old = fake, T._llm
        self.addCleanup(lambda: setattr(T, "_llm", old))

    def _build(self, **kw):
        return B.build_one("skills", self.groups, use_llm=True, max_new=None,
                           model="stub", workers=1, out_dir=self.out_dir, **kw)

    # ── ① 不作数的判定不许发 ────────────────────────────────────────────────
    def test_void_verdict_never_publishes_a_first_page(self):
        """每条都判成 X(不是 skill)= 这一趟坏了,没有旧页也不许发、要非零退出。"""
        self._stub("X")
        with self.assertRaises(SystemExit) as cm:
            self._build()
        self.assertIn("不作数", str(cm.exception))
        self.assertFalse(self.page.exists(), "不作数的判定被写成新页面了")
        self.assertFalse(T.cache_path(T.SKILLS).exists())
        self.assertFalse(T.staging_path(T.SKILLS).exists())

    def test_all_fallback_verdict_never_publishes_a_first_page(self):
        """全落「待归类」也是同一回事(fallback 是合法 code,更容易漏)。"""
        self._stub(T.SKILLS.fallback)
        with self.assertRaises(SystemExit):
            self._build()
        self.assertFalse(self.page.exists())

    def test_good_first_run_does_publish(self):
        """正常判定当然要发出来:上面那道闸不能把首次生成一起挡了。"""
        self._stub("S1")
        self._build()
        self.assertTrue(self.page.exists())
        self.assertEqual(B.P.page_stats(self.page),
                         {"nodes": 3, "classified": 3})

    # ── ② 页面原子落盘 ─────────────────────────────────────────────────────
    def _boom_on_page(self):
        """把「写页面的临时文件」做成写一截就炸,其它写盘(缓存)照常。"""
        real = Path.write_text

        def boom(self_, text, *a, **kw):
            if ".html." in self_.name and self_.name.endswith(".tmp"):
                real(self_, "PARTIAL")            # 先落一截,模拟磁盘满
                raise OSError("simulated disk full")
            return real(self_, text, *a, **kw)

        return mock.patch.object(Path, "write_text", boom)

    def test_write_failure_leaves_the_old_page_byte_identical(self):
        self._stub("S1")
        self._build()
        good = self.page.read_bytes()
        T.cache_path(T.SKILLS).unlink()          # 丢缓存,逼它重判出不同的一版
        self._stub("S2")
        with self._boom_on_page(), self.assertRaises(OSError):
            self._build()
        self.assertEqual(self.page.read_bytes(), good, "旧页面被写坏了")
        self.assertEqual(sorted(p.name for p in self.out_dir.iterdir()),
                         [self.page.name], "临时文件没清掉")

    def test_write_failure_does_not_commit_the_cache(self):
        """页面没落盘 = 这一轮不算数,正式缓存也不能提交(下轮重来)。"""
        self._stub("S1")
        with self._boom_on_page(), self.assertRaises(OSError):
            self._build()
        self.assertFalse(self.page.exists())
        self.assertFalse(T.cache_path(T.SKILLS).exists(),
                         "页面没写成却把判定提交进正式缓存了")


if __name__ == "__main__":
    unittest.main()
