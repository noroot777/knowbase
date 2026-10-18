"""固定队列说明须由保存的取材数量证明，不能把任意作者续行猜成后注。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily, daily_page
from kb.daily_materials import material_view
from kb.scanner import parse_frontmatter
from kb.web import daily_api
from tests.daily_material_queue_fixtures import queue_report


class MaterialQueueTests(unittest.TestCase):
    def test_normal_generated_footer_does_not_force_a_full_report_fallback(self):
        cases = [(2, 9, 0, 0), (2, 2, 1, 791), (2, 9, 1, 791), (0, 0, 1, 791)]
        for docs, total, late, late_total in cases:
            with self.subTest(docs=docs, total=total, late=late, late_total=late_total):
                meta, body = parse_frontmatter(queue_report(
                    docs=docs, total=total, late=late, late_total=late_total))
                self.assertIsNone(material_view(body))
                view = material_view(body, meta)
                self.assertIsNotNone(view)
                self.assertEqual(docs + late, len(view['items']))
                self.assertEqual(body[:view['span'][0]], view['before'])
                self.assertEqual(body[view['span'][1]:], view['after'])
                self.assertIn('没列出的留在队列里,下一份日报接着带。', view['after'])
                for number, item in enumerate(view['items'], 1):
                    self.assertEqual(number, item['number'])
                    self.assertEqual(f'Grok Bot 使用体验 {number}', item['title'])
                    self.assertEqual(f'https://example.org/queue-{number}', item['url'])
                    self.assertIn('完整原文', item['full_title'])

    def test_all_198_entries_and_the_saved_late_counts_are_preserved(self):
        meta, body = parse_frontmatter(queue_report())
        view = material_view(body, meta)
        self.assertEqual(198, len(view['items']))
        self.assertEqual('https://example.org/queue-198', view['items'][-1]['url'])
        self.assertEqual('\n\n> 补录共 791 条,以上列出 60 条。 '
                         '没列出的留在队列里,下一份日报接着带。\n', view['after'])

    def test_missing_or_invalid_saved_counts_cannot_authorize_footer_removal(self):
        meta, body = parse_frontmatter(queue_report(docs=2, late=1))
        for key in ('docs', 'total', 'late', 'late_total'):
            missing = dict(meta)
            missing.pop(key)
            self.assertIsNone(material_view(body, missing))
            for value in [None, True, -1, '2', [], {}, 2 ** 63, 10 ** 5000]:
                with self.subTest(key=key, value_type=type(value).__name__):
                    self.assertIsNone(material_view(body, {**meta, key: value}))
        self.assertIsNone(material_view(body, {**meta, 'total': 0}))
        self.assertIsNone(material_view(body, {**meta, 'late_total': 0}))

    def test_metadata_must_also_prove_the_total_number_of_materials(self):
        meta, body = parse_frontmatter(queue_report(docs=2, late=1))
        self.assertIsNone(material_view(body, {**meta, 'docs': 1, 'total': 1}))
        self.assertIsNone(material_view(body, {**meta, 'late_total': 790}))

    def test_arbitrary_postscripts_and_changed_footer_text_still_fall_back(self):
        meta, body = parse_frontmatter(queue_report(docs=2, late=1))
        for text in [body + '\n## 编辑备注\n\n这里是额外说明。\n',
                     body.replace('补录共 791 条', '补录共 792 条'),
                     body.replace('下一份日报接着带。', '下一份再说。')]:
            with self.subTest(text=text[-80:]):
                self.assertIsNone(material_view(text, meta))

    def test_a_multiline_author_is_not_cut_off_by_a_verified_queue_footer(self):
        for url in ['', 'https://example.org/queue-']:
            for account in ['作者第一行\n第二行', '作者第一行\n\n> 作者继续说明',
                            '作者第一行\n\n## 编辑备注']:
                with self.subTest(url=url, account=account):
                    meta, body = parse_frontmatter(queue_report(
                        docs=1, late=0, late_total=0, total=9, title='普通标题',
                        account=account, url=url))
                    self.assertIsNone(material_view(body, meta))

    def test_footer_words_inside_a_title_are_preserved_not_used_as_a_boundary(self):
        tail = daily_page.queue_tail(docs=1, total=9, late=0, late_total=0)
        title = '正常首行' + tail + '\n\n这里仍是原文。'
        meta, body = parse_frontmatter(queue_report(
            docs=1, total=9, late=0, late_total=0, title=title))
        view = material_view(body, meta)
        self.assertEqual('正常首行', view['items'][0]['title'])
        self.assertIn('这里仍是原文', view['items'][0]['full_title'])
        self.assertEqual(tail + '\n', view['after'])

    def test_trailing_newlines_are_preserved_byte_for_byte(self):
        meta, body = parse_frontmatter(queue_report(docs=1, late=0, total=9, late_total=0))
        body += '\n\n'
        view = material_view(body, meta)
        self.assertEqual(body[view['span'][1]:], view['after'])

    def test_real_read_api_keeps_files_copy_input_and_ledger_evidence_unchanged(self):
        text = queue_report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'daily' / '2026-10-03.md'
            path.parent.mkdir()
            path.write_text(text, encoding='utf-8')
            original = path.read_bytes()
            app = FastAPI()
            app.include_router(daily_api.router)
            with patch.object(daily, 'VAULT_ROOT', root), TestClient(app) as client:
                response = client.get('/api/daily/reports/2026-10-03')
            self.assertEqual(200, response.status_code)
            result = response.json()
            self.assertEqual(198, len(result['material_view']['items']))
            self.assertEqual(791, result['late_total'])
            self.assertEqual(parse_frontmatter(text)[1], result['body'])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(daily_page.carried_keys(text),
                             daily_page.carried_keys(result['body']))


if __name__ == '__main__':
    unittest.main()
