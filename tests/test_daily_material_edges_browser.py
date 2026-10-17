"""在真实日报读取页验证数字降级和 Markdown 导航语义。"""

import json
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class MaterialEdgesBrowserTests(unittest.TestCase):
    def test_numeric_inputs_and_markdown_link_targets(self):
        with fixture_server(script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/daily_material_edges_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-5000:] + result.stdout[-5000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(evidence['passed']), 12)
        self.assertEqual([], evidence['errors'])


if __name__ == '__main__':
    unittest.main()
