# -*- coding: utf-8 -*-
"""「这一篇没有正文」的两档,和它们各自的终态(kb/extract.py 的 body_gap)。

起因是一篇真实文章:2026-06-29「杭州见!学生党技术狂欢来袭,阿里硬核少年技术节
5.0」整篇是一张 1080x7355 的活动海报 —— 正文容器里 1 个 `<img>`、0 个字。
chunker 的 `_clean` 把图片引用剥掉之后不剩字,一个 chunk 都不产生,于是
`store.doc_body` 返回空串、提取跳过、`extracted_at` 永远是 NULL ——
**每一轮同步重报一次「正文为空,需回填」并把 extract 阶段标红,连报了半个月**,
而「重试失败项」每次都去重抓那张图,回来还是同一张。

所以分两档:回填救得了的继续进待办,回填救不了的落终态。终态绑 content_hash
而不是一个标记位 —— 正文哪天真补回来了,它得自己回到队列。

断言一律**先验后果**(「下一轮还在不在队列里」「文字补回来之后提没提成」),
只验「有没有打上标记」的话,一个什么都没防住的实现也能全绿。
"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from kb import cli, extract, maintenance, pipeline_retry, pipeline_stages, scanner, store
from kb.chunker import split_chunks
from kb.config import Source

_real_connect = store.connect

IMG_ONLY = "![](assets/poster.jpg)\n"          # 整篇一张长海报图
STUB = "\n   \n"                                 # 抓取失败留下的壳
REAL = "首段正文。\n\n第二段正文,足够喂给模型。\n"


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

    def test_untouched_file_keeps_its_terminal_state(self):
        """md 没变就别因为重跑索引又冒出来(content_hash 一致)。"""
        self.write("poster", IMG_ONLY)
        self.index()
        self.run_extract()
        self.index()
        self.assertEqual([], self.pending())


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


class BodyGapTests(NoBodyBase, unittest.TestCase):
    """判定本身。读不到文件要落在保守的那一边。"""

    def test_non_empty_body_is_not_a_gap(self):
        self.assertEqual("", extract.body_gap("source/rss/whatever.md", "有字"))

    def test_missing_file_falls_back_to_empty(self):
        # 保守档:继续进待办,**不**落成会让它消失的终态
        self.assertEqual(extract.NO_BODY_EMPTY,
                         extract.body_gap("source/rss/gone.md", ""))

    def test_table_only_also_counts_as_no_text(self):
        """判据是「剥完还剩不剩字」,不是「有没有图片」。"""
        self.write("tbl", "![](a.png)\n\n![](b.png)\n")
        self.assertEqual(extract.NO_BODY_NO_TEXT,
                         extract.body_gap("source/rss/tbl.md", ""))


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


if __name__ == "__main__":
    unittest.main()
