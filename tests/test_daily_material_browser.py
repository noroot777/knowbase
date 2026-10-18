"""简洁素材清单在真实 kb-web 中的浏览器回归。"""

import json
import unittest

from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


@requires_headless
class MaterialBrowserTests(unittest.TestCase):
    def test_snapshot_counts_prevent_an_injected_heading_from_replacing_materials(self):
        with fixture_server('--snapshot-fixtures', script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/daily_material_snapshot_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(20, len(evidence['passed']))
        self.assertEqual([], evidence['errors'])

    def test_generated_queue_footer_keeps_all_198_materials_compact(self):
        with fixture_server('--queue-fixtures', script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/daily_material_queue_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(6, len(evidence['passed']))
        self.assertEqual([], evidence['errors'])

    def test_saved_long_quotes_are_visible_through_all_scroll_containers(self):
        with fixture_server(script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/snip_scroll_probe.mjs', base],
                               timeout=120, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(16, len(evidence['jumps']))
        self.assertEqual(5, len(evidence['geometryChecks']))
        self.assertEqual([], evidence['errors'])

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

    def test_ambiguous_materials_use_the_original_reader(self):
        with fixture_server(script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/daily_material_ambiguity_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertEqual(12, len(evidence['passed']))
        self.assertEqual([], evidence['errors'])


if __name__ == '__main__':
    unittest.main()
