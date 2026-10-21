# -*- coding: utf-8 -*-
"""回归分层入口的回归(tests/tiers.py + tests/headless.py 的浏览器闸门)。

四条口径,各钉两面(完整语境 docs/decisions/test-tiers.md):

- **闸门与标记只有 browser_gate 一处**(requires_headless / requires_shell 是它
  的包装):设了 `KB_TEST_NO_BROWSER` 就整类拦下,skip 消息写清出路;缺工具按调
  用点自己的说法 skip,但 **kb_browser 标记无论如何都得带着** —— probes 层的覆盖
  账全靠它。反过来钉:没设变量、工具齐备就放行,requires_shell 不许强加 node。
- **层对开关的态度必须显式**:fast 设上,probes/full 清掉继承值 —— 不清的话,
  外头 export 过一次的壳里,工具齐备、显式全量照样把浏览器回归整层拦掉。
- **纯逻辑邻居的通过不许充当浏览器覆盖**:probes 按 kb_browser 标记记账,账要在
  run 之前收集(unittest 跑完会释放 suite 条目);**覆盖只认 TierResult 的正向
  执行事件,不许用「计划减 skip」反推** —— setUpClass/setUpModule 抛 SkipTest 时
  unittest 记的是 holder 不是方法 id,subTest 的 skip 记的是带参数的子例 id,
  反推会把一条都没开始的整组算成全跑了;空集合(一条浏览器用例都没发现)在
  probes 同样按失败。
- **浏览器工具可用性的门不许在测试文件里自己写**(结构守卫,AST 判据):事故形态
  是七组共 90 条真实浏览器用例各自 `skipUnless(SHELL/_REAL_CHROME...)`,不认快速
  层开关 —— fast 照样起浏览器;其中两组不引用 tests.headless,又漏出 probes 名单。
- **probes 名单现算,不许手工维护**:口径是「文件源码引用 tests.headless」,
  配合上一条(起浏览器的类必须挂共享闸门 → 必然 import tests.headless)圈全。
"""

import ast
import io
import os
import re
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

from tests import headless, tiers


class BrowserGateTests(unittest.TestCase):

    def test_the_gate_blocks_browser_classes_and_names_the_way_out(self):
        with mock.patch.dict(os.environ, {headless.NO_BROWSER_ENV: "1"}):

            @headless.requires_headless
            class Probe(unittest.TestCase):
                def test_anything(self):
                    pass

        self.assertTrue(getattr(Probe, "__unittest_skip__", False),
                        "设了快速层变量,浏览器类却没有被拦下")
        reason = Probe.__unittest_skip_why__
        self.assertIn(headless.NO_BROWSER_ENV, reason)
        # skip 消息要写清出路,不许只说「跳过了」
        self.assertIn("./kb.sh test", reason)
        # 被拦的也得带标记:分层统计按它数「浏览器用例该有几条」
        self.assertTrue(Probe.kb_browser)

    def test_without_the_gate_nothing_extra_is_blocked(self):
        # 反过来钉一边:没设变量时,放不放行只取决于工具。齐备(打桩成在)就必须
        # 放行 —— 闸门不许把本来能跑的也拦了;标记照样要带。
        with mock.patch.dict(os.environ), \
                mock.patch.object(headless, "NODE", "node"), \
                mock.patch.object(headless, "SHELL", Path("/stub/shell")):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.requires_headless
            class Probe(unittest.TestCase):
                def test_anything(self):
                    pass

        self.assertFalse(getattr(Probe, "__unittest_skip__", False),
                         "没设快速层变量,类却被拦下了")
        self.assertTrue(Probe.kb_browser)

    def test_a_blank_value_means_off(self):
        # 口径与其它 KB_* 开关一致:空白串算没设。
        with mock.patch.dict(os.environ, {headless.NO_BROWSER_ENV: "  "}), \
                mock.patch.object(headless, "NODE", "node"), \
                mock.patch.object(headless, "SHELL", Path("/stub/shell")):

            @headless.requires_headless
            class Probe(unittest.TestCase):
                def test_anything(self):
                    pass

        self.assertFalse(getattr(Probe, "__unittest_skip__", False))

    def test_missing_tools_still_leave_the_mark(self):
        # 缺工具被 skip 的类也得带 kb_browser:不带的话,probes 的覆盖账少记它,
        # 「浏览器零覆盖」恰好在最需要报警的那台机器上看不出来。
        with mock.patch.dict(os.environ):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate(["原生 Chrome(这一组验 SingletonLock)"])
            class Probe(unittest.TestCase):
                def test_anything(self):
                    pass

        self.assertTrue(Probe.kb_browser)
        self.assertTrue(getattr(Probe, "__unittest_skip__", False))
        self.assertIn("原生 Chrome", Probe.__unittest_skip_why__)

    def test_requires_shell_does_not_demand_node(self):
        # DOM 那几组是 Python 直驱 CDP,跟 node 无关 —— 共享闸门不许强加统一依赖。
        with mock.patch.dict(os.environ), \
                mock.patch.object(headless, "NODE", None), \
                mock.patch.object(headless, "SHELL", Path("/stub/shell")):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.requires_shell
            class Probe(unittest.TestCase):
                def test_anything(self):
                    pass

        self.assertFalse(getattr(Probe, "__unittest_skip__", False),
                         "只要壳的类被 node 缺席拦下了")
        self.assertTrue(Probe.kb_browser)


