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

import re
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import aggregate, extract, scanner, search, store  # noqa: E402


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


class DerivedEntryTest(unittest.TestCase):
    """hard 原文拆出来的条目也要挡住。

    条目化产物是另一条 document,自己的 promo_level 永远是空的:只挡原文的话,
    存量补判之后会出现「原文被挡了、从这篇广告拆出来的工具条目还在搜索结果和
    实体榜里」,等于没抛弃(第 1 轮审查发现)。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self._tmp.name) / "kb.db")
        rows = [
            # path, source, title, promo_level, url
            ("ad.md", "dr-gzh", "广告原文", "hard", "https://e.com/ad"),
            ("ad-tool.md", "dr-gzh-projects", "广告拆出的条目", "", "https://e.com/ad"),
            ("ok.md", "dr-gzh", "正常文章", "none", "https://e.com/ok"),
            ("ok-tool.md", "dr-gzh-projects", "正常条目", "", "https://e.com/ok"),
            # url 为空的文档:不能因为某篇 hard 没有 url 就被连坐
            ("noUrl.md", "dr", "没有链接的文档", "", ""),
        ]
        for path, source, title, level, url in rows:
            self.conn.execute(
                "INSERT INTO documents (path, source, title, promo_level, url,"
                " published) VALUES (?,?,?,?,?,'2026-09-16 10:00')",
                (path, source, title, level, url))
            cur = self.conn.execute(
                "INSERT INTO chunks (path, seq, text) VALUES (?,0,?)",
                (path, "Pavo 平台 免费 领取 注册"))
            self.conn.execute(
                "INSERT INTO chunks_fts (chunk_id, title_tok, text_tok)"
                " VALUES (?,?,?)",
                (cur.lastrowid, store.tokenize(title),
                 store.tokenize("Pavo 平台 免费 领取 注册")))
            self.conn.execute(
                "INSERT INTO entities (path, entity, kind, norm)"
                " VALUES (?,'Pavo','product','pavo')", (path,))
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_search_drops_derived_entry(self):
        paths = {h.path for h in search.search(self.conn, "Pavo 平台",
                                               use_vector=False, top_k=10)}
        self.assertNotIn("ad.md", paths)
        self.assertNotIn("ad-tool.md", paths)
        self.assertIn("ok.md", paths)
        self.assertIn("ok-tool.md", paths)

    def test_entity_enumeration_drops_derived_entry(self):
        docs = [d["path"] for item in aggregate.enumerate_entities(
            self.conn, topic="Pavo", limit=10) for d in item["docs"]]
        self.assertNotIn("ad.md", docs)
        self.assertNotIn("ad-tool.md", docs)
        self.assertIn("ok-tool.md", docs)

    def test_document_enumeration_drops_derived_entry(self):
        docs = [d["path"] for d in aggregate.enumerate_documents(
            self.conn, source="gzh", limit=50)]
        self.assertNotIn("ad.md", docs)
        self.assertNotIn("ad-tool.md", docs)
        self.assertIn("ok.md", docs)

    def test_blank_url_not_collaterally_dropped(self):
        # 把那篇空 url 的文档判成 hard:其它空 url 文档不能跟着消失
        self.conn.execute(
            "INSERT INTO documents (path, source, title, promo_level, url,"
            " published) VALUES ('adNoUrl.md','dr-gzh','空链接广告','hard','',"
            " '2026-09-16 10:00')")
        self.conn.commit()
        docs = [d["path"] for d in aggregate.enumerate_documents(
            self.conn, limit=50)]
        self.assertIn("noUrl.md", docs)
        self.assertNotIn("adNoUrl.md", docs)

    def test_opt_out_returns_everything(self):
        paths = {h.path for h in search.search(
            self.conn, "Pavo 平台", search.Filters(exclude_promo=False),
            use_vector=False, top_k=10)}
        self.assertIn("ad.md", paths)
        self.assertIn("ad-tool.md", paths)


class BackfillWritebackTest(unittest.TestCase):
    """补判判出 hard 时要把 flag 回写进 md。

    只写 doc_tags 留不住:下次 upsert_document 会整表删掉再按 frontmatter 重建
    (第 1 轮审查发现)。
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import promo_backfill
        self.mod = promo_backfill
        self._tmp = tempfile.TemporaryDirectory()
        self.conn = store.connect(Path(self._tmp.name) / "kb.db")
        self.conn.execute(
            "INSERT INTO documents (path, source, title, content_hash)"
            " VALUES ('a.md','dr-gzh','广告','old')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def test_writes_tag_into_frontmatter(self):
        vault = Path(self._tmp.name) / "vault"
        (vault / "").mkdir(parents=True, exist_ok=True)
        md = vault / "a.md"
        md.write_text('---\ntitle: "广告"\ntags:\n  - source/dr-gzh\n---\n正文\n',
                      encoding="utf-8")
        with mock.patch.object(extract, "VAULT_ROOT", vault):
            self.mod._flag_in_vault(self.conn, "a.md", "hard", False)
        self.assertIn(extract.PROMO_TAG, md.read_text(encoding="utf-8"))
        # 回写改了文件,hash 要跟着刷新,否则下次 index 白白重建
        self.assertNotEqual(self.conn.execute(
            "SELECT content_hash FROM documents WHERE path='a.md'"
        ).fetchone()["content_hash"], "old")

    def test_missing_file_does_not_raise(self):
        vault = Path(self._tmp.name) / "empty"
        vault.mkdir(parents=True, exist_ok=True)
        with mock.patch.object(extract, "VAULT_ROOT", vault):
            self.mod._flag_in_vault(self.conn, "gone.md", "hard", False)  # 不抛


class ReclassifyVaultTest(unittest.TestCase):
    """误判 hard 之后改判,md 里那行 flag 也得摘掉。

    writeback_tags 只会追加。DB 侧改判时 doc_tags 清了,但 frontmatter 还留着
    那行——这篇文件之后任何一次改动触发 upsert_document,doc_tags 会按
    frontmatter 重建,误判的标记就又回到库里(第 2 轮审查发现)。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vault = Path(self._tmp.name) / "vault"
        self.vault.mkdir()
        self.md = self.vault / "a.md"
        self.md.write_text(
            '---\ntitle: "广告"\ntags:\n  - source/dr-gzh\n---\n正文\n',
            encoding="utf-8")
        self.conn = store.connect(Path(self._tmp.name) / "kb.db")
        self.conn.execute(
            "INSERT INTO documents (path, source, title, content_hash, url)"
            " VALUES ('a.md','dr-gzh','广告','old','u')")
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _reindex(self):
        """模拟下一轮 index:doc_tags 按 md 当前的 frontmatter 重建。"""
        tags = re.findall(r"^\s*-\s+(\S+)\s*$", self.md.read_text(encoding="utf-8"),
                          re.M)
        doc = SimpleNamespace(path="a.md", source="dr-gzh", title="广告",
                              account="号", published="", url="u",
                              content_hash="new", tags=tags)
        store.upsert_document(self.conn, doc, [], "t2")
        self.conn.commit()
        return [r["tag"] for r in self.conn.execute(
            "SELECT tag FROM doc_tags WHERE path='a.md' ORDER BY tag")]

    def _judge(self, level: str):
        with mock.patch.object(extract, "VAULT_ROOT", self.vault):
            stale = extract.save_promo(self.conn, "a.md",
                                       {"level": level, "reason": "r"})
            extract.sync_promo_tag(self.conn, "a.md", level, stale)
        self.conn.commit()
        return stale

    def test_flag_removed_from_markdown_on_reclassify(self):
        self._judge("hard")
        self.assertIn(extract.PROMO_TAG, self.md.read_text(encoding="utf-8"))

        self.assertTrue(self._judge("none"))          # 需要摘 md 里那行
        text = self.md.read_text(encoding="utf-8")
        self.assertNotIn(extract.PROMO_TAG, text)
        self.assertIn("source/dr-gzh", text)          # 别的 tag 不能被误删
        self.assertIn("正文", text)                   # 正文不碰

        self.assertNotIn(extract.PROMO_TAG, self._reindex())   # 重索引不再带回

    def test_reindex_would_resurrect_without_cleanup(self):
        # 反证:只清 DB、不动 md 时,重索引会把标记带回来
        self._judge("hard")
        self.conn.execute("DELETE FROM doc_tags WHERE path='a.md' AND tag=?",
                          (extract.PROMO_TAG,))
        self.conn.commit()
        self.assertIn(extract.PROMO_TAG, self._reindex())

    def test_no_file_touch_when_never_hard(self):
        # 没判过 hard 的文章不该为了「摘 flag」去读写文件
        before = self.md.read_text(encoding="utf-8")
        self.assertFalse(self._judge("soft"))
        self.assertEqual(self.md.read_text(encoding="utf-8"), before)

    def test_save_extraction_reports_stale_flag(self):
        self._judge("hard")
        stale = extract.save_extraction(self.conn, "a.md", {
            "entities": [], "topics": [], "summary": "改判",
            "promo": {"level": "none", "reason": "不是广告"}})
        self.assertTrue(stale)

    def test_remove_tag_is_noop_on_missing_file(self):
        with mock.patch.object(extract, "VAULT_ROOT", self.vault / "nope"):
            self.assertFalse(
                extract.remove_writeback_tag("a.md", extract.PROMO_TAG))


class ExtractEntrypointsTest(unittest.TestCase):
    """三条提取路对 md 里的 flag/软广 必须一视同仁。

    管道之外还有两条用户直接跑的:`kb extract --writeback` 和
    scripts/extract_parallel.py(自称与前者等价)。漏接软广同步的话,hard
    写不进 frontmatter(重索引丢掉展示层标记)、改判后又摘不掉(重索引把
    误判带回 doc_tags)——第 1、2 轮防的正是这两种漂移,第 3 轮审查发现它们
    还留在这两条旁路上。所以这里对两个入口跑同一组断言。
    """

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        self._tmp = None
        self._fresh()

    def tearDown(self):
        self._tmp.cleanup()

    def _fresh(self):
        """每个入口各跑一遍,之间要把临时库和 vault 清空重来。"""
        if self._tmp is not None:
            self._tmp.cleanup()
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.db = self.root / "kb.db"

    def _seed(self, level: str, tags: list[str]) -> Path:
        md = self.vault / "a.md"
        md.write_text('---\ntitle: "文"\ntags:\n'
                      + "".join(f"  - {t}\n" for t in tags) + "---\n正文\n",
                      encoding="utf-8")
        conn = store.connect(self.db)
        # content_hash 要是**真**的那一份:生产里这一列只有 `scanner.file_hash`
        # 一个口径(索引写、`refresh_content_hash` 刷),写个假值等于摆出一个
        # 「索引还没扫到当前这份 md」的状态 —— `save_extraction` 的正文版本校验
        # 会如实把这次结果作废,而这条用例测的压根不是那件事。
        conn.execute("INSERT INTO documents (path, source, title, content_hash,"
                     " promo_level) VALUES ('a.md','dr-gzh','文',?,?)",
                     (scanner.file_hash(md.read_text(encoding="utf-8")), level))
        conn.execute("INSERT INTO chunks (path, seq, text) VALUES ('a.md',0,'正文')")
        conn.commit()
        conn.close()
        return md

    def _run(self, entry: str, llm_level: str):
        data = {"entities": [{"name": "某产品", "kind": "product"}],
                "topics": ["ai"], "summary": "s",
                "promo": {"level": llm_level, "reason": "r"}}
        fake = lambda *a, **kw: dict(data)          # noqa: E731
        opened = [store.connect(self.db)]
        with mock.patch.object(extract, "VAULT_ROOT", self.vault), \
                mock.patch.object(store, "connect",
                                  lambda *a, **kw: opened[0]):
            if entry == "cli":
                from kb import cli
                with mock.patch.object(extract, "extract_with_llm", fake):
                    cli.cmd_extract(SimpleNamespace(limit=0, writeback=True))
            else:
                import extract_parallel
                with mock.patch.object(extract_parallel, "extract_with_llm", fake):
                    extract_parallel.main()
        # 入口自己会 close;重开一条读结果
        conn = store.connect(self.db)
        level = conn.execute(
            "SELECT promo_level FROM documents WHERE path='a.md'").fetchone()[0]
        conn.close()
        return level

    def _entries(self):
        return ("cli", "parallel")

    def test_hard_is_written_into_frontmatter(self):
        for entry in self._entries():
            with self.subTest(entry=entry):
                self._fresh()
                md = self._seed("", ["source/dr-gzh"])
                self.assertEqual(self._run(entry, "hard"), "hard")
                text = md.read_text(encoding="utf-8")
                self.assertIn(extract.PROMO_TAG, text)
                self.assertIn("topic/ai", text)      # 常规 tag 照写

    def test_reclassify_removes_flag_from_frontmatter(self):
        for entry in self._entries():
            with self.subTest(entry=entry):
                self._fresh()
                md = self._seed("hard", ["source/dr-gzh", extract.PROMO_TAG])
                self.assertEqual(self._run(entry, "none"), "none")
                text = md.read_text(encoding="utf-8")
                self.assertNotIn(extract.PROMO_TAG, text)
                self.assertIn("source/dr-gzh", text)
                self.assertIn("正文", text)

    def test_promo_tag_is_not_slugged(self):
        # flag/软广 已是最终形态,跟着 topic/entity 过一遍 _tag_slug 会被改写
        tags = extract.extraction_tags(
            {"topics": ["AI Agent"], "entities": [{"name": "Claude Code"}],
             "promo": {"level": "hard"}})
        self.assertIn(extract.PROMO_TAG, tags)
        self.assertEqual(tags[-1], extract.PROMO_TAG)

    def test_nothing_to_write_skips_file_io(self):
        # 没 tag 又没改判时不该去读文件:补判 8000 多篇时这是纯浪费
        conn = store.connect(self.db)
        with mock.patch.object(extract, "VAULT_ROOT", self.root / "nope"):
            self.assertFalse(extract.writeback_extraction(
                conn, "missing.md", {"topics": [], "entities": [],
                                     "promo": {"level": "none"}}, False))
        conn.close()


if __name__ == "__main__":
    unittest.main()
