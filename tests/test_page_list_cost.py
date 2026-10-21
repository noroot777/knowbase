# -*- coding: utf-8 -*-
"""列表页解析的**代价**:同一片子树只被遍历固定次数。

跟定位判据那一份分开放,因为这里钉的全是**耗时上限**,而不是"收到了哪几
条" —— 两类用例的失败读法不一样:这边红了要先看机器忙不忙(上限都按"离
实现有几倍余量、离退化有几倍距离"两头取,并取多次最小值),那边红了就是
判据变了。

**为什么这一组必须存在**:单来源采集预算只有 20 秒,而 `sync` 那边
`as_completed` 要等整批结束,一张慢页面就堵住一个工作线程 —— 而且这几个
退化形态**产出全是对的**,只是贵,光看产出一个都发现不了。实测踩过四种:
从 `<a>` 起逐层重扫已排除的子树、按形状剪枝之后的逐层回退、每层一个空
`<time>` 的密集候选、日期在卡片外层时的向外逐层查。
"""

from __future__ import annotations

import time
import unittest

from kb.feeds.page_list import parse_page_list
from kb.feeds.fetching import FeedError

from tests.page_list_fixtures import BASE, listing, row


class CostTests(unittest.TestCase):
    """解析一张列表页的代价,按形态分别钉住上限。"""

    def test_a_deeply_nested_undated_page_does_not_rescan_the_same_subtree(self):
        """上移要从**原容器之外**开始找,不从 `<a>` 开始。

        原容器刚刚被判过没有日期,而 `<a>` 到原容器之间那些祖先的子树都在原
        容器**里面** —— 从 `<a>` 起逐层重扫等于把同一片已排除的子树按深度扫
        了一遍又一遍,`O(深度²)`。

        **上限 0.8 秒**:实测当前 0.125 秒、从 `<a>` 起重扫 1.83 秒(差 14.6
        倍),所以这个数既离实现有 6 倍余量,又拦得住那个退化。**取三次最小
        值**,免得一次调度抖动就把它判红(机器忙起来单次测量能差出好几倍)。

        这一页没有日期、两版都正确报错 —— 钉的是**报错有多贵**,不是报错本身。
        现有那条宽页用例走不到这里:它的新闻原生带日期、菜单没有标题标签,
        两种都不进上移。
        """
        deep = lambda href, title: ('<div>' * 3200 + f'<a href="{href}"><h3>{title}</h3></a>'
                                    + '</div>' * 3200)
        html = ('<html><body><main><ul>'
                f'<li>{deep("/news/a", "First actual headline")}</li>'
                f'<li>{deep("/news/b", "Second actual headline")}</li>'
                '</ul></main></body></html>').encode()
        best = min(self._timed_failure(html) for _ in range(3))
        self.assertLess(best, 0.8, f'3200 层无日期嵌套花了 {best:.2f} 秒')

    def _timed_failure(self, html: bytes) -> float:
        start = time.monotonic()
        with self.assertRaises(FeedError):
            parse_page_list(html, BASE)
        return time.monotonic() - start

    def test_an_illegal_date_deep_inside_does_not_cost_quadratic_time(self):
        """里面有日期形状时也不许逐层问每个祖先。

        原容器里有日期形状、却读不出日期(第一个形状非法、或 `<time>` 是空的)
        时,上移**必须**往里面找 —— 否则会把那几篇真文章一起漏掉,已有那条
        `..._does_not_hide_the_real_one_inside` 钉着。但"往里面找"写成逐层问
        每个祖先又是 `O(深度²)`:每一层都把同一片子树重扫一遍。

        出路是**整页建一次日期索引**,查一个节点变成区间二分 —— 于是"逐层问"
        本身就不贵了,不必按形状去猜该问谁。**按形状剪枝那条路走不通**,它
        按单个文本节点看形状、而提取按拼接后的子树文字,两边语义不一致(拆成
        两个 `<span>` 的日期就漏判),那一版的红记在 `..._split_across_two_
        text_nodes_...` 里。

        **上限 3 秒**:实测当前 0.51 秒、逐层重扫那版 11.4 秒(差 22 倍),
        所以这个数既离实现有 5.8 倍余量,又拦得住那个退化 —— 单来源采集预算
        只有 20 秒,而 `as_completed` 要等整批,一张慢页面会把一个工作线程堵住。

        **非法日期文本和空 `<time>` 是同一个判据的两个实例**,所以一起钉:
        两者都让"有形状、读不出来"成立。取两次最小值防调度抖动。
        """
        deep = lambda stamp, href, title: (
            '<div>' * 12000 + stamp + f'<a href="{href}"><h3>{title}</h3></a>'
            + '</div>' * 12000)
        for name, stamp in (('非法日期文本', '2026-02-30'), ('空 time', '<time></time>')):
            with self.subTest(name):
                html = ('<html><body><main>'
                        + deep(stamp, '/news/x', 'Headline with no readable date')
                        + deep('<time>2026-10-20</time>', '/news/y', 'First actual headline')
                        + deep('<time>2026-10-19</time>', '/news/z', 'Second actual headline')
                        + '</main></body></html>').encode()
                best, got = 9e9, None
                for _ in range(2):
                    start = time.monotonic()
                    got = parse_page_list(html, BASE)
                    best = min(best, time.monotonic() - start)
                self.assertEqual(sorted(e['url'].split('.com')[1] for e in got),
                                 ['/news/x', '/news/y', '/news/z'])
                self.assertLess(best, 3.0, f'12000 层里一个{name}花了 {best:.2f} 秒')

    def test_a_navigation_block_may_not_borrow_the_article_dates(self):
        """**装着一个列表的容器不是哪一条记录的容器。**

        一个导航区里几百条**同址**的标题链接,按地址定界时组内看上去"这一层
        只属于我",于是一路上移穿过 `<nav>` 走到 `<body>`,把 `<main>` 里真
        文章的日期借了过来;再靠"文字最长者当宿主"把那两篇真文章剔成自己的
        字段。实测基线收 2 篇真文章,没有那道闸只收 1 条导航链接,而且
        `errors` 为空、同步显示成功 —— 静默漏收。

        挡住它的判据是 `MIN_DATED`:`<body>` 下有**两条**异址、原生就带日期
        的记录。**差的就是那个 1** —— 按"路上有一条自带日期的异址链接就停"
        去挡试过六遍,一律让用户报的那个故障当场复发(数字在
        `page_list._holds_a_list` 里),因为改版后那一页的精选卡片正是"卡片里
        有一个自带日期的分类角标"。一张卡片一两个字段,一个列表一整排记录。

        同一个输入还钉住**代价**:日期查询要把整片子树的文字拼起来,几百条
        链接各问同一个大祖先一遍就是 `O(链接数 × 子树)`。**上限 1.5 秒**:
        实测当前 0.12 秒、不建索引那版 7.2 秒(差 60 倍)。

        反过来那一边(同一张卡片里只有一个自带日期的角标时,上移照旧读到
        日期)由 `..._a_single_dated_badge_...`、`..._linked_twice_in_one_card_...`
        和两份真样本钉着。
        """
        links = ''.join('<a href="/repeat"><h3>Repeated navigation heading</h3></a>'
                        for unused in range(2000))
        rows = ''.join(f'<li><a href="{href}"><time>{stamp}</time>'
                       f'<h3>{title}</h3></a></li>'
                       for href, stamp, title in (('/news/a', '2026-10-20', 'First actual headline'),
                                                  ('/news/b', '2026-10-19', 'Second actual headline')))
        html = (f'<html><body><nav>{links}</nav>'
                f'<main><ul>{rows}</ul></main></body></html>').encode()
        best, got = 9e9, None
        for _ in range(2):
            start = time.monotonic()
            got = parse_page_list(html, BASE)
            best = min(best, time.monotonic() - start)
        self.assertEqual([(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                         [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19')])
        self.assertLess(best, 1.5, f'2000 条同址链接花了 {best:.2f} 秒')

    def test_an_empty_time_on_every_level_does_not_cost_quadratic_time(self):
        """**候选密集时也要有界** —— "日期形状一般只有一两处"不是输入不变量。

        每一层都放一个空 `<time>` 时,"只问形状所在的那几个祖先"这种剪枝的
        候选数就和祖先数一样多,于是逐层跑完整的日期查询,`O(深度²)` 原样
        回来。整页索引没有这个弱点:候选多少都只是多几次区间二分。

        **上限 0.6 秒**:实测当前 0.10 秒、剪枝那版 0.85 秒(差 8.7 倍)。
        深度翻倍耗时也只翻倍,所以顺带钉住**增长是线性的** —— 1600 层不该
        比 400 层贵出 8 倍以上(剪枝那版实测 0.072 → 0.854,正好 11.9 倍)。

        **要上移的那条记录自己不许带日期。** 第一版把 `<time>` 写在 `<a>`
        里面,于是它原生就读得出日期、压根不进上移 —— 两种退化注入实测都
        **红 0 条**,整条用例对它们没有射程。空 `<time>` 要堆在那条记录的
        祖先链上,而它自己读不出日期,才走得到逐层那一段。
        """
        def page(depth: int) -> bytes:
            # **三条记录的结构路径必须逐字相同**(都是 `ul/li/div×N/a`),否则
            # 无日期那一条自成一个单条分组、整组落选,它压根不进产出 —— 那样
            # 这条用例又只在断言产出上红,钉不住代价。
            def row(href: str, title: str, cell: str, stamp: str) -> str:
                return (f'<li>{cell * depth}{stamp}<a href="{href}">'
                        f'<h3>{title}</h3></a>{"</div>" * depth}</li>')
            rows = (row('/news/x', 'Headline with no readable date',
                        '<div><time></time>', '')
                    + row('/news/a', 'First actual headline', '<div>',
                          '<time>2026-10-20</time>')
                    + row('/news/b', 'Second actual headline', '<div>',
                          '<time>2026-10-19</time>'))
            return f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode()
        spent = {}
        for depth in (400, 1600):
            best, got = 9e9, None
            for unused in range(2):
                start = time.monotonic()
                got = parse_page_list(page(depth), BASE)
                best = min(best, time.monotonic() - start)
            spent[depth] = best
            self.assertEqual(sorted(e['url'].split('.com')[1] for e in got),
                             ['/news/a', '/news/b', '/news/x'])
        self.assertLess(spent[1600], 0.6, f'1600 层空 time 花了 {spent[1600]:.2f} 秒')
        self.assertLess(spent[1600], spent[400] * 8,
                        f'400→1600 层涨了 {spent[1600] / max(spent[400], 1e-6):.1f} 倍')

    def test_looking_outward_for_the_date_does_not_cost_quadratic_time(self):
        """**向外查找是向内那条的对称方向,剪枝只覆盖了一半。**

        日期在卡片**外层**、而原容器自己不含任何日期形状时,"往里面找"那套
        剪枝一个字都用不上,上移还是要逐层问所有更大的祖先 —— 每一层都把下
        面那一整片重扫一遍。这一条连非法日期都不需要,只要嵌套够深。

        **上限 0.6 秒**:实测当前 0.100 秒、逐层重扫那版 1.55 秒(差 15.5
        倍)。同样钉住线性增长:深度翻一倍,那一版实测涨约四倍。
        """
        def page(depth: int) -> bytes:
            rows = ''.join(
                f'<li><time>{stamp}</time>{"<div>" * depth}'
                f'<a href="{href}"><h3>{title}</h3></a>'
                f'<a href="{href}/more">Read more</a>{"</div>" * depth}</li>'
                for href, stamp, title in (('/news/a', '2026-10-20', 'First actual headline'),
                                           ('/news/b', '2026-10-19', 'Second actual headline')))
            return f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode()
        spent = {}
        for depth in (800, 3200):
            best, got = 9e9, None
            for unused in range(2):
                start = time.monotonic()
                got = parse_page_list(page(depth), BASE)
                best = min(best, time.monotonic() - start)
            spent[depth] = best
            self.assertEqual([(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                             [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19')])
        self.assertLess(spent[3200], 0.6, f'3200 层向外查找花了 {spent[3200]:.2f} 秒')
        self.assertLess(spent[3200], spent[800] * 8,
                        f'800→3200 层涨了 {spent[3200] / max(spent[800], 1e-6):.1f} 倍')

    def test_a_wide_page_does_not_cost_quadratic_time(self):
        # `_containers` 按祖先计数算,不对每个祖先 `find_all` 一遍。后者在一张
        # 几千链接的页面上是 O(n²),会把采集预算吃光。
        #
        # **这一局的形状是故意的:链接都是同一个父节点的直接子节点。** 第一版
        # 把它们各自包进 `<li>`,于是 `find_all` 那版在 `ul` 这一层就 break 了、
        # 祖先链只有四层 —— 注入 O(n²) 实测红 0 条,用例对它压根不敏感。链接
        # 共享一个大父节点时才会逐个扫全量兄弟,而这是真实的扁平列表形态。
        items = ''.join(
            f'<a href="/news/{i}"><time>Oct 8, 2026</time>'
            f'<span>Story number {i} title</span></a>' for i in range(400))
        menu = ''.join(f'<a href="/p/{i}">Pricing {i}</a>' for i in range(2600))
        html = f'<html><body><nav>{menu}</nav><main>{items}</main></body></html>'.encode()
        start = time.monotonic()
        got = parse_page_list(html, BASE)
        spent = time.monotonic() - start
        self.assertEqual(len(got), 400)
        # 上限 2 秒:实测线性版 0.002 秒、`find_all` 版 4.54 秒(差 2270 倍),
        # 所以这个数既离基线有一千倍余量,又拦得住 O(n²)。第一版写 10 秒 ——
        # 注入 O(n²) 实测 4.54 秒、照样通过,整条用例对它不敏感。
        self.assertLess(spent, 2.0, f'3000 个链接花了 {spent:.1f} 秒')
