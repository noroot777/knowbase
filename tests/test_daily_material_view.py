"""用真实日报生成器验证简洁清单，展示不能改变落盘证据。"""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily, daily_page
from kb.daily_materials import material_view
from kb.scanner import parse_frontmatter
from kb.web import daily_api


def report_text(entries, *, body='## 今日要点\n\n- 观点 [1][2]', total=None):
    items = [dict(path=f'source/rss/{number}.md', source='rss', account='某号',
                  late=False, **entry) for number, entry in enumerate(entries)]
    material = daily.Material(items=items, total=len(items) if total is None else total)
    return daily._page(datetime(2026, 10, 16, 8), datetime(2026, 10, 17, 8),
                       material, body, 'stub/none')


def report_view(text):
    return material_view(text, parse_frontmatter(text)[0])


class MaterialViewTests(unittest.TestCase):
    def test_multiline_posts_are_individual_items_not_markdown_fragments(self):
        title = ('Grok Bot 套餐不够用怎么办？\n\n'
                 '让 Grok Bot 安装 Pi Agent。\n\n1. 第一步\n2. 第二步\n'
                 '## 素材清单(9 条)\n\n最后一段。')
        text = report_text([{'title': title, 'url': 'https://x.com/example/status/1'},
                            {'title': '下一条素材', 'url': 'https://example.org/next'}])
        view = report_view(text)
        self.assertIsNotNone(view)
        self.assertEqual(2, len(view['items']))
        self.assertEqual([1, 2], [item['number'] for item in view['items']])
        self.assertEqual('Grok Bot 套餐不够用怎么办？', view['items'][0]['title'])
        self.assertEqual(' '.join(title.split()), view['items'][0]['full_title'])
        self.assertEqual('https://x.com/example/status/1', view['items'][0]['url'])
        self.assertEqual('RSS·某号', view['items'][0]['source'])
        self.assertIn('- 观点', view['before'])

    def test_blank_first_lines_and_long_titles_are_compact(self):
        text = report_text([{'title': '\n\n' + '很长的标题' * 40 + '\n正文',
                             'url': 'https://example.org/long'}])
        item = report_view(text)['items'][0]
        self.assertEqual(72, len(item['title']))
        self.assertTrue(item['title'].endswith('…'))
        self.assertNotIn('正文', item['title'])
        self.assertIn('正文', item['full_title'])

    def test_unlinked_titles_keep_literal_brackets_and_backslashes(self):
        title = '[视频] C:\\models\\\n1. 内部编号\n2. 不是新素材'
        text = report_text([{'title': title, 'url': ''},
                            {'title': '紧跟的另一条', 'url': 'https://example.org/next'}])
        before = daily_page.carried_keys(text)
        view = report_view(text)
        self.assertEqual('[视频] C:\\models\\', view['items'][0]['title'])
        self.assertEqual('', view['items'][0]['url'])
        self.assertEqual(before, daily_page.carried_keys(text))

    def test_link_label_unescapes_only_the_generator_escapes(self):
        title = '[视频] [未闭合 C:\\models\\'
        text = report_text([{'title': title, 'url': 'https://example.org/paper-(clean).pdf'}])
        item = report_view(text)['items'][0]
        self.assertEqual(title, item['title'])
        self.assertEqual('https://example.org/paper-(clean).pdf', item['url'])

    def test_legacy_bare_urls_preserve_original_instead_of_guessing(self):
        text = ('## 素材清单(2 条)\n\n'
                '1. [旧标题\n\n原帖正文](https://example.org/paper-(clean).pdf)'
                ' — 大V观点·歸藏(guizang.ai)\n'
                '2. 没有链接 — RSS·某号 · 补录\n\n> 还有待报道的素材。\n')
        self.assertIsNone(material_view(text, dict(docs=2, total=2, late=0, late_total=0)))

    def test_parentheses_in_authors_and_late_suffix_are_preserved(self):
        text = ('## 素材清单(2 条)\n\n'
                '1. [标题](<https://example.org/paper-(clean).pdf>)'
                ' — 大V观点·歸藏(guizang.ai)\n'
                '2. 没有链接 — RSS·某号 · 补录\n')
        view = material_view(text, dict(docs=1, total=1, late=1, late_total=1))
        self.assertEqual('https://example.org/paper-(clean).pdf', view['items'][0]['url'])
        self.assertEqual('大V观点·歸藏(guizang.ai)', view['items'][0]['source'])
        self.assertEqual('RSS·某号 · 补录', view['items'][1]['source'])

    def test_a_separator_inside_an_unlinked_title_does_not_replace_the_source(self):
        text = report_text([{'title': 'AI — 观察\n正文 — 另一段', 'url': ''}])
        item = report_view(text)['items'][0]
        self.assertEqual('AI — 观察', item['title'])
        self.assertEqual('RSS·某号', item['source'])

    def test_fake_heading_in_the_digest_does_not_take_over_the_real_list(self):
        text = report_text([{'title': '真实素材', 'url': 'https://example.org/real'}],
                           body='## 素材清单(9 条)\n\n不是清单\n\n## 今日要点\n\n说明 [1]')
        view = report_view(text)
        self.assertIn('## 素材清单(9 条)', view['before'])
        self.assertEqual('素材清单(1 条)', view['heading'])
        self.assertEqual('真实素材', view['items'][0]['title'])

    def test_queue_notice_and_following_sections_keep_original(self):
        text = report_text([{'title': '素材', 'url': 'https://example.org/one'}], total=9)
        text += '\n## 额外说明\n\n普通正文。\n'
        self.assertIsNone(report_view(text))

    def test_unlinked_postamble_with_a_source_shape_is_not_absorbed(self):
        text = report_text([{'title': '有原文', 'url': 'https://example.org/one'},
                            {'title': '没有原文链接的素材', 'url': ''}])
        text += '\n## 编辑备注\n\n这段补充说明必须显示。\n\n资料整理 — RSS·编辑\n\n后续说明。\n'
        self.assertIsNone(report_view(text))

    def test_ambiguous_source_shapes_in_titles_and_authors_keep_original(self):
        cases = ['标题 — RSS·某号 — RSS·编辑',
                 '标题里的示例 — RSS·示例\n\n真正的标题结尾 — RSS·某号']
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(material_view('## 素材清单(1 条)\n\n1. ' + raw,
                                                dict(docs=1, total=1, late=0, late_total=0)))

    def test_bare_markdown_link_at_start_of_unlinked_title_is_not_an_outer_link(self):
        text = report_text([{'title': '[介绍](https://example.org/info)\n\n详细说明',
                             'url': ''}])
        self.assertIsNone(report_view(text))

    def test_original_spans_preserve_the_exact_trailing_whitespace(self):
        text = report_text([{'title': '相同标题', 'url': ''},
                            {'title': '相同标题', 'url': ''}])
        text += '\n \t\n'
        view = report_view(text)
        start, end = view['span']
        self.assertEqual(text[:start], view['before'])
        self.assertEqual(text[end:], view['after'])
        self.assertEqual('\n\n \t\n', view['after'])
        self.assertEqual(2, len(view['items']))
        for item in view['items']:
            entry_start, entry_end = item['span']
            self.assertEqual('相同标题 — RSS·某号', text[entry_start:entry_end])
        self.assertLess(view['items'][0]['span'][1], view['items'][1]['span'][0])

    def test_multiline_author_boundary_keeps_original_for_linked_and_unlinked_items(self):
        for title in ['标题', '[标题](<https://example.org/one>)']:
            with self.subTest(title=title):
                self.assertIsNone(material_view(
                    '## 素材清单(1 条)\n\n1. ' + title + ' — RSS·作者第一行\n作者第二行\n',
                    dict(docs=1, total=1, late=0, late_total=0)))

    def test_all_161_items_keep_their_numbers_and_urls(self):
        text = report_text([{'title': f'第 {number} 条\n\n很长的正文',
                             'url': f'https://example.org/{number}'} for number in range(161)])
        view = report_view(text)
        self.assertEqual(161, len(view['items']))
        self.assertEqual(161, view['items'][-1]['number'])
        self.assertEqual('https://example.org/160', view['items'][-1]['url'])

    def test_empty_or_unrecognized_lists_can_fall_back_without_losing_body(self):
        for body in ['## 今日要点\n\n没有素材清单。',
                     '## 素材清单(2 条)\n\n1. 只有一条 — RSS·某号',
                     '## 素材清单(1 条)\n\n1. 缺少出处']:
            self.assertIsNone(material_view(body, dict(docs=1, total=1, late=0, late_total=0)))
        view = material_view('## 今日要点\n\n## 素材清单(0 条)\n\n> 说明\n',
                             dict(docs=0, total=0, late=0, late_total=0))
        self.assertEqual([], view['items'])
        self.assertIn('> 说明', view['after'])

    def test_api_adds_read_only_view_without_changing_raw_body_or_file(self):
        text = report_text([{'title': '首行\n\n第二段 [标签]', 'url': ''},
                            {'title': '另一条', 'url': 'https://example.org/next'}])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'daily' / '2026-10-17.md'
            path.parent.mkdir()
            path.write_text(text, encoding='utf-8')
            original = path.read_bytes()
            app = FastAPI()
            app.include_router(daily_api.router)
            with patch.object(daily, 'VAULT_ROOT', root), TestClient(app) as client:
                response = client.get('/api/daily/reports/2026-10-17')
            self.assertEqual(200, response.status_code)
            result = response.json()
            self.assertEqual(parse_frontmatter(text)[1], result['body'])
            self.assertEqual('首行', result['material_view']['items'][0]['title'])
            self.assertEqual(original, path.read_bytes())
            self.assertEqual(daily_page.carried_keys(text),
                             daily_page.carried_keys(result['body']))


if __name__ == '__main__':
    unittest.main()
