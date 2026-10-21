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
