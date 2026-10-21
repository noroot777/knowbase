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

# `test_no_test_launches_a_sandbox_server_itself` 唯一的例外对象(同上,理由在那条用例里)
#: 夹具自退那一块的测试(拆成单元/后果两份);开例外的理由在守卫那一条里。
LIFE_TESTS = {"test_fixture_life.py", "test_fixture_life_outcome.py"}


# 类级 fixture 会建出来的那几种东西,和"收尾"长什么样
_MAKERS = ('TemporaryDirectory', 'mkdtemp', 'Popen', 'fixture_server', 'ExitStack')
_CLEANERS = ('cleanup', 'rmtree', 'terminate', 'kill', 'close', '__exit__', 'reap')


def _calls(node: ast.AST) -> list[str]:
    """`node` 子树里所有被调用的名字(`a.b()` 取 `b`)。"""
    out = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            out.append(getattr(sub.func, 'id', None) or getattr(sub.func, 'attr', '') or '')
    return out


def _bare_calls(node: ast.AST) -> set[str]:
    """`node` 子树里**以裸名字**调用的那些函数(`run_probe()` 算,`self.run_probe()` 不算)。

    和 `_headless_names` 一起回答「这一句调的到底是不是 `tests.headless` 那个」,各挡一
    种同名形态:这一道挡**方法桩**(`tests/test_x_login_import.py` 里真有一个
    `self.run_probe`,那一组自己的桩,压根不起浏览器),那一道挡**模块级同名函数**。
    **如实记着:今天哪一道单独去掉都不红** —— 树里只有前一种形态,而它被两道同时挡着
    (那份文件压根不导入 `headless`)。留着是因为两种形态都会把判据搞反:误报会逼着
    下一个人去给一个不起浏览器的类挂能力门。
    """
    return {sub.func.id for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)}


def _headless_names(tree: ast.AST) -> set[str]:
    """这份文件从 `tests.headless` 里导进来的名字(判据的另一半,见 `_bare_calls`)。"""
    return {a.asname or a.name for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom) and (n.module or '').endswith('headless')
            for a in n.names}


def _class_fixture_offenders(path: Path) -> list[str]:
    """`setUpClass` 建了资源、却把回收押在 `tearDownClass` 上的那些类。"""
    bad = []
    # 带上 filename:不带的话编译告警一律报 `<unknown>:行号`,看不出是哪份文件
    for node in ast.walk(ast.parse(path.read_text(encoding='utf-8'), str(path))):
        if not isinstance(node, ast.ClassDef):
            continue
        methods = {m.name: m for m in node.body
                   if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))}
        setup = methods.get('setUpClass')
        if setup is None or not any(c in _MAKERS for c in _calls(setup)):
            continue
        if 'addClassCleanup' not in _calls(setup):
            bad.append(f'{path.name}:{setup.lineno} {node.name} 没在建完时登记 class cleanup')
        teardown = methods.get('tearDownClass')
        if teardown is not None and any(c in _CLEANERS for c in _calls(teardown)):
            bad.append(f'{path.name}:{teardown.lineno} {node.name} 的回收还押在 tearDownClass 上')
    return bad


