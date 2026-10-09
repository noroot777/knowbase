# -*- coding: utf-8 -*-
"""「这一篇没有正文」的两档,和它们各自的终态(kb/extract.py 的 body_gap)。

起因是一篇真实文章:2026-06-29「杭州见!学生党技术狂欢来袭,阿里硬核少年技术节
5.0」整篇是一张 1080x7355 的活动海报 —— 正文容器里 1 个 `<img>`、0 个字。
chunker 的 `strip_markup` 把图片引用剥掉之后不剩字,一个 chunk 都不产生,于是
`store.doc_body` 返回空串、提取跳过、`extracted_at` 永远是 NULL ——
**每一轮同步重报一次「正文为空,需回填」并把 extract 阶段标红,连报了半个月**,
而「重试失败项」每次都去重抓那张图,回来还是同一张。

所以分两档:回填救得了的继续进待办,回填救不了的落终态。终态绑 content_hash
而不是一个标记位 —— 正文哪天真补回来了,它得自己回到队列。

断言一律**先验后果**(「下一轮还在不在队列里」「文字补回来之后提没提成」),
只验「有没有打上标记」的话,一个什么都没防住的实现也能全绿。
"""
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kb import cli, extract, maintenance, pipeline_retry, pipeline_stages, scanner, store
from kb.chunker import split_chunks
from kb.config import Source
from tests.headless import probe_env, requires_headless

_PROBE = Path(__file__).resolve().parent / "pipe_pending_probe.mjs"

_real_connect = store.connect

IMG_ONLY = "![](assets/poster.jpg)\n"          # 整篇一张长海报图
STUB = "\n   \n"                                 # 抓取失败留下的壳
REAL = "首段正文。\n\n第二段正文,足够喂给模型。\n"
# 图 + 一行**正文标题**:标题文字是真的,只是被 chunker 当成小节标题、而那一节
# 清理完是空的,于是整段连标题一起丢掉 —— doc_body 照样返回空串
HEADING_ONLY = "## 报名截止:2026年10月15日\n\n" + IMG_ONLY
# wiki 阶段的阅读装饰。**不是内容**:scanner.normalize_body 把它整段丢掉,
# 所以索引和 chunker 压根看不到它(见 kb/scanner.py 的 _GRAPH_FOOTER_RE)
GRAPH_FOOTER = "\n<!-- kb-graph -->\n## 关联推荐\n[[项目A]]\n"


def _wikilib():
    """真的那个 linkify_doc(不自己编尾区长什么样)。"""
    wiki = str(Path(__file__).resolve().parents[1] / "scripts" / "wiki")
    if wiki not in sys.path:
        sys.path.insert(0, wiki)
    import wikilib
    return wikilib


def _md(body: str, title: str) -> str:
    return (f'---\ntitle: "{title}"\nsource: "rss"\naccount: "A"\n'
            f'published: "2026-06-29 18:29"\nurl: "https://example.org/x"\n'
            f'---\n{body}')


