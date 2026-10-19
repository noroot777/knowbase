# -*- coding: utf-8 -*-
"""命令行那条入口的输入层:**判据是时间和预算**的那几条。

从 `tests/test_grab_cli_input.py` 拆出来的(单文件要过 700 行的上限)。分家的判据
是**拿什么作证**:那一份断言「发出去的是哪个地址」,这一份断言「认一段损坏或者
很长的输入要花多久、以及认不全时从哪个出口走」。两边都不许省 —— 保真那一类的
红在地址上,这一类的红在挂住整条命令上,而**「扫到一半放弃、把剩下的按裸地址拼
一条」正是保真那一类要堵的事**,所以预算超了要抛 `TangledInput`(退出 2、一句
人话、一个请求都不发),不许报「这一行没有链接」。

四类「答不上来就得扫到行尾」的输入(`[](`、`](a (`、`<?`、`<!--`)、反引号串的
两种规模、方括号配平与不配平、图片开括号撑着的那个栈、以及整批去重 —— 每一条
各判一件事,一条都不冗余(理由写在各自的 docstring 里)。
"""
from __future__ import annotations

import unittest

from tests.grab_cli_fixture import GrabCliCase, cli


class GrabCliBudgetTests(GrabCliCase):
    """认一段损坏或者很长的输入:**不许平方级变慢,也不许安静地少认**。"""

    def test_many_unclosed_backticks_do_not_blow_up_the_parse(self):
        """**反引号那一串的配对也不许随输入长度平方级变慢。**

        一长串未闭合的反引号,如果每个字符处都重新往后找等长收尾,就会反复扫同一段
        余文(实测 1000/2000/4000 个:0.011/0.037/0.112 秒,长度翻两倍耗时
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

    def test_input_we_cannot_parse_in_full_is_an_error_not_an_empty_answer(self):
        """**认不全的那一行要说「我认不全」,不许说「这一行没有链接」。**

        `](目标)` 的括号一路不配平、title 没有收尾引号、`<?` 找不到 `?>` ——
        这几种都得扫到行尾才知道答不上来,一行里塞上千个,同一段余文就被反复扫
        (实测 500/1000/2000 个 `[](`:0.019/0.065/0.258 秒,长度翻倍耗时四倍)。

        **出路不是「扫到一半放弃、把剩下的按裸地址拼一条」** —— 那正是保真那一类
        要堵的事。所以探测有一份预算(`_Budget`),超了抛 `TangledInput`:
        **退出 2、一句人话**(同打错 `--api` 那一档),一个请求都不发。「没有链接」
        是「确定没有」这个结论,而我们只知道没认全。

        判据两件:**时间**(四类损坏输入都要在一秒内收口,一类换一个判据)和
        **出口**(退出 2、stdout 一个字都没有、没发请求)。
        """
        import time
        for pat in ('[](', '[](a (', '<?', '<!--'):
            with self.subTest(pat):
                blob = pat * 4000 + ' 看 https://example.org/tail'
                start = time.monotonic()
                code, out, err = self.run_cli('--no-wait', '-', stdin=blob)
                spent = time.monotonic() - start
                self.assertLess(spent, 1.0, f'解析花了 {spent:.2f} 秒')
                self.assertEqual(code, 2)
                self.assertEqual(out, '')
                self.assertIn('认不全', err)
                self.assertEqual(self.plan['posted'], [])

    def test_many_distinct_addresses_are_deduped_without_comparing_pairs(self):
        """**按出现顺序去重,但不许两两比较。**

        `not in 列表` 那一版每条都跟前面所有条比一遍 —— 一次粘进来上万条互不相同
        的地址时是平方级。出路是**集合判在不在、列表保顺序**,而且这个判据只有
        一份(`grab_input.dedupe`):行内抽取和整批参数两个调用点共用它,所以这一条
        用例同时钉住两处。

        **反过来钉一边**:顺序和去重的结果一个都不许变 —— 收录是按这个顺序一条条
        投出去的,而重复的那几条只该投一次。

        规模要够:两万条时列表那一版 0.62 秒(还在阈值内),四万条 2.62 秒 ——
        长度翻倍耗时四倍。少给一半,把集合换回列表一条都不红。
        """
        import time
        many = [f'https://example.org/p/{k}' for k in range(40000)]
        blob = '看这些 ' + ' '.join(many + many[:3]) + ' 都值得读'
        start = time.monotonic()
        found = cli.links_in(blob)
        spent = time.monotonic() - start
        self.assertLess(spent, 1.0, f'解析花了 {spent:.2f} 秒')
        self.assertEqual(found, many, '顺序和去重的结果都不许变')

    def test_balanced_brackets_that_are_not_links_stay_linear(self):
        """**反过来钉一边:方括号全配平、后面却不是链接,耗时一样不许平方涨。**

        `[[[[…文字…]]]]` 里每个 `[` 都配得平,要到 `(` 那一步才失配 —— 下面那条
        未闭合的用例判不住这一类(未闭合的压根配不出对),而「每个起点各自往后找
        配对」在这儿会重扫同一段余文(实测 1000/2000/4000 对括号约
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

    def test_unclosed_image_brackets_do_not_make_the_stack_quadratic(self):
        """**「链接里不许再有链接」那一步不许每次都过一遍整个栈。**

        图片的开括号**不被作废**(它不占那个名额),所以 `'![' * n + '[x](#a)' * n`
        这种输入下栈长一直是 n,每成立一条链接过滤一次就是平方级(实测长度翻倍
        耗时约 3.5 倍;报告那一版 144 KB 要 2.0 秒)。**而这笔工作算不进探测预算**
        —— 预算计的是「为答一问扫了多远」,栈的失效处理不是扫描,所以只能让它本身
        是线性的(世代号惰性作废:每一项最多被丢一次)。

        判据同另外两条性能用例:**时间**,外加「末尾那条地址照旧认得出来」
        (别为了快把它漏掉)。
        """
        import time
        blob = '![' * 20000 + '[x](#a)' * 20000 + ' https://example.org/tail'
        start = time.monotonic()
        found = cli.links_in(blob)
        spent = time.monotonic() - start
        self.assertEqual(found, ['https://example.org/tail'])
        self.assertLess(spent, 1.0, f'解析花了 {spent:.2f} 秒')


if __name__ == '__main__':
    unittest.main()
