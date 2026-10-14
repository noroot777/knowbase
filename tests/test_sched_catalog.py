# -*- coding: utf-8 -*-
"""定时窗口的依赖就绪:**配置读到了、来源目录没读到**那一档。

单开一支探针(`tests/sched_catalog_probe.mjs`)的理由写在它的文件头:要的是整页加载
时那次目录请求就失败的那一刻,而目录一个进程只读一次 —— 在既有探针里复现得摆三个
产品变量,不如在网络层拦掉那个地址、真的开一页。
"""
import json
import shutil
import socket
import subprocess
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.headless import probe_env, requires_headless, run_probe

ROOT = Path(__file__).resolve().parents[1]


@requires_headless
class ScheduleCatalogTests(unittest.TestCase):
    def test_the_schedule_form_waits_for_its_source_catalog(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        server = subprocess.Popen(
            [sys.executable, 'tests/serve_sources.py', '--port', str(port),
             '--layout-fixture'], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f'http://127.0.0.1:{port}'
            for _ in range(150):
                try:
                    urllib.request.urlopen(base + '/', timeout=1).read()
                    break
                except (urllib.error.URLError, OSError):
                    if server.poll() is not None:
                        self.fail('隔离布局服务启动失败')
                    time.sleep(0.2)
            else:
                self.fail('隔离布局服务没有响应')
            result = run_probe([shutil.which('node'), 'tests/sched_catalog_probe.mjs', base],
                               timeout=180, env=probe_env(), cwd=ROOT)
            self.assertTrue(result.stdout.strip(), result.stderr)
            checks = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertGreaterEqual(len(checks), 7)
            for check in checks:
                with self.subTest(check=check['name']):
                    self.assertTrue(check['ok'], check['detail'])
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            server.terminate()
            server.wait(timeout=10)


if __name__ == '__main__':
    unittest.main()