class NoBodyBase:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.src = self.vault / "source" / "rss"
        self.src.mkdir(parents=True)
        for target, attr, value in (
                (scanner, "VAULT_ROOT", self.vault),
                (extract, "VAULT_ROOT", self.vault),
                (maintenance, "DATA_DIR", self.root),   # 别去 flock 生产那把锁
                (scanner, "SOURCES", [Source(name="rss", root=self.src)]),
                (extract, "SOURCES", [Source(name="rss", root=self.src)])):
            p = patch.object(target, attr, value)
            p.start(); self.addCleanup(p.stop)
        p = patch.object(store, "connect",
                         lambda *a, **k: _real_connect(self.root / "kb.db"))
        p.start(); self.addCleanup(p.stop)
        self.stages = []
        p = patch.object(pipeline_stages, "_stage",
                         side_effect=lambda *a, **k: self.stages.append((a, k)))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(pipeline_stages, "_log", lambda *a, **k: None)
        p.start(); self.addCleanup(p.stop)
        self.conn = store.connect()
        self.addCleanup(self.conn.close)
        self.llm_calls = []

    # ── 摆盘 ────────────────────────────────────────────────────────────────
    def write(self, name: str, body: str) -> str:
        (self.src / f"{name}.md").write_text(_md(body, name), encoding="utf-8")
        return f"source/rss/{name}.md"

    def index(self) -> None:
        """真走一遍索引(scan → split_chunks → upsert),content_hash 由它落。"""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seen = set()
        for doc in scanner.scan_all():
            seen.add(doc.path)
            if store.doc_needs_reindex(self.conn, doc.path, doc.content_hash):
                store.upsert_document(self.conn, doc, split_chunks(doc.body), now)
        store.remove_missing_documents(self.conn, seen)
        self.conn.commit()

    def fake_llm(self, title, body, account=""):
        self.llm_calls.append(title)
        return {"entities": [], "topics": [], "summary": "s",
                "promo": {"level": "none", "reason": ""}}

    def run_extract(self):
        """真跑一轮管道提取(线程池、落终态、收尾文案都在里面)。"""
        self.stages.clear()
        with patch.object(extract, "extract_with_llm", self.fake_llm), \
                patch.object(extract, "writeback_extraction", lambda *a, **k: False):
            stats = pipeline_stages._new_extract_stats()
            pipeline_stages._extract_pass("", stats)
            pipeline_stages._finish_extract_stage(stats)
        return stats

    def pending(self):
        return [r["path"] for r in extract.pending_docs(self.conn)]

    def last_stage(self):
        (name, status, detail), kw = self.stages[-1]
        return status, detail, kw.get("failures") or []


class ImageOnlyTests(NoBodyBase, unittest.TestCase):
    """整篇一张图:落终态、不判红、不再重报。"""

    def test_marked_terminal_and_leaves_the_queue(self):
        path = self.write("poster", IMG_ONLY)
        self.index()
        self.assertEqual([path], self.pending())      # 第一轮确实在队列里

        self.run_extract()

        # 后果先断言:下一轮它不在队列里了 —— 这才是「不再每轮重报」
        self.assertEqual([], self.pending())
        # 也不该再占着面板那个「当前待处理 N 项」(同一个 pending_docs 口径)
        self.assertEqual(0, len(extract.pending_docs(self.conn)))
        status, detail, failures = self.last_stage()
        self.assertEqual("ok", status)                # 不判红
        self.assertEqual([], failures)                # 不进「需处理」明细
        self.assertIn("无正文 1", detail)
        self.assertNotIn("正文空", detail)            # 不许触发回填(见下面那条)
        self.assertEqual([], self.llm_calls)          # 一次模型都没调

    def test_second_round_says_nothing_at_all(self):
        self.write("poster", IMG_ONLY)
        self.index()
        self.run_extract()
        self.run_extract()
        status, detail, failures = self.last_stage()
        self.assertEqual(("ok", "没有待提取的文档", []), (status, detail, failures))

    def test_body_coming_back_puts_it_back_in_the_queue(self):
        """反过来钉一边:终态绑 content_hash,不是永久封死。"""
        path = self.write("poster", IMG_ONLY)
        self.index()
        self.run_extract()
        self.assertEqual([], self.pending())

        self.write("poster", IMG_ONLY + REAL)         # 正文被回填了
        self.index()
        self.assertEqual([path], self.pending(), "补回正文之后应当重新排队")

        self.run_extract()
        self.assertEqual(["poster"], self.llm_calls)
        self.assertIsNotNone(self.conn.execute(
            "SELECT extracted_at FROM documents WHERE path=?", (path,)
        ).fetchone()["extracted_at"])

    def test_a_failed_round_then_a_terminal_round_is_not_red(self):
        """存量轮临时失败、增量轮落终态:旧失败计数要一起清掉。

        两轮是正常完整同步的形状(存量 + 增量)。只清 empty_paths 和 failures
        的话,收尾按留着的 failed_paths 判红 —— 面板写着「失败 1 · 需处理」,
        而「问题明细」里一条都没有,点进去什么也看不到(审查实测)。
        """
        self.write("poster", IMG_ONLY)
        self.index()
        stats = pipeline_stages._new_extract_stats()
        with patch.object(pipeline_stages, "_extract_one",
                          side_effect=RuntimeError("临时读失败")):
            pipeline_stages._extract_pass("存量", stats)
        self.assertEqual(1, len(stats["failed_paths"]))      # 第一轮确实红了
        with patch.object(extract, "extract_with_llm", self.fake_llm):
            pipeline_stages._extract_pass("增量", stats)
        self.stages.clear()
        pipeline_stages._finish_extract_stage(stats)

        status, detail, failures = self.last_stage()
        self.assertEqual("ok", status, detail)               # 后果先断言
        self.assertEqual([], failures)
        self.assertIn("失败 0", detail)
        self.assertEqual(set(), stats["failed_paths"])

    def test_untouched_file_keeps_its_terminal_state(self):
        """md 没变就别因为重跑索引又冒出来(content_hash 一致)。"""
        self.write("poster", IMG_ONLY)
        self.index()
        self.run_extract()
        self.index()
        self.assertEqual([], self.pending())


