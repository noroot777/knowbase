# -*- coding: utf-8 -*-
"""命令行那条入口的**输入解析层**:哪一档原样交出去、哪一档才抽取。

**判据落在「真发出去的那几个请求」上**,不是 `links_in` 的返回值 —— 收录、
`--state`、`--refetch` 走同一条解析,所以认错边界会让三种动作一起认错那一篇。

这一层唯一的不变量:**宁可留下一个抓不到的坏地址,也不截短成另一个可能存在的
目标**。后者是静默收错一篇文章(而 `put_items` 的 `DO NOTHING` 之下换不回来),
前者在终端上就是一行 `failed`,用户看得见、改写了再来。

夹具(假 kb-web)在 `tests/grab_cli_fixture.py`,收敛那一组在
`tests/test_grab_cli.py`。
"""
from __future__ import annotations

import unittest

from tests.grab_cli_fixture import (FULLWIDTH, OTHER, PAREN, STDIN_WHOLE_LINES, URL,
                                    GrabCliCase, cli)


class GrabCliInputTests(GrabCliCase):
    # ---------------------------------------------------------------- 地址本身

    def test_an_explicit_address_is_handed_over_untouched(self):
        """**显式给出的那一条一个字符都不许动。** 自带一对括号的地址(维基那一类)
        在第一个 `)` 处被截断之后,那个地址**可能照样存在** —— 收进来的于是是另一篇
        文章,比抓不到更坏;`--refetch` / `--state` 走同一条解析,所以还会连带认错
        「要替换哪一篇」「要查哪一篇」。
        """
        self.run_cli('--no-wait', FULLWIDTH)
        self.run_cli('--no-wait', '--refetch', FULLWIDTH)
        self.run_cli('--state', FULLWIDTH)
        self.run_cli('--no-wait', 'https://example.org/post!')
        self.assertEqual(self.plan['posted'],
                         [FULLWIDTH, FULLWIDTH, 'https://example.org/post!'])
        self.assertEqual(self.plan['asked'], [FULLWIDTH])

    def test_whole_lines_on_stdin_are_handed_over_untouched(self):
        """**标准输入里整行就是一条地址的那些,和显式参数一样保真。**

        `-` 既用来粘一段话、也用来喂「一行一条」的清单,而第 2 轮审查实测的四组
        恰恰都是后者:全角括号那一条被截成 `…/wiki/函数`(那一页真的存在),末尾带
        `;` `,` `:` 的三条被剥掉最后一个字符 —— 后端 `normalize` 本来原样收下它们。
        判据落在**真发出去的那几个请求**上,不是 `links_in` 的返回值。
        """
        self.run_cli('--no-wait', '-', stdin='\n'.join(STDIN_WHOLE_LINES))
        self.assertEqual(self.plan['posted'], STDIN_WHOLE_LINES)

    def test_prose_with_an_explicit_boundary_is_handed_over_untouched(self):
        """**行内**也有「边界是给定的」那一类:`<地址>`、Markdown `[文字](地址)`、
        引号包起来的那几种 —— 分隔符本身一定不属于地址(`< > "` 在 URI 里必须
        编码,Markdown 的圆括号由语法配平),所以这几档**一个字符都不剥**。

        第 3 轮审查实测的五组就在这儿:原先「整行才保真」,于是圈得明明白白的
        `<…/post;>` 照旧被剥成 `…/post`,而后端对原地址本来是收下并保留的 ——
        收录、`--state`、`--refetch` 走同一条解析,三种动作一起认错那一篇。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 <https://example.org/post;> 这篇\n'
                           '见 "https://example.org/post," 这篇\n'
                           '见 [原文](https://example.org/post:) 这篇\n'
                           '见 <https://example.org/函数，性质> 这篇\n'
                           '见 <https://example.org/post)> 这篇')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/post;', 'https://example.org/post,',
                          'https://example.org/post:', 'https://example.org/函数，性质',
                          'https://example.org/post)'])

    def test_a_single_quoted_target_is_not_left_with_its_quote(self):
        """**单引号那一档必须自己认。**

        `'` 和尖括号、双引号不一样:它是 URL **合法**的子分隔符(`sub-delims`),
        所以裸地址那一档不能把它当边界排除掉(`…/it's-here` 真的存在)。于是
        `'<地址>'` 这种写法只能靠「引号包起来」这一档认出来 —— 少了它,收上去的是
        一个带着尾引号的坏地址。
        """
        self.run_cli('--no-wait', '-', stdin="见 'https://example.org/post;' 这篇")
        self.assertEqual(self.plan['posted'], ['https://example.org/post;'])

    def test_a_scheme_in_capitals_is_still_a_link(self):
        """`HTTPS://…` 是合法写法(后端 `normalize` 收下并规范化成小写)。两档都要
        认得它 —— 漏掉的后果不是「少收一条」,是**整条输入连一行结果都没有**
        (当时 CLI 直接以 2 退出,说「没给出要收录的链接」)。
        """
        self.run_cli('--no-wait', '-', stdin='HTTPS://example.org/a\n见 HTTP://example.org/b 这篇')
        self.assertEqual(self.plan['posted'],
                         ['HTTPS://example.org/a', 'HTTP://example.org/b'])

    def test_a_markdown_link_hands_over_its_target_not_the_source_text(self):
        r"""**Markdown 那一段整段归它,显示文字落不进裸地址那一档。**

        第 4 轮审查实测的三组在这儿。根因是正则交替只在**同一个起点**上比优先
        级,管不了「后面那个结构分支该赢过前面已经匹配上的裸地址」:
        - 显示文字本身就是地址时,裸地址那一档从它起手、一路吃到
          `…label](https://…/target)`;
        - 目标里括号嵌了两层(`A_(B_(C))`,维基那一类)时结构分支失配,退回裸地址,
          把 Markdown 自己的收尾 `)` 也带走;
        - `\(` 这种 Markdown 转义没还原,收上去的地址里留着反斜杠。

        三组实测都是**9 次**认错(收录 / `--state` / `--refetch` 各三次)——
        同一条解析喂这三个动作,所以边界认错会连带认错「要替换哪一篇」「要查哪一
        篇」。判据落在真发出去的那几个请求上。
        """
        for text, want in (
                ('见 [https://example.org/label](https://example.org/target) 这篇',
                 'https://example.org/target'),
                ('见 [原文](https://example.org/A_(B_(C))) 这篇',
                 'https://example.org/A_(B_(C))'),
                (r'见 [原文](https://example.org/part\(one\)) 这篇',
                 'https://example.org/part(one)')):
            with self.subTest(text=text):
                self.plan['posted'].clear()
                self.plan['asked'].clear()
                self.run_cli('--no-wait', '-', stdin=text)
                self.run_cli('--no-wait', '--refetch', '-', stdin=text)
                self.run_cli('--state', '-', stdin=text)
                self.assertEqual(self.plan['posted'], [want, want])
                self.assertEqual(self.plan['asked'], [want])

    def test_character_entities_in_a_target_are_decoded(self):
        """**目标里的字符实体要按语法解码。**

        第 5 轮审查点名的形态:`[原文](https://…/A_&#40;B&#41;)` 原样交出去就是
        **另一个地址**(`&#40;` 是字面的 `(`)。CommonMark 把实体算进链接目标的语法,
        所以它和反斜杠转义一样,是「按语法取目标」的一部分 —— 而不是改写用户给定
        的边界。收录 / `--state` / `--refetch` 共用这条解析,所以三个动作一起错。
        """
        for text, want in (
                ('见 [原文](https://example.org/A_&#40;B&#41;) 这篇',
                 'https://example.org/A_(B)'),
                ('见 [原文](https://example.org/x&#x28;y&#x29;) 这篇',
                 'https://example.org/x(y)'),
                ('见 [原文](https://example.org/a&amp;b) 这篇',
                 'https://example.org/a&b')):
            with self.subTest(text=text):
                self.plan['posted'].clear()
                self.plan['asked'].clear()
                self.run_cli('--no-wait', '-', stdin=text)
                self.run_cli('--state', '-', stdin=text)
                self.assertEqual(self.plan['posted'], [want])
                self.assertEqual(self.plan['asked'], [want])

    def test_the_protocol_is_checked_after_the_target_is_decoded(self):
        """**筛协议是最后一道,排在按语法解码之后。**

        `[原文](https&#58;//example.org/post)` 的协议名里那个冒号本身就是实体 ——
        先筛后解的话这一段根本不像 http 地址,于是整条输入一条结果都没有(第 5 轮
        审查实测:没发请求、没有逐条 JSON、退出 2 说「没给出要收录的链接」)。
        这一条钉的是**那个顺序**,别的几条实体用例都钉不住它。
        """
        code, out, err = self.run_cli('--no-wait', '-',
                                      stdin='见 [原文](https&#58;//example.org/post) 这篇')
        self.assertEqual(code, 0, (out, err))
        self.assertEqual(self.plan['posted'], ['https://example.org/post'])

    def test_an_escaped_ampersand_is_not_an_entity(self):
        r"""**反过来钉一边:`\&#40;` 里那个 `&` 是字面量,不许再当实体解一遍。**

        两种解码在同一遍扫描里完成,顺序就定在那儿:反斜杠先把 `&` 吃掉,剩下的
        `#40;` 只是普通文字。先解实体再解转义的话,用户明明写的是 `&#40;` 五个
        字符,收上去的却变成了一个 `(` —— 又是一个「可能照样存在」的别的地址。
        """
        self.run_cli('--no-wait', '-',
                     stdin=r'见 [原文](https://example.org/p\&#40;q) 这篇')
        self.assertEqual(self.plan['posted'], ['https://example.org/p&#40;q'])

    def test_a_code_span_in_the_label_is_not_link_syntax(self):
        """**显示文字里的代码跨度不是语法。**

        ``[看 `](x)` 这个](目标)`` 里那对 `](` 夹在反引号中间,是字面文字。把它
        当成链接的收尾会让整段结构认不出来,于是退回裸地址 —— 一条本来好端端的
        链接要么被说成没有链接,要么被拼成另一个地址(第 5 轮审查点名的另一半)。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 [看 `](x)` 这个](https://example.org/real) 吧\n'
                           '见 [看 `]` 原文](https://example.org/one) 这篇\n'
                           '见 [``a`b``](https://example.org/dbl) 吧')
        # 第二行是第 5 轮审查实测那一组:当时发出的是 `…/one)`,**多带一个右括号**
        # —— label 在反引号里那个 `]` 处提前收尾,结构失配之后裸地址把 Markdown
        # 自己的收尾也吃了进去。
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/real', 'https://example.org/one',
                          'https://example.org/dbl'])

    def test_a_backtick_inside_one_link_does_not_move_the_next_ones_edge(self):
        r"""**一条链接的目标只由它自己那一段语法决定,邻近的文字说不上话。**

        ``见 [一](… "反引号 ` 的用法") 和 [二](…) 后文 `尾``:前一条的 title 里有个
        字面反引号,后文还有一个。**先独立判一遍「哪儿是代码跨度」的话**,那两个
        反引号会被配成一对,中间那条完整链接整个被跳过、退回裸地址 —— 发出去的是
        `…/target)`、**多带一个右括号**(第 2 轮审查实测;`markdown-it-py` 按
        CommonMark 渲染出来的是两条正常链接)。目标里带反引号是同源形态,一起钉。

        判据是**三条动作真发出去的那几个地址**:收录、`--state`、`--refetch` 走同一条
        解析,边界认错会连带认错「要查哪一篇」「要替换哪一篇」。
        """
        line = ('见 [一](https://example.org/one "反引号 ` 的用法") 和 '
                '[二](https://example.org/target) 后文 `尾')
        want = ['https://example.org/one', 'https://example.org/target']
        self.run_cli('--no-wait', '-', stdin=line)
        self.assertEqual(self.plan['posted'], want)
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '--refetch', '-', stdin=line)
        self.assertEqual(self.plan['posted'], want)
        self.run_cli('--state', '-', stdin=line)
        self.assertEqual(self.plan['asked'], want)
        # 目标里那个反引号同理(它也不是代码跨度起点)。
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '-',
                     stdin='见 [一](https://example.org/a`b) 和 '
                           '[二](https://example.org/target) 后文 `尾')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/a`b', 'https://example.org/target'])

    def test_a_backslash_before_a_plain_letter_stays_literal(self):
        r"""**只有 ASCII 标点转得义**(CommonMark 的「Backslash escapes」)。

        `\h` 里那个反斜杠是字面量 —— 认结构那一遍碰上反斜杠一律跳两格的话,
        `\https://…` 的协议名会被啃掉一个字母,整条地址**一行结果都没有**(漏报是
        静默丢数据)。**反过来钉一边**:`\[` 那个方括号确实不参与结构,所以那一段
        `(…)` 按行文处理、连右括号一起收下。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 \\https://example.org/esc 这条\n'
                           '见 \\[不是链接\\](https://example.org/x) 这条')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/esc', 'https://example.org/x)'])

    def test_an_escape_that_splits_a_backtick_run_is_not_a_crash(self):
        r"""**代码跨度的起点宽度要就地数,不借那份「串怎么切」的表。**

        `` \`` `` 里第一个反引号已经被转义吃掉了,剩下的那个才是起点(宽度 1,不是
        那份表记的 2)。借表的那一版在这种输入上直接 `KeyError` —— 一行这样的文字
        会让整条命令崩掉,而且**同一批后面几条压根没被处理**(同断线那一类的判据:
        一条输入一行结果,失败路径上也成立)。

        **收尾那一侧反过来**:`` `foo\` `` 的收尾就是那个被反斜杠挡着的反引号
        (CommonMark 的「Backslash escapes do not work in code spans」)—— 所以那份
        表**不**跳转义,起点才就地数。让表也跳转义就会把这一条反过来弄错。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 \\`` 这条 https://example.org/one\n'
                           '见 \\``a` 和 https://example.org/two\n'
                           # 第三行钉**收尾**那一侧:那段代码跨度的收尾正是被反斜杠
                           # 挡着的反引号,所以它盖掉了里面的 `](y)` —— 成立的是
                           # `…/three` 这一条。表也跳转义的话收尾就找不着了,那个
                           # `](y)` 于是变成一段链接结构,最后发出去的是 `…/three)`。
                           '[看 `](y)\\` 这个](https://example.org/three)')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/one', 'https://example.org/two',
                          'https://example.org/three'])

    def test_a_link_may_not_contain_another_link(self):
        """**链接里不许再有链接**(CommonMark 的「Links may not contain other links」)。

        `[outer [inner](A)](B)` 成立的是 inner 那一条,包着它的那个 `[` 随之作废。
        不作废的话选出来的是 B —— 那是**另一篇文章**,不是「多带了一个字符」那种错
        (第 2 轮审查实测:三条动作都只处理了 B)。

        外层那段 `](B)` 失配之后按行文处理,于是 `(B)` 里那条裸地址连右括号一起收下
        (`一个括号都不切`,见模块说明第②条)—— 它多半抓不到、报一行 `failed`,
        用户看得见;**漏掉 inner 才是静默收错一篇**。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 [outer [inner](https://example.org/inner)]'
                           '(https://example.org/outer) 这篇')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/inner', 'https://example.org/outer)'])

    def test_many_unclosed_backticks_do_not_blow_up_the_parse(self):
        """**反引号那一串的配对也不许随输入长度平方级变慢。**

        一长串未闭合的反引号,如果每个字符处都重新往后找等长收尾,就会反复扫同一段
        余文(第 2 轮审查实测 1000/2000/4000 个:0.011/0.037/0.112 秒,长度翻两倍耗时
        约十倍)。出路同方括号那一类:**一遍扫出所有串、按宽度分组,配对查表**。
        """
        import time
        # **两种形态各钉一件事**:①一长串未闭合的 —— 它整个是**一个**串,判据是
        # 「找不到收尾时跳过整串」(旧版在串里每个字符处都重新往后找一遍等长收尾,
        # 那才是平方级的来源);②很多个短串 —— 这一类串的数量才多,判据是「配对
        # 查表」(逐个往后找等长的那种写法在这儿是平方级)。少了②,把查表换成逐起点
        # 重扫也全绿(①只调用一次配对,多慢都看不出来)。
        for blob in (('`' * 20000) + ' 普通文字 https://example.org/tail',
                     ('`x' * 20000) + ' 普通文字 https://example.org/tail'):
            with self.subTest(head=blob[:6]):
                start = time.monotonic()
                found = cli.links_in(blob)
                spent = time.monotonic() - start
                self.assertEqual(found, ['https://example.org/tail'])
                self.assertLess(spent, 1.0, f'解析花了 {spent:.2f} 秒')

    def test_an_unpaired_bracket_elsewhere_does_not_spoil_a_real_link(self):
        """**一个 `[` 配不平,否定不了后面那条配得平的链接。**

        `备注里有个 [，见 [原文](目标) 这篇`:外层那个 `[` 到末尾都配不平(`]` 被
        内层用掉了),而内层那条链接语法完整。按「前面有个 `[` 没配平,后面的也一样
        配不平」剪枝的话这条链接整个被跳过、退回裸地址 —— 发出去的是 `…/target)`、
        **多带一个右括号**(第 6 轮审查实测;收录、`--state`、`--refetch` 三个动作
        一起认错对象)。**每条链接的目标只由它自己那一段语法决定。**
        """
        self.run_cli('--no-wait', '-',
                     stdin='备注里有个 [，见 [原文](https://example.org/target) 这篇\n'
                           '[A [B] C 见 [原文](https://example.org/second) 末')
        self.assertEqual(self.plan['posted'], ['https://example.org/target',
                                               'https://example.org/second'])

    def test_balanced_brackets_that_are_not_links_stay_linear(self):
        """**反过来钉一边:方括号全配平、后面却不是链接,耗时一样不许平方涨。**

        `[[[[…文字…]]]]` 里每个 `[` 都配得平,要到 `(` 那一步才失配 —— 下面那条
        未闭合的用例判不住这一类(未闭合的压根配不出对),而「每个起点各自往后找
        配对」在这儿会重扫同一段余文(第 6 轮审查实测 1000/2000/4000 对括号约
        0.033/0.126/0.508 秒,长度翻倍耗时约四倍)。判据同样落在时间上。
        """
        import time
        blob = ('[' * 20000) + '普通文字' + (']' * 20000) + ' https://example.org/tail'
        start = time.monotonic()
        found = cli.links_in(blob)
        spent = time.monotonic() - start
        self.assertEqual(found, ['https://example.org/tail'])
        self.assertLess(spent, 1.0, f'解析花了 {spent:.2f} 秒')

    def test_many_unclosed_brackets_do_not_blow_up_the_parse(self):
        """**一段带很多未闭合 `[` 的文字,耗时不许随长度平方涨。**

        每个 `[` 都从自己那儿扫到末尾找配对的话,粘一段这样的文字进来就卡住了。
        判据落在**时间**上:两万个未闭合方括号要在一秒内解析完,而且末尾那条地址
        照旧认得出来(别为了快把它漏掉)。
        """
        import time
        blob = ('[' * 20000) + ' https://example.org/tail'
        start = time.monotonic()
        found = cli.links_in(blob)
        spent = time.monotonic() - start
        self.assertEqual(found, ['https://example.org/tail'])
        self.assertLess(spent, 1.0, f'解析花了 {spent:.2f} 秒')

    def test_markdown_around_a_target_is_consumed_whole(self):
        """Markdown 那一段的几种写法:**目标取出来,别的字节一个都不带上。**

        `[文字](<目标>)`、带 title 的、显示文字里嵌一对方括号的 —— 这几种都是
        CommonMark 的合法写法,都会被人从别处原样粘进来。
        """
        for text, want in (
                ('见 [原文](https://example.org/t "标题") 这篇', 'https://example.org/t'),
                ('见 [看 [这篇]](https://example.org/nested) 吧',
                 'https://example.org/nested'),
                ("见 [原文](https://example.org/q) 和 '别的'", 'https://example.org/q')):
            with self.subTest(text=text):
                self.plan['posted'].clear()
                self.run_cli('--no-wait', '-', stdin=text)
                self.assertEqual(self.plan['posted'], [want])

    def test_a_target_in_angle_brackets_keeps_what_is_inside_it(self):
        """`[文字](<目标>)` 这一档必须自己认,**后面那个可选 title 也必须跳过**。

        不认它的后果不是「少收一条」,是**收到一个更短的地址**:裸地址那一档会兜底,
        但它按空白断 —— 于是 `<…/a b>` 只剩 `…/a`、`<…/c d> "标题"` 只剩 `…/c`,
        而那个更短的地址**可能照样存在**(静默收错一篇)。`<…>` 这种写法本来就是
        CommonMark 给「目标里有空白」准备的,所以这两件事只有在这个形态上才分得开
        (目标不带空白时裸地址兜底的边界恰好一样 —— 定点注入量过)。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 [原文](<https://example.org/a b>) 这篇\n'
                           '见 [原文](<https://example.org/c d> "标题") 这篇')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/a b', 'https://example.org/c d'])

    def test_a_link_to_somewhere_else_is_not_mined_for_its_label(self):
        """**结构认出来了就由它说了算,哪怕目标我们收不了。**

        `[看 https://…/real 很好](#锚点)`:这条链接指向的是锚点,显示文字里那条
        地址是**文字**。从显示文字里把它挖出来收录,收进来的是**另一篇**(用户要
        的是锚点那一篇)—— 那正是「结构不支持就退回裸地址拼一条出来」的后果。
        所以这一段整段消费、一条都不产出。

        **反过来钉一边**:`[备注] 看 https://…` 里那个方括号**压根不是链接结构**
        (后面没有 `(`),那时不消费,裸地址照常认出来 —— 不然一行好端端的文字会
        因为开头有个方括号就整行丢掉。
        """
        self.run_cli('--no-wait', '-',
                     stdin='[看 https://example.org/real 很好](#note)\n'
                           '[文档](/relative/path) 另见 https://example.org/also\n'
                           '[备注] 看 https://example.org/after 这个')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/also', 'https://example.org/after'])

    def test_several_links_on_one_line_keep_their_own_boundaries(self):
        """一行里几种写法混着:**各认各的边界,顺序不变、重复的去掉。**"""
        found = cli.links_in(
            '[一](https://example.org/md) 和 <https://example.org/angle> 还有 '
            'https://example.org/bare 以及 [一](https://example.org/md) 又一次')
        self.assertEqual(found, ['https://example.org/md',
                                 'https://example.org/angle',
                                 'https://example.org/bare'])

    def test_links_are_picked_out_of_pasted_text_and_deduped(self):
        found = cli.links_in(f'看这篇 {URL} 挺好，还有 <{OTHER}> 以及 {URL} 都值得读')
        self.assertEqual(found, [URL, OTHER])

    def test_prose_marks_never_turn_one_address_into_another(self):
        """**裸地址那一档一个字符都不剥。**

        它的边界本来就不确定,而这一层的不变量是「宁可留下一个抓不到的坏地址,
        也不截短成另一个**可能存在**的目标」—— 后者是静默收错一篇文章,前者在
        终端上就是一行 `failed`,用户看得见、改写了再来。所以这里断的是**两件事**:
        该完整取出的完整取出;边界含糊的那几条**绝不等于那个更短的地址**。
        """
        for text, want in (
                # 空白就是边界:这几条本来就完整
                (f'见 {PAREN} 这篇', [PAREN]),
                (f'见 {FULLWIDTH} 这篇', [FULLWIDTH]),
                (f'见 {URL} 和 {OTHER} 两篇', [URL, OTHER]),
                # ASCII 的 `!` `?` 是 URL 合法字符,不许当句末剥掉
                ('看 https://example.org/post! 这条', ['https://example.org/post!']),
                ('see https://example.org/x?', ['https://example.org/x?'])):
            with self.subTest(text=text):
                self.assertEqual(cli.links_in(text), want)
        # 紧贴着中文标点的那几条:**不许变成那个更短的、真的存在的地址**。
        for text in (f'（见 {URL}）以及别的', f'见 {URL}。后面还有话'):
            with self.subTest(text=text):
                got = cli.links_in(text)
                self.assertNotIn(URL, got, '把地址截短成了另一个可能存在的目标')
                self.assertTrue(got and got[0].startswith(URL), got)


if __name__ == '__main__':
    unittest.main()
