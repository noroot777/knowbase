"""HTML 正文转纯文本:行内元素不许断句,块级元素之间必须留出段落边界。

钉的是 2026-10-08 那次的形态 —— `soup.get_text('\n', strip=True)` 对每一个
文本节点都插一次换行,于是 <a>/<em>/<code> 把一句话切成好几行,而 <p> 之间
本该有的空行又被 strip 压成同一个 \n,段落信息在入库那一刻就没了。它一路
往下传:面板按 pre-wrap 一行行照显、vault md 整篇没有空行、翻译按 '\n\n'
切块于是整篇算一段、超长就从句子中间硬切,而 prompt 写着「保持原有的段落与
换行结构」—— 模型忠实照做,译文把碎行逐行译了一遍。
"""
import unittest

from kb.feeds import fetching, translate

# Simon Willison 那篇 Claude Haiku 5.5 的真实形态:三个链接都夹在句子中间。
ARTICLE = ('<p>As previously <a href="https://x/1">promised</a>, here\'s Anthropic\'s new fast, '
           'low cost model: <a href="https://x/2">Introducing Claude Haiku 5.5</a>.</p>\n'
           '<p>The previous Haiku, 4.5, was very much showing its age. It came out '
           '<a href="https://x/3">almost a year ago</a>, and was priced at $1/million input.</p>')


def feed(body_html: str, title: str = 'Claude Haiku 5.5') -> bytes:
    return ('<?xml version="1.0"?><rss version="2.0"><channel><title>Blog</title><item>'
            f'<title>{title}</title><link>https://example.org/haiku</link>'
            f'<description><![CDATA[{body_html}]]></description>'
            '</item></channel></rss>').encode()


class InlineElementsKeepSentencesWhole(unittest.TestCase):
    def test_links_inside_a_sentence_do_not_start_new_lines(self):
        lines = fetching.plain(ARTICLE).splitlines()
        self.assertEqual("As previously promised, here's Anthropic's new fast, low cost model: "
                         'Introducing Claude Haiku 5.5.', lines[0])
        # 链接文字不许独占一行,句末的句点也不许被甩成一行。
        self.assertNotIn('promised', lines[1:])
        self.assertNotIn('.', lines)

    def test_paragraphs_are_separated_by_a_blank_line(self):
        text = fetching.plain(ARTICLE)
        self.assertEqual(2, len(text.split('\n\n')))
        self.assertIn('\n\n', text)

    def test_nested_inline_tags_and_entities_stay_on_one_line(self):
        self.assertEqual('Hi there you, see x = 1 ok.',
                         fetching.plain('<p>Hi <em>there <strong>you</strong></em>, '
                                        'see <code>x = 1</code> ok.</p>'))

    def test_comments_and_scripts_never_reach_the_text(self):
        html = ('<p>Visible<!-- hidden note --> text</p>'
                '<script>var leak = 1;</script><style>p{color:red}</style>'
                '<noscript>enable js</noscript>')
        self.assertEqual('Visible text', fetching.plain(html))

    def test_source_indentation_collapses_like_html_does(self):
        self.assertEqual('hello world',
                         fetching.plain('<div>\n   <p>\n     hello\n     <b>world</b>\n   </p>\n</div>'))


class BlockBoundariesKeepTheirGranularity(unittest.TestCase):
    def test_br_breaks_one_line_and_list_items_get_their_own_paragraph(self):
        # <br> 前后各插一个标记,合并之后仍然只断一行 —— 不然一行地址会被撑成几段。
        self.assertEqual('one\ntwo', fetching.plain('<p>one<br>two</p>'))
        # li 之间留空行:vault md 里单换行会被 markdown 合并,几项会挤成一行。
        self.assertEqual('a b c\n\nd', fetching.plain('<ul><li>a <a href="#">b</a> c</li><li>d</li></ul>'))

    def test_table_cells_share_a_line_and_rows_break(self):
        self.assertEqual('a b\nc d', fetching.plain(
            '<table><tr><td>a</td><td>b</td></tr><tr><td>c</td> <td>d</td></tr></table>'))

    def test_pre_keeps_its_indentation_and_inner_blank_lines(self):
        text = fetching.plain('<p>Try:</p><pre><code>def f():\n    return 1\n\n    return 2\n</code></pre><p>Done.</p>')
        self.assertEqual('Try:\n\ndef f():\n    return 1\n\n    return 2\n\nDone.', text)

    def test_placeholder_control_chars_in_the_source_cannot_forge_a_pre_block(self):
        # <pre> 摘出去时在原位留的是 NUL 占位符;正文里写出同样的形状不许被当真。
        text = fetching.plain('<p>\x00 0 \x00 and \x020\x02</p><pre>SECRET</pre>')
        self.assertNotIn('SECRET', text.split('\n\n')[0])
        self.assertEqual('0 and 0\n\nSECRET', text)


class ParsedEntriesFeedTheRestOfThePipeline(unittest.TestCase):
    def test_entry_body_carries_paragraph_boundaries(self):
        entry = fetching.parse_feed(feed(ARTICLE), 'https://example.org/feed.xml')[0]
        self.assertEqual(2, len(entry['body'].split('\n\n')))
        self.assertTrue(entry['body'].startswith('As previously promised,'))

    def test_title_never_contains_newlines(self):
        # 标题和作者是单行字段:块级元素在正文里该换行,在这里只该是一个空格。
        entry = fetching.parse_feed(feed(ARTICLE, 'Claude <em>Haiku</em><br>5.5'),
                                    'https://example.org/feed.xml')[0]
        self.assertEqual('Claude Haiku 5.5', entry['title'])
        self.assertEqual('A b c d', fetching.oneline('<h1>A <em>b</em> c</h1><p>d</p>'))
        self.assertEqual('', fetching.oneline(''))

    def test_translation_splits_on_paragraphs_instead_of_cutting_mid_sentence(self):
        # 每段都短于 CHUNK、全文长于 CHUNK:有段落边界时按段落切,没有就只能硬切。
        para = 'Anthropic released a new model today and the price is unchanged. '
        html = ''.join(f'<p>{para * 10} Paragraph <a href="#">{i}</a> ends here.</p>' for i in range(6))
        body = fetching.plain(html)
        self.assertGreater(len(body), translate.CHUNK)
        chunks = translate._split(body)
        self.assertGreater(len(chunks), 1)
        # 按段落切时每块都是整段(或整段的拼接),原样拼回去逐字等于正文;
        # 从句中硬切的话这一句当场不成立。
        self.assertEqual(body, '\n\n'.join(chunks))
        for chunk in chunks:
            self.assertTrue(chunk.endswith('ends here.'), chunk[-60:])


if __name__ == '__main__':
    unittest.main()
