# -*- coding: utf-8 -*-
"""简报通道:一期一个汇总页,页里并列着几十条带外链的简讯。

判据一律**先验后果** —— 只断言"拆出几条"的话,一个把简讯换成广告的实现也能
凑够数;所以涉及内容的几条都点名具体那一条。
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from kb.collectors import FetchScope
from kb.feeds import digest, fetching
from kb.feeds.fetching import FeedError
from kb.source_types import feed_types

HERE = pathlib.Path(__file__).parent
SAMPLE = (HERE / 'digest_sample.html').read_bytes()
NOT_YET = (HERE / 'digest_notyet.html').read_bytes()
HOUSE_AD = (HERE / 'digest_housead.html').read_bytes()
ISSUE = 'https://tldr.tech/ai/2026-10-09'
FEED = 'https://tldr.tech/api/rss/ai'
DAY = '2026-10-09 08:00'


def feed_of(*items: tuple[str, str]) -> bytes:
    """`(期号地址的末段, 日期或空串)` → 期号清单。

    日期给空串 = **feed 没给 `pubDate`**;这时"地址末段像不像一个日期"就决定了
    `digest._url_day` 认不认得出来 —— `('2026-10-08', '')` 是 TLDR 漏一期日期
    的形态(地址里还有),`('issue-7', '')` 是两头都没有日期的形态。
    """
    xml = ''
    for slug, day in items:
        pub = f'<pubDate>{day[8:10]} Oct 2026 00:00:00 GMT</pubDate>' if day else ''
        xml += (f'<item><title>Issue {slug}</title>'
                f'<link>https://tldr.tech/ai/{slug}</link>'
                f'<guid>https://tldr.tech/ai/{slug}</guid>{pub}</item>')
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>TLDR AI</title>'
            f'<link>https://tldr.tech</link>{xml}</channel></rss>').encode()


def rss(*days: str) -> bytes:
    return feed_of(*((d, d) for d in days))


def page(blocks, *, host='tldr.tech') -> bytes:
    body = ''.join(
        f'<article><a href="{href}"><h3>{title}</h3></a><div>{text}</div></article>'
        for href, title, text in blocks)
    return (f'<html><body><nav><a href="https://{host}/">Home</a></nav>'
            f'<div><div><section>{body}</section></div></div></body></html>').encode()


def block(n, *, title=None, host='example.org', text=None):
    return (f'https://{host}/{n}', title or f'Story {n}',
            text or ('Body text for story %s. ' % n) * 8)


# 每期三条**各自不同链接**的简讯。真样本(SAMPLE)每期的外链是同一批,而
# `put_items` 按 `sha256(kind:url)` 去重 —— 拿它当多期样本的话"第二期一条都
# 没新增"会被误读成去重正常,正好把漏期这件事盖掉。
def issue_page(day: str, n: int = 3) -> bytes:
    return page([block(f'{day}-{i}') for i in range(n)])


DAYS9 = [f'2026-10-{d:02d}' for d in range(1, 10)]


class SplitTests(unittest.TestCase):
    """拆块的判据:并列的、结构相同的块,每块一个外链 + 一段够长的文字。"""

    def test_the_real_issue_page(self):
        got = digest.split_issue(SAMPLE, ISSUE, DAY)
        titles = [e['title'] for e in got]
        self.assertEqual(len(got), 17)       # 页面 19 块,两条赞助位不收
        self.assertIn('Gemini Agent (18 minute read)', titles)
        self.assertIn('Why is Speculative Decoding Fast? (4 minute read)', titles)
        for e in got:
            self.assertEqual(e['published'], DAY)
            # 比**主机名**,不比整条地址:招聘位的链接是
            # `jobs.ashbyhq.com/tldr.tech/...`,路径里带着这几个字。
            self.assertNotEqual(urlsplit(e['url']).hostname, 'tldr.tech')
            self.assertGreaterEqual(len(e['body']), 40)

    def test_the_body_is_the_blurb_not_just_the_headline(self):
        """容器取**最外层**那一层。`<article>` 和它里面的 `<a>` 同样满足条件,
        可 `<a>` 里只有标题 —— 取错一层的后果是每条正文都等于它的标题,而
        `page_list` 那套"链接的记录容器"正好会取到 `<a>`(实测:TLDR 有 3 个
        article 带第二个链接,`seen>1` 让容器停在 a 上,正文整个丢了)。"""
        got = {e['title']: e for e in digest.split_issue(SAMPLE, ISSUE, DAY)}
        one = got['Gemini Agent (18 minute read)']
        self.assertIn('Gemini', one['body'])
        self.assertNotEqual(one['body'], one['title'])
        self.assertGreater(len(one['body']), 150)

    def test_the_blurb_survives_when_the_link_has_its_own_teaser(self):
        """链接自己带一段引言时,`<div>` 里那段正文照样要收全。

        **如实记一笔**:这一条守住的是 `_outbound`(内层链接节点自己不算自己
        的后代,过不了"有外链"那一关),不是 `split_issue` 里那句"取最外层"的
        排序 —— 定点注入"取最内层"在这条上实测红 0 条,见 digest.py 里那段
        注释。保留它是因为"正文不许只剩标题"本身值得钉。
        """
        blocks = ''.join(
            f'<article><a href="https://example.org/{n}"><h3>Story {n}</h3>'
            f'<p>{"Teaser line. " * 10}</p></a>'
            f'<div>{"The real blurb body. " * 12}</div></article>'
            for n in range(4))
        got = digest.split_issue(
            f'<html><body><div><div><section>{blocks}</section></div></div>'
            f'</body></html>'.encode(), ISSUE, DAY)
        self.assertEqual(len(got), 4)
        for e in got:
            with self.subTest(e['title']):
                self.assertIn('The real blurb body', e['body'])

    def test_one_house_ad_does_not_void_the_whole_issue(self):
        """**真实世界的样本:2026-10-08 那一期。**

        它夹着一条 TLDR 自家的广告(链接指向 `advertise.tldr.tech`,是**站内
        子域**、过不了"外链"那一关)。判据原来要求组里**每一块都合格**,于是
        这一整期 18 条**一条都拆不出来** —— 真网络端到端实测时撞上的,面板上
        只会看到这一期失败,而下一期又好了。现在是"绝大多数合格就算简讯区,
        合格的收下、不合格的跳过",那条广告顺带自己就不在结果里。
        """
        got = digest.split_issue(HOUSE_AD, 'https://tldr.tech/ai/2026-10-08',
                                 '2026-10-08 08:00')
        self.assertEqual(len(got), 16)
        titles = [e['title'] for e in got]
        self.assertIn('Introducing Claude Haiku 5.5 (5 minute read)', titles)
        for e in got:
            with self.subTest(e['title'][:40]):
                self.assertNotIn('advertise.tldr.tech', e['url'])

    def test_a_group_that_is_mostly_unqualified_is_not_a_digest(self):
        """反过来钉一边:比例那道闸还得挡得住"恰好有几个外链的导航区"。"""
        good = [block(n) for n in range(2)]
        junk = [(f'https://example.org/x{n}', f'Nav {n}', 'tiny') for n in range(6)]
        with self.assertRaises(FeedError):
            digest.split_issue(page(good + junk), ISSUE, DAY)

    def test_sponsored_blocks_are_dropped_by_name(self):
        got = [e['title'] for e in digest.split_issue(SAMPLE, ISSUE, DAY)]
        for ad in ("Enterprise search used to be a list of links. Now it's a list of "
                   "actions (Sponsor)",
                   'AI copilots don’t understand enterprise. Redwood RangerAI does (Sponsor)'):
            self.assertNotIn(ad, got)
        self.assertFalse([t for t in got if t.endswith('(Sponsor)')])

    def test_the_job_slot_is_kept_on_purpose(self):
        """**反过来钉一边。** 招聘位没有任何稳定标记,靠招聘平台域名去认就是
        一张要维护的名单;多收一条,比"哪天换个平台就静默漏判"便宜。改成特判
        之前先想清楚判据是什么。"""
        got = [e for e in digest.split_issue(SAMPLE, ISSUE, DAY)
               if 'ashbyhq.com' in e['url']]
        self.assertEqual(len(got), 1)
        self.assertIn('Applied AI at TLDR', got[0]['title'])

    def test_ad_markers_are_matched_by_wording_not_by_host(self):
        for mark in ('(Sponsor)', '(Sponsored)', '[AD]', '（广告）', '(advertisement)'):
            with self.subTest(mark):
                got = digest.split_issue(
                    page([block(1), block(2), block(3),
                          block(9, title=f'Buy our thing {mark}')]), ISSUE, DAY)
                self.assertEqual(len(got), 3)
                self.assertNotIn('Buy our thing', ' '.join(e['title'] for e in got))

    def test_links_back_to_the_newsletter_itself_are_not_stories(self):
        """简报的每一条都是"荐读别人家的文章";站内的导航、往期链接不是内容。"""
        with self.assertRaises(FeedError):
            digest.split_issue(page([block(1, host='tldr.tech'),
                                     block(2, host='tldr.tech'),
                                     block(3, host='tldr.tech')]), ISSUE, DAY)

    def test_tracking_params_are_stripped_from_the_story_link(self):
        got = digest.split_issue(page([
            ('https://example.org/a?utm_source=tldrai&id=7', 'A story', 'Text. ' * 30),
            block(2), block(3)]), ISSUE, DAY)
        self.assertEqual(got[0]['url'], 'https://example.org/a?id=7')

    def test_the_same_story_twice_in_one_issue_lands_once(self):
        got = digest.split_issue(page([block(1), block(1, title='Repeat'), block(2),
                                       block(3)]), ISSUE, DAY)
        self.assertEqual(len(got), 3)

    def test_an_issue_that_is_not_out_yet_raises(self):
        """落地页对还没发的那一期回落成**订阅介绍页**(HTTP 200,不是 404)。
        静默返回空会让这条订阅从某天起永远是"今天没有新内容",而面板上一切
        正常 —— 站点改版也是同一个形态,两种都要人看一眼。"""
        with self.assertRaises(FeedError) as got:
            digest.split_issue(NOT_YET, 'https://tldr.tech/ai/2026-10-10', DAY)
        self.assertIn('这一期还没发布', str(got.exception))

    def test_two_blocks_are_not_a_digest(self):
        with self.assertRaises(FeedError):
            digest.split_issue(page([block(1), block(2)]), ISSUE, DAY)

    def test_short_blocks_are_not_stories(self):
        with self.assertRaises(FeedError):
            digest.split_issue(page([(f'https://example.org/{n}', f'T{n}', 'tiny')
                                     for n in range(5)]), ISSUE, DAY)

    def test_a_pdf_behind_the_issue_link_raises(self):
        with self.assertRaises(FeedError):
            digest.split_issue(b'%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\n' + b'x' * 900,
                               ISSUE, DAY)


class WatermarkTests(unittest.TestCase):
    """水位记的是"收到哪一期了",一期是原子的。"""

    def setUp(self):
        digest.reset()
        self.addCleanup(digest.reset)

    def run_prefetch(self, source, *, days=('2026-10-07', '2026-10-08', '2026-10-09'),
                     broken=()):
        seen = []

        def fake(url, headers=None, **kw):
            seen.append(url)
            if 'api/rss' in url:
                return fetching.Response(status=200, url=url, body=rss(*days), headers={})
            if any(url.endswith(d) for d in broken):
                raise OSError('boom')
            return fetching.Response(status=200, url=url, body=SAMPLE, headers={})

        with patch.object(digest, 'request', fake):
            out = digest.prefetch([source], {})
        return out[source['id']], seen

    def run_days(self, source, days, **kw):
        """每期一份**自己的**内容(见 `issue_page`),返回请求过的期号页面。"""
        seen = []

        def fake(url, headers=None, **kwargs):
            if 'api/rss' in url:
                return fetching.Response(status=200, url=url, body=rss(*days), headers={})
            seen.append(url)
            return fetching.Response(status=200, url=url,
                                     body=issue_page(url.rsplit('/', 1)[-1]), headers={})

        with patch.object(digest, 'request', fake):
            out = digest.prefetch([source], {}, **kw)
        return out[source['id']], seen

    def source(self, water='', sid=1, floor=''):
        return {'id': sid, 'kind': 'digest', 'url': FEED, 'etag': '', 'modified': '',
                'resolved_url': '', 'history_floor': floor,
                'config': json.dumps({'digest_issue': water} if water else {})}

    def test_only_issues_newer_than_the_watermark_are_fetched(self):
        # 水位和期号都按项目时区(`parse_feed` 把 GMT 换算成 Asia/Shanghai),
        # 所以 10-08 那期的时刻是 08:00 —— 写 00:00 的话它还算"比水位新"。
        got, seen = self.run_prefetch(self.source('2026-10-08 08:00'))
        pages = [u for u in seen if 'api/rss' not in u]
        self.assertEqual(pages, ['https://tldr.tech/ai/2026-10-09'])
        self.assertEqual(got['watermark'], '2026-10-09 08:00')

    def test_a_failed_issue_stops_the_watermark_there(self):
        """**后果先断言**:水位越过没拿到的那一期,中间那期从此任何一轮增量
        都选不到(口径同 x_feedgrab 那条"没抓全就不许推进水位")。"""
        got, _ = self.run_prefetch(self.source(), broken=('2026-10-08',))
        self.assertEqual(got['watermark'], '2026-10-07 08:00')
        self.assertIn('incomplete', got)
        self.assertTrue(got['entries'])

    def test_nothing_new_leaves_the_watermark_alone(self):
        got, seen = self.run_prefetch(self.source('2026-10-09 08:00'))
        self.assertEqual([u for u in seen if 'api/rss' not in u], [])
        self.assertEqual(got['watermark'], '2026-10-09 08:00')

    def test_the_first_issue_failing_keeps_the_old_watermark(self):
        got, _ = self.run_prefetch(self.source('2026-10-06 08:00'),
                                   broken=('2026-10-07', '2026-10-08', '2026-10-09'))
        self.assertEqual(got['watermark'], '2026-10-06 08:00')
        self.assertEqual(got['entries'], [])

    def test_at_most_max_issues_per_round(self):
        days = [f'2026-09-{d:02d}' for d in range(10, 30)]
        _, seen = self.run_prefetch(self.source(), days=days)
        self.assertEqual(len([u for u in seen if 'api/rss' not in u]), digest.MAX_ISSUES)

    def test_the_round_budget_truncates_the_new_end_not_the_old_one(self):
        """**后果先断言**:水位只能连续推进,所以额度必须截掉**新**的那一头。

        照 RSS 的新到旧顺序先截再排的话,取到的是最新六期、水位跟着推到最新,
        被截掉的三期从此任何一轮增量都选不到(2026-10-10 审查那份证据)。
        """
        got, seen = self.run_days(self.source(), DAYS9)
        self.assertEqual([u.rsplit('/', 1)[-1] for u in seen], DAYS9[:digest.MAX_ISSUES])
        self.assertEqual(got['watermark'], '2026-10-06 08:00')

    def test_the_leftover_issues_are_spelled_out(self):
        """"攒了九期只读了六期"和"全读完了"在面板上不该长得一模一样。"""
        got, _ = self.run_days(self.source(), DAYS9)
        self.assertIn('还有 3 期没读', got['incomplete'])
        self.assertIn('2026-10-07', got['incomplete'])

    def test_reading_everything_leaves_no_note(self):
        got, _ = self.run_days(self.source(), DAYS9[:3])
        self.assertNotIn('incomplete', got)

    def test_a_ranged_backfill_ignores_the_daily_watermark(self):
        """区间补抓读的是用户点名的那段日期,日常水位可能早就越过它了。"""
        got, seen = self.run_days(self.source('2026-10-09 08:00'), DAYS9,
                                  since='2026-10-01', until='2026-10-03')
        self.assertEqual([u.rsplit('/', 1)[-1] for u in seen], DAYS9[:3])
        self.assertTrue(got['entries'])

    def test_a_ranged_backfill_does_not_touch_the_daily_watermark(self):
        got, _ = self.run_days(self.source('2026-10-05 08:00'), DAYS9,
                               since='2026-10-01', until='2026-10-03')
        # 空串 = 不产生补丁,所以日常那一侧一个字都没动(第二道在
        # `sync._save_result` 重建 validators 那一句)。
        self.assertEqual(got['watermark'], '')
        self.assertEqual(digest.watermark_patch(got['watermark']), {})

    def test_a_ranged_backfill_rereads_a_day_the_daily_round_already_took(self):
        """审查 CASE 3:删掉已收录的那一期再补抓当天,得真去读那一页。"""
        _, seen = self.run_days(self.source('2026-10-09 08:00'), DAYS9,
                                since='2026-10-09', until='2026-10-09')
        self.assertEqual([u.rsplit('/', 1)[-1] for u in seen], ['2026-10-09'])

    def test_a_ranged_backfill_says_how_to_get_the_rest(self):
        """区间那一侧不推水位,下一轮不会自动接上 —— 重复点同一个区间永远只
        读最旧那几期,所以提示必须给出可操作的下一步。"""
        got, _ = self.run_days(self.source(), DAYS9, since='2026-10-01', until='2026-10-09')
        self.assertIn('还有 3 期没读', got['incomplete'])
        self.assertIn('把起始日期改到 2026-10-07', got['incomplete'])

    def test_a_range_older_than_the_feed_says_why_nothing_came_back(self):
        """期号清单只能从 RSS 拿,它只留最近几十期 —— 更早的日期无从抓起。
        不说的话面板上就是"新增 0 条、同步已完成",和"筛选把它排空了"同形。"""
        got, seen = self.run_days(self.source(), DAYS9,
                                  since='2026-09-01', until='2026-09-05')
        self.assertEqual(seen, [])
        self.assertIn('不在简报还列着的 9 期里', got['incomplete'])
        self.assertIn('2026-10-01', got['incomplete'])

    def test_issues_below_the_history_floor_are_not_even_requested(self):
        """下界之前的期号无论如何都入不了库(`sync._within_history` 会丢掉),
        而这条通道是"一期一次 HTTP" —— 不筛的话新订阅第一轮的额度全花在下界
        之前那几期上,一条都入不了库,要连抓三轮才够到今天的内容。"""
        _, seen = self.run_days(self.source(floor='2026-10-07'), DAYS9)
        self.assertEqual([u.rsplit('/', 1)[-1] for u in seen], DAYS9[6:])

    def test_a_ranged_backfill_is_the_escape_hatch_from_the_history_floor(self):
        """反过来钉一边:区间是那道闸的逃生口(口径同 `_within_history`)。"""
        _, seen = self.run_days(self.source(floor='2026-10-07'), DAYS9,
                                since='2026-10-01', until='2026-10-02')
        self.assertEqual([u.rsplit('/', 1)[-1] for u in seen], DAYS9[:2])

    def test_a_daily_round_still_filters_by_the_watermark(self):
        """反过来钉一边:只给一头日期不算区间,日常增量照旧。"""
        _, seen = self.run_days(self.source('2026-10-07 08:00'), DAYS9, since='2026-10-01')
        self.assertEqual([u.rsplit('/', 1)[-1] for u in seen], ['2026-10-08', '2026-10-09'])

    def test_take_hands_the_watermark_back_as_a_config_patch(self):
        """水位和条目要在**同一个事务**里提交,所以它是交回去的一份补丁,不是
        当场写盘(见 `sync._save_result`)。"""
        source = self.source()
        self.run_prefetch(source)
        entries, meta = digest.take(source, {})
        self.assertTrue(entries)
        self.assertEqual(meta['config_patch'], {'digest_issue': '2026-10-09 08:00'})

    def test_take_without_a_prefetch_does_not_look_like_an_empty_issue(self):
        with self.assertRaises(LookupError):
            digest.take(self.source(), {})

    def test_a_broken_feed_fails_only_that_subscription(self):
        def fake(url, headers=None, **kw):
            if 'api/rss' in url and url.endswith('bad'):
                raise OSError('nope')
            if 'api/rss' in url:
                return fetching.Response(status=200, url=url, body=rss('2026-10-09'), headers={})
            return fetching.Response(status=200, url=url, body=SAMPLE, headers={})

        bad = dict(self.source(sid=2), url=FEED + '/bad')
        with patch.object(digest, 'request', fake):
            out = digest.prefetch([bad, self.source(sid=3)], {})
        self.assertIn('error', out[2])
        self.assertTrue(out[3]['entries'])


class WiringTests(unittest.TestCase):
    """接线:注册、分派、默认订阅。"""

    def test_registered_everywhere_that_derives_from_feed_types(self):
        from kb import config
        from kb.collectors import _REGISTRY
        from kb.feeds import registry as reg
        self.assertIn('digest', feed_types())
        self.assertIn('digest', _REGISTRY)
        self.assertIn('digest', reg.KINDS)
        self.assertIn('digest', [x.name for x in config.SOURCES])

    def test_fetch_source_takes_the_prefetched_batch(self):
        digest.reset()
        self.addCleanup(digest.reset)
        source = {'id': 7, 'kind': 'digest', 'url': FEED, 'etag': '', 'modified': '',
                  'resolved_url': '', 'config': '{}'}

        def fake(url, headers=None, **kw):
            body = rss('2026-10-09') if 'api/rss' in url else SAMPLE
            return fetching.Response(status=200, url=url, body=body, headers={})

        with patch.object(digest, 'request', fake):
            digest.prefetch([source], {})
        # 真走 `fetch_source`:它开头那道分派要把这条订阅交给 take,而不是
        # 去 `ADAPTERS` 里找一个叫 digest 的 adapter(那里没有)。
        with patch.object(fetching, 'request', side_effect=AssertionError('不该再发 HTTP')):
            entries, meta = fetching.fetch_source(source, {})
        self.assertEqual(len(entries), 17)
        self.assertIn('config_patch', meta)

    def test_the_default_subscription_does_not_turn_on_fulltext(self):
        """那个外链指向的是**别人家的原文**,抓回来就不是这条简讯了。"""
        defaults = json.loads((pathlib.Path(digest.__file__).parent
                               / 'defaults.json').read_text())
        rows = [s for s in defaults if s['kind'] == 'digest']
        self.assertEqual([s['name'] for s in rows], ['TLDR AI'])
        self.assertFalse(rows[0].get('fulltext'))

    def test_slower_than_rss(self):
        self.assertGreater(feed_types()['digest'].interval_minutes,
                           feed_types()['rss'].interval_minutes)


class DigestDbCase(unittest.TestCase):
    """真走一轮 `sync.collect`、落进 feeds.db 的摆盘。

    **自己不带 `test_*`** —— `tests/test_digest_undated.py` 复用它(各写一份的话
    两边的前提会各漂各的,而带 `test_*` 的基类会被 unittest 跑两遍)。
    """

    def setUp(self):
        import kb.feeds.registry as reg
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(self.tmp, ignore_errors=True))
        self.old, reg.DB_PATH = reg.DB_PATH, self.tmp / 'feeds.db'
        self.addCleanup(lambda: setattr(reg, 'DB_PATH', self.old))
        self.reg = reg
        digest.reset()
        self.addCleanup(digest.reset)
        with reg.db() as c:
            c.execute("UPDATE subscriptions SET history_floor='2026-09-01' WHERE kind='digest'")

    def collect(self):
        from kb.feeds import sync

        def fake(url, headers=None, **kw):
            body = rss('2026-10-08', '2026-10-09') if 'api/rss' in url else SAMPLE
            return fetching.Response(status=200, url=url, body=body, headers={})

        with patch.object(digest, 'request', fake), patch.object(fetching, 'request', fake):
            return sync.collect('digest', FetchScope())

    def collect_feed(self, feed, scope=FetchScope()):
        """每期一份自己的内容,返回 `(summary, 请求过的期号页面)`。"""
        from kb.feeds import sync
        seen = []

        def fake(url, headers=None, **kw):
            if 'api/rss' in url:
                return fetching.Response(status=200, url=url, body=feed, headers={})
            seen.append(url.rsplit('/', 1)[-1])
            return fetching.Response(status=200, url=url,
                                     body=issue_page(url.rsplit('/', 1)[-1]), headers={})

        with patch.object(digest, 'request', fake), patch.object(fetching, 'request', fake):
            return sync.collect('digest', scope), seen

    def collect_days(self, days, scope=FetchScope()):
        return self.collect_feed(rss(*days), scope)

    def config(self) -> dict:
        with self.reg.db() as c:
            row = c.execute("SELECT config FROM subscriptions WHERE kind='digest'").fetchone()
        return json.loads(row[0] or '{}')

    def stored(self):
        with self.reg.db() as c:
            return sorted(r[0] for r in c.execute(
                "SELECT url FROM feed_items WHERE kind='digest'"))

    def water(self):
        with self.reg.db() as c:
            cfg = c.execute("SELECT config FROM subscriptions WHERE kind='digest'").fetchone()[0]
        return (json.loads(cfg) or {}).get('digest_issue', '')


class EndToEndTests(DigestDbCase):
    """真走一轮 `sync.collect`,落进 feeds.db。"""

    def test_stories_land_with_their_blurb_and_the_watermark_moves(self):
        summary = self.collect()
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['added'], 17)
        with self.reg.db() as c:
            rows = c.execute("SELECT title,url,body FROM feed_items WHERE kind='digest'").fetchall()
            cfg = c.execute("SELECT config FROM subscriptions WHERE kind='digest'").fetchone()[0]
        self.assertEqual(json.loads(cfg)['digest_issue'], '2026-10-09 08:00')
        one = [r for r in rows if 'Gemini Agent' in r['title']]
        self.assertEqual(len(one), 1)
        self.assertIn('Gemini', one[0]['body'])
        self.assertNotIn('tldr.tech', one[0]['url'])

    def test_a_second_round_re_reads_nothing(self):
        self.collect()
        self.assertEqual(self.collect()['added'], 0)

    def test_a_backlog_is_caught_up_round_by_round_without_losing_the_oldest(self):
        """**后果先断言**:九期积压分两轮收完,一期都不许静默消失。

        2026-10-10 审查那份证据的反面 —— 当时第一轮收最新六期(18 条)、水位
        推到 10-09,第二轮一期页面都不请求、新增 0 条,10-01~10-03 那九条简讯
        永久缺失。
        """
        first, seen1 = self.collect_days(DAYS9)
        self.assertEqual(first['errors'], [])
        self.assertEqual(first['added'], 18)
        self.assertEqual(seen1, DAYS9[:6])
        self.assertEqual(len(first['partial']), 1)
        self.assertIn('还有 3 期没读', first['partial'][0]['reason'])

        second, seen2 = self.collect_days(DAYS9)
        self.assertEqual(second['added'], 9)
        self.assertEqual(seen2, DAYS9[6:])
        self.assertEqual(self.water(), '2026-10-09 08:00')
        self.assertEqual(self.stored(),
                         sorted(f'https://example.org/{d}-{i}' for d in DAYS9 for i in range(3)))
        # 读完了就不该再留着"还没读完"那句提示。
        self.assertEqual(second['partial'], [])

    def test_a_ranged_backfill_reaches_days_the_daily_watermark_passed(self):
        """审查 CASE 2:日常水位已经越过那段日期,用户点名补抓时得真去读。"""
        self.collect_days(DAYS9[6:])                       # 日常先收最新三期
        self.assertEqual(self.water(), '2026-10-09 08:00')
        summary, seen = self.collect_days(
            DAYS9, FetchScope(since='2026-10-01', until='2026-10-03'))
        self.assertEqual(summary['errors'], [])
        self.assertEqual(summary['added'], 9)
        self.assertEqual(seen, DAYS9[:3])
        for d in DAYS9[:3]:
            self.assertIn(f'https://example.org/{d}-0', self.stored())
        # 区间补抓不改日常增量的任何状态。
        self.assertEqual(self.water(), '2026-10-09 08:00')

    def test_a_ranged_backfill_can_bring_back_a_day_that_was_deleted(self):
        """审查 CASE 3:收过、又被清掉的那一天,点名补抓要能回来。"""
        self.collect_days(DAYS9[8:])
        with self.reg.db() as c:
            # 顺序同 `scripts/feed_trim_history._cut_feeds`:先摘归属再删条目。
            c.execute("DELETE FROM subscription_items WHERE item_key IN "
                      "(SELECT key FROM feed_items WHERE kind='digest')")
            c.execute("DELETE FROM feed_items WHERE kind='digest'")
        self.assertEqual(self.stored(), [])
        summary, seen = self.collect_days(
            DAYS9, FetchScope(since='2026-10-09', until='2026-10-09'))
        self.assertEqual(seen, ['2026-10-09'])
        self.assertEqual(summary['added'], 3)
        self.assertEqual(self.stored(),
                         sorted(f'https://example.org/2026-10-09-{i}' for i in range(3)))
