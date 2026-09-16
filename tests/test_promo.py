# -*- coding: utf-8 -*-
"""软广判定(kb.extract 的 promo 一档)的回归测试。

不打网络:judge/extract 的 LLM 调用全部打桩,只验「判定结果怎么落库、落库之后
谁会照着它改行为」。

钉住的几条:
1. **认不出的判定一律落 none**。软广判定是宁可漏不可错杀——错杀会让一篇正常
   文章连同它的实体一起从条目化和检索里消失,漏判只是留着一篇广告。
2. **改判要能改回来**。hard → none 时 flag/软广 这个 tag 必须跟着摘掉,
   否则面板上永远挂着一个摘不掉的标记。
3. **pre_tagged 来源不能把已有判定抹成空**。parse_pretagged 的产物里没有
   promo 字段,save_extraction 要是跟 summary 一起写,源头判过的结果就没了。
4. **软广排除不能让每次检索都放大 KNN**。_vec_leg 按「有没有过滤条件」决定
   KNN 候选数,软广子句混进 _filter_sql 会让无过滤的普通查询也跑 4000 的 KNN。
5. **同稿归组看标题 + 长度**。光看标题会把「量子位编辑作者招聘」这类撞名的
   不同文章判成同一篇。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import extract, search, store  # noqa: E402


class _DB(unittest.TestCase):
    """一个临时库 + 两篇文档的公共夹具。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self._tmp.name) / "kb.db")
        for path, title in (("a.md", "正常文章"), ("b.md", "广告稿")):
            self.conn.execute(
                "INSERT INTO documents (path, source, title, account) "
                "VALUES (?, 'dr-gzh', ?, '某号')", (path, title))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _level(self, path: str) -> str:
        return self.conn.execute(
            "SELECT promo_level FROM documents WHERE path=?", (path,)
        ).fetchone()["promo_level"]

    def _has_tag(self, path: str) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM doc_tags WHERE path=? AND tag=?",
            (path, extract.PROMO_TAG)).fetchone() is not None


class NormalizeTest(unittest.TestCase):
    def test_unknown_falls_back_to_none(self):
        for raw in (None, {}, "不知道", {"level": "maybe"}, 42, []):
            self.assertEqual(extract.normalize_promo(raw)["level"], "none")

    def test_accepts_bare_string(self):
        self.assertEqual(extract.normalize_promo("hard")["level"], "hard")

    def test_keeps_levels_and_trims_reason(self):
        got = extract.normalize_promo({"level": "SOFT ", "reason": "x" * 400})
        self.assertEqual(got["level"], "soft")
        self.assertEqual(len(got["reason"]), 200)


class ClipBodyTest(unittest.TestCase):
    def test_short_body_untouched(self):
        self.assertEqual(extract.clip_body("短文"), "短文")

    def test_long_body_keeps_head_and_tail(self):
        body = "头" * 9000 + "扫码领取"
        got = extract.clip_body(body)
        # 文末的转化钩子是软广判定的主要依据,截断后必须还在
        self.assertIn("扫码领取", got)
        self.assertTrue(got.startswith("头" * 100))
        self.assertLess(len(got), len(body))

    def test_promo_window_is_smaller(self):
        body = "正" * 9000 + "尾"
        full = extract.clip_body(body)
        lite = extract.clip_body(body, extract.PROMO_HEAD, extract.PROMO_TAIL)
        self.assertLess(len(lite), len(full))
        self.assertIn("尾", lite)


class SavePromoTest(_DB):
    def test_hard_writes_level_and_tag(self):
        extract.save_promo(self.conn, "b.md", {"level": "hard", "reason": "注册引导"})
        self.assertEqual(self._level("b.md"), "hard")
        self.assertTrue(self._has_tag("b.md"))

    def test_reclassify_removes_tag(self):
        extract.save_promo(self.conn, "b.md", {"level": "hard", "reason": "x"})
        extract.save_promo(self.conn, "b.md", {"level": "none", "reason": "改判"})
        self.assertEqual(self._level("b.md"), "none")
        self.assertFalse(self._has_tag("b.md"))

    def test_pretagged_extraction_keeps_existing_verdict(self):
        extract.save_promo(self.conn, "b.md", {"level": "hard", "reason": "x"})
        # parse_pretagged 的产物:没有 promo 字段
        extract.save_extraction(self.conn, "b.md", {
            "entities": [], "topics": [], "summary": "条目摘要"})
        self.assertEqual(self._level("b.md"), "hard")

    def test_extraction_with_promo_writes_through(self):
        extract.save_extraction(self.conn, "a.md", {
            "entities": [], "topics": [], "summary": "s",
            "promo": {"level": "hard", "reason": "投放稿"}})
        self.assertEqual(self._level("a.md"), "hard")
        self.assertTrue(self._has_tag("a.md"))


