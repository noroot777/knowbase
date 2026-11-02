# -*- coding: utf-8 -*-
"""full 层的两路并行(tests/tier_split.py)与按标记切分(tests/tiers.py)。

- **两路不重不漏**:按 kb_browser 标记切,合起来等于整套;导入失败的那种落在逻辑
  那一路照样报红;类的嵌套保住,setUpClass 在各自那一路里各跑一次。
- **两路真的同时跑,各用自己的数据目录**;任一路红、或者没交回账本,整轮就红。
- **只有两路合起来把自测全跑绿了才落证**;自测这一轮跳过、或有一条没绿,都不落。
- **跳过的自测在一路里真被拿掉了**,其余用例一条不少。
- **收尾归父进程**:SIGTERM 进来,两路连同它们自己都得没了;父进程被 SIGKILL 时,
  子进程靠 watch_parent 自己退;**盯父进程那根线程不许碰全局 time.sleep**(有的用例
  打桩它记节拍,轮询线程会往桩里灌拍子)。

子进程一律是几行 `python -c` 的替身,不起真的整套(那就是在执行回合里跑全量了)。
"""

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from tests import tier_selftest, tier_split, tiers

ROOT = Path(__file__).resolve().parents[1]


def _cases(module="tests.fake_split"):
    calls = []

    class Logic(unittest.TestCase):
        @classmethod
        def setUpClass(cls):
            calls.append("Logic")

        def test_a(self):
            pass

        def test_b(self):
            pass

    class Browser(unittest.TestCase):
        kb_browser = True

        @classmethod
        def setUpClass(cls):
            calls.append("Browser")

        def test_page(self):
            pass

    for cls in (Logic, Browser):
        cls.__module__ = module
        cls.__qualname__ = cls.__name__     # id 里不带 `_cases.<locals>.`
    return Logic, Browser, calls


def _ids(suite):
    return [t.id() for t in tiers._iter_tests(suite)]


class PartitionTests(unittest.TestCase):

    def _full(self):
        logic, browser, calls = _cases()
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        broken, _ = unittest.loader._make_failed_import_test(
            "tests.fake_broken", unittest.TestSuite)
        module = unittest.TestSuite([load(logic), load(browser)])
        return unittest.TestSuite([module, broken]), calls

    def _part(self, tier):
        full, calls = self._full()
        with mock.patch.object(unittest.defaultTestLoader, "discover",
                               lambda *a, **k: full):
            return tiers.build_suite(tier), calls

    def test_two_parts_cover_everything_exactly_once(self):
        full, _ = self._full()
        logic, _ = self._part("full-logic")
        browser, _ = self._part("full-browser")
        self.assertEqual(sorted(_ids(full)), sorted(_ids(logic) + _ids(browser)))
        self.assertEqual(set(), set(_ids(logic)) & set(_ids(browser)))
        self.assertEqual(["tests.fake_split.Browser.test_page"], _ids(browser))
        self.assertIn("unittest.loader._FailedTest.tests.fake_broken", _ids(logic),
                      "导入失败落在逻辑那一路,照样报红")

    def test_class_fixtures_still_run_once_per_part(self):
        suite, calls = self._part("full-logic")
        unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(
            tiers.filter_suite(suite, lambda t: "fake_split" in t.id()))
        self.assertEqual(["Logic"], calls, "浏览器类的夹具不许在逻辑那一路起来")


