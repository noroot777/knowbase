# -*- coding: utf-8 -*-
"""没有 feed 的站点:列表页拆条目(`kb.feeds.page_list`)。

HTTP 传输那一层打桩,别的一行不打:定位判据、日期解析、标题提取、队列取数、
落库、补抓两道闸门全部真跑 —— 打桩打成"成功"正好绕开唯一要验的那件事。

真实形态那一份样本是 `tests/page_list_sample.html`:Anthropic news 那张页面
剥掉 script / style / **以及全部 class 属性**之后的骨架(429 KB → 21 KB)。
删掉类名不只是为了瘦:**样本里压根没有类名可依赖**,所以"不认 CSS 类名"这条
是样本自己证明的,不是靠一条注释声明的。
"""

from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kb import config, maintenance
from kb.collectors import FetchScope
from kb.feeds import fetching, fulltext, page_list, registry as reg, sync, translate
from kb.feeds.fetching import FeedError, Response
from kb.feeds.page_list import (item_published, item_subject, item_title,
                                parse_page_list, read_published, shell_body)
from kb.source_types import feed_types

SAMPLE = (Path(__file__).parent / 'page_list_sample.html').read_bytes()
BASE = 'https://www.anthropic.com/news'
ARTICLE = ('<html><body><nav><a href="/">Home</a><a href="/news">News</a></nav>'
           '<article><h1>Introducing the Anthropic Cyber Mission</h1>'
           '<p>We are launching a program to put frontier models in the hands of '
           'cyber defenders, because defense is where the asymmetry can be flipped. '
           'The gap between attacker and defender tooling has widened for a decade.</p>'
           '<p>The first cohort covers critical infrastructure operators and the '
           'open-source maintainers who carry a disproportionate share of the load. '
           'Each participant gets model access plus a dedicated review channel.</p>'
           '</article><footer>© 2026 Anthropic</footer></body></html>')


def resp(body: str | bytes, url: str = BASE, status: int = 200) -> Response:
    raw = body.encode() if isinstance(body, str) else body
    return Response(status, url, raw, {'content-type': 'text/html; charset=utf-8'})


def listing(rows, *, noise: int = 20, head: str = '') -> bytes:
    """一张最小列表页。

    `noise` 条**不带日期**的页脚链接刻意排在前面、而且数量远多于正文列表 ——
    这正是 Anthropic 那一页的真实形态(90 条页脚菜单 vs 10 条新闻),用来钉住
    判据是"带日期",不是"哪一组链接最多"。
    """
    menu = ''.join(f'<li><a href="/p/{i}">Pricing {i}</a></li>' for i in range(noise))
    items = ''.join(
        f'<li><a href="{href}"><div><time{datetime_attr}>{shown}</time>'
        f'<span>{subject}</span></div><span>{title}</span></a></li>'
        for href, shown, subject, title, datetime_attr in rows)
    return (f'<html><body>{head}<footer><ul>{menu}</ul></footer>'
            f'<main><ul>{items}</ul></main></body></html>').encode()


def row(href, shown, title, *, subject='Announcements', attr=''):
    return (href, shown, subject, title, f' datetime="{attr}"' if attr else '')


def today(offset: int = 0) -> str:
    """样本日期跟着今天走:写死的日期过几天就被 history_floor 挡住,而红的会是
    一堆跟它毫无关系的用例(口径同 `test_feed_sources` 的 `_FRESH`)。"""
    return time.strftime('%Y-%m-%d', time.localtime(time.time() - offset * 86400))


