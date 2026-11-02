# -*- coding: utf-8 -*-
"""回归分层入口的耗时账(tests/tiers.py 的 TierResult + timing_lines)。

提速只能对着重尾下手,而默认输出只有一串点和一个总秒数,所以每一层跑完都要把
最慢的文件和用例打出来。两条口径,各钉两面:

- **文件那一栏要含类级/模块级夹具。** 起沙箱服务、起浏览器多半写在 setUpClass 里,
  它们落在 startTest 之前 —— 只按单条用例记,重尾里最贵的那一截恰好看不见。
  反过来钉:这份时间不许同时记到用例头上,用例只认它自己 startTest→stopTest 那段。
- **文件耗时接力记账,合起来就是整轮,不重不漏**(最后一个文件的类级收尾在最后
  一条 stopTest 之后才跑,也要补记给它)。

时钟换成假钟(`tiers._clock`),判据不靠真睡。
"""

import contextlib
import io
import os
import unittest
from unittest import mock

from tests import tiers


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def tick(self, secs):
        self.now += secs


def _case(clock, name, module, *, setup_class=0.0, teardown_class=0.0, **cases):
    """一个临时 TestCase 类:每条用例把假钟拨 cases[方法名] 秒。"""
    body = {name_: (lambda secs: lambda self: clock.tick(secs))(secs)
            for name_, secs in cases.items()}
    body["setUpClass"] = classmethod(lambda cls: clock.tick(setup_class))
    body["tearDownClass"] = classmethod(lambda cls: clock.tick(teardown_class))
    cls = type(name, (unittest.TestCase,), body)
    cls.__module__ = module
    return cls


class TimingBookTests(unittest.TestCase):

    def setUp(self):
        self.clock = FakeClock()
        patcher = mock.patch.object(tiers, "_clock", self.clock)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *classes):
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        suite = unittest.TestSuite([load(c) for c in classes])
        start = self.clock.now
        result = unittest.TextTestRunner(
            stream=io.StringIO(), verbosity=0,
            resultclass=tiers.TierResult).run(suite)
        return result, self.clock.now - start

    def test_class_fixtures_count_toward_their_file_not_the_case(self):
        heavy = _case(self.clock, "Heavy", "tests.fake_heavy",
                      setup_class=40.0, test_a=1.0)
        light = _case(self.clock, "Light", "tests.fake_light",
                      test_b=3.0, test_c=0.5)
        result, _ = self._run(heavy, light)
        self.assertEqual(41.0, result.kb_file_secs["tests.fake_heavy"],
                         "setUpClass 起夹具的 40 秒要算进它所在的文件")
        self.assertEqual(3.5, result.kb_file_secs["tests.fake_light"])
        # 反过来钉:用例只认它自己那一段,夹具时间不许摊到第一条用例头上。
        self.assertEqual(1.0, result.kb_case_secs["tests.fake_heavy.Heavy.test_a"])
        self.assertEqual(3.0, result.kb_case_secs["tests.fake_light.Light.test_b"])

    def test_file_times_add_up_to_the_whole_run(self):
        first = _case(self.clock, "First", "tests.fake_first",
                      setup_class=2.0, test_a=1.0, test_b=1.5)
        last = _case(self.clock, "Last", "tests.fake_last",
                     setup_class=5.0, teardown_class=7.0, test_c=0.25)
        result, wall = self._run(first, last)
        self.assertEqual(wall, sum(result.kb_file_secs.values()),
                         "接力记账合起来就是整轮,不重不漏")
        self.assertEqual(12.25, result.kb_file_secs["tests.fake_last"],
                         "最后一个文件的类级收尾在最后一条之后才跑,也要记给它")

    def test_the_report_ranks_the_slowest_with_their_share(self):
        slow = _case(self.clock, "Slow", "tests.fake_slow",
                     setup_class=60.0, test_slow=30.0)
        quick = _case(self.clock, "Quick", "tests.fake_quick",
                      test_q1=5.0, test_q2=5.0)
        result, _ = self._run(slow, quick)
        lines = tiers.timing_lines(result, n=1)
        text = "\n".join(lines)
        self.assertIn("最慢的 1 个文件(含类级夹具)合计 90 秒,占 90%", text)
        self.assertIn("tests.fake_slow", text)
        self.assertNotIn("tests.fake_quick", text, "只列前 n 个")
        self.assertIn("最慢的 1 个用例合计 30 秒,占 30%", text)
        self.assertIn("tests.fake_slow.Slow.test_slow", text)

    def test_an_empty_run_reports_nothing(self):
        result, _ = self._run()
        self.assertEqual([], tiers.timing_lines(result))

    def test_main_prints_the_book_to_the_status_stream(self):
        slow = _case(self.clock, "MainSlow", "tests.fake_main",
                     setup_class=12.0, test_x=3.0)
        load = unittest.defaultTestLoader.loadTestsFromTestCase
        with mock.patch.object(tiers, "build_suite", lambda t: load(slow)), \
                mock.patch.dict(os.environ), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            rc = tiers.main(["full-logic", "--selftest=run"])
        self.assertEqual(0, rc, err.getvalue())
        self.assertIn("个文件(含类级夹具)合计 15 秒", err.getvalue())
        self.assertIn("tests.fake_main.MainSlow.test_x", err.getvalue())


if __name__ == "__main__":
    unittest.main()
