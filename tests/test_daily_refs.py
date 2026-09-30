"""正文出处编号换成链接时,别把日报原文改坏(`kb/daily_refs.py`)。

这段替换写进**文件**,页面、复制全文、Obsidian 一起受影响。2026-09-30 连着两轮
审查各抓出一批合法 markdown 写法被改坏:第 1 轮是行内代码和已有链接的文字,
第 2 轮是跨行代码跨度、单引号标题、引用式链接、两层嵌套的括号目标,外加多层围栏
认错开闭。这里逐个钉住 —— 判据统一是「原文那一段逐字还在,而句末那个出处确实
变成了链接」,两半缺一不可:只验前半句的话,一个什么都不替换的实现也能全绿。
"""
import unittest

from kb.daily_refs import link_refs, protected_spans

LINKS = {'1': '[\\[1\\]](<https://e.org/a> "甲文")',
         '2': '[\\[2\\]](<https://e.org/b> "乙文")'}


class KeepsOriginalMarkdown(unittest.TestCase):
    def keeps(self, original: str, note: str = ''):
        """原样保留 original,而它后面那句出处照样变成链接。"""
        out = link_refs(original + '\n\n出处 [1]。', LINKS)
        self.assertIn(original, out, note or original)
        self.assertIn('出处 ' + LINKS['1'] + '。', out, note or original)
        return out

    def test_inline_code(self):
        self.keeps('使用示例 `items[1]` 原样保留。')

    def test_code_span_across_lines(self):
        # 代码跨度可以换行:逐行正则原理上就匹配不到,那一行的 `[1]` 当场被改写,
        # <code> 里明晃晃多出一串网址,照着复制的命令是错的。
        self.keeps('使用示例 `items\n[1]` 原样保留。')

    def test_multi_backtick_code_span(self):
        # N 个反引号由**恰好** N 个关闭,中间那对单反引号不算结束
        self.keeps('多反引号 ``code `x` items[1]`` 也不动。')

    def test_link_with_double_quoted_title(self):
        self.keeps('[版本 [1]](https://e.org/keep "产品说明")')

    def test_link_with_single_quoted_title(self):
        # 只认双引号标题的话,这条链接会被拆成两个不同目的地:
        # 「版本 」指向 keep,而 [1] 指向素材
        self.keeps("[版本 [1]](https://e.org/keep '产品说明')")

    def test_link_with_parenthesised_title(self):
        self.keeps('[版本 [1]](https://e.org/keep (产品说明))')

    def test_link_destination_with_nested_parens(self):
        # 裸目标里的圆括号只要成对就还在目标里,深度不限
        self.keeps('[版本 [1]](https://e.org/paper(a(b)))')

    def test_link_destination_in_angle_brackets(self):
        self.keeps('[版本 [1]](<https://e.org/k e e p>)')

    def test_full_reference_link(self):
        self.keeps('[产品 [1]][product]\n\n[product]: https://e.org/reference')

    def test_collapsed_reference_link(self):
        self.keeps('[产品说明][]\n\n[产品说明]: https://e.org/reference')

    def test_shortcut_reference_link(self):
        self.keeps('[产品说明]\n\n[产品说明]: https://e.org/reference')

    def test_image_alt_text(self):
        self.keeps('![封面 [1]](https://e.org/cover.png)')

    def test_nested_image_inside_link(self):
        self.keeps('[![封面 [1]](https://e.org/c.png)](https://e.org/keep)')

    def test_code_span_inside_link_text(self):
        self.keeps('[见 `a[1]` 的说明](https://e.org/keep)')


class MatchesTheRenderer(unittest.TestCase):
    """和渲染器(CommonMark)同一套口径 —— 这几条都拿 markdown-it 核对过。

    保护得过宽和过窄一样是错的:该链接的出处漏掉,用户看到的还是一串对不上号的
    数字,而那正是这件事要解决的问题。
    """

    def test_a_numeric_definition_makes_the_citation_a_link_already(self):
        # `[1]: url` 是一条链接引用定义,之后正文里的 [1] 本身就渲染成链接了 ——
        # 再替换一次就是把它指向别处,连同定义行一起改坏。
        body = '[1]: https://e.org/reference\n\n出处 [1]。'
        self.assertEqual(body, link_refs(body, LINKS))

    def test_a_label_with_brackets_is_not_a_reference_link(self):
        # 链接标签里不许有未转义的方括号,所以 `[产品 [1]]` 是字面文本,不是链接
        # (markdown-it 实测渲染成 `[产品 [1]]`)。这里的 [1] 就是个普通出处。
        out = link_refs('[产品 [1]] 只是文字。', LINKS)
        self.assertEqual('[产品 ' + LINKS['1'] + '] 只是文字。', out)

    def test_link_text_may_hold_brackets(self):
        # 反过来,链接**文字**允许嵌套方括号 —— `[产品 [1]][product]` 是一条完整
        # 的引用式链接(整段不许动)
        self.keeps_pair('[产品 [1]][product]\n\n[product]: https://e.org/reference')

    def keeps_pair(self, original: str):
        out = link_refs(original + '\n\n出处 [1]。', LINKS)
        self.assertIn(original, out)
        self.assertIn('出处 ' + LINKS['1'] + '。', out)


