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

import itertools
import random
import time
import unittest
from html.parser import HTMLParser
from unittest.mock import patch

from kb.feeds import grab_page, readable


class _RawTextSpans(HTMLParser):
    """裁判:**html.parser 自己**把哪几段当成了 script/style 的原始文本。

    判据不能是「我觉得这里是脚本」—— 那正是被推翻过两次的那套直觉。这里问的是真
    解析器:原始文本从一个**真·开标签**收口之后开始,到它的闭标签为止;收尾还卡在
    cdata 里的那一段解析器直接丢掉(实测 `'<body><script>' + '<p>x</p>' * 200000`
    建出 0 个 `<p>`),同样不建树,所以也算原始文本。
    """

    def __init__(self, html: str):
        super().__init__(convert_charrefs=False)
        self.raw, self._open = 0, None
        self._lines, offset = [0], 0
        for line in html.splitlines(keepends=True):
            offset += len(line)
            self._lines.append(offset)

    def _offset(self) -> int:
        line, column = self.getpos()
        return self._lines[line - 1] + column

    def handle_starttag(self, tag, attrs):
        if tag in self.CDATA_CONTENT_ELEMENTS:
            self._open = self._offset() + len(self.get_starttag_text() or '')

    def handle_startendtag(self, tag, attrs):
        self._open = None          # `<script/>` 走的是这一支,不开原始文本模式

    def handle_endtag(self, tag):
        if self._open is not None and tag in self.CDATA_CONTENT_ELEMENTS:
            self.raw += self._offset() - self._open
            self._open = None


def real_markup(html: str) -> int:
    """真解析器会建成树的那部分有多大。`markup_size` 不许报得比它小。"""
    judge = _RawTextSpans(html)
    judge.feed(html)
    judge.close()
    raw = judge.raw + (len(html) - judge._open if judge._open is not None else 0)
    return len(html) - raw


#: 两轮审查逮到的那几种形状 + 常见的正常写法 + 几样边角。拼起来跑组合,
#: 因为**单独一片都不出事,出事的是「前一片把后一片的上下文改掉了」**。
PIECES = ('<p>x</p>', '<div title="<script>">', '<div data-example="<style>">', '<script/>',
          '<style/>', '<script data-example="', '<div data-x="><script>">', '<script',
          '<!-- <script> -->', '<!-- x -->', '<script>a</script>', '<style>b</style>',
          '<script src="/a.js"></script>', '<script nonce="n">c</script>', '</script>',
          "<div don't>", '<p a="b\'c">', '<SCRIPT>d</SCRIPT>', '<script >e</script >',
          '<scriptfoo>f</scriptfoo>', '<!doctype html>', '<x a="=">', '<div a=b c=\'d\'>',
          '<style media="(min-width:1px)">q</style>', '<div/>', '<br>', '<script>a<b</script>')


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

    def test_the_budget_never_undercounts_what_the_parser_builds(self):
        """**这条线唯一的不变量,两轮审查都栽在它上面:扣掉的那部分不许超过真解析器
        会当成原始文本的范围。** 少算 = 预算形同虚设(该拒的大页一路走到建树和打分);
        多算顶多是拒掉一篇、让人重抓一次。

        **判据不是「这几种形状修好了」,是拿真解析器当裁判比对**(`real_markup`)——
        上一版正是靠「再排除几个子串」修的,于是第二轮又拿五种新形状把它打穿:属性值
        里的字面量 `<script>`、`<script/>` / `<style/>` 自闭合、引号没收口的开标签。
        只钉那五种,第三种形状照样漏。

        **要跑组合,不是跑单片**:单独一片都不出事,出事的是「前一片把后一片的上下文
        改掉了」—— 第二轮那批反例全长这样。
        """
        for shape in PIECES:
            with self.subTest(shape=shape):
                self.assertGreaterEqual(readable.markup_size(shape), real_markup(shape))
        for combo in itertools.product(PIECES, repeat=2):
            html = ''.join(combo)
            self.assertGreaterEqual(readable.markup_size(html), real_markup(html), html)
        picker = random.Random(20261021)
        for _ in range(3000):
            html = ''.join(picker.choice(PIECES) for _ in range(picker.randint(3, 9)))
            self.assertGreaterEqual(readable.markup_size(html), real_markup(html), html)

    def test_the_five_shapes_that_slipped_past_the_budget_are_refused(self):
        """**第二轮审查那五种形状,各自钉住「后果」而不只是「数对了」。**

        每一份都是 3.2 MB 真标记、40 万个 `<p>` 节点,上一版报个位数字节,于是提取器
        和关卡检查**两处都把整整 3.2 MB 交给了建树**(审查实测:提取那一侧 12 秒没跑完,
        被父进程杀掉;基线 0.0000045 秒就拒了)。
        """
        body = '<p>x</p>' * 400000
        for prefix in ('<div title="<script>"></div>', '<div data-example="<style>"></div>',
                       '<script/>', '<style/>', '<script data-example="'):
            with self.subTest(prefix=prefix):
                html = prefix + body
                self.assertGreater(readable.markup_size(html), readable.MAX_HTML)
                self.assertEqual('', readable.extract(html))

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