class DateTests(unittest.TestCase):
    """日期解析:精度如实保留,时区按项目的来。"""

    def test_four_real_world_spellings(self):
        for text, want in [('2026-10-08', '2026-10-08'),
                           ('Oct 8, 2026', '2026-10-08'),
                           ('October 8, 2026', '2026-10-08'),
                           ('8 Oct 2026', '2026-10-08'),
                           ('8th October 2026', '2026-10-08'),
                           ('2026年10月8日', '2026-10-08')]:
            with self.subTest(text=text):
                self.assertEqual(read_published(text), want)

    def test_day_only_stays_day_only(self):
        # 不补一个 00:00 假装知道几点 —— `_within_history` 取前 10 位、
        # `daily.parse_ts` 按当天零点,两头都接得住只有日期的写法。
        self.assertEqual(read_published('Oct 8, 2026'), '2026-10-08')

    def test_utc_offsets_land_in_project_timezone(self):
        # 上海 +08:00:UTC 19:00 是次日 03:00。被比较的那一头(parse_feed 的
        # published)固定按 Asia/Shanghai,两头不同时区每天差一整天。
        self.assertEqual(read_published('2026-10-08T19:00:00Z'), '2026-10-09 03:00')
        self.assertEqual(read_published('2026-10-08T19:00:00+00:00'), '2026-10-09 03:00')
        self.assertEqual(read_published('2026-10-08T12:00:00+08:00'), '2026-10-08 12:00')

    def test_naive_timestamps_follow_the_project_not_the_host(self):
        # 不带时区的写法按项目时区补齐(口径同 `x_browser._utc`)。宿主机换个
        # TZ 不该让同一份页面解析出不同的日期。
        self.assertEqual(read_published('2026-10-08T09:30:00'), '2026-10-08 09:30')
        self.assertEqual(read_published('2026-10-08 09:30'), '2026-10-08 09:30')

    def test_impossible_dates_are_not_dates(self):
        for text in ('2026-02-30', '2026-13-01', 'Feb 30, 2026', '2026年13月1日'):
            with self.subTest(text=text):
                self.assertEqual(read_published(text), '')

    def test_out_of_range_clock_falls_back_to_the_day(self):
        self.assertEqual(read_published('2026-10-08T25:00'), '2026-10-08')

    def test_plain_prose_and_version_numbers_are_not_dates(self):
        for text in ('', 'Announcements', 'Claude 3.5 Sonnet', 'v2.1.0', '1-2-3'):
            with self.subTest(text=text):
                self.assertEqual(read_published(text), '')

    def test_time_tag_beats_prose(self):
        html = ('<li><time datetime="2026-10-08T19:00:00Z">yesterday</time>'
                '<span>filed 2026-01-01</span></li>')
        from bs4 import BeautifulSoup
        node = BeautifulSoup(html, 'html.parser').find('li')
        # `<time datetime>` 是"这是个时间"的标准语义,排在正文里的数字前面。
        self.assertEqual(item_published(node), '2026-10-09 03:00')


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


