# -*- coding: utf-8 -*-
"""列表页的**定位判据**:哪一组链接是文章列表、一张卡片里哪条链接是记录。

这一块跟别的用例分开放,是因为它钉的全是"同一张页面改版之后还认不认得出
来"这一类后果,两份真实样本的逐条产出都押在这里。
"""

from __future__ import annotations

import time
import unittest

from kb.feeds.page_list import parse_page_list
from kb.feeds.fetching import FeedError

from tests.page_list_fixtures import (BASE, ROWS, SAMPLE, card_listing, listing,
                                      row, rows_listing)


class GroupTests(unittest.TestCase):
    """定位判据:带发布时间的那一组链接就是文章列表。"""

    def test_the_dated_group_wins_not_the_biggest_one(self):
        # **这是整个模块的承重判据。** 实测 Anthropic 那一页上最大的链接组是
        # 90 条页脚菜单,真正的新闻列表只有 10 条。
        got = parse_page_list(listing([row('/news/a', 'Oct 8, 2026', 'First thing'),
                                       row('/news/b', 'Oct 7, 2026', 'Second thing'),
                                       row('/news/c', 'Oct 6, 2026', 'Third thing')],
                                      noise=90), BASE)
        self.assertEqual([e['title'] for e in got],
                         ['First thing', 'Second thing', 'Third thing'])
        for e in got:
            self.assertNotIn('Pricing', e['body'])

    def test_a_field_link_may_not_take_the_date_off_a_real_record(self):
        """**只有标题链接够资格上移。** 字段链接吸上来的容器盖住了真文章时,
        后果不是"多一条",是真文章**本人**被剔成它的字段 —— 整页只剩一条
        分类索引页,顶着真文章的标题和日期(审查 F1 实测)。

        这一页上两篇文章**原生就带日期**(日期嵌在 `<a>` 里),所以正确答案
        跟上移机制没关系:上移只该发生在读不到日期的标题链接身上。
        """
        page = ('<html><body><main><ul>'
                '<li><div><a href="/news/a"><time>Oct 18, 2026</time>'
                '<h3>First actual headline</h3></a></div>'
                '<p><a href="/news/announcements">Announcements</a></p></li>'
                '<li><div><a href="/news/b"><time>Oct 17, 2026</time>'
                '<h3>Second actual headline</h3></a></div>'
                '<p><a href="/news/announcements">Announcements</a></p></li>'
                '</ul></main></body></html>').encode()
        got = parse_page_list(page, BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['title']) for e in got],
                         [('/news/a', 'First actual headline'),
                          ('/news/b', 'Second actual headline')])

    def test_the_heading_may_wrap_the_anchor_instead_of_the_other_way_round(self):
        """`<h3><a>标题</a></h3>` 和 `<a><h3>标题</h3></a>` 都得认。

        **这里要连着地址一起断言。** 只验标题的话这条用例对那个缺陷不敏感:
        标题照样取对(`item_title` 读的是整个容器、两种写法都看得见),错的是
        "谁是这条记录" —— 同一张卡片里文字更长的那条角标当上了记录,于是输出
        真标题配 `/news/announcements`(审查 F2 实测)。
        """
        for name, article in (('heading wraps anchor', '<h3><a href="{href}">{title}</a></h3>'),
                              ('anchor wraps heading', '<a href="{href}"><h3>{title}</h3></a>')):
            with self.subTest(name):
                rows = ''.join(
                    '<li><div>' + article.format(href=href, title=title) + '</div>'
                    '<p><a href="/news/announcements">Announcements</a></p>'
                    f'<div><time>{shown}</time></div></li>'
                    for href, title, shown in (('/news/a', 'First news', 'Oct 18, 2026'),
                                               ('/news/b', 'Other news', 'Oct 17, 2026')))
                got = parse_page_list(
                    f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(), BASE)
                self.assertEqual([(e['url'].split('.com')[1], e['title']) for e in got],
                                 [('/news/a', 'First news'), ('/news/b', 'Other news')])

    def test_a_thumbnail_on_some_rows_only_does_not_split_the_list(self):
        """**缩略图是显示变体,不是记录身份。**

        把它算进分组签名,"这一行配了图、那一行没配"就被拆成两个单条分组,
        两条都够不着 `MIN_DATED`,整页当场报"找不到文章列表" —— 基线返回 2 篇,
        那一版抛异常(审查 F3 实测)。**恰好两条**是这条用例的要害:再多一条
        同构的行就凑够了阈值,缺陷被阈值兜住、用例对它不敏感。
        """
        page = ('<html><body><main><ul>'
                '<li><a href="/news/a"><time>Oct 18, 2026</time>'
                '<img src="/a.png"><h3>First actual headline</h3></a></li>'
                '<li><a href="/news/b"><time>Oct 17, 2026</time>'
                '<h3>Second actual headline</h3></a></li>'
                '</ul></main></body></html>').encode()
        got = parse_page_list(page, BASE)
        self.assertEqual([e['url'].split('.com')[1] for e in got], ['/news/a', '/news/b'])

    def test_the_same_article_linked_twice_in_one_card_still_gets_its_date(self):
        """**上移的边界按地址算,不按链接条数算。**

        同一篇文章在一张卡片里被**同签名**链两次(收起态和展开态各一个标题
        链接)是改版后的常见形态。按条数记的话,那一行的 `<li>` 就成了"同组里
        不止我一条",上移在读到日期**之前**就停手 —— 整组 0 条带日期、整页报
        「找不到文章列表」,这条源从此一条都收不到。

        **这一条钉的是漏报那一侧**:后果不是多收一条导航链接(看得见、删得掉),
        是整条订阅安静地停在"今天没有新内容"。
        """
        rows = ''.join(
            f'<li><div><a href="{href}"><h3>{title}</h3></a></div>'
            f'<div><a href="{href}"><h3>{title}</h3></a></div>'
            f'<div><time>{shown}</time></div></li>'
            for href, title, shown in (('/news/a', 'First actual headline', 'Oct 18, 2026'),
                                       ('/news/b', 'Second actual headline', 'Oct 17, 2026')))
        got = parse_page_list(
            f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(), BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                         [('/news/a', '2026-10-18'), ('/news/b', '2026-10-17')])

    def test_two_dated_articles_with_different_heading_styles_stay_one_list(self):
        """**整页那道闸按结构路径数,不按分组数。**

        签名里"是不是标题链接"管的是上移资格和选主 —— 拿它来数"这一页算不算
        列表"就会把同一个列表拆散:一行标题写 `<h3>`、另一行写 `<span>`(两条
        都**原生带日期**、都是实打实的文章)时,两条各成一个单条分组、一起够
        不着 `MIN_DATED`,整页当场拒收。实测到的后果不止是解析返回空:这条
        订阅会记一次失败、进退避。

        `<span>` 和 `<strong>` 是同一个判据的两个实例,所以一起钉。
        """
        first = ('<li><a href="/news/a"><time>2026-10-20</time>'
                 '<h3>First actual headline</h3></a></li>')
        for tag in ('span', 'strong', 'h3'):
            with self.subTest(tag):
                second = (f'<li><a href="/news/b"><time>2026-10-19</time>'
                          f'<{tag}>Second actual headline</{tag}></a></li>')
                got = parse_page_list(
                    f'<html><body><main><ul>{first}{second}</ul></main></body></html>'.encode(),
                    BASE)
                self.assertEqual(
                    [(e['url'].split('.com')[1], e['title'], e['published'][:10]) for e in got],
                    [('/news/a', 'First actual headline', '2026-10-20'),
                     ('/news/b', 'Second actual headline', '2026-10-19')])

    def test_widening_that_gate_did_not_lower_the_floor(self):
        """**反过来钉一边。** 整页那道闸从"按分组数"改成"按结构路径数"是
        **放宽**,所以得同时钉住它没把下限一起放掉。两个方向都要钉:

        - **带日期的只有一条** —— 页面上链接很多,但只有一条容器里有日期。
          (跟 `test_one_dated_link_is_not_a_list` 不重复:那条验的是"整页只有
          一条链接"。)
        - **两条带日期、但结构路径各不相同** —— 这一条才分得出"按结构路径
          数"和"按全页总数":散落在页头和页脚的两条带日期链接加起来够 2 条,
          但它们压根不在同一个列表里。**改成数全页总数就会把它们收进来**,
          而只看第一个形态的用例对那个放宽毫无反应。
        """
        menu = ''.join(f'<li><a href="/p/{i}">Pricing {i}</a></li>' for i in range(30))
        one_dated = ('<html><body><footer><ul>' + menu + '</ul></footer><main><ul>'
                     '<li><a href="/news/a"><time>2026-10-20</time>'
                     '<h3>The only dated link</h3></a></li>'
                     '</ul></main></body></html>').encode()
        scattered = ('<html><body>'
                     '<nav><div><a href="/news/x"><time>2026-10-20</time>'
                     '<h3>A dated link in the nav</h3></a></div></nav>'
                     '<footer><p><a href="/news/y"><time>2026-10-19</time>'
                     '<h3>A dated link in the footer</h3></a></p></footer>'
                     '</body></html>').encode()
        for name, page in (('only one dated link', one_dated),
                           ('two dated links on different paths', scattered)):
            with self.subTest(name):
                with self.assertRaises(FeedError):
                    parse_page_list(page, BASE)

    def test_one_undated_row_does_not_take_its_dated_neighbour_down(self):
        """**展示分工不许两次否决同一个列表。**

        整页那道闸认定这条结构路径是个列表之后,**组入选**还是按分组各自问的,
        于是同一个"分工"在第二道闸上又把列表切断一次:`<h3>` 那组 1 条 1 带
        日期(入选),`<span>` 那组 2 条 1 带日期(两道都不满足),于是那篇**原生
        带日期**的真文章跟着没日期的那条一起落选。

        **这一条钉的是静默漏收**:实测基线收 3 篇,只改整页计数的那版收 1 篇,
        而且 `errors` 为空、同步显示成功 —— 比报错坏,面板上看不出来。
        """
        page = ('<html><body><main><ul>'
                '<li><a href="/news/a"><time>2026-10-20</time>'
                '<h3>First dated article</h3></a></li>'
                '<li><a href="/news/b"><time>2026-10-19</time>'
                '<span>Second dated article</span></a></li>'
                '<li><a href="/news/c"><span>Third undated article</span></a></li>'
                '</ul></main></body></html>').encode()
        got = parse_page_list(page, BASE)
        self.assertEqual([e['url'].split('.com')[1] for e in got],
                         ['/news/a', '/news/b', '/news/c'])
        self.assertEqual([e['published'][:10] for e in got[:2]],
                         ['2026-10-20', '2026-10-19'])

    def test_a_group_with_no_dates_at_all_is_still_a_candidate(self):
        """**零日期的分组也要留着当候选。**

        整页那道闸认定一条结构路径是个列表之后,组入选还有第三种方式:
        "这条结构路径已经是个列表"。那条补救**只在候选里找** —— 收集候选时
        把 0 条带日期的分组顺手丢掉,它就压根补不回来。

        形态:一个列表里两篇带日期的写 `<h3>`、第三篇没标日期的写 `<span>`。
        `<span>` 那组**整组零日期**,和已有那条(组里还有一条带日期)不是同一
        道闸。`published` 为空是**合法条目** —— 历史下限对未知日期照收,不该
        因为"这一组里没人带日期"就判出去。

        **这一条钉的是静默漏收**:实测基线收 3 篇,丢掉候选的那版收 2 篇,
        而且 `errors` 为空、来源记成功。**两个写法反过来也钉**:哪种标签带
        日期不该影响结论。
        """
        for dated_tag, undated_tag in (('h3', 'span'), ('span', 'h3')):
            with self.subTest(f'{dated_tag} 带日期 / {undated_tag} 无日期'):
                rows = ''.join(
                    f'<li><a href="{href}"><time>{stamp}</time>'
                    f'<{dated_tag}>{title}</{dated_tag}></a></li>'
                    for href, stamp, title in (('/news/a', '2026-10-20', 'First actual headline'),
                                               ('/news/b', '2026-10-19', 'Second actual headline')))
                rows += (f'<li><a href="/news/c">'
                         f'<{undated_tag}>Third headline with no date</{undated_tag}></a></li>')
                got = parse_page_list(
                    f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(), BASE)
                self.assertEqual([(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                                 [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19'),
                                  ('/news/c', '')])

    def test_deeply_nested_same_name_containers_are_found_by_identity(self):
        """祖先定位按**节点身份**,不按内容相等。

        `box in chain` / `chain.index(box)` 走的是 bs4 的 `Tag.__eq__` —— 那是
        **内容相等**,会递归比较名字、属性和整棵子树。原容器本身也是无属性
        `<div>`、里面还包着几百层同样的 `<div>` 时,它在找到真正那个节点之前
        就把栈耗光:实测整页 `RecursionError`,三条记录(其中两条本来好好带着
        日期)一条都收不到。这个模块认节点一律按身份(别处都是 `id(node)`)。

        **原容器必须也是 `<div>`**:那条 3200 层的性能用例原容器是 `<li>`,
        比较时遇到内部 `<div>` 立刻因标签名不同返回 False,压根进不到递归比较
        里 —— 它本轮全程是绿的,证明不了这件事。**断言的是三条都在**,不是
        "没抛某一类异常"。
        """
        def record(href, title, stamp):
            return ('<div>' * 600 + stamp
                    + f'<a href="{href}"><h3>{title}</h3></a>' + '</div>' * 600)
        page = ('<html><body><main>'
                + record('/news/deep-0', 'Undated deep record', '')
                + record('/news/deep-1', 'First dated deep', '<time>2026-10-20</time>')
                + record('/news/deep-2', 'Second dated deep', '<time>2026-10-19</time>')
                + '</main></body></html>').encode()
        got = {e['url'].split('.com')[1]: e for e in parse_page_list(page, BASE)}
        self.assertEqual(sorted(got), ['/news/deep-0', '/news/deep-1', '/news/deep-2'])
        self.assertEqual(got['/news/deep-1']['published'][:10], '2026-10-20')
        self.assertEqual(got['/news/deep-2']['published'][:10], '2026-10-19')

    def test_a_category_heading_in_the_card_is_not_another_record(self):
        """**上移的定界只问同组,不许扩到整页。**

        扩到整页(数全页的标题链接地址)看着更严,实际上把**同一张卡片里的
        字段**也算成了"别人":一行里的分类角标写成 `<h4><a>Announcements</a></h4>`
        时它也满足 `titled`,于是真文章在读到本卡片的日期之前就被挡住,日期
        归了那条分类链接 —— 实测基线和组内定界都收 2 篇真文章,整页定界那版
        只收 1 条 `/news/announcements`,而且 `errors` 为空、同步显示成功。

        **`titled` 标的是"够不够资格上移",不等于"这是一条独立记录"**,这两
        件事不许合并。同一张卡片里的分类小标题、作者小标题、带 heading 的
        `Read more` 都是这个形态,`h1`-`h6` 加上 heading 在链接内外两个方向
        都走同一条规则,所以不能只把 `<h4>` 或某个分类地址特判掉。

        这一条钉的是**静默漏收**,比报错坏:面板上看不出来。
        """
        rows = ''.join(
            f'<li><div><a href="{href}"><h3>{title}</h3></a></div>'
            f'<header><h4><a href="/news/announcements">Announcements</a></h4>'
            f'<time>{stamp}</time></header></li>'
            for href, stamp, title in (('/news/a', '2026-10-20', 'Actual headline a'),
                                       ('/news/b', '2026-10-19', 'Actual headline b')))
        got = parse_page_list(
            f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(), BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                         [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19')])

    def test_a_date_split_across_two_text_nodes_is_still_found(self):
        """**找日期的地方和读日期的地方必须是同一套文本语义。**

        真日期写成 `<span>Oct 20,</span><span>2026</span>` —— 拼起来才是
        日期,两个文本节点**各自都不是**。按单个文本节点看"有没有日期形状"
        去剪枝的那一版,于是只把外面那个非法编号认成候选,里面真正读得出
        日期的 `<div>` 一个都没被问到,整页报「找不到带发布时间的文章列表」:
        实测两篇真文章一条都收不到、这条源还记一次失败进退避。

        **"至少有一个候选"证明不了"所有读得出日期的容器都进了候选"** ——
        任何一处无关的完整日期形状或 `<time>` 都能让跨节点的那一处失效,所以
        这不是给这个编号或这种月份写法加例外的事。现在候选和提取走同一串
        归一化文字、同一套正则、同一个优先级,裂缝不存在。

        **反过来钉一边**:同样的页面把日期写成一整段文本时本来就该收到两篇
        (`complete_valid_control` 那一侧),所以两种写法一起钉 —— 只钉拆开
        那一种的话,"整条回退到不剪枝"也能让它变绿,而那会让代价那一组红。
        """
        for name, shown in (('拆成两个文本节点', '<span>Oct {day},</span><span>2026</span>'),
                            ('一整段文本', 'Oct {day}, 2026')):
            with self.subTest(name):
                rows = ''.join(
                    f'<li>2026-02-30<div><a href="{href}"><h3>{title}</h3></a>'
                    + shown.format(day=day) + '</div></li>'
                    for href, day, title in (('/news/a', 20, 'Actual headline a'),
                                             ('/news/b', 19, 'Actual headline b')))
                got = parse_page_list(
                    f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(), BASE)
                self.assertEqual(
                    [(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                    [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19')])

    def test_a_clock_time_in_the_next_row_is_not_part_of_this_date(self):
        """**一条记录的日期只能来自它自己那一段**,下一行的钟点不算。

        日期查询在整页**一串**归一化文字上找命中,所以每次查询都要按节点的
        区间截断:这一行以 `2026-10-20` 收尾,下一行以时长 `12:30` 开头,整页
        那一串上两段连起来正好被 ISO 那条正则一口吃掉(它的时间段是可选的)。
        命中伸出右边界时必须按截到边界的那一段**重试一次**,才能拿回本行自己
        那个更短的命中。

        这一条钉的是两个方向:① 少了那次重试,这一行在自己容器里读不出日期,
        而上移到 `<ul>` 会被"同组里不止我一条"挡住 —— 于是这一组不满足"每条
        都带日期"、整页够不着 `MIN_DATED`,**这条源一篇都收不到**(实测 2 篇
        变 0 篇,并给这条订阅记一次失败、进退避);② 反过来直接认下那个过长的
        命中,这一行的发布时间就会多出一个和它无关的 `12:30`。
        """
        rows = ('<li><a href="/news/a"><h3>First actual headline</h3></a>'
                '<span>2026-10-20</span></li>'
                '<li><span>12:30</span><a href="/news/b"><h3>Second actual headline</h3></a>'
                '<span>2026-10-19</span></li>')
        got = parse_page_list(
            f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(), BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['published']) for e in got],
                         [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19')])

    def test_a_single_dated_badge_does_not_make_the_card_a_list(self):
        """**一张卡片里有一个自带日期的角标,卡片仍然是一条记录。**

        这是"装着一个列表的容器不是记录容器"那道闸的反方向。判据是
        `MIN_DATED` 而不是 1,差的就是那个 1:改版后那一页上的精选卡片正是
        「卡片里有一条自带日期的分类角标」,按"有一条就停"去挡会把 4 篇真
        文章的日期挡掉、把 `/news/safeguards` 等 8 个分类索引页重新收进库。

        两行各有一个**原生就带日期**的角标(角标那一组自己够两条,所以它
        确实是"原生带日期的异址链接"),而每一行的卡片容器里只装着一个 ——
        1 < `MIN_DATED`,所以真文章照旧上移到卡片、读到卡片的日期。
        """
        rows = ''.join(
            f'<li><div><a href="{href}"><h3>{title}</h3></a></div>'
            f'<div><a href="/news/announcements">Announcements</a>'
            f'<time>{stamp}</time></div></li>'
            for href, stamp, title in (('/news/a', '2026-10-20', 'Actual headline a'),
                                       ('/news/b', '2026-10-19', 'Actual headline b')))
        got = parse_page_list(
            f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(), BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                         [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19')])

    def test_an_illegal_date_in_the_parent_does_not_hide_the_real_one_inside(self):
        """"大的解析不出来、里面的小的也不会有"**不成立**,所以跳过内部要先问
        清楚是哪一种读不出来。

        `item_published` 把整片子树的文字拼成一串、只认其中**第一个**日期形状,
        所以父容器里一个非法编号(`2026-02-30`)就让它返回空,而里面那个小容器
        的 `2026-10-20` 本来读得出来。按"解析失败"去跳过内部会把这两篇真文章
        一起漏掉;按"压根没有日期形状"去跳才安全。

        (日期提取器"只试第一个匹配"这件事本身是既有边界,原始基线在这一页上
        也收不到 —— 这里钉的是**跳过的闸**别把它放大成漏收。)
        """
        page = ('<html><body><main><ul>'
                '<li>2026-02-30<div><a href="/news/a"><h3>First headline</h3></a>'
                ' 2026-10-20</div></li>'
                '<li>2026-02-30<div><a href="/news/b"><h3>Second headline</h3></a>'
                ' 2026-10-19</div></li>'
                '</ul></main></body></html>').encode()
        got = parse_page_list(page, BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['published'][:10]) for e in got],
                         [('/news/a', '2026-10-20'), ('/news/b', '2026-10-19')])

    def test_the_real_anthropic_page(self):
        got = parse_page_list(SAMPLE, BASE)
        titles = [e['title'] for e in got]
        self.assertIn('2026 Usage Policy update', titles)
        self.assertIn('Introducing the Anthropic Cyber Mission', titles)
        # 页脚菜单、导航、以及那三条标题是 `Newsroom`、地址指向首页的噪音,
        # 一条都没混进来。
        self.assertNotIn('Newsroom', titles)
        for e in got:
            self.assertTrue(e['published'], f'{e["title"]} 没有日期')
            self.assertTrue(e['url'].startswith('https://www.anthropic.com/'))
        self.assertEqual(got[0]['published'][:10], '2026-10-08')

    def test_the_featured_cards_on_the_real_page_are_not_dropped(self):
        """**先验后果**:只断言"收了几条"的话,一个把 featured 换成别的噪音
        的实现也能凑够数 —— 这里点名那三条真文章。

        它们是 Anthropic 当前页面上单独展示的发布公告,地址**不在** `/news/`
        下,所以旧版那句"只留胜出组"一条都收不到,而同步照常报成功(审查第 2
        条,边界说明里写的"当前页面没发现漏掉"只比了 `/news/` 下的链接)。
        """
        got = {e['title']: e for e in parse_page_list(SAMPLE, BASE)}
        for name, slug in (('Introducing Claude Haiku 5.5', 'claude-haiku-5-5'),
                           ('Introducing Claude Sonnet 5.5', 'claude-sonnet-5-5'),
                           ('Introducing Claude Opus 5.5', 'claude-opus-5-5')):
            with self.subTest(name):
                self.assertIn(name, got)
                self.assertEqual(got[name]['url'], f'https://www.anthropic.com/{slug}')
                self.assertTrue(got[name]['published'])
        # 真列表那 10 条一条没少 —— 多收不等于换了一批。
        news = [e for e in got.values() if '/news/' in e['url']]
        self.assertEqual(len(news), 10)

    def test_a_single_featured_card_above_the_list_is_kept(self):
        """审查那份最小样本的形状:顶部 `<section>` 里 1 条头条 + 下面 2 条列表。

        1 条够不着 `MIN_DATED`,靠的是"这一组里每条都带日期"那半句判据;而它
        能认出自己那张卡片,靠的是容器在**整页**尺度上算 —— 组内算的话它的
        容器是整份文档。
        """
        page = (b'<html><body><main>'
                b'<section><a href="/latest"><time datetime="2026-10-10">2026-10-10</time>'
                b'<h2>Newest announcement</h2></a></section>'
                b'<ul><li><a href="/older"><time datetime="2026-10-09">2026-10-09</time>'
                b'<h3>Older announcement</h3></a></li>'
                b'<li><a href="/oldest"><time datetime="2026-10-08">2026-10-08</time>'
                b'<h3>Oldest announcement</h3></a></li></ul>'
                b'</main></body></html>')
        got = parse_page_list(page, BASE)
        self.assertEqual([e['title'] for e in got],
                         ['Newest announcement', 'Older announcement', 'Oldest announcement'])
        self.assertEqual(got[0]['url'], 'https://www.anthropic.com/latest')
        self.assertEqual(got[0]['published'], '2026-10-10')

    def test_a_badge_rendered_twice_does_not_make_the_card_a_list(self):
        """**同一个角标渲染两份,还是一个字段,不是一个列表。**

        "这一层装着一个列表"那道闸按**地址**数,不按链接条数:一张卡片里同
        一个分类角标被渲染两份(收起态/展开态各一个,改版后那一页的常见形态)
        时,按条数记就把这张卡片判成了"装着两条记录",上移在读到本卡片的日期
        之前停手 —— 整组 0 条带日期、整页报"找不到文章列表",**这条源从此一条
        都收不到**(实测前两版都收 2 篇真文章,按条数记的那版 0 篇、给这条
        订阅记一次失败、进退避)。
        """
        cards = ''.join(
            f'<article><div><a href="{href}"><h3>{title}</h3></a></div>'
            + f'<aside><a href="/news/announcements">Announcements</a>'
              f'<time>{stamp}</time></aside>' * 2 + '</article>'
            for href, stamp, title in (('/news/alpha', '2026-10-20', 'Actual alpha headline'),
                                       ('/news/beta', '2026-10-19', 'Actual beta headline')))
        got = parse_page_list(f'<html><body><main>{cards}</main></body></html>'.encode(), BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['published']) for e in got],
                         [('/news/alpha', '2026-10-20'), ('/news/beta', '2026-10-19')])

    def test_a_category_badge_sharing_the_signature_is_still_a_field(self):
        """**角标和标题同签名时,角标也不是"同组的另一条记录"。**

        `<a><h4>Announcements</h4></a>` 和 `<a><h3>标题</h3></a>` 到 `<a>` 的
        结构路径逐字相同、`titled` 都为真,于是它们落在同一组里。组内定界只按
        地址算的话,角标就成了"同组另一条记录",真文章在读到本卡片的日期之前
        被它挡住 —— 实测前一版收 2 篇真文章 + 1 条分类页,那一版 0 篇、给这条
        订阅记一次失败。

        分开它们的不是标签层级(`h3`/`h4` 是**显示变体**),是**副本的落点**:
        同一个分类角标出现在好几张卡片上,它的副本跨出了任何一张卡片,所以它
        不是哪一张卡片里的记录。定界因此从"那几份副本的最近共同祖先"往上标。
        """
        cards = ''.join(
            f'<article><div><a href="{href}"><h3>{title}</h3></a></div>'
            f'<div><a href="/news/announcements"><h4>Announcements</h4></a>'
            f'<time>{stamp}</time></div></article>'
            for href, stamp, title in (('/news/alpha', '2026-10-20', 'Actual alpha headline'),
                                       ('/news/beta', '2026-10-19', 'Actual beta headline')))
        got = parse_page_list(f'<html><body><main>{cards}</main></body></html>'.encode(), BASE)
        self.assertEqual([(e['url'].split('.com')[1], e['published']) for e in got],
                         [('/news/alpha', '2026-10-20'), ('/news/beta', '2026-10-19')])

    def test_a_row_may_not_borrow_the_only_neighbour_date(self):
        """反过来钉一边:**只出现在这一层里的那条记录照旧挡着上移。**

        两行的列表、其中一行没标日期时,那一行不许借到隔壁那行的日期 ——
        借到的话它的容器就长到了整个 `<ul>`、把隔壁那条真记录**套在里面**,
        再按"容器互相嵌套时外层是记录"把那一行真记录剔成自己的一个字段:
        实测 2 篇真文章变 1 篇,`published` 还是借来的,而且 `errors` 为空、
        同步显示成功 —— 静默丢一半。

        代价是这一页按"找不到带发布时间的文章列表"拒收(两行里只有一行带
        日期,够不着 `MIN_DATED`、也不满足"每条都带日期")。**那是报错、
        面板上看得见、重试还在**,比静默丢掉一篇轻。
        """
        rows = ''.join(f'<li><a href="{href}"><h3>{title}</h3></a>{stamp}</li>'
                       for href, stamp, title in
                       (('/news/alpha', '<time>2026-10-20</time>', 'Actual alpha headline'),
                        ('/news/beta', '', 'Actual beta headline')))
        with self.assertRaises(FeedError):
            parse_page_list(f'<html><body><main><ul>{rows}</ul></main></body></html>'.encode(),
                            BASE)

    def test_a_broken_clock_in_the_footer_does_not_fail_the_page(self):
        """**无关节点上的坏时间不许让整页失败。**

        整页一次的日期索引会把页脚那个纯展示用的 `<time>` 也解析一遍 ——
        而过去它压根不会被读到。非法时区(`+25:00`)和换算溢出
        (`9999-12-31T23:00Z`)两族异常都要挡在"这一处读不出钟点"上:不然
        两篇真文章一条都收不到,这条订阅记一次失败、进退避(实测 added=2
        变 added=0,库里一条都没有)。
        """
        for clock in ('2026-10-20T12:00+25:00', '9999-12-31T23:00Z'):
            with self.subTest(clock):
                rows = ''.join(
                    f'<li><a href="{href}"><h3>{title}</h3><time>{stamp}</time></a></li>'
                    for href, stamp, title in
                    (('/news/alpha', '2026-10-20', 'Actual alpha headline'),
                     ('/news/beta', '2026-10-19', 'Actual beta headline')))
                html = (f'<html><body><footer><time datetime="{clock}">Display clock'
                        f'</time></footer><main><ul>{rows}</ul></main></body></html>')
                got = parse_page_list(html.encode(), BASE)
                self.assertEqual([(e['url'].split('.com')[1], e['published']) for e in got],
                                 [('/news/alpha', '2026-10-20'), ('/news/beta', '2026-10-19')])

    def test_the_redesigned_rows_page_finds_the_articles_not_the_categories(self):
        """**同一个站点改版之后的那一份。** 这是 2026-10-21 真实抓到的形态:
        每行把标题、摘要、分类角标写成三个平级的 `<a>`,日期是第四个兄弟。

        旧实现在这一页上收到的是 9 条**分类索引页**(`/news/safeguards` 那种,
        标题就一个词、压根没有正文),真文章一条都没有 —— 而同步照常报成功、
        面板上一切正常。这条用例点名两头:真文章在,分类页不在。
        """
        got = parse_page_list(ROWS, BASE)
        titles = {e['title']: e['url'] for e in got}
        for name, slug in (('Investing in the public benefit', '/news/public-benefit-fund'),
                           ('An update on independent evaluators',
                            '/news/an-update-on-independent-evaluators'),
                           ('Preparing for the 2026 US midterms', '/news/2026-us-midterms'),
                           ('How Claude’s text watermark works', '/news/claude-text-watermark')):
            with self.subTest(name):
                self.assertEqual(titles.get(name), f'https://www.anthropic.com{slug}')
        # 分类索引页一条都不许进来。单独列出来而不是只数条数 —— 数条数的话
        # 一个"把分类页换成别的噪音"的实现也能凑够数。
        for slug in ('policy', 'safeguards', 'security', 'partnerships', 'product'):
            with self.subTest(slug):
                self.assertNotIn(f'https://www.anthropic.com/news/{slug}',
                                 set(titles.values()))
        self.assertGreaterEqual(len(got), 25, '这一页上有 20 篇新闻 + 几张 featured 卡片')
        for e in got:
            self.assertTrue(e['published'], f'{e["title"]} 没有日期')

    def test_the_one_leftover_on_the_redesigned_page_is_named(self):
        """**如实钉住还漏着的那一条,别假装干净。**

        这一页的 featured 轮播里有一张卡片,它的 `<header>` 自己带着日期、而
        文章链接在 `<header>` 外面又恰好自成一组(组内只有它一条,所以不许
        上移找日期 —— 那条禁令是为了不把"容器一路长到根"的坑重新挖开)。于是
        这张卡片的 `<header>` 成了一条记录,收进来一个 `Announcements`。

        按这个模块既有的取舍,这是可接受的那一边:多一条导航链接看得见、
        删得掉,而漏掉一篇文章是静默丢数据。**但它只许是这一条** —— 这里
        把数量钉成 1,变多了就是上面某一道判定松了。
        """
        got = parse_page_list(ROWS, BASE)
        leftover = [e['url'] for e in got
                    if e['url'].rsplit('/', 1)[-1] in
                    ('announcements', 'policy', 'safeguards', 'security',
                     'partnerships', 'features', 'product')]
        self.assertEqual(leftover, ['https://www.anthropic.com/news/announcements'])

    def test_a_badge_sharing_the_cards_container_is_not_a_record(self):
        # 第一道:标题链接和分类角标吸到同一个容器时,装着标题标签的那条才是
        # 记录。不判的话库里会多出 `/news/announcements` 这种条目,而且它顶着
        # 真文章的标题和日期(实测如此)。
        got = parse_page_list(rows_listing(
            [row('/news/a', 'Oct 8, 2026', 'First real headline'),
             row('/news/b', 'Oct 7, 2026', 'Second real headline'),
             row('/news/c', 'Oct 6, 2026', 'Third real headline')]), BASE)
        self.assertEqual([e['title'] for e in got],
                         ['First real headline', 'Second real headline',
                          'Third real headline'])
        self.assertEqual([e['published'] for e in got],
                         ['2026-10-08', '2026-10-07', '2026-10-06'])
        for e in got:
            self.assertNotIn('/news/announcements', e['url'])

    def test_a_badge_in_its_own_dated_block_loses_to_the_card(self):
        # 第二道:角标自己那一小块里带着日期(featured 卡片的形态),靠嵌套
        # 关系判 —— 它落在卡片容器里面,所以它是卡片的一个字段。
        got = parse_page_list(card_listing(
            [row('/news/a', 'Oct 8, 2026', 'First real headline', subject='Policy'),
             row('/news/b', 'Oct 7, 2026', 'Second real headline', subject='Policy')]), BASE)
        self.assertEqual([e['url'] for e in got],
                         ['https://www.anthropic.com/news/a',
                          'https://www.anthropic.com/news/b'])

    def test_which_one_is_the_record_does_not_depend_on_document_order(self):
        """**反过来钉一边。** 两道判定都不许看"谁在文档里排前面"。

        实测:角标那一组和文章那一组在三个排序键上完全并列(都是 5 条、5 个
        地址、5 条带日期),于是谁先谁后只取决于文档顺序 —— 角标写在卡片头部
        所以它赢,5 个分类页照旧进库。
        """
        for name, page in (('角标在前', rows_listing(
                                [row('/news/a', 'Oct 8, 2026', 'First real headline'),
                                 row('/news/b', 'Oct 7, 2026', 'Second real headline'),
                                 row('/news/c', 'Oct 6, 2026', 'Third real headline')],
                                badge_first=True)),
                           ('角标在后', card_listing(
                                [row('/news/a', 'Oct 8, 2026', 'First real headline'),
                                 row('/news/b', 'Oct 7, 2026', 'Second real headline')],
                                header_first=False))):
            with self.subTest(name):
                got = parse_page_list(page, BASE)
                self.assertTrue(got)
                for e in got:
                    self.assertNotIn('/news/announcements', e['url'])
                    self.assertTrue(e['published'])

    def test_rows_that_differ_in_furniture_stay_in_one_list(self):
        """分组签名只看"这是不是标题链接",不比 `<a>` 内部的 tag 整串。

        整串比的话,"这一行多个角标、那一行少一段摘要"就被拆成两组、各自够
        不着 `MIN_DATED`,整页当场报"找不到文章列表" —— 第一版实测把
        `test_heading_tag_wins` 和 `test_a_blurb_is_not_a_title` 打红。
        """
        html = ('<html><body><main><ul>'
                '<li><a href="/news/a"><time>Oct 8, 2026</time><img src="/a.png"/>'
                '<h3>With a thumbnail</h3><p>and a blurb</p></a></li>'
                '<li><a href="/news/b"><time>Oct 7, 2026</time>'
                '<h3>Without either one</h3></a></li>'
                '<li><a href="/news/c"><time>Oct 6, 2026</time><img src="/c.png"/>'
                '<h3>With a thumbnail again</h3></a></li>'
                '</ul></main></body></html>').encode()
        got = parse_page_list(html, BASE)
        self.assertEqual([e['title'] for e in got],
                         ['With a thumbnail', 'Without either one',
                          'With a thumbnail again'])

    def test_newest_first(self):
        got = parse_page_list(listing([row('/news/old', 'Sep 1, 2026', 'Older one'),
                                       row('/news/new', 'Oct 8, 2026', 'Newer one'),
                                       row('/news/mid', 'Oct 1, 2026', 'Middle one')]), BASE)
        self.assertEqual([e['title'] for e in got],
                         ['Newer one', 'Middle one', 'Older one'])

    def test_one_dated_link_is_not_a_list(self):
        # 1 条太容易是巧合,而**宁可报错也不要把导航当列表**:误报用户看得见,
        # 漏报是往知识库里灌垃圾。
        with self.assertRaises(FeedError):
            parse_page_list(listing([row('/news/a', 'Oct 8, 2026', 'Only one')]), BASE)

    def test_tracking_parameters_are_stripped(self):
        got = parse_page_list(listing([row('/news/a?utm_source=x', 'Oct 8, 2026', 'Tracked one'),
                                       row('/news/b?utm_medium=y', 'Oct 7, 2026', 'Tracked two')]), BASE)
        for e in got:
            self.assertNotIn('utm_', e['url'])

    def test_same_url_twice_lands_once(self):
        got = parse_page_list(listing([row('/news/a', 'Oct 8, 2026', 'A once'),
                                       row('/news/a', 'Oct 8, 2026', 'A again'),
                                       row('/news/b', 'Oct 7, 2026', 'B second')]), BASE)
        self.assertEqual(len(got), 2)