class SelftestFilterTests(unittest.TestCase):

    def _run(self, selftest, failing=False):
        class Probe(unittest.TestCase):
            def test_ok(self):
                pass

            def test_maybe(self):
                if failing:
                    self.fail("红")

        class Product(unittest.TestCase):
            def test_x(self):
                pass

        Probe.__module__ = tier_selftest.SELFTEST_MODULES[0]
        Product.__module__ = "tests.fake_product"
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        suite = unittest.TestSuite([load(Probe), load(Product)])
        with tempfile.TemporaryDirectory() as td:
            report = Path(td) / "r.json"
            with mock.patch.object(tiers, "build_suite", lambda t: suite), \
                    mock.patch.dict(os.environ), \
                    mock.patch.object(sys, "stderr", io.StringIO()):
                tiers.run_tier("full-logic", selftest=selftest, report=str(report))
            return json.loads(report.read_text(encoding="utf-8"))

    def test_skip_removes_only_the_selftests(self):
        r = self._run("skip")
        self.assertEqual([], r["selftest_planned"])
        self.assertEqual(1, r["executed"], "产品用例一条不少")

    def test_run_keeps_them_and_books_who_passed(self):
        r = self._run("run", failing=True)
        self.assertEqual(3, r["executed"])
        self.assertEqual(2, len(r["selftest_planned"]))
        self.assertEqual(1, len(r["selftest_passed"]), "红的那条不许记成绿")


CHILD = textwrap.dedent('''
    import json, os, sys, time
    part, selftest, report, mode = sys.argv[1:5]
    t0 = time.time()
    print(f"hello from {part}", flush=True)
    if mode == "hang":
        open(os.environ["PID_DIR"] + "/" + part, "w").write(str(os.getpid()))
        time.sleep(60)
    time.sleep(0.8)
    if mode == "noreport" and part == "full-browser":
        sys.exit(3)
    rc = 1 if mode == "red" and part == "full-logic" else 0
    ids = ["x." + part]
    passed = [] if mode == "selftest-red" and part == "full-browser" else ids
    json.dump({"rc": rc, "executed": 2, "wall": 0.8, "browser_total": 1,
               "browser_ran": 1, "selftest_planned": ids, "selftest_passed": passed,
               "data": os.environ["KB_DATA_DIR"], "t0": t0, "t1": time.time()},
              open(report, "w"))
    sys.exit(rc)
''')


class SplitRunTests(unittest.TestCase):

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.base = Path(td.name) / "data"
        self.cache = Path(td.name) / "cache"
        env = mock.patch.dict(os.environ, {"KB_DATA_DIR": str(self.base),
                                           "XDG_CACHE_HOME": str(self.cache)})
        env.start()
        self.addCleanup(env.stop)
        self.reports = {}

    def _run(self, mode, skip=False):
        real_summarize = tier_split._summarize

        def keep_reports(procs, logs, work, *rest):
            for part in tiers.PARTS:
                path = Path(work) / f"{part}.json"
                if path.exists():
                    self.reports[part] = json.loads(path.read_text(encoding="utf-8"))
            return real_summarize(procs, logs, work, *rest)

        out = io.StringIO()
        with mock.patch.object(tier_split, "child_argv", lambda part, selftest, report, fd:
                               [sys.executable, "-c", CHILD, part, selftest, report, mode]), \
                mock.patch.object(tier_selftest, "decide", lambda: (skip, "测试")), \
                mock.patch.object(tier_selftest, "fingerprint", lambda: "fp"), \
                mock.patch.object(tier_split, "_summarize", keep_reports):
            rc = tier_split.run_full(status=out)
        return rc, out.getvalue()

    def test_both_parts_run_at_once_in_their_own_data_dirs(self):
        rc, out = self._run("green")
        self.assertEqual(0, rc, out)
        a, b = (self.reports[p] for p in tiers.PARTS)
        self.assertLess(max(a["t0"], b["t0"]), min(a["t1"], b["t1"]), "两路的执行区间要重叠")
        self.assertNotEqual(a["data"], b["data"])
        for r in (a, b):
            self.assertEqual(str(self.base), str(Path(r["data"]).parent))
        for part in tiers.PARTS:
            self.assertIn(f"hello from {part}", out, "两路的输出都要整段打出来")
        self.assertIn("两路合计跑了 4 条", out)

    def test_one_red_part_makes_the_round_red(self):
        rc, out = self._run("red")
        self.assertEqual(1, rc, out)

    def test_a_part_without_a_report_is_red(self):
        rc, out = self._run("noreport")
        self.assertEqual(1, rc, out)
        self.assertIn("没交回账本", out)

    def test_a_clean_selftest_round_records_the_pass(self):
        self._run("green")
        self.assertTrue(tier_selftest.cache_path().exists())

    def test_no_record_when_a_selftest_case_was_not_green(self):
        self._run("selftest-red")
        self.assertFalse(tier_selftest.cache_path().exists())

    def test_no_record_when_selftests_were_skipped(self):
        self._run("green", skip=True)
        self.assertFalse(tier_selftest.cache_path().exists())


