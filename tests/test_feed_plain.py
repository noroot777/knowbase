"""HTML 正文转纯文本:行内元素不许断句,块级元素之间必须留出段落边界。

钉的是 2026-10-08 那次的形态 —— `soup.get_text('\n', strip=True)` 对每一个
文本节点都插一次换行,于是 <a>/<em>/<code> 把一句话切成好几行,而 <p> 之间
本该有的空行又被 strip 压成同一个 \n,段落信息在入库那一刻就没了。它一路
往下传:面板按 pre-wrap 一行行照显、vault md 整篇没有空行、翻译按 '\n\n'
切块于是整篇算一段、超长就从句子中间硬切,而 prompt 写着「保持原有的段落与
换行结构」—— 模型忠实照做,译文把碎行逐行译了一遍。

连带钉住修这件事时自己踩的两脚:<pre> 摘出去时整个标签一起换掉的话,它自己
那道块级边界跟着没了(两个代码块粘成一个);而给 HTML 做的空白折叠套到
feedparser 标明的纯文本正文上,原本好好分段的文章会被压成一整段。
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

    def test_adjacent_pre_blocks_do_not_run_together(self):
        # 摘出去时换掉的只能是内容:整个标签 replace_with 成裸文本的话,pre 虽然在
        # _BLOCK 里,后面那趟 find_all 已经找不到它,两个代码块会直接首尾相接。
        self.assertEqual('first()\n    more_first()\n\nsecond()\n    more_second()',
                         fetching.plain('<pre>first()\n    more_first()</pre>'
                                        '<pre>second()\n    more_second()</pre>'))

    def test_pre_is_separated_from_text_right_next_to_it(self):
        # 两侧没有 <p> 代它补边界的形态 —— 原来那条用例恰好被 <p> 遮住了。
        self.assertEqual('Before\n\ncode\n    indent\n\nAfter',
                         fetching.plain('<div>Before<pre>code\n    indent</pre>After</div>'))

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


def atom(body: str, kind: str = 'text', tag: str = 'content') -> bytes:
    return ('<feed xmlns="http://www.w3.org/2005/Atom"><title>Blog</title><entry>'
            '<title type="text">Plain Text</title><id>https://example.org/p</id>'
            '<link href="https://example.org/p"/>'
            f'<{tag} type="{kind}">{body}</{tag}></entry></feed>').encode()


class PlainTextBodiesKeepTheirOwnNewlines(unittest.TestCase):
    """纯文本正文的换行就是内容,不许按 HTML 空白规则折叠掉。

    feedparser 会如实给出正文类型(Atom 的 type="text" → text/plain),照它选解析
    方式;HTML 那套空白折叠套到纯文本上,一篇本来分段的文章会被压成一整段。
    """

    BODY = 'First paragraph.\n\nSecond paragraph.\nThird line.'

    def test_atom_text_content_keeps_paragraphs_and_single_breaks(self):
        entry = fetching.parse_feed(atom(self.BODY), 'https://example.org/feed.xml')[0]
        self.assertEqual(self.BODY, entry['body'])
        self.assertEqual('content', entry['content_kind'])

    def test_atom_text_summary_keeps_them_too(self):
        # summary 走的是 summary_detail.type,和 content 各有一份类型。
        entry = fetching.parse_feed(atom(self.BODY, tag='summary'), 'https://example.org/feed.xml')[0]
        self.assertEqual(self.BODY, entry['body'])
        self.assertEqual('summary', entry['content_kind'])

    def test_html_bodies_are_unaffected(self):
        entry = fetching.parse_feed(feed(ARTICLE), 'https://example.org/feed.xml')[0]
        self.assertEqual(2, len(entry['body'].split('\n\n')))

    def test_a_generic_parameter_survives_in_declared_plain_text(self):
        # 「这段文字能被解析成标签」证明不了作者标错了类型:List<T> 正是 type="text"
        # 要原样显示的东西。嗅探会把 <T> 当标签删掉、段落再按 HTML 规则折叠掉。
        body = 'Use List&lt;T&gt; for the new generic API.\n\nThe second paragraph stays separate.'
        entry = fetching.parse_feed(atom(body), 'https://example.org/feed.xml')[0]
        self.assertEqual('Use List<T> for the new generic API.\n\n'
                         'The second paragraph stays separate.', entry['body'])

    def test_literal_markup_in_a_plain_text_summary_survives_too(self):
        body = 'Literal &lt;b&gt;not bold&lt;/b&gt; sample.\n\nSecond paragraph.'
        entry = fetching.parse_feed(atom(body, tag='summary'), 'https://example.org/feed.xml')[0]
        self.assertEqual('Literal <b>not bold</b> sample.\n\nSecond paragraph.', entry['body'])

    def test_an_article_whose_body_is_only_a_script_example_is_still_collected(self):
        # 走 HTML 路径的话这一篇会被 script 清理整个掏空,parse_feed 当场跳过条目、
        # 抛 FeedError —— 文章有正常标题和链接却收录不进来。它是待显示的示例文字,
        # 不是要执行的脚本(阅读页本来就用 textContent 显示正文)。
        entry = fetching.parse_feed(atom('&lt;script&gt;alert(1)&lt;/script&gt;'),
                                    'https://example.org/feed.xml')[0]
        self.assertEqual('<script>alert(1)</script>', entry['body'])
        self.assertEqual('Plain Text', entry['title'])

    def test_html_typed_bodies_still_get_their_tags_stripped(self):
        # 反过来钉住:类型说是 HTML 就按 HTML 处理,别因为上面几条把分流判反了。
        html = '&lt;p&gt;A &lt;a href="#"&gt;link&lt;/a&gt; here.&lt;/p&gt;&lt;p&gt;Two.&lt;/p&gt;'
        entry = fetching.parse_feed(atom(html, kind='html'), 'https://example.org/feed.xml')[0]
        self.assertEqual('A link here.\n\nTwo.', entry['body'])

    def test_a_missing_type_is_not_treated_as_plain_text(self):
        # 类型取不到(None / 空串)时按 HTML 走,和 oneline/author 那几个不带类型的
        # 调用口径一致;None 也不许让它崩。
        self.assertEqual('A b', fetching.body_text('<p>A</p><p>b</p>', None).replace('\n\n', ' '))
        self.assertEqual('A b', fetching.body_text('<p>A</p><p>b</p>', '').replace('\n\n', ' '))

    def test_plain_text_fields_still_collapse_to_one_line(self):
        self.assertEqual('A b', fetching.oneline('A\n\nb', 'text/plain'))

    def test_trailing_whitespace_and_runs_of_blank_lines_are_normalized(self):
        self.assertEqual('a\n\nb', fetching.body_text('  a   \n\n\n\nb  \n\n', 'text/plain'))
        self.assertEqual('a\nb', fetching.body_text('a\r\nb', 'text/plain'))


if __name__ == '__main__':
    unittest.main()
