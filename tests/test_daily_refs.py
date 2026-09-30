"""正文出处编号换成链接时,别把日报原文改坏(`kb/daily_refs.py`)。

这段替换写进**文件**,页面、复制全文、Obsidian 一起受影响。2026-09-30 连着四轮
审查各抓出一批合法 markdown 写法被改坏:第 1 轮是行内代码和已有链接的文字;
第 2 轮是跨行代码跨度、单引号/圆括号标题、引用式链接、两层嵌套的括号目标,外加
多层围栏认错开闭;第 3 轮是多行定义、引用段里的定义、原始 HTML 的 `<a>` 与
`<code>`、独立缩进代码块、autolink 的地址 —— 前三轮补的都是"哪些片段不许动"的
规则,第三轮证明这条路没有终点,于是改成**每次替换渲染一遍、由渲染器裁决**。
第 4 轮抓的是这个裁决本身的两处不实:裁决用 CommonMark 而页面的 marked 开着
GFM(裸网址、表格都看不见),以及"这一处之外一个字都没动"其实只比了代码/图片/
链接那几类(表格挤错列、不可见的定义行冒出来都漏过)。

判据统一是「原文那一段逐字还在,而它后面那个出处确实变成了链接」,两半缺一不可:
只验前半句的话,一个什么都不替换的实现也能全绿 —— 而"保护得过宽"和"改坏原文"
一样是错的,该链接的出处漏掉,用户看到的还是一串对不上号的数字。
"""
import unittest

from kb.daily_refs import link_refs

# link_refs 收的是「编号 → (已转义的目标, 已转义的标题)」,转义口径在 kb.daily
LINKS = {'1': ('<https://e.org/a>', '甲文'), '2': ('<https://e.org/b>', '乙文')}
# 它该落盘成什么样
OUT = {n: f'[\\[{n}\\]]({d} "{t}")' for n, (d, t) in LINKS.items()}


class Keeps(unittest.TestCase):
    """基类:原样保留 original,而它后面那句出处照样变成链接。"""

    def keeps(self, original: str, note: str = ''):
        out = link_refs(original + '\n\n出处 [1]。', LINKS)
        self.assertIn(original, out, note or original)
        self.assertIn('出处 ' + OUT['1'] + '。', out, note or original)
        return out


class CodeStaysCode(Keeps):
    def test_inline_code(self):
        self.keeps('使用示例 `items[1]` 原样保留。')

    def test_code_span_across_lines(self):
        # 代码跨度可以换行:逐行正则原理上就匹配不到,那一行的 `[1]` 当场被改写,
        # <code> 里明晃晃多出一串网址,照着复制的命令是错的。
        self.keeps('使用示例 `items\n[1]` 原样保留。')

    def test_multi_backtick_code_span(self):
        # N 个反引号由**恰好** N 个关闭,中间那对单反引号不算结束
        self.keeps('多反引号 ``code `x` items[1]`` 也不动。')

    def test_raw_html_code(self):
        # 第 3 轮:`<code>` 是用 HTML 直接写的,markdown 那层看不出它是代码,
        # 可渲染出来的页面里就是一段代码 —— 实测里面被插进了一条 <a>
        self.keeps('使用示例 <code>items[1]</code>。')

    def test_standalone_indented_code_block(self):
        # 第 3 轮:空行后四空格缩进是独立代码块
        self.keeps('使用示例:\n\n    items[1]')

    def test_code_span_inside_link_text(self):
        self.keeps('[见 `a[1]` 的说明](https://e.org/keep)')


class LinksStayWhole(Keeps):
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

    def test_reference_destination_on_next_line(self):
        # 第 3 轮:定义的目标可以另起一行
        self.keeps('[产品 [1]][p]\n\n[p]:\n  https://e.org/keep')

    def test_reference_definition_in_blockquote(self):
        # 第 3 轮:引用段里的定义照样在全篇生效
        self.keeps('> [p]: https://e.org/keep\n\n[产品 [1]][p]')

    def test_autolink_destination(self):
        # 第 3 轮:autolink 的地址就是它的文字,改了文字就是改了跳转目标
        # (实测 href 从 %5B1%5D 变成 %5B%5C%5B1%5C%5D%5D)
        self.keeps('已有地址 <https://e.org/path/[1]>。')

    def test_raw_html_link(self):
        # 第 3 轮:HTML 直接写的链接。链接套链接在 HTML 里非法,浏览器会把内层
        # 提升成兄弟 —— 原来那条的文字被切掉一截,点 [1] 跳到素材而不是产品页。
        self.keeps('<a href="https://e.org/keep">版本 [1]</a>')

    def test_image_alt_text(self):
        self.keeps('![封面 [1]](https://e.org/cover.png)')

    def test_nested_image_inside_link(self):
        self.keeps('[![封面 [1]](https://e.org/c.png)](https://e.org/keep)')

    def test_a_number_that_is_already_a_link_is_not_nested(self):
        self.assertEqual('已是链接 [1](http://x) 不动。',
                         link_refs('已是链接 [1](http://x) 不动。', LINKS))