def _server_names_in_code(path: Path, names: set[str]) -> list[int]:
    """`path` 的**代码**里提到这些沙箱服务名的行号(文档和注释不算)。

    判据是「提到名字」而不是「这一行上有 Popen」:路径常量、cmd 列表、
    `sys.executable` 往往分散在好几行上,按窗口往后看的写法漏得一声不响(实测:
    `SERVER = ...` 写在 Popen 前两行就漏掉了)。这个名字只该出现在一个位置 ——
    `fixture_server(script=...)` 的实参,所以那一处放过。

    走 AST 而不是逐行 `in`:docstring 里本来就会写「数据由 tests/serve_reading.py
    摆出来」,逐行扫会把它当违例(第一版就是这么误报的),而注释压根不进 AST。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
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

    def test_every_test_class_that_runs_a_probe_carries_the_capability_gate(self):
        """起探针的测试类必须挂 `@requires_headless`,**一个都不许漏**。

        CI 刻意不装无头壳,而 `probe_env()` 一进去就断言失败 —— 同一份 discovery 里
        别的浏览器类都如实 skip 并说清怎么补,漏掉那一个报的是红,而红的原因和被测
        的那件事毫无关系(实测形态:新加一个浏览器类忘了挂,在没有无头壳的机器上
        `FAILED (failures=1, skipped=1)`)。**这是结构守卫,行为用例证明不了它** ——
        有壳的机器上漏没漏一个字都看不出来。

        判据落在**导入**和**裸名字调用**上:`self.run_probe()` 那种同名桩不算
        (理由在 `_bare_calls`)。
        """
        offenders = []
        for f in sorted(HERE.glob("test_*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), str(f))
            gated = {"run_probe", "probe_env"} & _headless_names(tree)
            if not gated:
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.ClassDef) or not (_bare_calls(node) & gated):
                    continue
                decos = {getattr(d, "id", None) or getattr(d, "attr", "")
                         for d in node.decorator_list}
                if "requires_headless" not in decos:
                    offenders.append(f"{f.name}:{node.name}")
        self.assertEqual([], offenders,
                         "这些测试类起探针却没挂 @requires_headless —— 没装无头壳的"
                         "机器上它们会报红而不是 skip,给它们加上那个装饰器")

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
            # `test_fixture_life.py` 是唯一的例外,而且必须是:它那条验的正是
            # **夹具自己**退出时删不删得掉那份根,所以不能给 `KB_FIXTURE_ROOT`
            # ——走 fixture_server 的话根归父进程,它的 finally 会把结论盖掉,
            # 被测的实现(收场发哪个信号)坏掉了也照样全绿。
            if f.name in LIFE_TESTS:
                continue
            offenders += [f"{f.name}:{line}"
                          for line in _server_names_in_code(f, owning)]
        self.assertEqual([], offenders,
                         "这些地方又自己起沙箱服务了 —— 那份临时库会留在盘上,"
                         "要走 tests.headless.fixture_server(script=...)")


    def test_class_fixtures_register_their_cleanup_when_they_build_it(self):
        """类级 fixture 必须**建完就登记** `addClassCleanup`,不许只写 `tearDownClass`。

        `setUpClass` 抛异常之后 unittest **不调** `tearDownClass`(只跑
        `doClassCleanups`)—— 而 `setUpClass` 里建完资源之后往往还有一长段会抛的活:
        摆盘、起服务、等端口、跑探针、解析回包。实测过的后果是服务连端口留在后台;
        第 6 轮按代码指出的是"回包非空但不是合法 JSON"那一支(`json.loads` 在清理域
        外面),服务和整份临时库都留着。

        判据落在**源码结构**上,因为这几条路正常跑一次都走不到:行为用例全绿也证明
        不了。`tearDownClass` 本身不禁止(恢复全局之类),禁的是把**回收**押在它上面。
        """
        offenders = []
        for f in sorted(HERE.glob("test_*.py")):
            offenders += _class_fixture_offenders(f)
        self.assertEqual([], offenders,
                         "这几处又把类级资源的回收押在 tearDownClass 上了:"
                         "建完立刻 cls.addClassCleanup(...)")

    def test_the_probe_profile_root_is_handed_over_by_the_caller(self):
        """临时 profile 的根由 `run_probe` 给、由它删(`KB_PROBE_PROFILE_ROOT`)。

        后果那一条在 `tests/test_probe_leak.py`(超时之后目录还在不在);这一条只
        钉住两侧都认同一个变量名 —— 哪天一侧改了名字,两边各自都"正确",而目录又
        回到那个会被 SIGKILL 的进程手上。
        """
        shell = (HERE / "probe_shell.mjs").read_text(encoding="utf-8")
        headless = (HERE / "headless.py").read_text(encoding="utf-8")
        # 判据要落在**真正读写它的那一句**上,不是"文件里出现过这个词":注释和
        # docstring 里到处都写着它,按词找的话一侧改了名字照样全绿(实测注入 0 红)。
        self.assertIn("process.env.KB_PROBE_PROFILE_ROOT", shell,
                      "探针那一侧不认父进程给的根了")
        self.assertIn('PROFILE_ROOT_ENV = "KB_PROBE_PROFILE_ROOT"', headless,
                      "run_probe 那一侧的变量名改了")
        self.assertIn("child_env[PROFILE_ROOT_ENV] = profiles", headless,
                      "run_probe 不把根传下去了")
        self.assertIn("rmtree(profiles", headless, "run_probe 自己不删那个根了")
        others = [f.name for f in sorted(HERE.glob("*.mjs"))
                  if f.name != "probe_shell.mjs"
                  and "KB_PROBE_PROFILE_ROOT" in f.read_text(encoding="utf-8")]
        self.assertEqual([], others, "这些探针自己去读那个根了 —— 口径只有一份")


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