class TierEnvTests(unittest.TestCase):

    def test_fast_sets_and_other_tiers_clear_the_inherited_gate(self):
        # probes/full 必须把继承值清掉:不清的话,外头 export 过一次的壳里,
        # 工具齐备、显式全量照样把浏览器回归整层拦掉,而且一切显示正常。
        with mock.patch.dict(os.environ, {headless.NO_BROWSER_ENV: "1"}):
            tiers._apply_tier_env("probes")
            self.assertNotIn(headless.NO_BROWSER_ENV, os.environ)
            os.environ[headless.NO_BROWSER_ENV] = "1"
            tiers._apply_tier_env("full")
            self.assertNotIn(headless.NO_BROWSER_ENV, os.environ)
            tiers._apply_tier_env("fast")
            self.assertEqual("1", os.environ[headless.NO_BROWSER_ENV])


class BrowserCoverageTests(unittest.TestCase):
    """覆盖账只认 TierResult 的正向执行事件,纯逻辑邻居的通过不作数。

    事故形态有两代(都是审查实测):缺壳时浏览器用例全 skip、几百条邻居通过,
    probes 照样报成功;修掉它的第一版用「计划 id 减 skipped id」反推,而
    setUpClass/setUpModule 抛 SkipTest 时 unittest 记的是 `setUpClass (模块.类)`
    这种 holder、subTest 的 skip 记的是带参数的子例 id —— 都对不上方法 id,
    一条没开始的整组照样被算成「全跑了」(Ran 0 tests + OK + 真跑 2/2)。
    """

    def _run(self, suite):
        ids = tiers.browser_case_ids(suite)   # run 之前收账:跑完条目就被释放了
        result = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0,
            resultclass=tiers.TierResult).run(suite)
        return ids, result

    def test_skipped_browser_cases_do_not_count_as_coverage(self):
        with mock.patch.dict(os.environ):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate(["根本不存在的工具"])
            class Browserish(unittest.TestCase):
                def test_browser(self):
                    pass

        class Neighbour(unittest.TestCase):
            def test_logic(self):
                pass

        suite = unittest.TestSuite([
            unittest.defaultTestLoader.loadTestsFromTestCase(Browserish),
            unittest.defaultTestLoader.loadTestsFromTestCase(Neighbour)])
        ids, result = self._run(suite)
        total, ran = tiers.browser_coverage(ids, result)
        self.assertTrue(result.wasSuccessful(), "邻居通过,unittest 本身是绿的")
        self.assertEqual((1, 0), (total, ran),
                         "浏览器用例全 skip 时覆盖必须是 0,邻居的通过不作数")

    def test_an_executed_browser_case_counts(self):
        # 反过来钉:真跑了的要记上,别把部分覆盖误报成零覆盖。
        with mock.patch.dict(os.environ):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate([])
            class Browserish(unittest.TestCase):
                def test_browser(self):
                    pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Browserish)
        ids, result = self._run(suite)
        self.assertEqual((1, 1), tiers.browser_coverage(ids, result))

    def test_a_class_level_fixture_skip_counts_nothing_as_ran(self):
        # setUpClass 抛 SkipTest:两条方法一条都没开始,skipped 里只有 holder。
        with mock.patch.dict(os.environ):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate([])
            class ClassSkipBrowserish(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest("夹具不可用")

                def test_one(self):
                    pass

                def test_two(self):
                    pass

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            ClassSkipBrowserish)
        ids, result = self._run(suite)
        self.assertEqual((2, 0), tiers.browser_coverage(ids, result),
                         "类级 skip 的组一条都没开始,不许算成跑了")

    def test_a_module_level_fixture_skip_counts_nothing_as_ran(self):
        # setUpModule 抛 SkipTest:同理,skipped 里只有 `setUpModule (模块)`。
        fake = types.ModuleType("kb_tiers_module_skip_probe")

        def setUpModule():
            raise unittest.SkipTest("模块夹具不可用")

        fake.setUpModule = setUpModule
        with mock.patch.dict(os.environ), \
                mock.patch.dict(sys.modules, {fake.__name__: fake}):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate([])
            class ModuleSkipBrowserish(unittest.TestCase):
                def test_one(self):
                    pass

            ModuleSkipBrowserish.__module__ = fake.__name__
            suite = unittest.defaultTestLoader.loadTestsFromTestCase(
                ModuleSkipBrowserish)
            ids, result = self._run(suite)
        self.assertEqual((1, 0), tiers.browser_coverage(ids, result),
                         "模块级 skip 的组一条都没开始,不许算成跑了")

    def test_skipped_subtests_do_not_count_as_full_coverage(self):
        # 子例 skip 记的是带参数的子例 id,对不上方法 id —— 正向口径按
        # 「跑了一半不算验全」记:方法开始了,但该验的那部分没验。
        with mock.patch.dict(os.environ):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate([])
            class SubtestSkipBrowserish(unittest.TestCase):
                def test_parts(self):
                    with self.subTest(part=1):
                        self.skipTest("这一半没跑")

        suite = unittest.defaultTestLoader.loadTestsFromTestCase(
            SubtestSkipBrowserish)
        ids, result = self._run(suite)
        self.assertEqual((1, 0), tiers.browser_coverage(ids, result),
                         "子例被 skip 的方法不算验全")


