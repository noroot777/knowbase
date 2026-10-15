import json
import shutil
import unittest
from pathlib import Path

from tests.headless import fixture_server, probe_env, requires_headless, run_probe

ROOT = Path(__file__).resolve().parents[1]


@requires_headless
class SubsLayoutTests(unittest.TestCase):
    def test_populated_subscription_workspace(self):
        # 沙箱服务和它的临时库都归 fixture_server 管(见那边的说明:被 SIGTERM
        # 收掉的子进程不会执行自己的清理,库会留在盘上)。
        with fixture_server('--layout-fixture') as base:
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
