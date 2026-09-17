# -*- coding: utf-8 -*-
"""soft 软广那一档的处理:检索降权 + 各展示面打标记。

hard 是直接抛弃(不进检索、不进枚举、不进图鉴),soft 不同——那一档是厂商
自家通稿、带返利链接的推荐,技术信息通常还有用,所以**留着但往后排**,同时
在界面上标出来让人自己判断。

钉住的几条:
1. **降权发生在截断 top_k 之前**。排完再降只是在这 k 篇里换位置,被压下去的
   soft 仍然占着名额,排在 k+1 的中立文章永远补不上来——这是最容易写错的一步。
2. **降的是名次不是生死**。RRF 的分数挤在窄带里,乘 0.85 约等于往后挪 11 名;
   一篇明显更相关的 soft 还是该排在勉强相关的中立文章前面。
3. **衍生条目按 url 继承原文的档位**。条目自己永远没判过软广,不继承的话
   「原文标了、拆出来的条目干干净净」,标记等于没加(挡 hard 时同理)。
4. **图鉴也得管**。它按 vault 文件聚合、不走 search/aggregate,那条统一排除
   口径覆盖不到——一篇 hard 投放稿拆出来的工具条目原来照样在图鉴里占一行。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import projects, search, store            # noqa: E402


def _doc(conn, path, level="", url="", source="dr-gzh"):
    conn.execute(
        "INSERT INTO documents (path, source, title, url, promo_level)"
        " VALUES (?, ?, '标题', ?, ?)", (path, source, url, level))


class EffectiveLevelTest(unittest.TestCase):
    """有效档位:自己的判定,没有就按 url 从原文继承。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self._tmp.name) / "kb.db")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _levels(self, *paths):
        return store.effective_promo_levels(self.conn, list(paths))

    def test_own_verdict_wins(self):
        _doc(self.conn, "a.md", "soft", "u1")
        _doc(self.conn, "b.md", "none", "u2")
        self.conn.commit()
        self.assertEqual(self._levels("a.md", "b.md"), {"a.md": "soft"})

    def test_derived_entry_inherits_soft(self):
        # 条目化产物自己的 promo_level 永远是空的,靠同 url 的原文
        _doc(self.conn, "src.md", "soft", "u1")
        _doc(self.conn, "entry.md", "", "u1", source="dr-gzh-projects")
        self.conn.commit()
        self.assertEqual(self._levels("entry.md")["entry.md"], "soft")

    def test_blank_url_does_not_inherit(self):
        # 库里几十篇没有 url 的文档,少了这条守卫会互相继承
        _doc(self.conn, "src.md", "soft", "")
        _doc(self.conn, "other.md", "", "")
        self.conn.commit()
        self.assertEqual(self._levels("other.md"), {})

    def test_heaviest_level_wins(self):
        # 同一个 url 挂多篇时取最重的一档:宁可多提示一次
        _doc(self.conn, "s.md", "soft", "u1")
        _doc(self.conn, "h.md", "hard", "u1")
        _doc(self.conn, "e.md", "", "u1", source="dr-gzh-projects")
        self.conn.commit()
        self.assertEqual(self._levels("e.md")["e.md"], "hard")

    def test_batches_over_sqlite_variable_limit(self):
        for i in range(450):
            _doc(self.conn, f"p{i}.md", "soft", f"u{i}")
        self.conn.commit()
        got = self._levels(*[f"p{i}.md" for i in range(450)])
        self.assertEqual(len(got), 450)


