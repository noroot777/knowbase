# -*- coding: utf-8 -*-
"""验证设施的**结构契约**:谁拥有浏览器 profile、谁拥有沙箱那份临时库。

跟 `tests/test_probe_leak.py` 是同一个所有权问题的两半 —— 那份验「进程」在任何一条
退出路径上都被收掉(判据是 pid 还在不在),这一份验「目录」不许由一个**收不到清理
机会的进程**拥有,并且用源码结构把口径钉住,防止下一个探针/沙箱又各写一份。

为什么要结构守卫而不是只验行为:漏掉的那条路都在「建目录 → 起进程 → 连上去」这一
段,而这一段恰恰在 `try` 之前,正常跑的时候一次都走不到,行为用例全绿也证明不了。
实测的后果是 TMPDIR 里留下整份沙箱库(`kb-source-qa-*`、`kb-reading-qa-*`,几十 MB
带 vault),而测试报告一切正常。

这里头不碰浏览器的那几条**不挂** `@requires_headless`:没装无头壳的机器上,上面那些
行为用例会被 skip 掉,靠这几条守住结构。
"""

from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.headless import fixture_server, requires_headless  # noqa: E402

HERE = Path(__file__).resolve().parent


# `test_no_test_runs_a_browser_probe_bare` 唯一的例外对象(理由见那条用例的注释)
LEAK_TESTS = "test_probe_leak.py"


