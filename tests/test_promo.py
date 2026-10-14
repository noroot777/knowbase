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
5. **同稿归组看账号 + 标题 + 长度**。光看标题会把「量子位编辑作者招聘」这类
   撞名的不同文章判成同一篇;不看账号会把搬运号抄走的稿子和原创那篇归成一组,
   搬运版判 hard 时把原创那篇一起广播掉。
6. **重判要有目标,而且不许改变默认那条路**。`--recheck` 覆盖已有判定,没有
   `--account` 就直接拒绝;不给 `--recheck` 时取数范围一个字不变。

跑:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

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


class RecheckScopeTest(_DB):
    """`--recheck` 的取数范围。判定口径改了之后存量靠它跟上。"""

    def _paths(self, **kw) -> list[str]:
        return [r["path"] for r in
                extract.pending_promo_docs(self.conn, ["dr-gzh", "gzh"], **kw)]

    def test_recheck_includes_already_judged(self):
        extract.save_promo(self.conn, "a.md", {"level": "none", "reason": ""})
        self.assertEqual(sorted(self._paths(recheck=True)), ["a.md", "b.md"])

    def test_default_still_skips_judged(self):
        # 反过来钉一边:不给 recheck 时行为一个字都不许变,否则每轮管道的
        # 存量补判会把全库判定重跑一遍
        extract.save_promo(self.conn, "a.md", {"level": "hard", "reason": ""})
        self.assertEqual(self._paths(), ["b.md"])

    def test_accounts_filter(self):
        self.conn.execute("INSERT INTO documents (path, source, title, account)"
                          " VALUES ('d.md','dr-gzh','搬运稿','搬运号')")
        self.assertEqual(self._paths(accounts=["搬运号"]), ["d.md"])

    def test_empty_accounts_means_no_filter(self):
        # 空列表当「没给」。解释成「哪个号都不要」的话,调用方一次参数失误会
        # 静默变成「一篇都没判」,而脚本照旧报成功退出
        self.assertEqual(sorted(self._paths(accounts=[])), ["a.md", "b.md"])


