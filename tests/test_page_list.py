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
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kb import config, maintenance
from kb.collectors import FetchScope
from kb.feeds import fetching, fulltext, page_list, registry as reg, sync, translate
from kb.feeds.fetching import FeedError
from kb.feeds.page_list import (item_published, item_subject, parse_page_list,
                                read_published, shell_body)
from kb.source_types import feed_types

from tests.page_list_fixtures import (ARTICLE, BASE, SAMPLE, listing, resp,
                                      row, rows_listing, today)


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

    def test_a_time_tag_does_not_read_its_own_datetime_attribute(self):
        """问一个 `<time>` 节点自己,`datetime` 属性**不算** —— 既有口径。

        原实现问的是 `node.find_all('time')`,而 bs4 的 `find_all` 只找
        **后代**。所以对一个文字为空、日期全在属性里的 `<time>` 节点,答案是
        空串,不是那个属性值。

        **这一条是冗余带**:`item_published` 在解析里只被用来问"记录容器"和
        它的祖先,而容器是链接的独占祖先或链接自己,永远不会是 `<time>`。
        留着是因为它是公开函数(别处可以拿任意节点来问),而把节点自己算进去
        这个差错在改日期查询实现时真的犯过一次 —— 逐节点差分在 400 份随机
        输入上逮到 81 处,全是这一类。
        """
        from bs4 import BeautifulSoup
        node = BeautifulSoup('<time datetime="2026-10-20"></time>', 'html.parser').find('time')
        self.assertEqual(item_published(node), '')


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

    def test_a_redesigned_row_lands_the_article_not_its_category_badge(self):
        """**先断言后果:库里那条是什么。**

        只验解析结果的话,"分类角标不是记录"这件事还可能在落库那一圈被别的
        东西带回来(壳正文里带着角标地址、补抓顺着它去读)。这里真跑一轮
        采集 + 补抓,断言库里那条是文章、而角标那个地址压根没有行。
        """
        page = rows_listing([row('/news/cyber', today(1), 'Introducing the Anthropic Cyber Mission'),
                             row('/news/midterms', today(2), 'Preparing for the midterms')])
        with patch.object(fetching, 'request', Mock(return_value=resp(page))):
            summary = sync.collect('web', FetchScope())
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['added'], 2, '两篇文章,角标不算一条')

        with reg.db() as c:
            urls = [r['url'] for r in c.execute(
                'SELECT url FROM feed_items WHERE kind=?', ('web',)).fetchall()]
        self.assertEqual(sorted(urls), ['https://www.anthropic.com/news/cyber',
                                        'https://www.anthropic.com/news/midterms'])

        key = self.key('https://www.anthropic.com/news/cyber')
        before = self.row(key)
        self.assertEqual(before['title'], 'Introducing the Anthropic Cyber Mission')
        self.assertIn('Announcements', before['body'])     # 角标是个字段,写进壳里
        with patch.object(fulltext, 'request', Mock(return_value=resp(ARTICLE))):
            done = fulltext.fetch_pending('web')
        self.assertEqual(done['failed'], 0)
        self.assertIn('frontier models in the hands of cyber defenders',
                      self.row(key)['body'])

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
