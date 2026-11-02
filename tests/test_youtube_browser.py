import json
from pathlib import Path
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class YouTubeBrowser(unittest.TestCase):
    def test_subscription_ranking_budget_and_chinese_reader(self):
        root = Path(__file__).resolve().parents[1]
        with fixture_server('--youtube-fixture') as base:
            result = run_probe([NODE, 'tests/youtube_probe.mjs', base], timeout=150,
                               env=probe_env(), cwd=root)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            checks = json.loads(result.stdout.strip().splitlines()[-1])
            self.assertGreaterEqual(len(checks), 12)
            for check in checks:
                with self.subTest(check=check['name']):
                    self.assertTrue(check['ok'], check['detail'])
            files = list((Path(base.root) / 'vault/source/youtube').rglob('*.md'))
            self.assertEqual(1, len(files))
            self.assertIn('完整中文转录', files[0].read_text())
            self.assertNotIn('&t=', files[0].read_text())