class RecheckGuardTest(unittest.TestCase):
    """`--recheck` 必须带 `--account`:无目标重判会盖掉全库历史结论。"""

    def setUp(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import promo_backfill
        self.mod = promo_backfill

    def test_recheck_without_account_refuses(self):
        with self.assertRaises(SystemExit):
            self.mod._scope_args(SimpleNamespace(account=None, recheck=True))

    def test_recheck_with_account_passes(self):
        self.assertEqual(
            self.mod._scope_args(SimpleNamespace(account=["GitHub猿"], recheck=True)),
            (["GitHub猿"], True))

    def test_plain_run_needs_no_account(self):
        # 反过来钉一边:不重判时没有 --account 照旧放行,守卫只管重判那条路
        self.assertEqual(
            self.mod._scope_args(SimpleNamespace(account=None, recheck=False)),
            ([], False))

    def test_absent_attributes_do_not_raise(self):
        """手工拼的 Namespace 不带这两个属性时也得能跑。

        `cmd_run` 有这样的调用方(回写时序那组回归直接拼 Namespace),
        新参数不该把既有调用方打成 AttributeError。
        """
        self.assertEqual(self.mod._scope_args(SimpleNamespace()), ([], False))


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

    def _c(self, path, title, n, tail=""):
        return {"path": path, "title": title, "account": "x",
                "body": "正" * n + tail}

    def test_identical_input_grouped(self):
        # 同账号 + 同标题 + 同一份正文 = 喂进去的 prompt 一个字节不差,
        # 结论当然共用 —— 这是归组唯一站得住的理由
        groups = self.mod._group([self._c("a.md", "同一篇广告", 1000),
                                  self._c("b.md", "同一篇广告", 1000)])
        self.assertEqual(len(groups), 1)

    def test_same_title_similar_length_different_body_split(self):
        """同题、长度落在同一个 500 字桶里、正文不同 —— 不许归到一组。

        这是旧键(账号 + 标题 + 长度桶)下真实发生过的误杀:一个号把「每日
        资讯」当标题反复用,一篇是开源教程、另一篇是代充值广告,广告那篇稍长
        当了代表,教程跟着被判 hard,连实体带条目一起从检索和图鉴里摘掉,而
        脚本报的是「软广 ×2」。
        """
        groups = self.mod._group([self._c("tut.md", "每日资讯", 910, "教程"),
                                  self._c("ad.md", "每日资讯", 910, "代充值")])
        self.assertEqual(len(groups), 2)

    def test_same_title_different_length_split(self):
        # 撞名但长度差一截的是两篇不同文章,归错组会张冠李戴
        groups = self.mod._group([self._c("a.md", "招聘", 300),
                                  self._c("b.md", "招聘", 9000)])
        self.assertEqual(len(groups), 2)

    def test_different_account_not_grouped(self):
        """搬运号抄走原创号的稿子,标题和长度都一样,结论不许互相广播。

        后果不对称:搬运版文末挂代充值导流判 hard,广播过去原创那篇就连实体
        带条目一起从检索和图鉴里消失,而 hard 不是「降权」是「摘掉」。
        """
        a = self._c("yuan.md", "谷歌Gemini 4开跑了", 3000)
        b = dict(self._c("ban.md", "谷歌Gemini 4开跑了", 3000), account="搬运号")
        groups = self.mod._group([a, b])
        self.assertEqual(len(groups), 2)
        self.assertEqual({g[0]["account"] for g in groups}, {"x", "搬运号"})


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

    def _candidate(self, vault, body="正文"):
        """摆一个**真实**候选:落库那道条件写比 `documents.content_hash`,
        夹具里对不上的话红出来的是「压根没走到回写」而不是回写本身。

        候选本身从 `store.doc_input_snapshot` 取 —— 和 `_candidates` 同一条
        语句。手写字典的话,交付层哪天多要一样输入(标题、账号都进 prompt)
        夹具就对不上,红出来的是 `KeyError`,证明不了被测的行为。
        """
        md = vault / "a.md"
        text = md.read_text(encoding="utf-8")
        self.conn.execute("UPDATE documents SET content_hash=? WHERE path='a.md'",
                          (scanner.file_hash(text),))
        self.conn.execute("INSERT INTO chunks (path, seq, text)"
                          " VALUES ('a.md', 0, ?)", (body,))
        self.conn.commit()
        return store.doc_input_snapshot(self.conn, "a.md")

    def test_writes_tag_into_frontmatter(self):
        vault = Path(self._tmp.name) / "vault"
        (vault / "").mkdir(parents=True, exist_ok=True)
        md = vault / "a.md"
        md.write_text('---\ntitle: "广告"\ntags:\n  - source/dr-gzh\n---\n正文\n',
                      encoding="utf-8")
        cand = self._candidate(vault)
        with mock.patch.object(extract, "VAULT_ROOT", vault):
            self.assertEqual(("", True), self.mod._deliver(self.conn, cand,
                                                           {"level": "hard"}))
        self.assertIn(extract.PROMO_TAG, md.read_text(encoding="utf-8"))
        # 回写改了文件,hash 要跟着刷新,否则下次 index 白白重建
        self.assertNotEqual(self.conn.execute(
            "SELECT content_hash FROM documents WHERE path='a.md'"
        ).fetchone()["content_hash"], cand["version"])

    def test_missing_file_does_not_raise(self):
        """文件已经不在了:如实返回一句原因、一行都不写,不抛。

        原来这一档靠回写那边吞 `OSError` 兜住;挪进协调入口之后由
        `_still_indexed` 在**落库之前**就拦下 —— 这才是对的那一档:`doc_tags`
        对 `documents(path)` 是外键,文章被清理掉之后照写会当场报 FOREIGN KEY,
        被记成一次判定失败,而文件是用户自己清掉的。
        """
        vault = Path(self._tmp.name) / "empty"
        vault.mkdir(parents=True, exist_ok=True)
        cand = {"path": "a.md", "title": "广告", "account": "dr-gzh",
                "version": "old", "body": "正文"}
        with mock.patch.object(extract, "VAULT_ROOT", vault):
            why, _ = self.mod._deliver(self.conn, cand,
                                       {"level": "hard"})      # 不抛
        self.assertTrue(why, "什么都没写却没说为什么")
        self.assertEqual("", self.conn.execute(
            "SELECT promo_level FROM documents WHERE path='a.md'"
        ).fetchone()["promo_level"])


    def _vault_with_md(self) -> Path:
        vault = Path(self._tmp.name) / "vault"
        vault.mkdir(parents=True, exist_ok=True)
        (vault / "a.md").write_text(
            '---\ntitle: "广告"\ntags:\n  - source/dr-gzh\n---\n正文\n',
            encoding="utf-8")
        return vault

    def test_a_write_error_on_the_file_does_not_void_the_verdict(self):
        """摘 md 那行失败(文件只读、没有 frontmatter)不该让判定作废。

        判定已经落库了。但这**也不是一次成功的交付**:两处标记此刻不一致,
        所以第二位返回 False、库里留下 `promo_tag_pending` 这条待办 ——
        少了待办,这一篇按 `promo_level <> ''` 掉出所有候选,再没有人回来看它
        (`sync-tags` 就是那条待办的出口)。**反过来钉一边在下一条**:别的异常
        不许跟着一起吞,那会把「协调区间里出了我们没想到的事」压成一次成功。
        """
        vault = self._vault_with_md()
        cand = self._candidate(vault)
        with mock.patch.object(extract, "VAULT_ROOT", vault), \
             mock.patch.object(extract, "writeback_extraction",
                               side_effect=OSError("只读文件系统")):
            self.assertEqual(("", False), self.mod._deliver(
                self.conn, cand, {"level": "hard"}))
        row = self.conn.execute(
            "SELECT promo_level, promo_tag_pending FROM documents"
            " WHERE path='a.md'").fetchone()
        self.assertEqual("hard", row["promo_level"], "判定该落库的照样要落库")
        self.assertTrue(row["promo_tag_pending"], "没留下可修的待办")

    def test_an_unexpected_error_is_not_swallowed(self):
        """反过来钉一边:不是交付失败的那类异常必须抛出去,不许压成一次成功。"""
        vault = self._vault_with_md()
        cand = self._candidate(vault)
        with mock.patch.object(extract, "VAULT_ROOT", vault), \
             mock.patch.object(extract, "writeback_extraction",
                               side_effect=RuntimeError("没想到的事")):
            with self.assertRaises(RuntimeError):
                self.mod._deliver(self.conn, cand, {"level": "hard"})


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

    def _run(self, entry: str, llm_level: str, promo: dict | None = None):
        data = {"entities": [{"name": "某产品", "kind": "product"}],
                "topics": ["ai"], "summary": "s",
                "promo": promo or {"level": llm_level, "reason": "r"}}
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

    def test_a_fallback_none_does_not_erase_an_existing_hard(self):
        """回包里缺 promo 字段 —— 已有的 hard 在**每条**提取路上都要留着。

        `judge_promo(require_verdict)` 那道 strict 只管得到重判那条路。全量
        提取不能开 strict(绝大多数文章是第一次判,一个坏回包不该把
        entity/topic/summary 整份丢掉),而它同样会覆盖已有判定:正文补抓回来后
        `fulltext.retire_stale` 只清 `extracted_at`、**明确保留** promo,于是
        一篇判过 hard 的广告带着结论重新排进提取,一个缺 promo 的回包就把它擦成
        none —— 广告重新进检索、两处 flag 一起摘掉,而界面显示提取成功、
        `extracted_at` 非空,这一篇从此掉出所有待办。闸设在落库那一层
        (`extract_promo.save_promo` 的 fallback 那段),所以这里对每个入口问
        同一组末态。
        """
        for entry in self._entries():
            with self.subTest(entry=entry):
                self._fresh()
                md = self._seed("hard", ["source/dr-gzh", extract.PROMO_TAG])
                # 兜底那个 none 由真实的 normalize_promo 产出(带 fallback 标记)
                self.assertEqual(
                    self._run(entry, "", promo=extract.normalize_promo({})),
                    "hard", "兜底的 none 把已经判出来的 hard 擦掉了")
                text = md.read_text(encoding="utf-8")
                self.assertIn(extract.PROMO_TAG, text, "md 里那行也不许摘")
                self.assertIn("topic/ai", text, "别的 tag 照旧写进去")

    def test_an_explicit_none_still_reclassifies_on_every_entry(self):
        """反过来钉一边:模型**明确**说 none 时,每条路都照旧改判 + 摘 flag。

        不然这道闸就成了「hard 再也改不回来」,比原来的问题更糟。
        """
        for entry in self._entries():
            with self.subTest(entry=entry):
                self._fresh()
                md = self._seed("hard", ["source/dr-gzh", extract.PROMO_TAG])
                self.assertEqual(self._run(entry, "none"), "none")
                self.assertNotIn(extract.PROMO_TAG,
                                 md.read_text(encoding="utf-8"))

    def test_promo_tag_is_not_slugged(self):
        # flag/软广 已是最终形态,跟着 topic/entity 过一遍 _tag_slug 会被改写
        tags = extract.extraction_tags(
            {"topics": ["AI Agent"], "entities": [{"name": "Claude Code"}],
             "promo": {"level": "hard"}})
        self.assertIn(extract.PROMO_TAG, tags)
        self.assertEqual(tags[-1], extract.PROMO_TAG)

    def test_nothing_to_write_skips_file_io(self):
        """没 tag、也没有 promo 结论时不该去读文件(pre_tagged 那条路就是这样)。

        **带明确 promo 结论的那条路现在一定会读一次** —— md 那行按库里的判定
        幂等对齐,代价就是这一次读(理由见 `extract_promo.align_promo_tag`)。
        所以这条用例的前置是「产物里压根没有 promo 字段」:VAULT_ROOT 指向一个
        不存在的目录,读文件就会抛,安静返回 False 才说明它压根没读。
        """
        conn = store.connect(self.db)
        with mock.patch.object(extract, "VAULT_ROOT", self.root / "nope"):
            self.assertFalse(extract.writeback_extraction(
                conn, "missing.md", {"topics": [], "entities": []}))
        conn.close()


if __name__ == "__main__":
    unittest.main()