class MatchesTheGfmRenderer(Keeps):
    """页面的 marked 默认开着 GFM,裁决也得开 —— 第 4 轮两条拦验收都出在这儿。

    按 CommonMark 裁决的话:裸网址只是一段文字(于是放行,而页面上它是链接,替换完
    点过去是另一个地址)、表格压根不解析(单元格被标题里的 `|` 挤错位也看不出来)。
    """

    def test_bare_url(self):
        # 实测 href 从 …/%5B1%5D 变成 …/%5B%5C%5B1%5C%5D%5D,点了跳错地方
        self.keeps('已有地址 https://e.org/path/[1] 原样保留。')

    def test_www_url(self):
        self.keeps('已有地址 www.e.org/path/[1] 原样保留。')

    def test_strikethrough(self):
        out = link_refs('~~不再推荐 [1]~~。', LINKS)
        self.assertEqual('~~不再推荐 ' + OUT['1'] + '~~。', out)

    def test_table_cell_keeps_its_columns(self):
        # 标题里的 `|` 已在 kb.daily 那边转义,所以这一格照样能加链接、状态还在
        body = ('| 主题 | 出处 | 状态 |\n| --- | --- | --- |\n'
                '| 产品 | [1] | 已可用 |')
        out = link_refs(body, LINKS)
        self.assertIn('| 产品 | ' + OUT['1'] + ' | 已可用 |', out)

    def test_a_pipe_in_the_title_would_split_the_row(self):
        # 没转义的 `|` 会把那一格从中间劈开、把「已可用」挤出表外 —— 裁决得拦下来,
        # 宁可这一处漏链接。表格外的同一条出处不受影响。
        bad = {'1': ('<https://e.org/a>', '技术 | 新闻')}
        body = ('| 主题 | 出处 | 状态 |\n| --- | --- | --- |\n'
                '| 产品 | [1] | 已可用 |\n\n表外出处 [1]。')
        out = link_refs(body, bad)
        self.assertIn('| 产品 | [1] | 已可用 |', out)
        self.assertIn('表外出处 [\\[1\\]](<https://e.org/a> "技术 | 新闻")。', out)

    def test_an_unused_definition_stays_invisible(self):
        # 没被正文引用的定义行在页面上是不显示的。把它改成出处链接等于凭空多出
        # 一行可见正文 —— 比对只挑代码/图片/链接那几类时漏过了(第 4 轮不拦项)。
        body = '[1]: https://e.org/unused\n\n另一条出处 [2]。'
        out = link_refs(body, LINKS)
        self.assertIn('[1]: https://e.org/unused', out)
        self.assertIn('另一条出处 ' + OUT['2'] + '。', out)


class MatchesTheRenderer(Keeps):
    """和渲染器(CommonMark)同一套口径 —— 这几条都拿 markdown-it 核对过。"""

    def test_a_numeric_definition_makes_the_citation_a_link_already(self):
        # `[1]: url` 是一条链接引用定义,之后正文里的 [1] 本身就渲染成链接了 ——
        # 再替换一次就是把它指向别处,连同定义行一起改坏。
        body = '[1]: https://e.org/reference\n\n出处 [1]。'
        self.assertEqual(body, link_refs(body, LINKS))

    def test_a_label_with_brackets_is_not_a_reference_link(self):
        # 链接标签里不许有未转义的方括号,所以 `[产品 [1]]` 是字面文本,不是链接
        # (markdown-it 实测渲染成 `[产品 [1]]`)。这里的 [1] 就是个普通出处。
        out = link_refs('[产品 [1]] 只是文字。', LINKS)
        self.assertEqual('[产品 ' + OUT['1'] + '] 只是文字。', out)

    def test_link_text_may_hold_brackets(self):
        # 反过来,链接**文字**允许嵌套方括号 —— `[产品 [1]][product]` 是一条完整
        # 的引用式链接(整段不许动)
        self.keeps('[产品 [1]][product]\n\n[product]: https://e.org/reference')

    def test_a_line_that_only_looks_like_a_definition(self):
        # 冒号后面跟着目标**再跟一段文字**的不是定义,那个 [1] 就是个普通出处 ——
        # 照定义登记下来的话这一条永远漏链接。
        out = link_refs('[1]: https://e.org/x 多余文字\n\n出处 [1]。', LINKS)
        self.assertEqual(2, out.count(OUT['1']), out)

    def test_a_definition_inside_a_code_span_is_not_a_definition(self):
        # 代码里那行长得像定义,可它是代码 —— 既不该改它,也不该让它影响别处
        out = self.keeps('`[1]: https://e.org/keep`')
        self.assertNotIn('e.org/keep>', out)

    def test_escaped_bracket_stays_plain_text(self):
        # `\[1]` 渲染出来是字面的 [1],不是可替换的出处:换了只会得到一串
        # 谁也点不动的 `[[1]](…)` 字面量
        self.assertEqual('单边转义 \\[1] 原样。',
                         link_refs('单边转义 \\[1] 原样。', LINKS))

    def test_html_block_is_left_alone(self):
        # HTML 块内部不解析 markdown,塞一条链接进去只会得到一行字面量
        self.keeps('<div>\n[1]\n</div>')


