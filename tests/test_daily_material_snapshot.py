"""成功的素材投影始终以原始快照证明条数，不依赖是否剥离队列尾注。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily, daily_page
from kb.daily_materials import material_view
from kb.scanner import parse_frontmatter
from kb.web import daily_api
from tests.daily_material_queue_fixtures import queue_report
from tests.daily_material_snapshot_fixtures import snapshot_documents, snapshot_report


class MaterialSnapshotTests(unittest.TestCase):
    def test_injected_heading_cannot_replace_materials_in_any_queue_branch(self):
        for total, late_total in [(1, 1), (9, 1), (1, 9), (9, 9)]:
            meta, body = parse_frontmatter(snapshot_report(total=total, late_total=late_total))
            tail = daily_page.queue_tail(docs=1, total=total, late=1, late_total=late_total)
            bodies = [body, body.replace(tail, '')] if tail else [body]
            for text in bodies:
                with self.subTest(total=total, late_total=late_total, removed=text != body):
                    self.assertIsNone(material_view(text, meta))

    def test_regular_materials_stay_compact_with_or_without_a_queue_tail(self):
        for total, late_total in [(1, 1), (9, 1), (1, 9), (9, 9)]:
            meta, body = parse_frontmatter(snapshot_report(
                total=total, late_total=late_total, adversarial=False))
            tail = daily_page.queue_tail(docs=1, total=total, late=1, late_total=late_total)
            bodies = [body, body.replace(tail, '')] if tail else [body]
            for text in bodies:
                with self.subTest(total=total, late_total=late_total, removed=text != body):
                    view = material_view(text, meta)
                    self.assertIsNotNone(view)
                    self.assertEqual(2, len(view['items']))
                    self.assertEqual('真实首条素材', view['items'][0]['title'])
                    self.assertEqual('真实补录素材', view['items'][1]['title'])
                    self.assertEqual('大V观点·补录作者 · 补录', view['items'][1]['source'])

    def test_counts_are_checked_even_when_a_predicted_tail_is_absent(self):
        meta, body = parse_frontmatter(queue_report(docs=2, total=2, late=1, late_total=1))
        for total, late_total in [(1, 1), (9, 1), (1, 9), (9, 9)]:
            for docs, late in [(1, 1), (0, 1), (1, 0), (0, 0)]:
                with self.subTest(total=total, late_total=late_total, docs=docs, late=late):
                    self.assertIsNone(material_view(body, {**meta, 'total': total,
                                                          'late_total': late_total,
                                                          'docs': docs, 'late': late}))

    def test_missing_or_invalid_counts_cannot_authorize_any_projection(self):
        invalid_values = [None, True, False, -1, '1', 1.0, [], {}, 2 ** 63, 10 ** 5000]
        for adversarial in [True, False]:
            meta, body = parse_frontmatter(snapshot_report(adversarial=adversarial))
            for report in [None, {}, [], 'snapshot', False]:
                with self.subTest(adversarial=adversarial, report_type=type(report).__name__):
                    self.assertIsNone(material_view(body, report))
            for key in ('docs', 'total', 'late', 'late_total'):
                missing = dict(meta)
                missing.pop(key)
                self.assertIsNone(material_view(body, missing))
                for value in invalid_values:
                    with self.subTest(adversarial=adversarial, key=key,
                                      value_type=type(value).__name__):
                        self.assertIsNone(material_view(body, {**meta, key: value}))
            for overrides in [{'total': 0}, {'late_total': 0}]:
                self.assertIsNone(material_view(body, {**meta, **overrides}))

    def test_read_api_preserves_originals_and_validates_before_count_coercion(self):
        originals = snapshot_documents()
        meta, body = parse_frontmatter(snapshot_report(adversarial=False))
        day = 11
        for key in ('docs', 'total', 'late', 'late_total'):
            for value in [None, True, '1', 1.5]:
                changed = {**meta, key: value}
                if value is None:
                    changed.pop(key)
                originals[f'2026-11-{day:02d}'] = (
                    '---\n' + yaml.safe_dump(changed, allow_unicode=True) + '---\n' + body)
                day += 1
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / 'daily'
            reports.mkdir()
            for date, text in originals.items():
                (reports / f'{date}.md').write_text(text, encoding='utf-8')
            app = FastAPI()
            app.include_router(daily_api.router)
            with patch.object(daily, 'VAULT_ROOT', root), TestClient(app) as client:
                for date, text in originals.items():
                    with self.subTest(date=date):
                        response = client.get('/api/daily/reports/' + date)
                        self.assertEqual(200, response.status_code)
                        result = response.json()
                        if date == '2026-11-03':
                            self.assertEqual(2, len(result['material_view']['items']))
                        else:
                            self.assertIsNone(result['material_view'])
                        self.assertEqual(parse_frontmatter(text)[1], result['body'])
                        self.assertEqual(text.encode(), (reports / f'{date}.md').read_bytes())
                        self.assertEqual(daily_page.carried_keys(text),
                                         daily_page.carried_keys(result['body']))


if __name__ == '__main__':
    unittest.main()
