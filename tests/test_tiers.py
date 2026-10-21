# -*- coding: utf-8 -*-
"""回归分层入口的回归(tests/tiers.py + tests/headless.py 的浏览器闸门)。

两条口径,各钉两面(完整语境 docs/decisions/test-tiers.md):

- **闸门只有 requires_headless 一处**:设了 `KB_TEST_NO_BROWSER` 就整类拦下,
  而且 skip 消息必须写清这批测试还能怎么跑起来 —— 「静默跳过」是这套机制当年
  要修的事故形态,不许在这里复活。反过来钉:没设变量时放不放行只取决于
  node/无头壳,闸门不许顺手把本来能跑的也拦了。
- **probes 名单现算,不许手工维护**:口径是「文件源码引用 tests.headless」。
  手工列表会静默过期 —— 新探针文件没人登记,就落进 fast 层,快速层又慢回去,
  而且没有任何一处会红。这里钉住:最重的探针在册、纯逻辑文件不在册、名单上的
  名字个个真有其文件。
"""

import os
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

    def test_without_the_gate_nothing_extra_is_blocked(self):
        # 反过来钉一边:没设变量时,放不放行只取决于 node 和无头壳。两个都在
        # (打桩成在)就必须放行 —— 闸门不许把本来能跑的也拦了。
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


class ProbeRosterTests(unittest.TestCase):

    def test_the_roster_is_computed_and_catches_the_heavy_probes(self):
        names = tiers.probe_modules()
        # 最重的那几个探针文件必须在册 —— 它们落进 fast 层,分层就白做了。
        self.assertIn("tests.test_subs_layout", names)
        self.assertIn("tests.test_probe_leak", names)
        self.assertIn("tests.test_x_channels", names)
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