def _gone(pid, within=10.0):
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


class CleanupTests(unittest.TestCase):

    def test_sigterm_takes_both_parts_with_it(self):
        with tempfile.TemporaryDirectory() as td:
            pid_dir = Path(td) / "pids"
            pid_dir.mkdir()
            driver = textwrap.dedent(f'''
                import sys
                from unittest import mock
                from tests import tier_selftest, tier_split
                CHILD = {CHILD!r}
                with mock.patch.object(tier_split, "child_argv", lambda p, s, r, fd:
                                       [sys.executable, "-c", CHILD, p, s, r, "hang"]), \\
                        mock.patch.object(tier_selftest, "decide", lambda: (True, "x")):
                    sys.exit(tier_split.run_full())
            ''')
            env = {**os.environ, "KB_DATA_DIR": str(Path(td) / "data"),
                   "PID_DIR": str(pid_dir)}
            proc = subprocess.Popen([sys.executable, "-c", driver], cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 30
                while len(list(pid_dir.iterdir())) < 2 and time.monotonic() < deadline:
                    time.sleep(0.1)
                pids = [int(p.read_text()) for p in pid_dir.iterdir()]
                self.assertEqual(2, len(pids), "两路都该起来了")
                proc.send_signal(signal.SIGTERM)
                self.assertEqual(128 + signal.SIGTERM, proc.wait(timeout=30))
                for pid in pids:
                    self.assertTrue(_gone(pid), f"子进程 {pid} 在父进程收到 SIGTERM 之后还活着")
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()

    def _middle(self, td, child_body):
        """父(本用例)→ 中间进程(握写端)→ 子进程(watch_parent 读端)。"""
        pid_file = Path(td) / "child"
        child = textwrap.dedent(f'''
            import os, sys, time
            from tests import tier_split
            real_sleep = time.sleep
            exec({child_body!r})
            tier_split.watch_parent(int(sys.argv[1]))
            open({str(pid_file)!r}, "w").write(str(os.getpid()))
            real_sleep(60)
        ''')
        middle = textwrap.dedent(f'''
            import os, subprocess, sys, time
            r, w = os.pipe()
            subprocess.Popen([sys.executable, "-c", {child!r}, str(r)], pass_fds=(r,))
            os.close(r)
            time.sleep(60)
        ''')
        proc = subprocess.Popen([sys.executable, "-c", middle], cwd=ROOT)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        deadline = time.monotonic() + 30
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        return proc, int(pid_file.read_text())

    def test_a_part_leaves_when_its_parent_is_killed(self):
        with tempfile.TemporaryDirectory() as td:
            proc, pid = self._middle(td, "")
            self.assertFalse(_gone(pid, within=1.5), "父进程还在,子进程不许自己退")
            proc.kill()
            proc.wait()
            self.assertTrue(_gone(pid), "父进程被 SIGKILL 之后,子进程没自己退")

    def test_watching_the_parent_never_touches_time_sleep(self):
        """用例打桩全局 time.sleep 期间,盯父进程那根线程一拍都不许往桩里灌。"""
        with tempfile.TemporaryDirectory() as td:
            beats = Path(td) / "beats"
            body = textwrap.dedent(f'''
                def fake(secs):
                    with open({str(beats)!r}, "a") as f:
                        f.write("x")
                    real_sleep(0.001)
                time.sleep = fake
            ''')
            proc, pid = self._middle(td, body)
            time.sleep(1.5)
            self.assertFalse(beats.exists() and beats.read_text(),
                             "盯父进程的线程在空转全局 time.sleep")
            proc.kill()
            proc.wait()
            self.assertTrue(_gone(pid))


if __name__ == "__main__":
    unittest.main()
