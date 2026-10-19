# -*- coding: utf-8 -*-
"""命令行那条入口的**输入解析层**:哪一档原样交出去、哪一档才抽取。

**判据落在「真发出去的那几个请求」上**,不是 `links_in` 的返回值 —— 收录、
`--state`、`--refetch` 走同一条解析,所以认错边界会让三种动作一起认错那一篇。

这一层唯一的不变量:**宁可留下一个抓不到的坏地址,也不截短成另一个可能存在的
目标**。后者是静默收错一篇文章(而 `put_items` 的 `DO NOTHING` 之下换不回来),
前者在终端上就是一行 `failed`,用户看得见、改写了再来。

夹具(假 kb-web)在 `tests/grab_cli_fixture.py`,收敛那一组在
`tests/test_grab_cli.py`;**判据是时间和预算**的那几条在
`tests/test_grab_cli_budget.py`(按「拿什么作证」分家:这一份断言发出去的是哪个
地址,那一份断言认一段损坏或者很长的输入要花多久、从哪个出口走)。
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

        `-` 既用来粘一段话、也用来喂「一行一条」的清单,而实测的四组
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

        实测的五组就在这儿:原先「整行才保真」,于是圈得明明白白的
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

        实测的三组在这儿。根因是正则交替只在**同一个起点**上比优先
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

        实测过的形态:`[原文](https://…/A_&#40;B&#41;)` 原样交出去就是
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
        先筛后解的话这一段根本不像 http 地址,于是整条输入一条结果都没有(实测实测:没发请求、没有逐条 JSON、退出 2 说「没给出要收录的链接」)。
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
        链接要么被说成没有链接,要么被拼成另一个地址(同一类的另一半)。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 [看 `](x)` 这个](https://example.org/real) 吧\n'
                           '见 [看 `]` 原文](https://example.org/one) 这篇\n'
                           '见 [``a`b``](https://example.org/dbl) 吧')
        # 第二行是实测那一组:当时发出的是 `…/one)`,**多带一个右括号**
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
        `…/target)`、**多带一个右括号**(实测;`markdown-it-py` 按
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

        **三段之间空一行**:代码跨度在 CommonMark 下是可以跨行的(同一段里,
        第一段那个落单的反引号会跟第二段的配成一对、把中间那条地址盖进代码里
        —— `markdown-it-py` 也是这个结果)。这一条要钉的是「`` \`` `` 不让解析
        崩掉」,所以让三段各自成段;**空行断段**那件事本身在
        `test_a_structure_that_spans_lines_is_not_cut_in_half` 里钉。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 \\`` 这条 https://example.org/one\n\n'
                           '见 \\``a` 和 https://example.org/two\n\n'
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
        (实测过:三条动作都只处理了 B)。

        **外层剩下的那段 `](B)` 一个字都不许产出。** 它在 CommonMark 下是字面文字,
        但「字面文字」不等于「该从里面抽一条裸地址」:那一段的 `)` 是 Markdown 的
        收尾,不是地址的一部分。交给第二趟的那一版发出的是 `B)` —— 多一个右括号
        就是**另一篇文章**,而实测它能通过后端的规范化、真的落进库(不能靠
        「它多半抓不到」兜着)。所以认成结构就整段消费、不产出。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 [outer [inner](https://example.org/inner)]'
                           '(https://example.org/outer) 这篇')
        self.assertEqual(self.plan['posted'], ['https://example.org/inner'])

    def test_every_url_we_send_is_what_its_own_structure_says(self):
        """**发出去的每个地址,都得是它那一段结构真正表达的目标。**

        这是「地址保真」那一类的**统一判据**,不是某几种字符组合的排除项:第一趟
        认不出、或者认出来但不该成立为链接的那一段,**不许退回去按裸地址拼一条
        出来**。拼出来的多一个右括号就是**另一篇文章**,而实测它能通过后端的
        `grab.normalize`、真的落进库 —— 不能靠「它多半抓不到」兜着。

        下面每一组右边那几条,是 `markdown-it-py` 的 commonmark 模式渲染出来的
        http(s) 链接(逐条对过)。栽过的形态都在里头,各是一种**别处的语法**:
        行内 HTML 的属性值、注释、`<?…?>`、CDATA、declaration、代码跨度、
        嵌套作废掉的外层残片、筛不过协议的目标。

        **三处刻意和 CommonMark 不一样**(都在「多收一条」那一侧,不是保真那一侧):
        ①`<p>`、`<!--` 开头的那几种在 CommonMark 里是 HTML **块**,整块不做行内
        解析;这一层只做行内 —— 用户粘一段带标签的文字进来,里面那条链接正是他要
        的。②标签的**属性值**里那条地址照旧收(`href="https://…"`),CommonMark 不
        把它当链接。③裸写在行文里的照旧收。这三条的代价都是「多抓一篇」,
        而保真那一侧的代价是「收错一篇且换不回来」。
        """
        NO, YES = 'https://example.org/no', 'https://example.org/yes'
        cases = (
            ('行内 HTML 的属性值里有个 `]`',
             f'见 [看 <span title="]">标签</span>]({YES}) 这篇', [YES]),
            ('属性值里是一整段链接残片',
             f'<a href="{YES}" title="]({NO})">文字</a>', [YES]),
            ('嵌套三层,成立的是最里面那条',
             f'[a [b [c]({YES})]({NO}/b)]({NO}/a) 末', [YES]),
            ('注释', f'<!-- [注释里的]({NO}) --> [真的]({YES})', [YES]),
            ('处理指令', f'<?php echo "]({NO})"; ?> [真的]({YES})', [YES]),
            ('CDATA', f'<![CDATA[ [x]({NO}) ]]> [真的]({YES})', [YES]),
            ('declaration', f'<!DOCTYPE html> [真的]({YES})', [YES]),
            ('代码跨度里的链接是字面代码',
             f'`[不是]({NO})` [真的]({YES})', [YES]),
            ('筛不过协议的目标,那一段照样整段消费',
             f'[文字](ftp://a/no) [真的]({YES})', [YES]),
            ('认不成链接的那一段,`[` 当文字、裸地址照旧认',
             f'[备注] 看 {YES} 这条', [YES]),
            ('标签之后回到普通正文', f'<span>标签</span> 然后 {YES} 这条', [YES]),
            ('没收尾的标签是字面文字',
             f'未闭合 <span title="x" 然后 [一篇]({YES})', [YES]),
            ('没收尾的注释同理', f'<!-- 没收尾 [一篇]({YES})', [YES]),
            # **图片不是链接**:它表达的是「这儿嵌一张图」。外层那条文章链接
            # 照旧成立 —— 混在一起算的那一版只产出那张图、把文章静默漏掉。
            ('图片套在链接里,成立的是外层那条文章链接',
             f'[![封面]({NO}/image.jpg)]({YES})', [YES]),
            ('单独一张图,一条都不产出', f'![封面]({NO}/image.jpg) 这张', []),
            # **引用式链接**:我们不解引用定义,但它是一段结构 —— 显示文字和
            # `][标签]` 残片不许被拼成一个地址。
            ('引用式链接的显示文字不许被拼成地址',
             f'见 [{NO}/label][ref] 这篇', []),
            # **定义行里那个目标是用户写的真实地址**:引用式链接自己说不出要收
            # 哪一篇,真正的地址正写在这儿,两件事合起来才完整。而它是**块级**
            # 结构 —— 不认它的那一版把这一行交给第二趟,标签本身像地址时就多发
            # 一条 `…/label]:`(实测过得了后端规范化、真落库真导出真索引)。
            ('引用定义那一行给出它的目标', f'[ref]: {YES}', [YES]),
            ('定义行 + 引用式链接合起来只有真目标那一条',
             f'看 [原文][1] 这篇\n\n[1]: {YES}', [YES]),
            ('标签本身像地址时也不许多发一条残片',
             f'[原文][{NO}/label]\n\n[{NO}/label]: {YES}', [YES]),
            ('定义行的 title 里那条地址是标题文字,不产出',
             f'[1]: {YES} "见 {NO}/in-title"', [YES]),
            # **反过来钉一边:定义是块级的,段里有过正文行之后就不是定义了**
            # (CommonMark 的口径)。判得太宽会把行文里那一行整段消费掉,而那一行
            # 上可能正写着用户要收的地址 —— 这里那条 title 里的地址就是判据:
            # 按定义认它不产出,按文字认(正确)它是行文里的一条裸地址。
            ('段里有过正文行之后,`[x]: y` 只是文字',
             f'见下文\n[1]: {YES} "见 {NO}/in-title"',
             [YES, f'{NO}/in-title']),      # 裸地址那一档排除 `"`(URI 里必须编码)
            # **实体得是真的存在的那几个。** 形状对就解的那一版把
            # `&notanentity;` 按 `&not` 前缀解成 `¬anentity;`(`html.unescape`
            # 是「一般文本」那一份),发出去的是另一个地址、真落库;CommonMark 对
            # 认不出的实体是**原样保留**。
            ('认不出的命名实体原样保留,不许按前缀解一半',
             f'见 [原文]({YES}/?q=&notanentity;) 这篇', [f'{YES}/?q=&notanentity;']),
            ('真的存在的那些照旧解', f'见 [原文]({YES}/?q=&amp;x=1) 这篇',
             [f'{YES}/?q=&x=1']),
            # **简写引用**(`[标签]`):有定义它就是一条链接,目标在定义行里。
            # 不认它的那一版退回裸地址,多发一条 `…/label]` —— 实测它和真目标两条
            # 都真写入真导出真索引,命令对两条都报 `saved`。
            ('简写引用 + 定义:只收定义里那个目标',
             f'[{NO}/label]\n\n[{NO}/label]: {YES}', [YES]),
            ('引用块里的定义一样算(block quote 的内容照旧是块级序列)',
             f'> [{NO}/label]: {YES}\n\n看 [{NO}/label] 这篇', [YES]),
            ('反过来钉一边:标签没有定义时 `[x]` 只是文字,裸地址照旧认',
             f'[备注] 看 {YES} 这条', [YES]),
            # **属性值按 HTML 自己那一套解码**(不是 Markdown 那一份的宽松版):
            # 数字引用缺分号照样算,命名引用缺分号反而有个历史特例。
            ('HTML 属性值里的字符实体要解开',
             f'见 <a href="{YES}?a=1&amp;b=2">原文</a> 这篇', [f'{YES}?a=1&b=2']),
            ('属性值里缺分号的数字引用也要解开',
             f'见 <a href="{YES}/A&#47B">原文</a> 这篇', [f'{YES}/A/B']),
            ('缺分号的命名引用后面紧跟字母时不解码(HTML5 的属性值特例)',
             f'见 <a href="{YES}/?a&ampb=1">原文</a> 这篇', [f'{YES}/?a&ampb=1']),
            ('属性值不是一条干净地址时一条都不产出',
             f'<img src="{YES}" srcset="{NO}/2x.jpg 2x">', [YES]),
        )
        for why, line, want in cases:
            with self.subTest(why):
                self.plan['posted'].clear()
                self.plan['asked'].clear()
                # **三条动作走同一条解析**,所以边界认错会连带认错「要查哪一篇」。
                self.run_cli('--no-wait', '-', stdin=line)
                self.assertEqual(self.plan['posted'], want)
                self.run_cli('--state', '-', stdin=line)
                self.assertEqual(self.plan['asked'], want)

    def test_a_structure_that_spans_lines_is_not_cut_in_half(self):
        """**粘进来的是一段文字,不是一行一行各看一遍。**

        Markdown 的链接允许在 `(` 之后换行(CommonMark 的 link destination 前后
        可以有空白)。**先 `splitlines` 再逐行判**的那一版把它切成两截:第一截是
        半个结构、第二截里那条裸地址连 Markdown 的收尾 `)` 一起收下 —— 实测
        **真库落成带右括号的那个地址、真导出真索引、退出 0、`status=saved`**,
        而原目标压根不在库里。

        **「整行就是一条地址」那一档只在「它前面没有正在攒的段」时才成立。**
        判据只看这一行自己的那一版照旧会切断结构 —— 因为 `(` 之后换行的那个目标
        **本身**往往整个就是一条地址(右括号是 URL 的合法字符),于是又被原样
        拿走、带着 Markdown 的收尾落进库。所以这里钉**两种**多行链接:行尾还有
        叙述的、以及行尾什么都没有的。目标独占一行时**解码照旧要走**,
        多行注释里那一行独立地址**一条都不许产出**(完整结构扫描本来就不产出,
        那是凭空多一次收录)。

        **反过来钉一边**:一行一条的清单照旧逐行原样交(段始终是空的),
        **独立地址行里的方括号不许被当成 Markdown 语法**(`…/a[b](c)` 整条原样
        —— 牺牲这一档的保真去修多行是另一个方向的错),**空行断段**
        (CommonMark 的口径:空行终止 paragraph,行内结构不跨段)。
        """
        self.run_cli('--no-wait', '-',
                     stdin='见 [原文](\n'
                           'https://example.org/multiline) 这篇')
        self.assertEqual(self.plan['posted'], ['https://example.org/multiline'])
        # 行尾什么都没有的那一种:第二行整个就是一条地址(`)` 是 URL 合法字符),
        # 所以「整行」那一档会去抢它 —— 实测带 `)` 的那个地址真落库、真导出真索引。
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '-',
                     stdin='见 [原文](\nhttps://example.org/tail)')
        self.assertEqual(self.plan['posted'], ['https://example.org/tail'])
        # 目标独占一行:解码那一步不许被绕过。
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '-',
                     stdin='见 [原文](\nhttps://example.org/A&#40;B&#41;\n) 这篇')
        self.assertEqual(self.plan['posted'], ['https://example.org/A(B)'])
        # 多行注释里的独立地址行:整段消费,一条都不产出。
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '-',
                     stdin='见下文 <!-- 注释开始\n'
                           'https://example.org/in-comment\n--> 完')
        self.assertEqual(self.plan['posted'], [])
        # 清单那一档:每行各是一条,顺序照原文。
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '-',
                     stdin='https://example.org/a\nhttps://example.org/b\n'
                           '看这篇 https://example.org/c 挺好')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/a', 'https://example.org/b',
                          'https://example.org/c'])
        # 空行断段:第二段那个 `)` 不属于第一段那个没写完的链接。
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '-',
                     stdin='见 [原文](\n\nhttps://example.org/split) 这篇')
        self.assertEqual(self.plan['posted'], ['https://example.org/split)'],
                         '断段之后那一段里的裸地址按空白断、一个标点都不剥')
        # **反过来钉一边**:真正独立的那一行**原样**交出去 —— URL 里的 `[` `]`
        # 是合法字符,按 Markdown 语法去切会把它截成一个更短的地址,而那个地址
        # 可能真的存在(收错一篇,`DO NOTHING` 之下换不回来)。
        self.plan['posted'].clear()
        self.run_cli('--no-wait', '-',
                     stdin='https://example.org/wiki/a[b](c)\n'
                           '\nhttps://example.org/wiki/d[e](f)')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/wiki/a[b](c)',
                          'https://example.org/wiki/d[e](f)'])

    def test_an_autolink_inside_a_label_does_not_void_the_outer_link(self):
        """**反过来钉一边:`<地址>` 不作废包着它的那条链接。**

        「链接里不许再有链接」管的是内联/引用链接的嵌套,autolink 在 bracket
        matching 里不参与 —— `[<A>](B)` 两条都成立(markdown-it 的 commonmark 模式
        也给两条)。把它当成「里面有链接了,外层作废」的那一版**漏掉 B 那一篇**,
        而漏是静默的:终端上那一行看不出少了什么。

        **反引号圈起来的那一档同样要收**:它的边界是用户自己画的(同 `<…>`、
        引号),整段就是一条地址时不许因为「代码跨度不是链接」而丢掉。
        """
        self.run_cli('--no-wait', '-',
                     stdin='[<https://example.org/auto>](https://example.org/outer)\n'
                           '看 `https://example.org/code` 这个')
        self.assertEqual(self.plan['posted'],
                         ['https://example.org/auto', 'https://example.org/outer',
                          'https://example.org/code'])

    def test_an_unpaired_bracket_elsewhere_does_not_spoil_a_real_link(self):
        """**一个 `[` 配不平,否定不了后面那条配得平的链接。**

        `备注里有个 [，见 [原文](目标) 这篇`:外层那个 `[` 到末尾都配不平(`]` 被
        内层用掉了),而内层那条链接语法完整。按「前面有个 `[` 没配平,后面的也一样
        配不平」剪枝的话这条链接整个被跳过、退回裸地址 —— 发出去的是 `…/target)`、
        **多带一个右括号**(实测;收录、`--state`、`--refetch` 三个动作
        一起认错对象)。**每条链接的目标只由它自己那一段语法决定。**
        """
        self.run_cli('--no-wait', '-',
                     stdin='备注里有个 [，见 [原文](https://example.org/target) 这篇\n'
                           '[A [B] C 见 [原文](https://example.org/second) 末')
        self.assertEqual(self.plan['posted'], ['https://example.org/target',
                                               'https://example.org/second'])

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