def _server_names_in_code(path: Path, names: set[str]) -> list[int]:
    """`path` 的**代码**里提到这些沙箱服务名的行号(文档和注释不算)。

    判据是「提到名字」而不是「这一行上有 Popen」:路径常量、cmd 列表、
    `sys.executable` 往往分散在好几行上,按窗口往后看的写法漏得一声不响(实测:
    `SERVER = ...` 写在 Popen 前两行就漏掉了)。这个名字只该出现在一个位置 ——
    `fixture_server(script=...)` 的实参,所以那一处放过。

    走 AST 而不是逐行 `in`:docstring 里本来就会写「数据由 tests/serve_reading.py
    摆出来」,逐行扫会把它当违例(第一版就是这么误报的),而注释压根不进 AST。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    allowed: list[tuple[int, int]] = []
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                docstrings.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
        if isinstance(node, ast.Call):
            func = node.func
            label = getattr(func, "id", None) or getattr(func, "attr", None)
            if label == "fixture_server":
                allowed.append((node.lineno, node.end_lineno or node.lineno))
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
            continue
        if not any(name in node.value for name in names):
            continue
        if node.lineno in docstrings:
            continue
        if any(lo <= node.lineno <= hi for lo, hi in allowed):
            continue
        hits.append(node.lineno)
    return sorted(hits)


class ProbeSourceContractTests(unittest.TestCase):
    """口径只有一份:浏览器和沙箱都由共享启动器起,调用点不许再各写一份。

    五条分别钉:探针不许自己 spawn 浏览器、不许自己管 profile 目录;测试不许裸
    `subprocess.run` 跑探针、不许自己 `Popen` 沙箱服务;沙箱服务不许自己拥有临时根。
    都不需要浏览器,所以不挂 `@requires_headless`。
    """

    def test_no_probe_spawns_the_shell_itself(self):
        offenders = []
        for f in sorted(HERE.glob("*.mjs")):
            if f.name == "probe_shell.mjs":
                continue
            s = f.read_text(encoding="utf-8")
            if "HEADLESS_SHELL" in s and "spawn(" in s and "launchShell" not in s:
                offenders.append(f.name)
        self.assertEqual([], offenders,
                         "这些探针又自己 spawn 浏览器了,收尾就不归 probe_shell 管了")

    def test_no_probe_owns_its_own_profile_directory(self):
        """临时 profile 由 `launchShell` 建和删(`{profile: 前缀}`)。探针自己
        `mkdtempSync` 的那种写法在「建目录 → 起浏览器 → 连 CDP」这段抛异常时会把
        目录留下,而那一段恰恰在 try 之前。"""
        offenders = []
        for f in sorted(HERE.glob("*.mjs")):
            if f.name == "probe_shell.mjs":
                continue
            text = f.read_text(encoding="utf-8")
            if "user-data-dir" in text or ("mkdtempSync" in text and "launchShell" in text):
                offenders.append(f.name)
        self.assertEqual([], offenders,
                         "这些探针又自己管 profile 目录了,抛异常那条路上会留下它:"
                         "改成 launchShell(args, {profile: '前缀-'})")

    def test_no_test_runs_a_browser_probe_bare(self):
        offenders = []
        for f in sorted(HERE.glob("test_*.py")):
            # `test_probe_leak.py` 是唯一的例外,而且必须是:它那几条验的正是
            # **探针自己**退出时收不收得干净,走 run_probe 的话它那道 killpg 会把
            # 结论盖掉 —— 被测的实现坏掉了也照样全绿。
            if f.name == LEAK_TESTS:
                continue
            s = f.read_text(encoding="utf-8")
            if "probe_env()" not in s:
                continue
            for i, line in enumerate(s.splitlines(), 1):
                if "subprocess.run(" in line and "run_probe" not in line:
                    tail = "\n".join(s.splitlines()[i - 1:i + 3])
                    if "probe_env()" in tail:
                        offenders.append(f"{f.name}:{i}")
        self.assertEqual(
            [], offenders,
            "这些地方又裸 subprocess.run 跑探针了 —— 它的 timeout 是 SIGKILL,"
            "探针起的浏览器会被漏在外面,要走 tests.headless.run_probe")


    def test_every_fixture_server_lets_the_caller_own_the_root(self):
        """`tests/serve_*.py` 里自己建沙箱根的,必须先认 `KB_FIXTURE_ROOT`。

        口径只有一份(`tests/fixture_root.py` 的 `fixture_root`)。就地写
        `with tempfile.TemporaryDirectory()` 的那种,被 SIGTERM 收掉时清理一行
        都不跑 —— 父进程也不知道目录在哪,删不掉。
        """
        offenders = []
        for f in sorted(HERE.glob("serve_*.py")):
            text = f.read_text(encoding="utf-8")
            if "TemporaryDirectory" not in text and "mkdtemp" not in text:
                continue                      # 压根不建根的(serve_workspace)
            if "fixture_root(" not in text:
                offenders.append(f.name)
        self.assertEqual([], offenders,
                         "这些沙箱服务又自己拥有临时根了:改成 "
                         "tests/fixture_root.py 的 fixture_root(stack, '前缀-')")

    def test_no_test_launches_a_sandbox_server_itself(self):
        """起这些沙箱的那一侧必须是 `fixture_server`(它建根、传路径、finally 删)。

        调用点各写一份 `Popen` + `terminate` 的话:端口各挑一次(同跑会撞,症状是
        探针等不到页面),而那份库的所有权落在被 SIGTERM 的子进程手上。
        """
        owning = {f.stem for f in HERE.glob("serve_*.py")
                  if "fixture_root(" in f.read_text(encoding="utf-8")}
        self.assertTrue(owning, "一个自带沙箱根的服务都没找到,这条用例已经失效")
        offenders = []
        for f in sorted(HERE.glob("test_*.py")):
            if f.name == Path(__file__).name:
                continue
            offenders += [f"{f.name}:{line}"
                          for line in _server_names_in_code(f, owning)]
        self.assertEqual([], offenders,
                         "这些地方又自己起沙箱服务了 —— 那份临时库会留在盘上,"
                         "要走 tests.headless.fixture_server(script=...)")


@requires_headless
class FixtureOwnershipTests(unittest.TestCase):
    """沙箱服务那份临时库的所有权,在**起它的这一侧**。

    服务进程是被 SIGTERM 收掉的,而 Python 对没有 handler 的 SIGTERM 是直接结束 ——
    它那边 `with tempfile.TemporaryDirectory()` 的清理一行都不跑,一份完整的沙箱库
    就留在盘上(第 5 轮实测:定向回归之后仍残留 `kb-source-qa-*`,全量回归留下两个)。
    跟上面那几条是同一个所有权问题的另一半(那边是 profile 目录)。
    """

    def test_the_fixture_root_belongs_to_the_caller(self):
        import tempfile
        tmp = Path(tempfile.gettempdir())
        qa_before = set(tmp.glob("kb-source-qa-*"))
        roots_before = set(tmp.glob("kb-fixture-*"))
        with fixture_server() as base:
            self.assertTrue(base.startswith("http://127.0.0.1:"), base)
            mine = set(tmp.glob("kb-fixture-*")) - roots_before
            self.assertEqual(1, len(mine), mine)
            root = mine.pop()
            # **子进程不许自己另建一份**:它自己建的那种才是收不到清理机会的那一档
            self.assertEqual(qa_before, set(tmp.glob("kb-source-qa-*")),
                             "服务进程自己建了一份沙箱库 —— 它收不到清理机会")
        self.assertFalse(root.exists(), f"沙箱库 {root} 留在盘上了")


class FixtureRootOwnershipTests(unittest.TestCase):
    """子进程那一侧的口径:父进程给了根就用它、**而且不许删它**;没给才自己建。"""

    def test_a_given_root_is_used_and_left_alone(self):
        import contextlib
        import os
        import tempfile
        from tests.fixture_root import fixture_root
        given = tempfile.mkdtemp(prefix="kb-given-root-")
        try:
            os.environ["KB_FIXTURE_ROOT"] = given
            with contextlib.ExitStack() as stack:
                self.assertEqual(given, fixture_root(stack, "kb-unused-"))
            # 退出 stack 之后还在:删它是父进程的事(我们这边一个 SIGTERM 就没了)
            self.assertTrue(Path(given).is_dir(), "把父进程那份根删了")
        finally:
            os.environ.pop("KB_FIXTURE_ROOT", None)
            Path(given).rmdir()

    def test_without_one_it_makes_and_removes_its_own(self):
        """反过来钉一边:手动跑那个脚本时照旧自己建,收尾也照旧归自己。"""
        import contextlib
        import os
        from tests.fixture_root import fixture_root
        os.environ.pop("KB_FIXTURE_ROOT", None)
        with contextlib.ExitStack() as stack:
            mine = Path(fixture_root(stack, "kb-own-root-"))
            self.assertTrue(mine.is_dir())
            self.assertTrue(mine.name.startswith("kb-own-root-"), mine.name)
        self.assertFalse(mine.exists(), "自己建的那份没删掉")

    def test_an_empty_variable_counts_as_not_given(self):
        """空串是「没给」,不是「给了一个叫空的目录」(`Path('')` 是当前目录)。"""
        import contextlib
        import os
        from tests.fixture_root import fixture_root
        os.environ["KB_FIXTURE_ROOT"] = "   "
        try:
            with contextlib.ExitStack() as stack:
                mine = Path(fixture_root(stack, "kb-blank-root-"))
                self.assertTrue(mine.is_dir() and mine.is_absolute(), mine)
        finally:
            os.environ.pop("KB_FIXTURE_ROOT", None)


if __name__ == "__main__":
    unittest.main()