class FieldTests(unittest.TestCase):
    """标题、角标、壳正文。"""

    def test_heading_tag_wins(self):
        got = parse_page_list(listing(
            [row('/news/a', 'Oct 8, 2026', 'ignored block'),
             row('/news/b', 'Oct 7, 2026', 'also ignored')],
            head=''), BASE)
        self.assertEqual(got[0]['title'], 'ignored block')   # 没有 h 标签时走最长块
        html = ('<html><body><main><ul>'
                '<li><a href="/news/a"><time>Oct 8, 2026</time>'
                '<h3>The heading</h3><span>a much longer trailing blurb here</span></a></li>'
                '<li><a href="/news/b"><time>Oct 7, 2026</time><h3>Other heading</h3></a></li>'
                '</ul></main></body></html>').encode()
        got = parse_page_list(html, BASE)
        self.assertEqual([e['title'] for e in got], ['The heading', 'Other heading'])

    def test_longest_block_wins_and_dates_are_excluded(self):
        got = parse_page_list(listing([row('/news/a', 'Oct 8, 2026', 'A properly long title'),
                                       row('/news/b', 'Oct 7, 2026', 'Another long title')]), BASE)
        self.assertEqual(got[0]['title'], 'A properly long title')
        self.assertNotIn('Oct 8', got[0]['title'])

    def test_a_blurb_is_not_a_title(self):
        # featured 卡片那种"标题 + 一长段摘要"的形态:超过 MAX_TITLE 的块不是标题。
        blurb = 'x' * (page_list.MAX_TITLE + 50)
        html = ('<html><body><main><ul>'
                f'<li><a href="/news/a"><time>Oct 8, 2026</time><span>Short title</span>'
                f'<p>{blurb}</p></a></li>'
                '<li><a href="/news/b"><time>Oct 7, 2026</time><span>Second title</span></a></li>'
                '</ul></main></body></html>').encode()
        got = parse_page_list(html, BASE)
        self.assertEqual(got[0]['title'], 'Short title')

    def test_subject_badge(self):
        from bs4 import BeautifulSoup
        node = BeautifulSoup('<li><time>Oct 8, 2026</time><span>Announcements</span>'
                             '<span>A real title here</span></li>', 'html.parser').find('li')
        self.assertEqual(item_subject(node, 'A real title here'), 'Announcements')

    def test_the_shell_does_not_repeat_the_title(self):
        # 面板上标题就显示在正文正上方,壳里再写一遍就是同一句话读两遍。
        body = shell_body('Announcements', '2026-10-08', 'https://e.org/news/a')
        self.assertNotIn('title', body.lower())
        self.assertIn('https://e.org/news/a', body)
        self.assertIn('Announcements', body)
        got = parse_page_list(listing([row('/news/a', 'Oct 8, 2026', 'A real title'),
                                       row('/news/b', 'Oct 7, 2026', 'Another title')]), BASE)
        self.assertNotIn('A real title', got[0]['body'])

    def test_the_shell_has_words_so_it_never_becomes_a_backfill_ghost(self):
        # 正文为空的条目会落进 `body_gap` 那个"需回填"的永动清单,每一轮同步
        # 重报一次(见 kb/extract.py 的 body_gap)。壳必须有字。
        got = parse_page_list(SAMPLE, BASE)
        for e in got:
            self.assertTrue(e['body'].strip())
            self.assertEqual(e['content_kind'], 'summary')

    def test_bare_url_shell_when_nothing_else_is_known(self):
        self.assertEqual(shell_body('', '', 'https://e.org/a'), 'https://e.org/a')


class ErrorTests(unittest.TestCase):
    """读不懂这一页时的语义:一律抛,不返回空列表。"""

    def test_no_list_raises_instead_of_returning_empty(self):
        # 静默返回空会让这条订阅从某天起永远是"今天没有新文章",而面板上一切
        # 正常(口径同 x_browser 那句 `if seen_articles and not parsed: raise`)。
        plain = b'<html><body><article><h1>One article</h1><p>Body text.</p></article></body></html>'
        with self.assertRaises(FeedError) as caught:
            parse_page_list(plain, BASE)
        self.assertIn('列表页', str(caught.exception))

    def test_a_feed_url_points_at_the_rss_source(self):
        feed = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
                b'<item><title>I</title><link>https://e.org/1</link></item>'
                b'</channel></rss>')
        with self.assertRaises(FeedError) as caught:
            parse_page_list(feed, BASE)
        self.assertIn('RSS', str(caught.exception))

    def test_a_declared_feed_is_named_in_the_error(self):
        html = ('<html><head><link rel="alternate" type="application/rss+xml" '
                'href="/feed.xml"></head><body><p>Nothing listed.</p></body></html>').encode()
        with self.assertRaises(FeedError) as caught:
            parse_page_list(html, BASE)
        self.assertIn('https://www.anthropic.com/feed.xml', str(caught.exception))

    def test_a_list_with_no_titles_raises(self):
        # 站点改版的形态:链接和日期还在,装标题的那一块没了。
        html = ('<html><body><main><ul>'
                '<li><a href="/news/a"><time>Oct 8, 2026</time></a></li>'
                '<li><a href="/news/b"><time>Oct 7, 2026</time></a></li>'
                '</ul></main></body></html>').encode()
        with self.assertRaises(FeedError) as caught:
            parse_page_list(html, BASE)
        self.assertIn('结构', str(caught.exception))

    def test_a_pdf_behind_the_url_raises(self):
        pdf = b'%PDF-1.4\n1 0 obj << /Type /Catalog >>\n' + b'x' * 800
        with self.assertRaises(FeedError):
            parse_page_list(pdf, BASE, {'content-type': 'application/pdf'})


