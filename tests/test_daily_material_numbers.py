"""不可信数字不能让可选展示投影阻断整份日报读取。"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily
from kb.daily_materials import material_view
from kb.scanner import parse_frontmatter
from kb.web import daily_api
from tests.daily_material_edge_fixtures import numeric_documents


class MaterialNumberTests(unittest.TestCase):
    def test_all_numeric_positions_are_safe_without_changing_python_limits(self):
        previous_limit = sys.get_int_max_str_digits()
        for digits in ['9' * 5000, '0' * 5000 + '1', '９' * 5000]:
            for date, text in numeric_documents(digits).items():
                with self.subTest(digits=digits[:8], date=date):
                    meta, body = parse_frontmatter(text)
                    view = material_view(body, meta)
                    if view is not None:
                        start, end = view['span']
                        self.assertEqual(body[:start], view['before'])
                        self.assertEqual(body[end:], view['after'])
                        self.assertIn(digits, view['items'][-1]['full_title'])
        self.assertEqual(previous_limit, sys.get_int_max_str_digits())

    def test_read_api_keeps_original_body_and_files_for_every_numeric_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            reports = root / 'daily'
            reports.mkdir()
            originals = numeric_documents()
            for date, text in originals.items():
                (reports / f'{date}.md').write_text(text, encoding='utf-8')
            app = FastAPI()
            app.include_router(daily_api.router)
            with patch.object(daily, 'VAULT_ROOT', root), TestClient(
                app, raise_server_exceptions=False
            ) as client:
                for date, text in originals.items():
                    with self.subTest(date=date):
                        response = client.get('/api/daily/reports/' + date)
                        self.assertEqual(200, response.status_code, response.text[:200])
                        result = response.json()
                        self.assertEqual(parse_frontmatter(text)[1], result['body'])
                        self.assertIn('正常日报正文。', result['body'])
                        self.assertEqual(text.encode(), (reports / f'{date}.md').read_bytes())
                        if date == '2026-10-07':
                            self.assertIsNone(result['material_view'])

    def test_noncanonical_numbers_keep_original_instead_of_being_converted(self):
        for count, number in [('01', '1'), ('1', '01'), ('１', '1'), ('1', '１')]:
            with self.subTest(count=count, number=number):
                self.assertIsNone(material_view(
                    f'## 素材清单({count} 条)\n\n{number}. 标题 — RSS·作者\n',
                    dict(docs=1, total=1, late=0, late_total=0)))


if __name__ == '__main__':
    unittest.main()
