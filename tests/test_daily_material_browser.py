"""简洁素材清单在真实 kb-web 中的浏览器回归。"""

import json
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class MaterialBrowserTests(unittest.TestCase):
    def test_material_list_in_real_report_reader(self):
        with fixture_server(script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/daily_material_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(evidence['passed']), 12)
        self.assertEqual([], evidence['errors'])

    def test_material_view_keeps_legacy_anchors_and_whole_markdown_context(self):
        with fixture_server(script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/daily_material_compat_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(evidence['passed']), 14)
        self.assertEqual([], evidence['errors'])


if __name__ == '__main__':
    unittest.main()