class WiringTests(unittest.TestCase):
    """接进管道那一圈 —— 索引、检索、定时全靠 feed_types() 自动派生。"""

    def test_registered_everywhere_that_derives_from_feed_types(self):
        self.assertIn('web', feed_types())
        self.assertEqual(feed_types()['web'].adapter, 'page')
        self.assertIn('page', fetching.ADAPTERS)
        self.assertIn('web', reg.KINDS)
        self.assertIn('web', [s.name for s in config.SOURCES])
        self.assertEqual(config.SOURCE_GROUPS['web'], ['web'])
        self.assertEqual(config.SOURCE_ALIASES['网页专栏'], 'web')
        # vault 目录和别的来源并列,不嵌在 rss 下面。
        root = next(s.root for s in config.SOURCES if s.name == 'web')
        self.assertEqual(root, config.VAULT_ROOT / 'source' / 'web')

    def test_the_seeded_subscription_carries_fulltext(self):
        # 列表页只给得出一份壳,所以这条 defaults **必须**开着补抓 —— 不开的话
        # 新库里那条订阅收进来的是一堆只有链接的条目(见 `page_list.shell_body`)。
        # defaults 只对新库生效,所以这里直接读那份清单。
        import json
        seeds = json.loads((Path('kb/feeds/defaults.json')).read_text(encoding='utf-8'))
        web = [s for s in seeds if s['kind'] == 'web']
        self.assertTrue(web, 'defaults 里该有一条 web 示例,否则新库看到一个空来源')
        for seed in web:
            self.assertTrue(seed.get('fulltext'), f'{seed["name"]} 没开补抓')

    def test_the_interval_is_slower_than_rss(self):
        # 列表页更新慢,爬取又比读 feed 贵。
        self.assertGreater(feed_types()['web'].interval_minutes,
                           feed_types()['rss'].interval_minutes)

    def test_fetch_source_dispatches_to_the_page_adapter(self):
        source = {'kind': 'web', 'url': BASE, 'feed_url': '', 'resolved_url': '',
                  'etag': '', 'modified': ''}
        with patch.object(fetching, 'request', Mock(return_value=resp(SAMPLE))) as req:
            got, meta = fetching.fetch_source(source, {}, unconditional=True)
        self.assertEqual(req.call_args[0][0], BASE)
        self.assertEqual(len([e for e in got if '/news/' in e['url']]), 10)
        self.assertEqual(meta['resolved_url'], BASE)

    def test_rss_still_goes_through_the_feed_parser(self):
        # 反过来钉一边:新 adapter 不许把现有两条线的分派带走。
        feed = (b'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>'
                b'<item><title>Post</title><link>https://e.org/1</link>'
                b'<description>Body here.</description>'
                b'<pubDate>Wed, 08 Oct 2026 09:00:00 +0800</pubDate></item></channel></rss>')
        source = {'kind': 'rss', 'url': 'https://e.org/feed.xml', 'feed_url': '',
                  'resolved_url': '', 'etag': '', 'modified': ''}
        with patch.object(fetching, 'request', Mock(return_value=resp(feed))):
            got, _ = fetching.fetch_source(source, {}, unconditional=True)
        self.assertEqual([e['title'] for e in got], ['Post'])


