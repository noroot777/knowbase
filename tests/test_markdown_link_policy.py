"""主文档正文链接不能导航走知识库；回归只在验收阶段运行。"""

import json
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class MarkdownLinkPolicyTests(unittest.TestCase):
    def test_renderers_preserve_the_page_and_share_link_semantics(self):
        with fixture_server(script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/markdown_link_policy_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(evidence['passed']), 10)
        self.assertEqual([], evidence['errors'])