class TierVerdictTests(unittest.TestCase):
    """main 的层判定,各钉一边:probes 没验全必败(零覆盖、空集合、类级 skip
    都是它的形态)、fast 有浏览器用例真跑即失败(闸门失效的运行时信号)、full
    沿用 README/CI 那条 discover 的「缺工具自然 skip」政策不因部分跳过翻红,
    但覆盖数要如实;汇总的执行条数不许出现负数。"""

    def _browser_class(self, missing, name):
        # 账本按 test id(模块.类.方法)记,两个临时类不许同名 —— 同名会在
        # set 里合并成一条,账目失真。
        with mock.patch.dict(os.environ):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate(missing)
            class Browserish(unittest.TestCase):
                def test_browser(self):
                    pass

        Browserish.__name__ = Browserish.__qualname__ = name
        return Browserish

    def _fixture_broken_class(self, exc, name):
        # setUpClass 一上来就抛:两条方法一条都不会开始。
        with mock.patch.dict(os.environ):
            os.environ.pop(headless.NO_BROWSER_ENV, None)

            @headless.browser_gate([])
            class Browserish(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise exc

                def test_one(self):
                    pass

                def test_two(self):
                    pass

        Browserish.__name__ = Browserish.__qualname__ = name
        return Browserish

    def _main(self, tier, make_suite):
        import contextlib
        with mock.patch.object(tiers, "build_suite", lambda t: make_suite()), \
                mock.patch.dict(os.environ), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = tiers.main([tier])
        return rc, err.getvalue()

    def _partial_suite(self):
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        return unittest.TestSuite([
            load(self._browser_class(["根本不存在的工具"], "SkippedBrowserish")),
            load(self._browser_class([], "RanBrowserish")),
            load(type("Neighbour", (unittest.TestCase,),
                      {"test_logic": lambda self: None}))])

    def test_probes_fails_when_browser_cases_are_left_unrun(self):
        rc, err = self._main("probes", self._partial_suite)
        self.assertEqual(1, rc, err)
        self.assertIn("没真跑", err)
        self.assertIn("根本不存在的工具", err)   # 缺什么要在默认输出里看得到

    def test_probes_passes_when_all_browser_cases_ran(self):
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        rc, err = self._main(
            "probes", lambda: load(self._browser_class([], "RanBrowserish")))
        self.assertEqual(0, rc, err)

    def test_full_keeps_the_natural_skip_policy(self):
        rc, err = self._main("full", self._partial_suite)
        self.assertEqual(0, rc, err)
        self.assertIn("真跑 1/2", err)   # 但覆盖数要如实打出来

    def test_fast_fails_if_a_browser_case_actually_ran(self):
        # 套件里有一个没被拦住的浏览器类(模拟闸门/层表态坏了):fast 必须翻红。
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        rc, err = self._main(
            "fast", lambda: load(self._browser_class([], "RanBrowserish")))
        self.assertEqual(1, rc, err)
        self.assertIn("闸门失效", err)

    def test_probes_rejects_an_empty_browser_set(self):
        # 空集合不是「没什么可验」,是「这层什么都没验」:名单口径或标记坏了时
        # 恰好表现为 0 条浏览器用例,不许借「0 < 0 为假」混成成功。
        rc, err = self._main("probes", unittest.TestSuite)
        self.assertEqual(1, rc, err)
        self.assertIn("一条浏览器用例都没发现", err)

    def test_probes_rejects_a_logic_only_suite(self):
        # 同一面的另一形态:只有纯逻辑邻居时 total == ran == 0,同样不许绿。
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        rc, err = self._main("probes", lambda: load(
            type("LogicOnlyNeighbour", (unittest.TestCase,),
                 {"test_logic": lambda self: None})))
        self.assertEqual(1, rc, err)
        self.assertIn("一条浏览器用例都没发现", err)

    def test_a_class_level_skip_fails_probes_with_sane_counts(self):
        # 类级 SkipTest 走完 main 全链路:必须红,且汇总里的执行条数是 0 ——
        # 旧算法(testsRun 减 skipped 条数)在这里打出 -1。
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        rc, err = self._main("probes", lambda: load(self._fixture_broken_class(
            unittest.SkipTest("夹具不可用"), "ClassSkipViaMain")))
        self.assertEqual(1, rc, err)
        self.assertIn("真跑 0/2", err)
        self.assertIn("跑了 0 条", err)
        self.assertNotIn("跑了 -", err)

    def test_a_class_fixture_error_reports_honest_coverage(self):
        # setUpClass 抛普通异常:rc 本来就红(wasSuccessful 为假),但覆盖数
        # 也得如实 —— 一条都没开始就是 0/2,不许打成 2/2。
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        rc, err = self._main("full", lambda: load(self._fixture_broken_class(
            RuntimeError("夹具坏了"), "ClassErrorViaMain")))
        self.assertEqual(1, rc, err)
        self.assertIn("真跑 0/2", err)


class AvailabilityGateGuardTests(unittest.TestCase):
    """浏览器工具可用性的门只许走 tests.headless 的共享闸门(结构守卫)。

    判据走 AST 的 skipUnless/skipIf **条件源码**(docstring 和注释不进射程,
    口径同 tests/test_probe_contract.py 的结构守卫):条件里出现 shell/chrome
    一族的,就是在自己做浏览器门 —— 它不认快速层开关,fast 层照样起浏览器;
    也不带 kb_browser 标记,probes 层数不到它。
    """

    _BROWSERISH = re.compile(r"shell|chrome", re.I)

    def test_no_private_browser_availability_gates(self):
        bad = []
        for path in sorted(tiers.TESTS_DIR.glob("test_*.py")):
            src = path.read_text(encoding="utf-8")
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) \
                    else getattr(func, "id", "")
                if name not in ("skipUnless", "skipIf"):
                    continue
                cond = ast.get_source_segment(src, node.args[0]) or ""
                if self._BROWSERISH.search(cond):
                    bad.append(f"{path.name}:{node.lineno} 条件 {cond!r}")
        self.assertEqual([], bad,
                         "浏览器工具的门必须走 tests.headless 的"
                         " browser_gate/requires_headless/requires_shell:\n"
                         + "\n".join(bad))

    def test_the_shared_gates_are_actually_in_use(self):
        # 反过来钉:守卫保护的东西得真实存在 —— 共享闸门在测试树里被广泛使用。
        users = [p.name for p in sorted(tiers.TESTS_DIR.glob("test_*.py"))
                 if re.search(r"requires_shell|requires_headless|browser_gate",
                              p.read_text(encoding="utf-8"))]
        self.assertGreaterEqual(len(users), 25, users)


class ProbeRosterTests(unittest.TestCase):

    def test_the_roster_is_computed_and_catches_the_heavy_probes(self):
        names = tiers.probe_modules()
        # 最重的那几个探针文件必须在册 —— 它们落进 fast 层,分层就白做了。
        self.assertIn("tests.test_subs_layout", names)
        self.assertIn("tests.test_probe_leak", names)
        self.assertIn("tests.test_x_channels", names)
        # 审查抓到的那两个漏网文件:原生 Chrome 那组、借 dom.SHELL 的那组。
        self.assertIn("tests.test_chrome_shutdown", names)
        self.assertIn("tests.test_x_access_gate", names)
        self.assertGreaterEqual(len(names), 20, names)

    def test_pure_logic_files_stay_out(self):
        # 反面:不引用 tests.headless 的纯逻辑文件不许被圈进 probes 层。
        names = tiers.probe_modules()
        self.assertNotIn("tests.test_source_aliases", names)

    def test_every_name_on_the_roster_is_a_real_file(self):
        for name in tiers.probe_modules():
            stem = name.rsplit(".", 1)[-1]
            self.assertTrue((tiers.TESTS_DIR / f"{stem}.py").is_file(), name)


if __name__ == "__main__":
    unittest.main()
