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
                         self.calls("register_indexed"),
                         "`register_indexed` 多了一个直接调用方 —— 它拿得出"
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
        """「那三样一致吗」只有一份实现 —— 补判那道和登记那道共用它。"""
        self.assertEqual({"kb/doc_rewrite.py": 1, "kb/extract_promo.py": 1},
                         self.calls("index_input_matches"))
        promo = (ROOT / "kb/extract_promo.py").read_text(encoding="utf-8")
        self.assertNotIn("input_fingerprint(title, account",
                         promo, "补判那边又自己拼了一遍索引输入指纹")


if __name__ == "__main__":
    unittest.main()
