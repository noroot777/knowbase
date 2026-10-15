# -*- coding: utf-8 -*-
"""定时窗口的依赖就绪:**配置读到了、来源目录没读到**那一档。

单开一支探针(`tests/sched_catalog_probe.mjs`)的理由写在它的文件头:要的是整页加载
时那次目录请求就失败的那一刻,而目录一个进程只读一次 —— 在既有探针里复现得摆三个
产品变量,不如在网络层拦掉那个地址、真的开一页。

沙箱服务走 `tests.headless.fixture_server`:**临时库由这一侧拥有**(服务进程被
SIGTERM 收掉时它那边的清理一行都不跑,库会留在盘上)。
"""
import json
import shutil
import unittest
from pathlib import Path

from tests.headless import fixture_server, probe_env, requires_headless, run_probe

ROOT = Path(__file__).resolve().parents[1]


@requires_headless
class ScheduleCatalogTests(unittest.TestCase):
    def test_the_schedule_form_waits_for_its_source_catalog(self):
        with fixture_server('--layout-fixture') as base:
            result = run_probe([shutil.which('node'), 'tests/sched_catalog_probe.mjs', base],
                               timeout=180, env=probe_env(), cwd=ROOT)
            self.assertTrue(result.stdout.strip(), result.stderr)
            checks = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertGreaterEqual(len(checks), 7)
            for check in checks:
                with self.subTest(check=check['name']):
                    self.assertTrue(check['ok'], check['detail'])
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