class ConcurrentReindexTests(NoBodyBase, unittest.TestCase):
    """判定在工作线程、落终态在主线程,这中间索引可能把正文换进来。

    提取不持维护锁,所以这个交错是合法的、随时会发生。终态要是盖到了新版上,
    **那份真的正文从此既不提取也不提醒**,后续同内容的索引也不会让它回队列
    (审查实测:库里正文是真的,而 pending=[]、extracted_at=null、
    no_body_hash 与新 content_hash 相等)。
    """

    def test_a_body_arriving_between_judgment_and_mark_is_not_sealed(self):
        path = self.write("poster", IMG_ONLY)
        self.index()
        judged, go_on = threading.Event(), threading.Event()
        real_gap = extract.body_gap

        def paused_gap(doc_path, body):
            answer = real_gap(doc_path, body)
            judged.set()
            if not go_on.wait(10):
                raise RuntimeError("屏障超时")
            return answer

        stats = pipeline_stages._new_extract_stats()
        with patch.object(extract, "body_gap", paused_gap), \
                patch.object(extract, "extract_with_llm", self.fake_llm):
            with ThreadPoolExecutor(max_workers=1) as pool:
                fut = pool.submit(pipeline_stages._extract_pass, "", stats)
                self.assertTrue(judged.wait(10), "没等到判定做完")
                try:
                    self.write("poster", IMG_ONLY + REAL)   # 正文回来了
                    self.index()                            # 换了 content_hash
                finally:
                    go_on.set()
                fut.result(timeout=15)

        # 后果先断言:那份真的正文还提得了
        self.assertEqual([path], self.pending(), "新正文被终态封掉了")
        self.assertEqual(set(), stats["no_text_paths"])
        self.assertEqual("", self.conn.execute(
            "SELECT no_body_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["no_body_hash"])

        self.run_extract()                                  # 下一轮按新版重判
        self.assertEqual(["poster"], self.llm_calls)
        self.assertIsNotNone(self.conn.execute(
            "SELECT extracted_at FROM documents WHERE path=?", (path,)
        ).fetchone()["extracted_at"])

    def test_mark_only_touches_the_version_it_judged(self):
        """判据待在 UPDATE 的 WHERE 里:版本对不上就是 0 行,不是写个别的值。"""
        path = self.write("poster", IMG_ONLY)
        self.index()
        now = self.conn.execute(
            "SELECT content_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["content_hash"]

        self.assertFalse(extract.mark_no_body(self.conn, path, "另一版的哈希"))
        self.conn.commit()
        self.assertEqual("", self.conn.execute(
            "SELECT no_body_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["no_body_hash"])
        self.assertEqual([path], self.pending())

        self.assertTrue(extract.mark_no_body(self.conn, path, now))
        self.conn.commit()
        self.assertEqual([], self.pending())

    def test_the_version_and_the_body_come_from_one_snapshot(self):
        path = self.write("poster", IMG_ONLY + REAL)
        self.index()
        version, body = store.doc_body_version(self.conn, path)
        self.assertEqual(version, self.conn.execute(
            "SELECT content_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["content_hash"])
        self.assertEqual(store.doc_body(self.conn, path), body)
        self.assertEqual(("", ""), store.doc_body_version(self.conn, "source/rss/nope.md"))


class EmptyStubTests(NoBodyBase, unittest.TestCase):
    """真的什么都没有:照旧进待办,这一档回填救得了。"""

    def test_stub_still_asks_for_refill_every_round(self):
        path = self.write("stub", STUB)
        self.index()
        self.run_extract()

        self.assertEqual([path], self.pending(), "待回填的不许落终态")
        status, detail, failures = self.last_stage()
        self.assertEqual("error", status)
        self.assertIn("正文空", detail)
        self.assertEqual(["正文为空,需回填"], [f["reason"] for f in failures])
        self.assertEqual("", self.conn.execute(
            "SELECT no_body_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["no_body_hash"])

    def test_a_heading_with_text_keeps_asking_for_refill(self):
        """端到端:图 + 一行正文标题照旧进待办,维持本档出现之前的行为。"""
        path = self.write("heading", HEADING_ONLY)
        self.index()
        self.run_extract()

        self.assertEqual([path], self.pending(), "有字的内容不许落终态")
        self.assertEqual("", self.conn.execute(
            "SELECT no_body_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["no_body_hash"])
        status, detail, failures = self.last_stage()
        self.assertEqual("error", status)
        self.assertIn("正文空", detail)                # 回填那条暗号照旧
        self.assertEqual(["正文为空,需回填"], [f["reason"] for f in failures])

    def test_two_kinds_in_one_round_are_counted_apart(self):
        img = self.write("poster", IMG_ONLY)
        stub = self.write("stub", STUB)
        self.write("good", REAL)
        self.index()
        self.run_extract()

        self.assertEqual([stub], self.pending())
        status, detail, failures = self.last_stage()
        self.assertEqual("error", status)             # 待回填那档仍然判红
        self.assertIn("成功 1", detail)
        self.assertIn("跳过 1(正文空,需回填)", detail)
        self.assertIn("无正文 1", detail)
        self.assertEqual(1, len(failures))            # 只有 stub 那篇进明细
        self.assertNotIn(img, self.pending())


class GraphFooterTests(NoBodyBase, unittest.TestCase):
    """阅读装饰不是内容:「空没空」要在剥掉它之后问。

    `wikilib.linkify_doc` 对正文为空的壳子**照样**追加图谱尾区,所以真实
    vault 里「什么都没有」的常见形态是「只剩一行 <!-- kb-graph -->」。拿装饰
    前的原文判空会把它顶成非空、落进 no_text 终态 —— 一篇真要回填的空文章
    从此不再提醒、也不再提取(2026-10-09 第 2 轮审查实测)。
    """

    def _no_body_hash(self, path):
        return self.conn.execute(
            "SELECT no_body_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["no_body_hash"]

    def test_an_empty_stub_wearing_a_footer_still_asks_for_refill(self):
        path = self.write("decorated-empty", GRAPH_FOOTER)
        self.index()
        self.run_extract()

        # 后果先断言:下一轮还选得到它、终态一个字都没写
        self.assertEqual([path], self.pending(), "装饰过的空文章不许落终态")
        self.assertEqual("", self._no_body_hash(path))
        status, detail, failures = self.last_stage()
        self.assertEqual("error", status)
        self.assertIn("正文空", detail)                # 回填那条暗号照旧
        self.assertEqual(["正文为空,需回填"], [f["reason"] for f in failures])
        self.assertEqual(extract.NO_BODY_EMPTY, extract.body_gap(path, ""))

    def test_the_real_linkifier_output_still_asks_for_refill(self):
        """走一遍真的 linkify_doc,不自己编尾区的样子。"""
        wikilib = _wikilib()
        path = self.write("wiki-stub", STUB)
        universe = SimpleNamespace(doc_ents={}, topic_docs={})
        with patch.object(wikilib, "VAULT", self.vault):
            decorated = wikilib.linkify_doc(universe, path)
        self.assertIn(wikilib.GRAPH_MARK, decorated, "前提:空壳也被加了尾区")
        (self.vault / path).write_text(decorated, encoding="utf-8")
        self.index()
        self.run_extract()

        self.assertEqual([path], self.pending(), "真装饰过的空文章不许落终态")
        self.assertEqual("", self._no_body_hash(path))
        status, detail, failures = self.last_stage()
        self.assertEqual("error", status)
        self.assertEqual(["正文为空,需回填"], [f["reason"] for f in failures])

    def test_an_image_wearing_the_same_footer_still_leaves_the_queue(self):
        """反过来钉一边:尾区剥掉之后图还在,那一档照旧落终态、不重报。"""
        path = self.write("poster", IMG_ONLY + GRAPH_FOOTER)
        self.index()
        self.run_extract()

        self.assertEqual([], self.pending(), "有图无字的照旧离开队列")
        self.assertEqual(self.conn.execute(
            "SELECT content_hash FROM documents WHERE path=?", (path,)
        ).fetchone()["content_hash"], self._no_body_hash(path))
        status, detail, failures = self.last_stage()
        self.assertEqual("ok", status)
        self.assertIn("无正文 1", detail)
        self.assertEqual([], failures)

        self.run_extract()                             # 第二轮一个字都不说
        status, detail, _ = self.last_stage()
        self.assertEqual("ok", status)
        self.assertNotIn("无正文", detail)


class BodyGapTests(NoBodyBase, unittest.TestCase):
    """判定本身。读不到文件要落在保守的那一边。"""

    def test_non_empty_body_is_not_a_gap(self):
        self.assertEqual("", extract.body_gap("source/rss/whatever.md", "有字"))

    def test_missing_file_falls_back_to_empty(self):
        # 保守档:继续进待办,**不**落成会让它消失的终态
        self.assertEqual(extract.NO_BODY_EMPTY,
                         extract.body_gap("source/rss/gone.md", ""))

    def test_several_images_and_nothing_else_counts_as_no_text(self):
        """判据是「剥完还剩不剩字」,不是「有没有图片」(这里是两张,不是一张)。"""
        self.write("imgs", "![](a.png)\n\n![](b.png)\n")
        self.assertEqual(extract.NO_BODY_NO_TEXT,
                         extract.body_gap("source/rss/imgs.md", ""))

    def test_text_the_chunker_threw_away_is_not_no_text(self):
        """剥完**还剩字**就不是「没有文字」,哪怕一个 chunk 都没产生。

        `## 报名截止:…` 是正文里看得见的字,只是被 chunker 当成小节标题、
        而那一节清理完是空的,于是整段连标题一起丢掉。落终态等于把看得见的
        内容藏起来、从此不再提取(审查实测)—— 一律按最保守的那一档办。
        """
        self.write("heading", HEADING_ONLY)
        self.assertEqual([], split_chunks(HEADING_ONLY), "前提:它确实一个 chunk 都没有")
        self.assertEqual(extract.NO_BODY_EMPTY,
                         extract.body_gap("source/rss/heading.md", ""))

    def test_judgment_uses_the_chunkers_own_rule(self):
        """口径只有一份:问的就是 chunker 决定 chunk 存不存的那条规则。"""
        from kb.chunker import strip_markup
        self.assertEqual("", strip_markup(IMG_ONLY))
        self.assertTrue(strip_markup(HEADING_ONLY).strip())


class RefillPlanTests(NoBodyBase, unittest.TestCase):
    """那个暗号:只有回填救得了的那一档才排 refill。"""

    def _plan(self, detail):
        prev = {"stages": {"extract": {"status": "ok", "detail": detail}}}
        with patch.object(pipeline_retry, "load_last_run", return_value=prev), \
                patch.object(pipeline_retry, "refill_script",
                             return_value=Path("/tmp/refill.py")), \
                patch.object(extract, "pending_docs", return_value=[]):
            return pipeline_retry.retry_failed_plan()

    def test_no_text_does_not_trigger_refill(self):
        # 回填重抓回来还是同一张图 —— 排进计划就是每一轮白跑一遍脚本
        self.assertNotIn("refill", self._plan("成功 3,失败 0,无正文 1(仅图片等,已记录不再提示)"))

    def test_empty_still_triggers_refill(self):
        self.assertIn("refill", self._plan("成功 3,失败 0,跳过 1(正文空,需回填)"))


class CliExtractTests(NoBodyBase, unittest.TestCase):
    """命令行那条路共用同一套判定,否则它跑完管道又重报一次。"""

    def test_cli_marks_terminal_too(self):
        import argparse
        self.write("poster", IMG_ONLY)
        self.index()
        with patch.object(extract, "extract_with_llm", self.fake_llm), \
                patch.object(cli.store, "connect",
                             lambda *a, **k: _real_connect(self.root / "kb.db")):
            cli.cmd_extract(argparse.Namespace(limit=0, writeback=False))
        self.assertEqual([], self.pending())
        self.assertEqual([], self.llm_calls)


class OldLibraryMigrationTests(unittest.TestCase):
    """新增那一列在**老库**上是 ALTER 补的,而 connect() 是所有人共用的入口。

    「读列集合」和 ALTER 分在两个语句、中间不持锁的话,同时首访同一个老库的
    连接会读到同一份旧列集合,后来者的 ALTER 撞 `duplicate column name` ——
    那一个请求当场 500。页面 API、提取 worker、CLI 走的都是这个 connect,
    所以「升级后第一次并发使用」有一批操作直接报错(审查实测 12 个并发连接
    11 个失败)。
    """

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / "kb.db"
        self._make_old_library()

    def _make_old_library(self) -> None:
        """造一个「no_body_hash 还不存在」的老库。

        先正常建一遍(于是 WAL、别的表、索引都齐了,并发首访不会去争 journal
        模式那一句),再把 documents 换成**去掉这一列**的同一份列定义 ——
        列表是现读 PRAGMA 拿的,不抄一份建表语句:以后再加列这条摆盘不用改。
        connect() 的 CREATE TABLE IF NOT EXISTS 对已存在的表是空操作,所以
        补列这条路和真实升级走的是同一条。
        """
        conn = store.connect(self.db)
        info = [dict(r) for r in conn.execute("PRAGMA table_info(documents)")]
        conn.close()
        self.assertIn("no_body_hash", {c["name"] for c in info})
        cols = []
        for col in info:
            if col["name"] == "no_body_hash":
                continue
            piece = f'"{col["name"]}" {col["type"]}'
            if col["pk"]:
                piece += " PRIMARY KEY"
            if col["notnull"]:
                piece += " NOT NULL"
            if col["dflt_value"] is not None:
                piece += f' DEFAULT {col["dflt_value"]}'
            cols.append(piece)
        raw = sqlite3.connect(self.db)
        raw.execute("PRAGMA foreign_keys=OFF")
        raw.execute("PRAGMA legacy_alter_table=ON")   # 别去改子表里的外键指向
        raw.execute(f"CREATE TABLE documents_old ({', '.join(cols)})")
        raw.execute("DROP TABLE documents")
        raw.execute("ALTER TABLE documents_old RENAME TO documents")
        raw.commit()
        self.assertNotIn("no_body_hash",
                         {r[1] for r in raw.execute("PRAGMA table_info(documents)")})
        raw.close()

    def _columns(self) -> list[str]:
        raw = sqlite3.connect(self.db)
        try:
            return [r[1] for r in raw.execute("PRAGMA table_info(documents)")]
        finally:
            raw.close()

    def test_twelve_first_connections_all_succeed(self):
        count = 12
        gate = threading.Barrier(count)

        def connect_together(_):
            gate.wait(timeout=20)
            try:
                store.connect(self.db).close()
                return ""
            except Exception as e:                      # noqa: BLE001
                return f"{type(e).__name__}: {e}"

        with ThreadPoolExecutor(max_workers=count) as pool:
            errors = [e for e in pool.map(connect_together, range(count)) if e]
        self.assertEqual([], errors)
        # 列只许出现一次:补两遍的库后面每一句 SELECT 都是歧义的
        self.assertEqual(1, self._columns().count("no_body_hash"))

    def test_a_second_pass_over_an_upgraded_library_changes_nothing(self):
        store.connect(self.db).close()
        before = self._columns()
        store.connect(self.db).close()
        self.assertEqual(before, self._columns())


@requires_headless
class PendingCountTests(unittest.TestCase):
    """跑得极快的那一轮结束之后,面板上的「当前待处理 N 项」自己归零。

    这一档一次模型都不调、几百毫秒就结束,比面板的轮询间隔短得多,于是页面
    一次都没见过 running=true —— 原来刷新积压数只认 running→idle 这个跳变,
    这一轮压根不触发:后端 pending.extract 已经是 0,面板还写着「当前待处理
    1 项」,只有手动刷新页面才归零(审查实测)。判据是**页面上那行字**。
    """

    def test_the_panel_clears_the_backlog_without_a_reload(self):
        r = subprocess.run([shutil.which("node"), str(_PROBE)],
                           capture_output=True, text=True, timeout=300,
                           env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