class Fences(unittest.TestCase):
    def test_longer_fence_survives_inner_fence(self):
        # 「见到三个以上反引号就翻转一次」会把四反引号围栏里那行三反引号当成
        # 结束:围栏中间的编号被改写,真正结束之后的出处反倒漏掉链接。
        body = '````\n```\n围栏里的 [1] 不动\n```\n````\n\n出处 [1]。'
        out = link_refs(body, LINKS)
        self.assertIn('围栏里的 [1] 不动', out)
        self.assertIn('出处 ' + OUT['1'] + '。', out)

    def test_tilde_fence_is_not_closed_by_backticks(self):
        body = '~~~\n```\n波浪围栏里的 [1] 不动\n~~~\n\n出处 [1]。'
        out = link_refs(body, LINKS)
        self.assertIn('波浪围栏里的 [1] 不动', out)
        self.assertIn('出处 ' + OUT['1'] + '。', out)

    def test_unclosed_fence_protects_to_the_end(self):
        out = link_refs('```\n没关上的围栏里 [1]', LINKS)
        self.assertEqual('```\n没关上的围栏里 [1]', out)

    def test_info_string_with_backtick_is_not_a_fence(self):
        # ``` 后面再有反引号的那行压根不是围栏,后面的出处照样要加链接
        out = link_refs('```a`b\n\n出处 [1]。', LINKS)
        self.assertIn('出处 ' + OUT['1'] + '。', out)


class StillLinksTheRealCitations(unittest.TestCase):
    def test_plain_citation(self):
        self.assertEqual('两条 ' + OUT['1'] + OUT['2'] + '。',
                         link_refs('两条 [1][2]。', LINKS))

    def test_merged_citation(self):
        for raw in ('[1, 2]', '[1，2]', '[1、2]'):
            self.assertEqual('也认 ' + OUT['1'] + OUT['2'],
                             link_refs('也认 ' + raw, LINKS), raw)

    def test_out_of_range_numbers_are_left_alone(self):
        self.assertEqual('第九条 [9],年份 [2026]。',
                         link_refs('第九条 [9],年份 [2026]。', LINKS))

    def test_a_lone_backtick_does_not_swallow_the_rest(self):
        # 代码跨度不跨空行:落单的反引号只是个普通字符,后面段落的出处照样加链接
        out = link_refs('一个反引号 ` 落单。\n\n出处 [1]。', LINKS)
        self.assertIn('出处 ' + OUT['1'] + '。', out)

    def test_indented_continuation_lines_still_get_links(self):
        # 日报正文以列表为主,列表项续行本来就缩进四格 —— 那是段落,不是代码块,
        # 按缩进一刀切会让这些行的出处全部漏掉。和上面那条独立缩进代码块成对看。
        out = link_refs('- 一条\n    续行里的出处 [1]。', LINKS)
        self.assertIn('续行里的出处 ' + OUT['1'] + '。', out)

    def test_every_citation_in_a_long_body_gets_linked(self):
        # 逐个候选各判各的:一处被拒不该带累其它
        body = '\n\n'.join(['`a[1]`'] + [f'第 {i} 句 [1]。' for i in range(12)])
        out = link_refs(body, LINKS)
        self.assertIn('`a[1]`', out)
        self.assertEqual(12, out.count(OUT['1']), out)

    def test_no_links_means_no_change(self):
        self.assertEqual('出处 [1]。', link_refs('出处 [1]。', {}))

    def test_a_group_with_no_match_is_left_alone(self):
        self.assertEqual('区间 [7、8]。', link_refs('区间 [7、8]。', LINKS))


if __name__ == '__main__':
    unittest.main()
