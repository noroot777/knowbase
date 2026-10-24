import json
import unittest

from kb.daily_prompt import PROMPT, late_section
from tests.headless import NODE, fixture_server, probe_env, requires_headless, run_probe


class DailyLayoutPromptTests(unittest.TestCase):
    def test_line_items_require_markdown_list_markers(self):
        self.assertIn('每个条目必须以 "- " 开头', PROMPT)
        self.assertIn('不能只靠单个换行分隔', PROMPT)
        self.assertIn('没有内容时的说明句不必写成列表', PROMPT)

    def test_each_section_and_late_batch_use_list_items_instead_of_lines(self):
        sections = PROMPT.split('\n## ')[1:]
        self.assertEqual(4, len(sections))
        for section in sections:
            self.assertIn('"- " 列表项', section)
        for instruction in (PROMPT, late_section(3), late_section(305)):
            for ambiguous in ('每条一行', '各一行', '每行一类', '最多 20 行', '最多 12 行', '最多 30 行'):
                self.assertNotIn(ambiguous, instruction)


@requires_headless
class DailyLayoutBrowserTests(unittest.TestCase):
    def test_plain_line_sections_keep_their_layout_without_changing_text(self):
        with fixture_server(script='serve_daily_materials.py') as base:
            result = run_probe([NODE, 'tests/daily_layout_probe.mjs', base],
                               timeout=90, env=probe_env())
        self.assertEqual(0, result.returncode, result.stderr[-4000:] + result.stdout[-4000:])
        evidence = json.loads(result.stdout.strip().splitlines()[-1])
        self.assertGreaterEqual(len(evidence['passed']), 23)
        self.assertEqual([], evidence['errors'])


if __name__ == '__main__':
    unittest.main()
