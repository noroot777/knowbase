# -*- coding: utf-8 -*-
"""「重试失败项」那一区在**真页面**上的红(判据和理由都在探针自己的抬头里)。

单独一个文件只为**让它进全量**:这个探针原来没有 python 包装,于是它一条都不在
`unittest discover` 的射程里 —— 前端那一半因此连一条自动回归都没有,而「页面说还欠
一步、点下去说没有可补」这个死局正是在那一半上反复回来的。后端那一半在
`tests/test_retry_daybook.py`,两份各有自己的红(元规则那条)。

这里只负责**经 `run_probe` 起它** —— 探针不许自己 spawn 浏览器、测试不许裸
`subprocess.run` 跑探针(见 `docs/decisions/probe-shell.md`)。
"""
import shutil
import unittest
from pathlib import Path

from tests.headless import probe_env, requires_headless, run_probe

_PROBE = Path(__file__).resolve().parent / "pipe_owed_probe.mjs"


@requires_headless
class PipeOwedProbeTests(unittest.TestCase):
    def test_the_panel_only_offers_what_can_actually_be_fixed_now(self):
        r = run_probe([shutil.which("node"), str(_PROBE)],
                      timeout=300, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
