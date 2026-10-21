# -*- coding: utf-8 -*-
"""「我的订阅」里的同步按钮改成打开起跑弹窗、并锁定在当前那一页,在**真页面**上的红。

判据和理由都在探针自己的抬头里(`tests/pipe_entry_probe.mjs`);硬约束在
`docs/decisions/sync-entry.md` 第一节。

单独一个文件只为**让它进全量**:没有 python 包装的探针一条都不在
`unittest discover` 的射程里。

这里只负责**经 `run_probe` 起它** —— 探针不许自己 spawn 浏览器、测试不许裸
`subprocess.run` 跑探针(见 `docs/decisions/probe-shell.md`)。
"""
import shutil
import unittest
from pathlib import Path

from tests.headless import probe_env, requires_headless, run_probe

_PROBE = Path(__file__).resolve().parent / "pipe_entry_probe.mjs"


@requires_headless
class PipeEntryProbeTests(unittest.TestCase):
    def test_subscription_page_sync_opens_the_locked_dialog(self):
        r = run_probe([shutil.which("node"), str(_PROBE)],
                      timeout=300, env=probe_env())
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
