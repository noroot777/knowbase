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
            self.assertTrue({'跳过提示只属于自己的频道', '干净频道不继承别人的跳过提示',
                             '零返回的跳过提示不虚构其他条目或访问限制原因',
                             '代码密集译文保留原始命令且不发布格式样例',
                             '没列完的频道显示部分同步而不是成功', '内容积压不伪装成频道覆盖失败',
                             'YouTube覆盖提示说明按窗口重查而非接续位置',
                             '真实待办接口只报告覆盖失败而非内容积压'} <= {check['name'] for check in checks})
            self.assertTrue((root / 'output/youtube/subscriptions-status-desktop.png').is_file())
            files = list((Path(base.root) / 'vault/source/youtube').rglob('*.md'))
            self.assertEqual(1, len(files))
            self.assertIn('中文译文（自动生成）', files[0].read_text())
            self.assertIn('未经逐段完整性核验', files[0].read_text())
            self.assertNotIn('完整中文转录', files[0].read_text())
            self.assertNotIn('&t=', files[0].read_text())
            self.assertNotIn('翻译核验提示', files[0].read_text())
            self.assertIn('接口预算为六十四', files[0].read_text())
            self.assertIn('npm install --save-dev vitest && npx vitest run --coverage', files[0].read_text())
            self.assertNotIn('"translations"', files[0].read_text())
