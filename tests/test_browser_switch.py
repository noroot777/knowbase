# -*- coding: utf-8 -*-
"""「这一轮不跑浏览器回归」那个开关(`KB_SKIP_BROWSER_TESTS`)。

为什么要有它:起无头浏览器的那一组是全量回归的时间大头(整轮约 13 分钟,这一组
占掉其中大半),而**不给 `HEADLESS_SHELL` 并不等于不跑** —— `tests.headless` 的
口径会自己去 playwright 缓存里找一份(本机实测找到了),所以「我没给路径」关不掉它。

这里钉三件事:
1. **开关认哪些写法**,以及**反过来**:没写、写了别的一律当没关(误判成关掉 =
   一轮什么界面都没验、而收尾那行写着 OK);
2. **两个入口各自跳过,而且理由里说得出是人关的** —— 「人关的」和「这台机器缺
   东西」必须是两句话:一句 `OK (skipped=30)` 在两种情形下长得一模一样;
3. **结构:自己起浏览器的回归都得过这两道闸**。这一条是这个开关真正的射程 ——
   原先 `test_cdp_attach` / `test_x_article_dom` / `test_x_article_fidelity` 各写
   一份 `skipUnless(SHELL is not None, ...)`,它们起的也是真浏览器,而开关只认得
   过 `tests.headless` 那两个装饰器的那几组,**关不住它们**;
4. **闸件只许从 `tests.headless` 取**,不许隔着别的测试模块伸手拿。上面那条按
   「从 `tests.headless` 导入了哪几个名字」判,所以它**看不见**这一路:
   `test_x_access_gate` 自己不导入无头壳,而是写 `dom.SHELL` / `dom.INSTALL_HINT`
   从 `test_x_post_dom` 的命名空间里借。那边收进统一闸、不再导入 `INSTALL_HINT`
   之后,这一行在**导入期**就 AttributeError —— 不是跳过、不是失败,是整个模块
   连带着 `from tests.test_x_access_gate import gate` 的那一组一起 load 不起来
   (实测两条 ERROR、少跑 16 条用例,而收尾只写 `FAILED (errors=2)`)。
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import headless

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent

#: 从 `tests.headless` 导入这几个名字 = 这一组要起真浏览器,必须挂闸。
BROWSER_NAMES = frozenset({'run_probe', 'fixture_server', 'SHELL'})
#: 闸(口径只有一份,都在 `tests.headless` 里):两个现成包装,加上它们共用的
#: 底座 `browser_gate`(工具要求特殊的组直接挂它,如原生 Chrome 那组)。
GATES = frozenset({'requires_headless', 'requires_shell', 'browser_gate'})
#: 闸件:只许从 `tests.headless` 取,隔着别的测试模块拿到的不算。
GATE_PARTS = frozenset({'SHELL', 'NODE', 'INSTALL_HINT'})
#: 断言「跳过理由」的用例:开关排在缺件之前,所以它们必须自己把开关清掉。
#: 一个是属性(`headless.INSTALL_HINT`),一个是 `getattr` 的那个字符串字面量。
REASON_ATTRS = frozenset({'INSTALL_HINT'})
REASON_KEYS = frozenset({'__unittest_skip_why__'})


class SwitchTests(unittest.TestCase):
    def test_what_counts_as_turned_off(self):
        for value in ('1', 'true', 'TRUE', 'Yes', ' on ', 'On'):
            with patch.dict('os.environ', {headless.SKIP_ENV: value}):
                self.assertTrue(headless.browser_tests_off(), repr(value))

    def test_anything_else_still_runs_them(self):
        """**反过来钉一边**:没写、写了别的,一律当没关。

        方向是不对称的:误判成「人关了」的后果是一轮界面都没验,而收尾那行照旧
        写着 OK —— 这正是本仓库最怕的那种绿。
        """
        for value in ('', ' ', '0', 'no', 'off', 'false', '2', 'yes please'):
            with patch.dict('os.environ', {headless.SKIP_ENV: value}):
                self.assertFalse(headless.browser_tests_off(), repr(value))
        with patch.dict('os.environ', {}, clear=True):
            self.assertFalse(headless.browser_tests_off(), '压根没设')

    def test_both_gates_skip_and_say_it_was_a_human_choice(self):
        """**两份实现各有自己的红**:两道闸各走一遍,各自的理由都要点出那个变量名。

        **每道闸各用一个新类**:`unittest.skip` 是**就地**往类上写
        `__unittest_skip__` 的,两道闸共用一个类时第一道留下的痕迹会被第二道
        认成自己的 —— 定点注入实测:只给其中一道挂开关,这一条照旧全绿。
        """
        for gate in (headless.requires_headless, headless.requires_shell):
            target = type('Target', (unittest.TestCase,), {})
            with patch.dict('os.environ', {headless.SKIP_ENV: '1'}):
                decorated = gate(target)
            self.assertTrue(getattr(decorated, '__unittest_skip__', False), gate.__name__)
            why = getattr(decorated, '__unittest_skip_why__', '')
            self.assertIn(headless.SKIP_ENV, why, f'{gate.__name__} 没说清是人关的:{why}')
            self.assertIn('没有验过界面', why, why)

    def test_the_gates_let_them_through_when_the_switch_is_not_set(self):
        """**反过来钉一边**:开关没开时两道闸都不许自己加 skip。

        本机有 node 也有无头壳,所以这一局只在装得起浏览器的机器上有意义 ——
        缺件的机器上它跳过(那正是闸该做的事)。
        """
        if headless.SHELL is None or headless.NODE is None:
            self.skipTest('这台机器本来就缺无头壳或 node')
        with patch.dict('os.environ', {}, clear=True):
            for gate in (headless.requires_headless, headless.requires_shell):
                target = type('Target', (unittest.TestCase,), {})
                self.assertFalse(getattr(gate(target), '__unittest_skip__', False),
                                 gate.__name__)

    def test_every_browser_regression_goes_through_one_of_the_two_gates(self):
        """**开关的射程**:谁起真浏览器,谁就得挂闸 —— 否则它关不住。

        判据按**导入了哪几个名字**走 AST(注释和 docstring 里要能照常讨论它们);
        `tests/test_headless_shell.py` 验的是「怎么找那份壳」本身,它导入的是模块、
        一个浏览器都不起,所以自然落在射程外。
        """
        offenders = []
        for path in sorted(TESTS.glob('test_*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            imported = {alias.name for node in ast.walk(tree)
                        if isinstance(node, ast.ImportFrom) and node.module == 'tests.headless'
                        for alias in node.names}
            if not (imported & BROWSER_NAMES):
                continue
            if not (imported & GATES):
                offenders.append(f'{path.name}:没导入任何一道闸')
        self.assertEqual([], offenders,
                         f'这几组起真浏览器却不过闸,{headless.SKIP_ENV} 关不住它们')

    def test_no_module_borrows_the_gate_parts_through_another_test_module(self):
        """**闸件只许从 `tests.headless` 取** —— 隔着别的测试模块拿就会在导入期炸。

        上一条按 import 判,这一条按**属性取用**判(`<别的模块>.SHELL`):两条判的
        不是同一件事,所以缺了这条,`dom.SHELL` 那一路在上一条眼里是干净的。
        自己人用 `headless.SHELL` 照旧放行(**反过来钉一边**:本文件第 2 条用例
        就是这么写的,它不该被自己这条红咬到)。
        """
        offenders = []
        for path in sorted(TESTS.glob('test_*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute) or node.attr not in GATE_PARTS:
                    continue
                base = node.value
                if isinstance(base, ast.Name) and base.id == 'headless':
                    continue
                where = ast.unparse(node) if hasattr(ast, 'unparse') else node.attr
                offenders.append(f'{path.name}:{node.lineno}:{where}')
        self.assertEqual([], offenders,
                         '闸件要走 tests.headless 的出口:被借的那个模块一改导入,'
                         '这里就是导入期 AttributeError,整个模块 load 不起来')


    def _run(self, module, *, switch):
        """子进程里跑一组用例,返回 (returncode, 尾部输出)。"""
        env = {**os.environ, 'PYTHONPATH': f'{ROOT}{os.pathsep}{TESTS}'}
        if switch:
            env[headless.SKIP_ENV] = '1'
        else:
            env.pop(headless.SKIP_ENV, None)
        done = subprocess.run([sys.executable, '-m', 'unittest', module],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)
        return done.returncode, (done.stderr or done.stdout)[-1200:]

    def test_turning_the_switch_on_does_not_fail_the_tests_that_still_run(self):
        """**后果**:按说明关掉浏览器回归再跑,剩下的必须照旧全绿 —— 不是「少几条红」。

        开关只该让起浏览器的那些跳过。而 `browser_gate` 把「人显式关了」排在缺件
        之前,于是**断言缺件提示**的那一组(`tests/test_headless_shell.py`,它一个
        浏览器都不起)在开关落进环境时拿到的是开关那句话:实测 `KB_SKIP_BROWSER_TESTS=1`
        跑全量 3471 条,红 `test_skip_message_says_how_to_install` 和
        `test_missing_node_is_named_too` 两条 —— 「省时间」变成「拿不到通过结果」,
        而人会以为是自己机器的问题。

        **两种模式各跑一次**(反过来钉一边):开着要绿,关着也要绿。判据落在
        子进程的退出码上,因为这正是人在命令行里看到的那个结果。
        """
        for switch in (True, False):
            with self.subTest(switch=switch):
                code, tail = self._run('tests.test_headless_shell', switch=switch)
                self.assertEqual(0, code, tail)

    def test_modules_that_assert_a_skip_reason_deal_with_the_switch(self):
        """上面那条按后果判、只盯现有这一组;这条是**类守卫**,拦住以后新写的。

        判据:真去读「跳过理由」的 test 文件(属性 `INSTALL_HINT`,或 `getattr`
        那个 `__unittest_skip_why__`),都必须显式处理这个开关 —— 要么清掉它(缺件
        提示那一组),要么本来就在验它(本文件)。

        **走 AST,不按文本**:注释和 docstring 里要能照常讨论这些名字 ——
        按文本判的那一版当场咬住了 `test_x_access_gate.py`,而它只是在注释里讲
        「原来隔着 `dom.INSTALL_HINT` 借」这段历史,一行代码都没读过跳过理由。
        这条只当早一道提醒,真正的闸是上面那条跑出来的退出码。
        """
        offenders = []
        for path in sorted(TESTS.glob('test_*.py')):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            reads = any(
                (isinstance(node, ast.Attribute) and node.attr in REASON_ATTRS)
                or (isinstance(node, ast.Constant) and node.value in REASON_KEYS)
                for node in ast.walk(tree))
            if not reads:
                continue
            names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
            names |= {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
            names |= {node.value for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)}
            if 'SKIP_ENV' not in names and headless.SKIP_ENV not in names:
                offenders.append(path.name)
        self.assertEqual([], offenders,
                         f'这几组断言跳过理由却没管 {headless.SKIP_ENV}:'
                         '开关一开,它们拿到的是开关那句话')


if __name__ == '__main__':
    unittest.main()
