"""真实收藏页验证:文档不在后仍有内容、可编辑,没有坏的本地阅读入口。"""

import json
from pathlib import Path
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class FavoriteLifecycleBrowserTests(unittest.TestCase):
    def test_detached_favorites_remain_usable(self):
        with fixture_server('--detached', script='serve_favorites.py') as base:
            result = run_probe(
                [NODE, str(Path(__file__).with_name('favorite_lifecycle_probe.mjs')), base],
                env=probe_env(), timeout=90)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual([], report['errors'])
        self.assertEqual([], [check for check in report['checks'] if not check['ok']], report)
        self.assertGreaterEqual(len(report['checks']), 12)


if __name__ == '__main__':
    unittest.main()