class PendingTest(_DB):
    def test_only_unjudged_docs(self):
        extract.save_promo(self.conn, "a.md", {"level": "none", "reason": ""})
        rows = extract.pending_promo_docs(self.conn, ["dr-gzh", "gzh"])
        self.assertEqual([r["path"] for r in rows], ["b.md"])

    def test_other_sources_untouched(self):
        self.conn.execute(
            "INSERT INTO documents (path, source, title) VALUES ('c.md','dr','x')")
        rows = extract.pending_promo_docs(self.conn, ["dr-gzh", "gzh"])
        self.assertNotIn("c.md", [r["path"] for r in rows])


class SearchFilterTest(unittest.TestCase):
    def test_promo_clause_not_in_filter_sql(self):
        # _vec_leg 拿 _filter_sql 的返回值判断「召回被缩到小子集了没」,
        # 软广子句混进去会让无过滤的普通查询也跑放大版 KNN
        where, params = search._filter_sql(search.Filters())
        self.assertEqual(where, "")
        self.assertEqual(params, [])

    def test_promo_sql_default_on(self):
        self.assertIn("promo_level", search._promo_sql(search.Filters()))
        self.assertEqual(
            search._promo_sql(search.Filters(exclude_promo=False)), "")

    def test_excludes_hard_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            conn = store.connect(Path(tmp) / "kb.db")
            try:
                for path, level in (("n.md", "none"), ("s.md", "soft"),
                                    ("h.md", "hard")):
                    conn.execute(
                        "INSERT INTO documents (path, source, title, promo_level)"
                        " VALUES (?, 'dr-gzh', '同一个词', ?)", (path, level))
                    cur = conn.execute(
                        "INSERT INTO chunks (path, seq, text) VALUES (?, 0, ?)",
                        (path, "同一个词 出现在正文里"))
                    conn.execute(
                        "INSERT INTO chunks_fts (chunk_id, title_tok, text_tok)"
                        " VALUES (?, ?, ?)",
                        (cur.lastrowid, store.tokenize("同一个词"),
                         store.tokenize("同一个词 出现在正文里")))
                conn.commit()
                hits = search.search(conn, "同一个词", use_vector=False)
                paths = {h.path for h in hits}
                self.assertEqual(paths, {"n.md", "s.md"})   # soft 留着,hard 挡掉
                hits = search.search(conn, "同一个词",
                                     search.Filters(exclude_promo=False),
                                     use_vector=False)
                self.assertIn("h.md", {h.path for h in hits})
            finally:
                conn.close()


class GateShortCircuitTest(unittest.TestCase):
    """条目化 gate 读判定结果短路——软广省的是 gate + transcribe 两次调用。"""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import gzh_projects_export
        self.mod = gzh_projects_export

    def test_hard_is_rejected_without_llm(self):
        got = self.mod._promo_gate({"promo_level": "hard"})
        self.assertIsNotNone(got)
        self.assertFalse(got["project_article"])
        self.assertEqual(got["by"], "promo")

    def test_others_go_to_llm(self):
        for level in ("", "none", "soft"):
            self.assertIsNone(self.mod._promo_gate({"promo_level": level}))


class GroupingTest(unittest.TestCase):
    """补判脚本的同稿归组。"""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import promo_backfill
        self.mod = promo_backfill

    def _c(self, path, title, n):
        return {"path": path, "title": title, "account": "x", "body": "正" * n}

    def test_same_title_same_length_grouped(self):
        groups = self.mod._group([self._c("a.md", "同一篇广告", 1000),
                                  self._c("b.md", "同一篇广告", 1050)])
        self.assertEqual(len(groups), 1)

    def test_same_title_different_length_split(self):
        # 撞名但长度差一截的是两篇不同文章,归错组会张冠李戴
        groups = self.mod._group([self._c("a.md", "招聘", 300),
                                  self._c("b.md", "招聘", 9000)])
        self.assertEqual(len(groups), 2)

    def test_representative_is_longest(self):
        # 转发版常被砍掉尾部的推广段,而那正是判定依据,所以代表取最长的
        groups = self.mod._group([self._c("short.md", "稿", 1000),
                                  self._c("long.md", "稿", 1400)])
        self.assertEqual(groups[0][0]["path"], "long.md")


if __name__ == "__main__":
    unittest.main()