class Fences(unittest.TestCase):
    def test_longer_fence_survives_inner_fence(self):
        # 「见到三个以上反引号就翻转一次」会把四反引号围栏里那行三反引号当成
        # 结束:围栏中间的编号被改写,真正结束之后的出处反倒漏掉链接。
        body = '````\n```\n围栏里的 [1] 不动\n```\n````\n\n出处 [1]。'
        out = link_refs(body, LINKS)
        self.assertIn('围栏里的 [1] 不动', out)
        self.assertIn('出处 ' + LINKS['1'] + '。', out)

    def test_tilde_fence_is_not_closed_by_backticks(self):
        body = '~~~\n```\n波浪围栏里的 [1] 不动\n~~~\n\n出处 [1]。'
        out = link_refs(body, LINKS)
        self.assertIn('波浪围栏里的 [1] 不动', out)
        self.assertIn('出处 ' + LINKS['1'] + '。', out)

    def test_unclosed_fence_protects_to_the_end(self):
        out = link_refs('```\n没关上的围栏里 [1]', LINKS)
        self.assertEqual('```\n没关上的围栏里 [1]', out)

    def test_info_string_with_backtick_is_not_a_fence(self):
        # ``` 后面再有反引号的那行压根不是围栏,后面的出处照样要加链接
        out = link_refs('```a`b\n\n出处 [1]。', LINKS)
        self.assertIn('出处 ' + LINKS['1'] + '。', out)


class StillLinksTheRealCitations(unittest.TestCase):
    def test_plain_citation(self):
        self.assertEqual('两条 ' + LINKS['1'] + LINKS['2'] + '。',
                         link_refs('两条 [1][2]。', LINKS))

    def test_merged_citation(self):
        for raw in ('[1, 2]', '[1，2]', '[1、2]'):
            self.assertEqual('也认 ' + LINKS['1'] + LINKS['2'],
                             link_refs('也认 ' + raw, LINKS), raw)

    def test_out_of_range_numbers_are_left_alone(self):
        self.assertEqual('第九条 [9],年份 [2026]。',
                         link_refs('第九条 [9],年份 [2026]。', LINKS))

    def test_a_number_that_is_already_a_link_is_not_nested(self):
        self.assertEqual('已是链接 [1](http://x) 不动。',
                         link_refs('已是链接 [1](http://x) 不动。', LINKS))

    def test_a_lone_backtick_does_not_swallow_the_rest(self):
        # 代码跨度不跨空行:落单的反引号只是个普通字符,后面段落的出处照样加链接
        out = link_refs('一个反引号 ` 落单。\n\n出处 [1]。', LINKS)
        self.assertIn('出处 ' + LINKS['1'] + '。', out)

    def test_indented_continuation_lines_still_get_links(self):
        # 故意不认「四空格缩进 = 代码块」:日报正文以列表为主,列表项续行本来就
        # 缩进四格,按代码块保护会让那些行的出处全部漏掉。
        out = link_refs('- 一条\n    续行里的出处 [1]。', LINKS)
        self.assertIn('续行里的出处 ' + LINKS['1'] + '。', out)

    def test_no_links_means_no_change(self):
        self.assertEqual('出处 [1]。', link_refs('出处 [1]。', {}))

    def test_a_group_with_no_match_is_left_alone(self):
        self.assertEqual('区间 [7、8]。', link_refs('区间 [7、8]。', LINKS))


class ProtectedSpans(unittest.TestCase):
    def test_spans_are_sorted_and_disjoint(self):
        body = ('`code[1]` 和 [文字 [1]](https://e.org/k "t") 和 ![图 [1]](https://e.org/i.png)\n'
                '```\n围栏 [1]\n```\n[p]: https://e.org/p\n')
        spans = protected_spans(body)
        self.assertTrue(spans)
        for (a, b), (c, d) in zip(spans, spans[1:]):
            self.assertLessEqual(b, c, f'{(a, b)} 和 {(c, d)} 重叠或没排序')

    def test_a_plain_citation_is_not_protected(self):
        self.assertEqual([], protected_spans('正文里的出处 [1]。'))


if __name__ == '__main__':
    unittest.main()
