# -*- coding: utf-8 -*-
"""「出口与解析」那块面板在**真页面**上的红。

判据和理由都在探针自己的抬头里(`tests/egress_panel_probe.mjs`);这一块的硬约束在
`docs/decisions/dns-egress.md`。

单独一个文件只为**让它进全量**:没有 python 包装的探针一条都不在 `unittest discover`
的射程里。这里只负责经 `run_probe` 起它(见 `docs/decisions/probe-shell.md`)。
"""
import shutil
import unittest
from pathlib import Path

from tests.headless import probe_env, requires_headless, run_probe

_PROBE = Path(__file__).resolve().parent / "egress_panel_probe.mjs"


@requires_headless
class EgressPanelProbeTests(unittest.TestCase):
    def test_the_panel_shows_what_is_in_effect_and_saves_without_a_restart(self):
        r = run_probe([shutil.which("node"), str(_PROBE)],
                      timeout=300, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
