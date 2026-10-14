# -*- coding: utf-8 -*-
"""谁有资格写 `documents.content_hash` —— 名单本身也要钉住(静态,按 AST)。

行为那三层在 `tests/test_doc_rewrite.py`:窗口关着没关着、两道证明各自的红、
等不到锁怎么收场。这一份不摆任何盘、不跑任何旁路,只读全仓源码 —— 所以它和那边
共用不了什么,单独一份。
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "wiki"))

class CallSiteRosterTests(unittest.TestCase):
    """谁有资格写 `documents.content_hash` —— 名单本身也要钉住。

    那条缺陷的形态就是「名单之外还有两个登记方」,而旧名单只枚举了
    `refresh_content_hash` 的调用点、两条旁路都在上面、全绿照过。所以这里换成按
    **写那一句 SQL**和**真实调用**枚举:多一处就在这里红,而不是等某天有人报
    「新正文搜不到」。

    **按 AST 数,不按行 grep。** grep 会把 docstring 里的用法示例和注释里的函数名
    一起数进来 —— 自测时 `rewriting()` 的用法示例就让名单红了一次,而那种红是
    「拦住写注释」,不是「拦住新调用点」。
    """

    @staticmethod
    def sources() -> list[Path]:
        return (sorted(ROOT.glob("kb/**/*.py"))
                + sorted(ROOT.glob("scripts/**/*.py")))

    @staticmethod
    def _callee(func) -> str | None:
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return None

    @staticmethod
    def tree(py: Path) -> ast.Module:
        """解析一份源码。**吞掉 DeprecationWarning** —— 全仓有三处 docstring
        里带着反斜杠加字母那种序列(都是在讲正则,不是正则本身)。解析别人的
        文件时那几条警告会把这组用例的输出刷花,和被测的事情无关。
        """
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            return ast.parse(py.read_text(encoding="utf-8"))

    def calls_in(self, scope, name: str) -> list[ast.Call]:
        return [n for n in ast.walk(scope) if isinstance(n, ast.Call)
                and self._callee(n.func) == name]

    def calls(self, name: str) -> dict[str, int]:
        """全仓真实调用 `name(...)` 的地方。"""
        found: dict[str, int] = {}
        for py in self.sources():
            n = len(self.calls_in(self.tree(py), name))
            if n:
                found[py.relative_to(ROOT).as_posix()] = n
        return found

    def kb_defs(self) -> set[str]:
        """`kb/` 里定义过的函数名 —— 用来把索引那一趟的调用筛成「我们自己的」。

        不这么筛的话,`print` / `commit` / `append` 一类噪声也会进名单,加一句
        无关的打印就红一次;筛完剩下的才是「这一趟都调了我们哪些东西」。
        """
        out: set[str] = set()
        for py in sorted(ROOT.glob("kb/**/*.py")):
            for node in ast.walk(self.tree(py)):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.add(node.name)
        return out

    def func(self, rel: str, name: str) -> ast.FunctionDef:
        """这份源码里叫 `name` 的那个函数(找不到就红 —— 多半是改名了)。"""
        for node in ast.walk(self.tree(ROOT / rel)):
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return node
        raise AssertionError(f"{rel} 里没有 {name}() —— 名单跟着改名一起改")

    def sql(self, pattern: str) -> dict[str, int]:
        """字符串字面量里出现这句 SQL 的地方 —— **docstring 不算**。"""
        rx = re.compile(pattern)
        found: dict[str, int] = {}
        for py in self.sources():
            tree = self.tree(py)
            prose = {id(n.value) for n in ast.walk(tree)
                     if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)}
            n = sum(1 for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in prose and rx.search(node.value))
            if n:
                found[py.relative_to(ROOT).as_posix()] = n
        return found

    def test_only_two_statements_write_documents_content_hash(self):
        """索引自己的 upsert,和那道带两样证明的登记。再多一处就得重新想一次。"""
        self.assertEqual({"kb/store.py": 1, "kb/doc_rewrite.py": 1},
                         self.sql(r"documents SET content_hash"
                                  r"|content_hash=excluded\.content_hash"),
                         "多了一处写 documents.content_hash 的语句 —— 它凭什么"
                         "说得出「磁盘上这一份已经索引过了」")

    def test_every_caller_of_the_registration_is_accounted_for(self):
        """登记那张收据的入口只有两个,它们的调用方就这几处。"""
        self.assertEqual({"kb/doc_rewrite.py": 1, "kb/extract_writeback.py": 1},
                         self.calls("register_written"),
                         "`register_written` 多了一个直接调用方 —— 它拿得出"
                         "「动手之前那份 hash」和「本次写出去的那一份文本」吗")
        self.assertEqual({"kb/extract.py": 1},
                         self.calls("refresh_content_hash"),
                         "多了一处刷新索引收据的调用点 —— 它在维护锁里吗")
        self.assertEqual({"scripts/backfill_youtube_summaries.py": 1,
                          "scripts/wiki/wiki_daily.py": 1},
                         self.calls("rewriting"),
                         "多了一条走协调入口的旁路 —— 名单跟着它一起改")

    def test_the_two_bypasses_do_not_write_the_vault_on_their_own(self):
        """两条旁路不许再自己 `write_text` 那篇文章 —— 写回必须经协调入口。

        绕过去写就等于绕过了那两道证明:文件改了、收据却是另一份内容的。
        (wiki 日报别的步骤照旧自己写 wiki 页面和 index.md,那些不是索引过的
        文章,所以只圈 `step_linkify` 和回填脚本的 `main`。)
        """
        for rel, fn in (("scripts/backfill_youtube_summaries.py", "main"),
                        ("scripts/wiki/wiki_daily.py", "step_linkify")):
            self.assertEqual(
                [], self.calls_in(self.func(rel, fn), "write_text"),
                f"{rel} 的 {fn}() 绕过协调入口自己写 vault")

    def test_the_index_input_criterion_has_a_single_source(self):
        """「那三样一致吗」只有一份实现 —— 软广补判用它(**登记不用**)。

        登记那张收据换成了更强的 `index_projection_matches`(整份索引投影),
        理由见它自己的 docstring:模型输入那三样只是索引写进库的一部分。两道
        判据各有射程,所以这里连「登记那边没有偷偷退回用这一道」一起钉住。
        """
        self.assertEqual({"kb/extract_promo.py": 1},
                         self.calls("index_input_matches"),
                         "模型输入那道判据多了一个消费方 —— 它判的是「能不能"
                         "固化一个判定」,不是「能不能签已索引的收据」")
        self.assertEqual({"kb/doc_rewrite.py": 1},
                         self.calls("index_projection_matches"),
                         "整份索引投影那道判据多了一个调用方,或者登记那边不"
                         "再过它了")
        promo = (ROOT / "kb/extract_promo.py").read_text(encoding="utf-8")
        self.assertNotIn("input_fingerprint(title, account",
                         promo, "补判那边又自己拼了一遍索引输入指纹")

    def test_the_index_projection_reads_the_same_fields_the_indexer_writes(self):
        """投影判据比的那几样,要和 `store.upsert_document` 真正写的对得上。

        这是**名单式**的钉法:`upsert_document` 哪天多写一列(或多一张表),
        判据这边不跟着加,登记就又开始替一份「重新索引会变」的文件签收据,而
        症状只有「那一列永远是旧值」。所以这里按它的 SQL 枚举列名。
        """
        src = (ROOT / "kb/store.py").read_text(encoding="utf-8")
        head = src[src.index("def upsert_document"):src.index(
            "def remove_missing_documents")]
        # documents 的 INSERT 列表(content_hash / indexed_at 不是索引投影:
        # 前者正是我们要写的,后者是时间戳)
        cols = re.search(r"INSERT INTO documents \(([^)]*)\)", head, re.S)
        wrote = {c.strip() for c in cols.group(1).replace("\n", " ").split(",")}
        self.assertEqual(
            {"path", "source", "title", "account", "published", "url",
             "content_hash", "indexed_at"}, wrote,
            "索引 upsert 的列变了 —— `index_projection_matches` 要跟着改")
        for table in ("doc_tags", "chunks"):
            self.assertIn(f"INSERT INTO {table} ", head.replace(" OR IGNORE", ""),
                          f"索引不再写 {table} 了?判据跟着改")
        # upsert 之后那一步(`parse_pretagged` → `save_extraction`)写的那几样
        ext = (ROOT / "kb/extract.py").read_text(encoding="utf-8")
        save = ext[ext.index("def save_extraction"):ext.index("def extraction_tags")]
        for stmt in ("DELETE FROM entities", "INSERT OR IGNORE INTO entities",
                     "INSERT OR IGNORE INTO doc_tags",
                     "UPDATE documents SET extracted_at=?, summary=?"):
            self.assertIn(stmt, save,
                          f"`save_extraction` 不再写「{stmt}」了?判据跟着改")
        proj = (ROOT / "kb/doc_rewrite.py").read_text(encoding="utf-8")
        body = proj[proj.index("def _extraction_claims"):
                    proj.index("def register_written")]
        for col in ("source", "title", "account", "published", "url",
                    "summary"):
            self.assertIn(f'"{col}"', body, f"投影判据没比 {col}")
        for table in ("doc_tags", "chunks", "entities"):
            self.assertIn(table, body, f"投影判据没比 {table}")

    def test_the_index_pass_has_no_derived_step_the_projection_does_not_know(self):
        """**索引不是 upsert 完就完了** —— 那一趟还接着写什么,名单钉在这里。

        三个真实索引入口都在 `upsert_document` 之后接着跑 `parse_pretagged` →
        `save_extraction`(写 `documents.summary` 和 `entities`),而
        `content_hash` 跳过的是**整趟**,不只是 upsert 那一句。审查实测过判据漏掉
        这一层的后果:外来编辑改了 md 里的 summary 或 project/kind,收据照签,
        连跑两趟 `kb index` 都报「未变 1」,库里和实体枚举一直是旧值,强制重建
        才纠正得了。

        所以这里按「这一趟都调了我们自己哪些函数」枚举:哪天多一步写库,这条红
        会来提醒 `index_projection_matches` 跟着知道它 —— 那一句一个字不改的话,
        症状只有「那一样永远是旧值」。
        """
        base = {"connect", "close", "scan_all", "doc_needs_reindex",
                "split_chunks", "upsert_document",
                "parse_pretagged", "save_extraction", "embed_pending_chunks"}
        expected = {
            # `now` 是 `datetime.now()`,恰好和 kb 里某个同名函数撞上(按名字筛
            # 的代价),不是索引自己的一步。
            ("kb/cli.py", "_index_pass"):
                base | {"remove_missing_documents", "now"},
            ("kb/pipeline_stages.py", "_index_pass"):
                base | {"remove_missing_documents", "_now", "_stage"},
            ("kb/pipeline_stages.py", "_ingest_pass"): base | {"_now"},
        }
        kb_defs = self.kb_defs()
        for (rel, fn), want in expected.items():
            called = {self._callee(n.func)
                      for n in ast.walk(self.func(rel, fn))
                      if isinstance(n, ast.Call)}
            self.assertEqual(want, called & kb_defs,
                             f"{rel} 的 {fn}() 这一趟调的东西变了 —— 多出来的"
                             f"那一步也写库吗?`index_projection_matches` 要"
                             f"跟着知道它")

    def test_every_source_lives_under_the_path_shape_the_fallback_assumes(self):
        """`source_name_of` 按 `source/<name>/...` 推来源名 —— 钉住这个前提。

        `config.SOURCES` 里每一条现在都是 `root = VAULT_ROOT/"source"/name`。
        哪天加一条 root 形状不同的来源,那个推法对它就只能退回库里那个值(判据
        在那一篇上退化一档),这条红是来提醒改判据,不是来拦新来源的。
        """
        from kb.config import SOURCES, VAULT_ROOT
        for src in SOURCES:
            self.assertEqual(VAULT_ROOT / "source" / src.name, src.root,
                             f"来源 {src.name} 的 root 不是那个形状了")


if __name__ == "__main__":
    unittest.main()
