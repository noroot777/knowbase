# -*- coding: utf-8 -*-
"""命令行那条入口的**输入层**:地址由调用方给定,原样交出去。

**这一份的核心是一条不变量**:发出去的每一条地址,都**逐字等于调用方给的某一个
参数、或者标准输入里的某一行**;给进来的东西不是「一行一条地址」时,**一个请求
都不发**。

**为什么判据是这一条。** 这里曾经从一整段文字里挑链接,那时的判据是「这一段结构
真正表达的目标是哪一条」—— 它**等于 CommonMark 规范**(官方 conformance 650+ 条),
穷不尽,十一轮审查在这一件事上红了十一次;而猜错**不可恢复**
(`registry.put_items` 的 `ON CONFLICT DO NOTHING`)。换成现在这一条之后,判据落在
**一个有限集合**上(调用方给的那几行),可以穷尽地钉住。始末见
`docs/decisions/link-grab.md` 的「为什么输入层不解析 Markdown」。

**判据落在「真发出去的那几个请求」上**(`plan['posted']` / `['asked']` /
`['calls']`),不是某个函数的返回值 —— 收录、`--state`、`--refetch` 走同一条解析。

夹具(假 kb-web)在 `tests/grab_cli_fixture.py`,收敛那一组在
`tests/test_grab_cli.py`。
"""
from __future__ import annotations

import argparse
import time
import unittest

from tests.grab_cli_fixture import (FULLWIDTH, OTHER, PAREN, STDIN_WHOLE_LINES, URL,
                                    GrabCliCase, cli)

#: 原先每一种都有一条「要从里面挑出哪个目标」的解析用例。**判据换了之后它们合成
#: 一条**:一个都不猜,一律报错、一个请求都不发。左边是那个形态的名字,右边是输入。
TANGLED = [
    ('夹着叙述文字的裸地址', f'推荐 {URL} 这篇'),
    ('Markdown 链接', f'见 [原文]({URL}) 这篇'),
    ('Markdown 的目标写在下一行', f'见 [原文](\n{URL})'),
    ('Markdown 的 title 里也有地址', f'[原文]({URL} "另见 {OTHER}")'),
    ('显示文字本身就是一条地址', f'[{OTHER}]({URL})'),
    ('链接定义行', f'[标签]: {URL}'),
    ('简写引用加定义(跨空行)', f'看 [标签] 这篇\n\n[标签]: {URL}'),
    ('引用块里的定义', f'> [标签]: {URL}'),
    ('反引号里的代码示例', f'看这个写法 `[原文]({URL})`'),
    ('HTML 标签的属性值', f'<a href="{URL}">原文</a>'),
    ('HTML 注释里的地址', f'<!-- {URL} -->'),
    ('autolink', f'见 <{URL}> 这篇'),
    ('引号圈起来的', f'见 "{URL}" 这篇'),
    ('带字符引用的目标', f'[原文]({URL}?a=1&amp;b=2)'),
    ('图片夹在显示文字里', f'[看 ![图](img.png) 这个]({URL})'),
    ('收不了的目标', '[原文](#节) 和 [另一篇](../x.html)'),
]


