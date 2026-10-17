"""展示解析只接受唯一完整的结构，不能用归属容错为展示猜字段。"""

import unittest
from datetime import datetime
from unittest.mock import patch

from kb import daily
from kb.daily_materials import material_view
from tests.test_daily_material_view import report_text


class MaterialAmbiguityTests(unittest.TestCase):
    def test_same_count_headings_inside_linked_and_unlinked_titles_keep_original(self):
        for url in ['', 'https://example.org/real']:
            for count in [1, 2]:
                entries = [{'title': f'第一行\n\n## 素材清单({count} 条)\n\n1. 内部示例',
                            'url': url}]
                entries += [{'title': '后面的真实素材', 'url': url}] * (count - 1)
                with self.subTest(url=url, count=count):
                    self.assertIsNone(material_view(report_text(entries)))

    def test_source_candidates_are_unique_for_both_link_branches(self):
        for url in ['', 'https://example.org/real']:
            for title in ['标题 — RSS·示例', '[介绍](https://example.org/info) — RSS·示例']:
                with self.subTest(url=url, title=title):
                    self.assertIsNone(material_view(report_text([{'title': title, 'url': url}])))

    def test_link_prefix_does_not_take_over_the_whole_title(self):
        for suffix in ['', ' — 使用说明', ' — RSS·示例', '\n\n详细说明']:
            with self.subTest(suffix=suffix):
                text = report_text([{'title': '[介绍](https://example.org/info)' + suffix,
                                     'url': ''}])
                self.assertIsNone(material_view(text))

    def test_suffix_block_shapes_do_not_prove_where_a_multiline_author_ends(self):
        suffixes = ['第二行', '## 第二行', '> 第二行', '[说明]: /doc/second', '2. 第二行']
        for url in ['', 'https://example.org/real']:
            for suffix in suffixes:
                for gap in ['\n', '\n\n']:
                    for last in [True, False]:
                        text = ('## 素材清单(1 条)\n\n1. '
                                + ('[标题](<' + url + '>)' if url else '标题')
                                + ' — RSS·作者第一行' + gap + suffix + '\n')
                        if not last:
                            text = text.replace('(1 条)', '(2 条)')
                            text += '2. 下一条素材 — RSS·正常作者\n'
                        with self.subTest(url=url, suffix=suffix, gap=gap, last=last):
                            self.assertIsNone(material_view(text))

    def test_display_structure_does_not_depend_on_the_ledger_tolerance_parser(self):
        text = report_text([{'title': '正常标题', 'url': 'https://example.org/real'}])
        with patch('kb.daily_page._rows', side_effect=AssertionError('不得调用归属容错')):
            self.assertEqual('正常标题', material_view(text)['items'][0]['title'])

    def test_only_a_complete_generator_link_can_be_decoded(self):
        for title in ['[介绍](https://example.org/real)',
                      '[介绍](<https://example.org/real>) — 使用说明',
                      '[介绍](<https://example.org/real>)正文(<https://example.org/fake>)',
                      '[介绍](< https://example.org/real >)',
                      '[未转义[标签]](<https://example.org/real>)']:
            with self.subTest(title=title):
                self.assertIsNone(material_view('## 素材清单(1 条)\n\n1. ' + title
                                                + ' — RSS·正常作者\n'))

    def test_generator_matrix_never_projects_a_different_field(self):
        titles = [
            '普通标题', '[视频] C:\\models\\', '标题 — 普通说明', '标题 — RSS·示例',
            '[介绍](https://example.org/info)', '[介绍](https://example.org/info) — 使用说明',
            '[介绍](https://example.org/info) — RSS·示例',
            '[介绍](https://example.org/info)\n\n详细说明',
            '第一行\n\n1. 内部列表\n2. 还有一条',
            '第一行\n\n## 素材清单(1 条)\n\n1. 内部示例', '很长的标题' * 50,
        ]
        authors = ['正常作者', '歸藏(guizang.ai)', '作者 — RSS·其他人',
                   '作者第一行\n第二行', '作者第一行\n\n第二行']
        projected = 0
        for title in titles:
            for url in ['', 'https://example.org/real']:
                for account in authors:
                    with self.subTest(title=title, url=url, account=account):
                        entry = dict(path='source/one.md', title=title, url=url,
                                     account=account, source='rss', late=False)
                        text = daily._page(datetime(2026, 10, 16, 8),
                                           datetime(2026, 10, 17, 8),
                                           daily.Material(items=[entry], total=1),
                                           '## 今日要点\n\n说明。', 'stub/none')
                        view = material_view(text)
                        if view is not None:
                            projected += 1
                            self.assertEqual(1, len(view['items']))
                            item = view['items'][0]
                            self.assertEqual(' '.join(title.split()), item['full_title'])
                            self.assertEqual(url, item['url'])
                            self.assertEqual('RSS·' + ' '.join(account.split()), item['source'])
        self.assertGreater(projected, 20)


if __name__ == '__main__':
    unittest.main()