class _SearchDB(unittest.TestCase):
    """两篇文档 + 各一个 chunk;召回顺序由测试直接指定,不跟 bm25 较劲。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self._tmp.name) / "kb.db")
        self.cid = {}
        for path, level in (("s.md", "soft"), ("n.md", "none")):
            _doc(self.conn, path, level, path)
            cur = self.conn.execute(
                "INSERT INTO chunks (path, seq, text) VALUES (?, 0, '正文')", (path,))
            self.cid[path] = cur.lastrowid
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _search(self, order, top_k=10):
        """order:召回名次,第 0 名在最前。"""
        legs = [(self.cid[p], p) for p in order]
        with mock.patch.object(search, "_fts_leg", lambda *a, **kw: legs):
            return search.search(self.conn, "正文", top_k=top_k, use_vector=False)


class SoftDemotionTest(_SearchDB):
    def test_soft_loses_to_adjacent_none(self):
        # soft 召回第 1、none 第 2:降权后应该换位
        hits = self._search(["s.md", "n.md"])
        self.assertEqual([h.path for h in hits], ["n.md", "s.md"])

    def test_demotion_happens_before_truncation(self):
        # 只要 1 条时必须是 none。降权若放在排序截断之后,这里会返回 soft ——
        # 被压下去的那篇仍然占着名额,n.md 永远补不上来
        hits = self._search(["s.md", "n.md"], top_k=1)
        self.assertEqual([h.path for h in hits], ["n.md"])

    def test_clearly_better_soft_still_wins(self):
        # 降的是名次不是生死:none 落到第 16 名时,soft 照样排前面。
        # 中间那些占位 path 在库里不存在,只用来把 n.md 的名次推下去,
        # 拼 Hit 时会被跳过,所以 top_k 放宽到 30 让两篇真文档都留下。
        legs = [(self.cid["s.md"], "s.md")] + \
               [(-i - 1, f"x{i}.md") for i in range(14)] + \
               [(self.cid["n.md"], "n.md")]
        with mock.patch.object(search, "_fts_leg", lambda *a, **kw: legs):
            hits = search.search(self.conn, "正文", top_k=30, use_vector=False)
        self.assertEqual([h.path for h in hits], ["s.md", "n.md"])

    def test_score_is_the_demoted_one(self):
        # 报原分的话列表里会出现「下面一条分数更高」
        hits = self._search(["s.md", "n.md"])
        self.assertGreater(hits[0].score, hits[1].score)

    def test_hit_carries_level(self):
        hits = self._search(["s.md", "n.md"])
        lv = {h.path: h.promo_level for h in hits}
        self.assertEqual(lv["s.md"], "soft")
        self.assertEqual(lv["n.md"], "")     # none 不打标记

    def test_weight_is_a_rank_shift(self):
        # 系数与名次的换算写在 SOFT_PROMO_WEIGHT 的注释里,改值时这条会提醒
        shift = (1 / search.SOFT_PROMO_WEIGHT - 1) * (search.RRF_K + 1)
        self.assertTrue(5 < shift < 20, f"降权幅度 {shift:.1f} 名,过松或过狠")


class GalleryPromoTest(unittest.TestCase):
    """图鉴按 vault 文件聚合,不走 search/aggregate —— 得自己过一遍判定。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "kb.db"
        conn = store.connect(self.db)
        _doc(conn, "e_hard.md", "", "u_hard", source="dr-gzh-projects")
        _doc(conn, "e_soft.md", "", "u_soft", source="dr-gzh-projects")
        _doc(conn, "e_ok.md", "", "u_ok", source="dr-gzh-projects")
        _doc(conn, "src_hard.md", "hard", "u_hard")
        _doc(conn, "src_soft.md", "soft", "u_soft")
        _doc(conn, "src_ok.md", "none", "u_ok")
        conn.commit()
        conn.close()
        self.entries = [{"path": p, "name": "工具"}
                        for p in ("e_hard.md", "e_soft.md", "e_ok.md")]

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self):
        real = store.connect      # _apply_promo 调的是无参 store.connect()
        with mock.patch.object(store, "connect", lambda *a, **kw: real(self.db)):
            return projects._apply_promo(list(self.entries))

    def test_hard_entry_dropped_soft_marked(self):
        kept = self._apply()
        self.assertEqual([e["path"] for e in kept], ["e_soft.md", "e_ok.md"])
        self.assertEqual(kept[0]["promo_level"], "soft")
        self.assertEqual(kept[1]["promo_level"], "")

    def test_db_failure_degrades_to_keeping_everything(self):
        # 图鉴不该因为查不到库就整个打不开
        def boom(*a, **kw):
            raise RuntimeError("库没了")
        with mock.patch.object(store, "connect", boom):
            kept = projects._apply_promo(list(self.entries))
        self.assertEqual(len(kept), 3)


if __name__ == "__main__":
    unittest.main()