class GrabCliInputTests(GrabCliCase):
    # ------------------------------------------------- 给定的那一条原样交出去

    def test_an_explicit_address_is_handed_over_untouched(self):
        """**参数里那一条一个字符都不许动。** 自带一对括号的地址(维基那一类)在第一个
        `)` 处被截断之后,那个地址**可能照样存在** —— 收进来的于是是另一篇文章,比
        抓不到更坏;`--refetch` / `--state` 走同一条解析,所以还会连带认错「要替换
        哪一篇」「要查哪一篇」。
        """
        self.run_cli('--no-wait', FULLWIDTH)
        self.run_cli('--no-wait', '--refetch', FULLWIDTH)
        self.run_cli('--state', FULLWIDTH)
        self.run_cli('--no-wait', PAREN)
        self.run_cli('--no-wait', 'https://example.org/post!')
        self.assertEqual(self.plan['posted'],
                         [FULLWIDTH, FULLWIDTH, PAREN, 'https://example.org/post!'])
        self.assertEqual(self.plan['asked'], [FULLWIDTH])

    def test_whole_lines_on_stdin_are_handed_over_untouched(self):
        """**标准输入里那几行和显式参数一样保真。**

        实测过的四组都是「一行一条」的清单:全角括号那一条曾被截成 `…/wiki/函数`
        (那一页真的存在),末尾带 `;` `,` `:` 的三条曾被剥掉最后一个字符 —— 后端
        `normalize` 本来原样收下它们。
        """
        self.run_cli('--no-wait', '-', stdin='\n'.join(STDIN_WHOLE_LINES))
        self.assertEqual(self.plan['posted'], STDIN_WHOLE_LINES)

    def test_blank_lines_are_skipped_rather_than_refused(self):
        """**空行跳过,不是错。** 文件末尾那个换行、段与段之间的空行都是常态
        (`grab - < links.txt` 的清单里尤其常见)。**反过来钉一边**:跳过空行不等于
        跳过认不出的行 —— 后者照旧报错(见下面那条)。
        """
        self.run_cli('--no-wait', '-', stdin=f'\n\n{URL}\n\n\n{OTHER}\n\n')
        self.assertEqual(self.plan['posted'], [URL, OTHER])

    def test_surrounding_whitespace_is_not_part_of_the_address(self):
        """**前后的空白剥掉** —— 它不是地址的一部分(复制粘贴常带一个尾随空格,
        而它会让后端把这一条当成另一个身份)。中间的空白不在此列:那样的行压根不是
        一条地址,走报错那一档。
        """
        self.run_cli('--no-wait', '-', stdin=f'  {URL}  \n\t{OTHER}\t\n')
        self.assertEqual(self.plan['posted'], [URL, OTHER])

    def test_a_scheme_in_capitals_is_still_an_address(self):
        """**协议名不分大小写。** `HTTPS://…` 是合法写法(后端收下、规范化成小写)。
        漏掉 `re.I` 的后果不是「少收一条」,是这一行压根不算一条地址、整段输入当场以
        2 退出 —— 用户给的是个合法地址,却被告知「不是一条地址」。
        """
        self.run_cli('--no-wait', '-', stdin='HTTPS://example.org/Upper')
        self.assertEqual(self.plan['posted'], ['HTTPS://example.org/Upper'])

    # ------------------------------------------------- 认不出的一律失败关闭

    def test_every_url_we_send_is_a_line_the_caller_gave_us(self):
        """**核心不变量:发出去的地址只能是调用方逐字给的那一行,否则一个请求都不发。**

        这一条替代了原先那二十多条「从这一段里挑出真目标」的解析用例 —— 那些形态
        全在 `TANGLED` 里,现在答案统一是「报错」。**挑地址是调用方(大模型)那一步
        的活**,它读 Markdown 没有困难;这一层再猜一遍,猜错就是静默收错一篇、而且
        换不回来。

        四件一起钉:退出 2、**stdout 一个字都没有**、**一个请求都没发**、
        stderr 里有那句人话。最后一件尤其要紧 —— 退出 2 却已经投出去了,是最坏的
        那一档。
        """
        for name, blob in TANGLED:
            with self.subTest(name):
                self.plan['calls'].clear()
                self.plan['posted'].clear()
                code, out, err = self.run_cli('--no-wait', '-', stdin=blob)
                self.assertEqual(code, 2, name)
                self.assertEqual(out, '', '不许给出任何结果行')
                self.assertEqual(self.plan['calls'], [], '一个请求都不许发')
                self.assertIn('不是一条地址', err)
                self.assertNotIn('Traceback', err)

    def test_the_error_says_which_line_to_fix(self):
        """**要指出是第几行** —— 那是用户唯一能据以改输入的信息(一份几十行的清单里
        混进一行说明文字,不说第几行就只能一行一行试)。**行号按原始输入数,空行也
        占一行**:跳过空行不等于它不存在,否则报出来的行号对不上用户看到的那一份。
        """
        code, out, err = self.run_cli(
            '--no-wait', '-', stdin=f'{URL}\n\n推荐 {OTHER} 这篇\n')
        self.assertEqual(code, 2)
        self.assertEqual(self.plan['calls'], [], '前面那条合格的也不许先投出去')
        self.assertIn('第 3 行', err)
        self.assertIn('网页端', err, '要给出路,不是只说不行')

    def test_a_long_line_is_snipped_so_the_way_out_stays_visible(self):
        """**回显那一行要截断。** 剪贴板里可能是一整篇文章,整行打出来会把后面那句
        「怎么办」冲到屏幕外 —— 那句才是用户要看的。
        """
        blob = '推荐这篇 ' + 'x' * 4000 + f' {URL}'
        code, out, err = self.run_cli('--no-wait', '-', stdin=blob)
        self.assertEqual(code, 2)
        self.assertLess(len(err), 500, '回显没截断,出路那句被冲掉了')
        self.assertIn('…', err)
        self.assertIn('网页端', err)

    def test_a_scheme_we_cannot_grab_is_refused_not_posted(self):
        """**整行是一条地址,但协议不是 http/https** —— 这一层的职责就是回答「它是不是
        一条我们能收的地址」,所以在这儿拦掉,不投出去。
        """
        for one in ('ftp://example.org/x', 'file:///etc/passwd', 'javascript:alert(1)'):
            with self.subTest(one):
                self.plan['calls'].clear()
                code, out, err = self.run_cli('--no-wait', '-', stdin=one)
                self.assertEqual(code, 2)
                self.assertEqual(self.plan['calls'], [])
                self.assertIn('不是一条地址', err)

    def test_nothing_but_blank_lines_is_a_missing_argument(self):
        """**全是空行 = 没给链接**,不是「收了零条」。0 条不该报成成功。"""
        code, out, err = self.run_cli('--no-wait', '-', stdin='\n\n  \n\t\n')
        self.assertEqual(code, 2)
        self.assertEqual(self.plan['calls'], [])
        self.assertIn('没给出要收录的链接', err)

    # ------------------------------------------------- 顺序与去重

    def test_addresses_keep_their_order_and_are_deduped(self):
        """**按出现顺序投,重复的只投一次。** 顺序是用户看结果的顺序;重复投两次会在
        终端上给出两行自相矛盾的档位(第二条必然是「库里已经有了」)。
        """
        self.run_cli('--no-wait', '-', stdin=f'{URL}\n{OTHER}\n{URL}\n')
        self.assertEqual(self.plan['posted'], [URL, OTHER])

    def test_many_distinct_addresses_are_deduped_without_comparing_pairs(self):
        """**去重不许两两比较。** `not in 列表` 那一版每条都跟前面所有条比一遍 ——
        一次给上万条互不相同的地址时是平方级。出路是**集合判在不在、列表保顺序**
        (`grab_input.dedupe`,参数和标准输入两个来源共用它)。

        规模要够:两万条时列表那一版 0.62 秒(还在阈值内),四万条 2.62 秒 ——
        长度翻倍耗时四倍。少给一半,把集合换回列表一条都不红。

        **反过来钉一边**:顺序和去重的结果一个都不许变。
        """
        many = [f'https://example.org/p/{k}' for k in range(40000)]
        start = time.monotonic()
        got = cli._targets(argparse.Namespace(url=many + many[:3]))
        spent = time.monotonic() - start
        self.assertLess(spent, 1.0, f'去重花了 {spent:.2f} 秒')
        self.assertEqual(got, many, '顺序和去重的结果都不许变')


if __name__ == '__main__':
    unittest.main()
