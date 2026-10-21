# -*- coding: utf-8 -*-
"""共享正文提取器那道**大小预算**:量的是什么、花多少钱量。

判据全部落在 `readable.markup_size` / `readable.extract` 上,**不经过任何一个
消费方** —— 这一层被手动收录(`grab_page`)和订阅补全文(`fulltext`)两头用着,
把它的用例挂在其中一头底下,另一头换实现时就没人钉着它了。

手动收录那一侧「脚本撑大的页面照旧收得下」在 `tests/test_grab.py`(那一条走的是
真入口,要的是端到端的后果),这里只钉这条线自己的三件事:
①量对了吗(注释上下文)②该拒的照旧拒 ③量这一下自己不许变成新的开销。
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from kb.feeds import grab_page, readable


class MarkupBudgetTests(unittest.TestCase):
    def test_a_tag_inside_a_comment_is_not_a_script(self):
        """**第一轮审查两个洞之一。** 无上下文的正则(`<script…>.*?</script>`)会从
        前一段注释里的 `<script>` 一路吃到后一段注释里的 `</script>`:中间 3.2 MB
        真标记、40 万个 `<p>` 节点,它报 **26 字节** —— 这条线形同虚设,解析前的预算
        可以被一对注释绕过去。注释里的标签按 HTML 规范压根不开原始文本模式。
        """
        html = '<!-- <script> -->' + '<p>x</p>' * 400000 + '<!-- </script> -->'
        self.assertGreater(readable.markup_size(html), readable.MAX_HTML)
        self.assertEqual('', readable.extract(html))     # 后果:照旧拒,不去解析它
        # 反过来钉一边:真的脚本照旧扣得掉,不是「注释一出现就整份算真标记」。
        self.assertLess(readable.markup_size('<script>' + 'a' * 4000000 + '</script>'),
                        readable.MAX_HTML)

    def test_the_budget_counts_real_markup_not_raw_text(self):
        """**反过来钉一边。** 那条线量的是「要建成树的那部分」,不是「扣掉脚本就
        放行」:真标记自己超线的照旧交白卷,而 `<script></script>` 重复几十万次的
        那种页面(内容短、节点多)也不许从底下溜过去。
        """
        self.assertEqual('', readable.extract('<p>x</p>' * 400000))
        self.assertEqual('', readable.extract('<script></script>' * 300000))
        # 而脚本**内容**撑大的那种照旧解析得到(手动收录那条路的端到端在 test_grab)
        padded = '<article>' + '<p>这一段是真正文，长度要过得了那道门槛。</p>' * 40 + '</article>'
        self.assertGreater(len(readable.extract(
            '<script>' + 'a' * readable.MAX_HTML + '</script>' + padded)), readable.MIN_CHARS)

    def test_counting_is_linear_even_without_closing_tags(self):
        """**第一轮审查两个洞之二,而且这一个会把人卡在界面上。**

        那条正则对每个 `<script` 开头都要把剩下整份重找一遍 `</script>`,找不到
        再挪一格重来 —— 实测 `'<script>' * 16000`(128 KB)要 3.7 秒、
        `'<script>' * 64000`(512 KB)42 秒还没回来,而响应体远在网络那道 8 MB
        之下。`GRAB_SECONDS` 只随 HTTP 请求走,**拦不住一次本地正则**:用户看到的
        是「正在抓取」一直转,同进程里别人的请求一起等。

        预算给到 5 秒是因为这里要分的是**三个数量级**(0.0002 秒 vs 42 秒),不是
        毫秒级的紧预算 —— 机器忙的时候慢十倍也还差着三个数量级。
        """
        for tag, count in (('script', 64000), ('style', 64000)):
            with self.subTest(tag=tag):
                html = f'<{tag}>' * count
                started = time.monotonic()
                size = readable.markup_size(html)
                self.assertLess(time.monotonic() - started, 5.0)
                # 没有闭合标签 = 剩下的全是它的内容(html.parser 也是这么收场的:
                # `'<body><script>' + '<p>x</p>' * 200000` 建出 0 个 `<p>`)
                self.assertLess(size, 100)

    def test_a_small_page_is_never_scanned_twice(self):
        """**量这一下自己不许变成新的开销。** 两个消费方的短路顺序要一致:整份没
        超线就压根不去量 —— 第一轮审查里 `gate_reason` 是无条件调用的,于是每一份
        小页面都白扫一趟(而那一版的扫描还是平方级的)。
        """
        small = '<html><body><article><p>' + '正文' * 400 + '</p></article></body></html>'
        self.assertLess(len(small), readable.MAX_HTML)
        with patch.object(readable, 'markup_size',
                          side_effect=AssertionError('小页面不该去量真标记')):
            readable.extract(small)
            grab_page.gate_reason('https://example.org/a', small, '正文' * 400)


if __name__ == '__main__':
    unittest.main()