class EndToEndTests(unittest.TestCase):
    """真走一轮采集 + 补抓,断言库里那条是什么。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        # 维护锁的 lockfile 和 kb.db 都要指到 tmp,否则这些用例会 flock 生产
        # 那一个、写生产索引库(见 kb.maintenance 与 test_feed_fulltext)。
        for p in (patch.object(reg, 'DB_PATH', self.root / 'feeds.db'),
                  patch.object(maintenance, 'DATA_DIR', self.root),
                  patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                  patch.object(sync, 'VAULT_ROOT', self.root / 'vault'),
                  patch.object(translate, 'available', lambda: False)):
            p.start(); self.addCleanup(p.stop)
        # 用 defaults 播种的那一条,不另建:自己建会和它撞 URL(说明 defaults
        # 真生效了),而且跑真实那条比跑一条摆出来的更有说服力。
        self.source = next(s for s in reg.subscriptions('web') if 'anthropic.com' in s['url'])
        self.assertTrue(self.source['fulltext'], 'defaults 那条该开着补抓')

    def key(self, url: str) -> str:
        return hashlib.sha256(f'web:{url}'.encode()).hexdigest()[:32]

    def row(self, key: str) -> dict:
        with reg.db() as c:
            return dict(c.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone())

    def test_a_listing_becomes_items_and_fulltext_fills_the_body(self):
        page = listing([row('/news/cyber', f'{today(1)}', 'Introducing the Anthropic Cyber Mission'),
                        row('/news/policy', f'{today(2)}', 'Usage Policy update')])
        with patch.object(fetching, 'request', Mock(return_value=resp(page))):
            summary = sync.collect('web', FetchScope())
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['added'], 2)

        key = self.key('https://www.anthropic.com/news/cyber')
        before = self.row(key)
        self.assertIn('https://www.anthropic.com/news/cyber', before['body'])
        self.assertEqual(before['content_kind'], 'summary')
        self.assertEqual(before['fulltext_at'], '')

        with patch.object(fulltext, 'request', Mock(return_value=resp(ARTICLE))):
            done = fulltext.fetch_pending('web')
        self.assertEqual(done['failed'], 0)
        after = self.row(key)
        self.assertIn('frontier models in the hands of cyber defenders', after['body'])
        self.assertIn('open-source maintainers', after['body'])
        for furniture in ('Home', '© 2026 Anthropic'):
            self.assertNotIn(furniture, after['body'])
        self.assertEqual(after['content_kind'], 'content')
        self.assertTrue(after['fulltext_at'])

    def test_history_floor_applies_to_this_kind_too(self):
        page = listing([row('/news/fresh', today(1), 'Fresh enough'),
                        row('/news/old', '2024-04-10', 'Ancient archive piece'),
                        row('/news/older', '2023-01-01', 'Even older')])
        with patch.object(fetching, 'request', Mock(return_value=resp(page))):
            sync.collect('web', FetchScope())
        with reg.db() as c:
            titles = [r['title'] for r in c.execute(
                'SELECT title FROM feed_items WHERE kind=?', ('web',)).fetchall()]
        self.assertEqual(titles, ['Fresh enough'])

    def test_a_site_redesign_reports_an_error_instead_of_going_quiet(self):
        # **这是这条来源独立成一类的理由**:读 feed 坏在对方服务,解析页面还会
        # 坏在对方改版 —— 而"今天没有新文章"和"再也解析不出来"长得一模一样。
        with patch.object(fetching, 'request',
                          Mock(return_value=resp(b'<html><body><p>Redesigned.</p></body></html>'))):
            summary = sync.collect('web', FetchScope())
        self.assertTrue(summary['errors'], '改版之后这一轮必须报错')
        self.assertIn('列表页', ' '.join(e['reason'] for e in summary['errors']))
        after = reg.subscription('web', self.source['id'])
        self.assertTrue(after['last_error'])


if __name__ == '__main__':
    unittest.main()
