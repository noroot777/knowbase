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
class SubsLayoutTests(unittest.TestCase):
    def test_populated_subscription_workspace(self):
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        server = subprocess.Popen(
            [sys.executable, 'tests/serve_sources.py', '--port', str(port),
             '--layout-fixture'], cwd=ROOT,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            base = f'http://127.0.0.1:{port}'
            for attempt in range(150):
                try:
                    urllib.request.urlopen(base + '/', timeout=1).read()
                    break
                except (urllib.error.URLError, OSError):
                    if server.poll() is not None:
                        self.fail('隔离布局服务启动失败')
                    time.sleep(0.2)
            else:
                self.fail('隔离布局服务没有响应')
            result = run_probe(
                [shutil.which('node'), 'tests/subs_layout_probe.mjs', base],
                timeout=180, env=probe_env(), cwd=ROOT)
            self.assertTrue(result.stdout.strip(), result.stderr)
            checks = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertGreater(len(checks), 30)
            for check in checks:
                with self.subTest(check=check['name']):
                    self.assertTrue(check['ok'], check['detail'])
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            server.terminate()
            server.wait(timeout=10)
