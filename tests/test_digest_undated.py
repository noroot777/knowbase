# -*- coding: utf-8 -*-
"""简报缺日期的那一期:**不许静默消失**。

**按形状单独一个文件**:和上面那一组挤在 `tests/test_digest.py` 里是 764 行、
越过 700 行上限;摆盘(`feed_of`/`issue_page`/`DigestDbCase`)仍复用那一份,各写
一份的话两边的前提会各漂各的。

日期在这条通道上是承重的 —— 选期、历史下界、增量水位全靠它。原来空日期被下界
和水位两个条件**一起**剔掉,而水位又照常推进到更新的那一期:那一期简报静默消失,
源站之后把日期补上也补不回来(水位已经越过它),而面板上 `errors`/`partial`/
`last_note` 全是空(2026-10-10 第 2 轮审查)。

那份已读名单自己又带出第 3 轮那条:它原来按**容量**淘汰,而被淘汰的地址仍然
列在 RSS 里 —— 于是真正未读的末期被永久饿死(见 `taken_patch`)。
"""

from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from kb.feeds import digest, fetching
from tests.test_digest import FEED, DigestDbCase, feed_of, issue_page, rss

THREE = ('2026-10-07', '2026-10-08', '2026-10-09')
# feed 漏了中间那一期的 `pubDate`(地址里那个日期还在)。
NO_PUBDATE = feed_of(('2026-10-07', '2026-10-07'), ('2026-10-08', ''),
                     ('2026-10-09', '2026-10-09'))
# 两头都没有日期:地址末段是个不透明的期号。
NO_DAY_AT_ALL = feed_of(('2026-10-07', '2026-10-07'), ('issue-7', ''),
                        ('2026-10-09', '2026-10-09'))
_SEEN = 'digest_undated'


class UndatedIssueTests(unittest.TestCase):
    """缺日期的那一期:**不许静默消失**。

    日期在这条通道上是承重的 —— 选期、历史下界、增量水位全靠它。原来空日期被
    下界和水位两个条件**一起**剔掉,而水位又照常推进到更新的那一期:那一期简报
    静默消失,源站之后把日期补上也补不回来(水位已经越过它),而面板上
    `errors`/`partial`/`last_note` 全是空(2026-10-10 第 2 轮审查)。
    """

    def setUp(self):
        digest.reset()
        self.addCleanup(digest.reset)

    def source(self, *, water='', floor='', seen=()):
        cfg = {}
        if water:
            cfg['digest_issue'] = water
        if seen:
            cfg['digest_undated'] = list(seen)
        return {'id': 1, 'kind': 'digest', 'url': FEED, 'etag': '', 'modified': '',
                'resolved_url': '', 'history_floor': floor, 'config': json.dumps(cfg)}

    def run_round(self, source, feed, *, broken=()):
        """跑一轮预取 + `take`,返回 `(条目, 请求过的期号, take 交回的 meta)`。"""
        seen = []

        def fake(url, headers=None, **kw):
            if 'api/rss' in url:
                return fetching.Response(status=200, url=url, body=feed, headers={})
            slug = url.rsplit('/', 1)[-1]
            seen.append(slug)
            if slug in broken:
                raise OSError('boom')
            return fetching.Response(status=200, url=url, body=issue_page(slug), headers={})

        with patch.object(digest, 'request', fake):
            digest.prefetch([source], {})
            entries, meta = digest.take(source, {})
        digest.reset()
        return entries, seen, meta

    def carry(self, source, meta):
        """把 config 补丁并回订阅 —— `reg.merge_config` 在生产里干的就是这个。"""
        cfg = json.loads(source['config'] or '{}')
        cfg.update(meta.get('config_patch') or {})
        return {**source, 'config': json.dumps(cfg)}

    def test_a_date_in_the_issue_url_fills_in_for_a_missing_pubdate(self):
        got = dict(digest.issues(NO_PUBDATE, FEED))
        self.assertEqual(got['https://tldr.tech/ai/2026-10-08'], '2026-10-08')

    def test_the_url_date_is_only_taken_from_the_last_path_segment(self):
        """认出来的日期**会把这一期排除掉**(`_wanted` 拿它比水位和下界),所以
        认错一个就是静默丢一期 —— 而认不出来只是多付一次 HTTP(走"照收"那一档)。
        所以这道判据宁可窄:只认最后一段,中间那些目录名一律不算。
        """
        for url in ('https://tldr.tech/ai/2026-02-30',     # 2 月没有 30 日
                    'https://tldr.tech/ai/2026-13-01',     # 没有 13 月
                    'https://d.example/i/issue-7',         # 不透明期号
                    # 归档目录名不算期号日期 —— 扫整条 path 取最右边会认成它。
                    'https://d.example/archive/2026-10-09/latest',
                    # 只看 path:查询串里的跟踪参数不算。
                    'https://d.example/i/x?utm=2026-10-09'):
            with self.subTest(url):
                self.assertEqual(digest._url_day(url), '')
        for url in ('https://tldr.tech/ai/2026-10-09',
                    'https://tldr.tech/ai/2026-10-09/',    # 末尾一个斜杠
                    'https://tldr.tech/ai/tldr-2026-10-09',  # 带前缀的 slug
                    'https://d.example/2026-01-02/x/2026-10-09',
                    # 一段里有两个日期时取最右边那个。**如实记一笔**:这是条
                    # 确定性兜底,不是防住了某个已知事故 —— 真实期号地址里一段
                    # 只有一个日期(真 RSS 上 20 期都是),留着是为了别让结果
                    # 取决于正则的遍历顺序。
                    'https://d.example/i/2026-01-02-to-2026-10-09'):
            with self.subTest(url):
                self.assertEqual(digest._url_day(url), '2026-10-09')

    def test_an_issue_the_feed_forgot_to_date_is_still_read(self):
        """**后果先断言**:三期全读到、9 条都在(审查那份证据的反面:当时只读
        了两期 6 条,10-08 那三条简讯静默消失)。"""
        entries, seen, meta = self.run_round(
            self.source(water='2026-10-06 08:00'), NO_PUBDATE)
        self.assertEqual(seen, list(THREE))
        self.assertEqual(len(entries), 9)
        self.assertIn('https://example.org/2026-10-08-0', [e['url'] for e in entries])
        self.assertEqual(meta['config_patch']['digest_issue'], '2026-10-09 08:00')

    def test_a_new_subscription_with_a_floor_still_reads_it(self):
        """新订阅那一档单独钉一条:非空下界和非空水位**各自都能单独**把空日期
        剔掉,所以只修一边不够。"""
        entries, seen, _ = self.run_round(self.source(floor='2026-10-05'), NO_PUBDATE)
        self.assertEqual(seen, list(THREE))
        self.assertEqual(len(entries), 9)

    def test_restoring_the_date_adds_nothing_because_it_was_already_read(self):
        """源站补回日期之后这一期不该再读一遍 —— 它上一轮已经收进去了。"""
        src = self.source(water='2026-10-06 08:00')
        _, _, meta = self.run_round(src, NO_PUBDATE)
        entries, seen, _ = self.run_round(self.carry(src, meta), rss(*THREE))
        self.assertEqual(seen, [])
        self.assertEqual(entries, [])

    def test_an_issue_with_no_date_anywhere_is_read_once_and_remembered(self):
        """两头都认不出日期时照收(口径同 `sync._within_history`),并按地址记一笔
        —— 日期水位记不住它,不记的话它每一轮都被重读一次。"""
        src = self.source(water='2026-10-06 08:00')
        entries, seen, meta = self.run_round(src, NO_DAY_AT_ALL)
        # 空日期排序时恒在最前面,所以它先被读。
        self.assertEqual(seen, ['issue-7', '2026-10-07', '2026-10-09'])
        self.assertIn('https://example.org/issue-7-0', [e['url'] for e in entries])
        patch_ = meta['config_patch']
        self.assertEqual(patch_['digest_undated'], ['https://tldr.tech/ai/issue-7'])
        self.assertEqual(patch_['digest_issue'], '2026-10-09 08:00')
        entries2, seen2, _ = self.run_round(self.carry(src, meta), NO_DAY_AT_ALL)
        self.assertEqual(seen2, [])
        self.assertEqual(entries2, [])

    def test_an_undated_issue_that_failed_is_not_remembered(self):
        """读不到就不许记成"已经读过" —— 否则它从此再也不会被重试,而这正是
        那份名单要是记错了方向就会造成的那种静默丢失。"""
        entries, seen, meta = self.run_round(
            self.source(water='2026-10-06 08:00'), NO_DAY_AT_ALL, broken=('issue-7',))
        self.assertEqual(seen, ['issue-7'])
        self.assertEqual(entries, [])
        self.assertNotIn('digest_undated', meta.get('config_patch') or {})
        self.assertIn('未标日期的一期', meta['note'])
        # 水位停在原处,不许越过它。
        self.assertEqual(meta['config_patch']['digest_issue'], '2026-10-06 08:00')

    def test_more_undated_issues_than_the_quota_drain_round_by_round(self):
        """**没有日期的期号多过一轮额度时也得分轮读完。**

        它们不受水位管、排序时又恒排在最前面(空串最小)—— 不记"读过"的话每一轮
        读的都是同样那前六期,排在额度之外的那些永远读不到,和这条规则本来要挡的
        事故同一个形状。
        """
        slugs = [f'issue-{i}' for i in range(8)]
        feed = feed_of(*((s, '') for s in slugs))
        src = self.source()
        e1, seen1, m1 = self.run_round(src, feed)
        self.assertEqual(seen1, slugs[:6])
        self.assertIn('还有 2 期没读', m1['note'])
        e2, seen2, m2 = self.run_round(self.carry(src, m1), feed)
        self.assertEqual(seen2, slugs[6:])
        self.assertEqual(m2.get('note', ''), '')
        self.assertEqual(sorted(e['url'] for e in e1 + e2),
                         sorted(f'https://example.org/{s}-{i}'
                                for s in slugs for i in range(3)))

    def test_the_remembered_list_is_trimmed_to_the_current_listing(self):
        """**名单的上限是当前清单自己,不是一个常数。**

        按容量淘汰(原来是 60 条、挤最旧的)会把真正未读的那几期**饿死** ——
        被淘汰的地址仍然列在 RSS 里,下一轮又算"未读",而空日期恒排在最前面、
        先把每轮额度用光。后果在下面那条端到端上;这里钉的是判据本身:仍然
        列着的一个都不丢、滚出清单的自动掉出去、清单这一轮没拿到时一条都不裁。
        """
        urls = [f'https://d.example/i/{i}' for i in range(80)]
        src = {'config': json.dumps({'digest_undated': urls})}
        # 80 条全都还列着 —— 一个都不许丢(原来那个上限是 60)。
        self.assertEqual(digest.taken_patch(src, [], urls), {})
        # 前 10 期滚出清单 —— 掉出去的只有它们。
        live = urls[10:]
        self.assertEqual(digest.taken_patch(src, [], live)[_SEEN], live)
        # 清单这一轮没拿到(空的)时一条都不裁:那时我们对"谁还列着"没有证据。
        self.assertEqual(digest.taken_patch(src, [], []), {})
        # 新读的那一期追加在末尾。
        fresh = 'https://d.example/i/new'
        self.assertEqual(digest.taken_patch(src, [fresh], live + [fresh])[_SEEN],
                         live + [fresh])
        # 同一个地址不许在名单里重复堆积(没变就不写盘)。
        one = {'config': json.dumps({'digest_undated': ['a']})}
        self.assertEqual(digest.taken_patch(one, ['a'], ['a']), {})

    def test_the_unread_note_names_the_oldest_dated_issue(self):
        """提示里那个「最早 X」只认有日期的那几期 —— 空日期恒排在最前面,拿它
        去填「把起始日期改到 X」会给出一句空话。"""
        note = digest._unread_note([('u', ''), ('a', '2026-10-03 08:00')], False, [])
        self.assertIn('还有 2 期没读', note)
        self.assertIn('最早 2026-10-03', note)
        bare = digest._unread_note([('u', '')], False, [])
        self.assertIn('还有 1 期没读', bare)
        self.assertNotIn('最早', bare)


class UndatedEndToEndTests(DigestDbCase):
    """真走一轮 `sync.collect`,落进 feeds.db。"""

    def test_an_issue_the_feed_forgot_to_date_still_lands(self):
        """审查第 2 轮那份证据的反面:三期九条全进库,源站补回日期之后也不多不少。

        当时第一轮只请求 10-07、10-09 两期(新增 6 条)、`errors`/`partial`/
        `last_note` 全是空、水位推到 10-09;补回日期再同步新增 0 条、一期页面都
        不请求,10-08 那三条简讯永久缺失。
        """
        first, seen = self.collect_feed(NO_PUBDATE)
        self.assertEqual(first['errors'], [])
        self.assertEqual(first['partial'], [])
        self.assertEqual(seen, list(THREE))
        self.assertEqual(first['added'], 9)
        want = sorted(f'https://example.org/{d}-{i}' for d in THREE for i in range(3))
        self.assertEqual(self.stored(), want)
        self.assertEqual(self.water(), '2026-10-09 08:00')
        second, seen2 = self.collect_feed(rss(*THREE))
        self.assertEqual(second['added'], 0)
        self.assertEqual(seen2, [])
        self.assertEqual(self.stored(), want)

    def test_an_issue_with_no_date_anywhere_is_remembered_in_the_db(self):
        """那份名单和条目走同一份 config 补丁、同一个事务(`_save_result`)——
        分开提交的话存盘失败那一轮"已经读过"已经落下了,而简讯一条都没入库。"""
        first, seen = self.collect_feed(NO_DAY_AT_ALL)
        self.assertEqual(first['errors'], [])
        self.assertEqual(first['added'], 9)
        self.assertIn('https://example.org/issue-7-0', self.stored())
        cfg = self.config()
        self.assertEqual(cfg[_SEEN], ['https://tldr.tech/ai/issue-7'])
        self.assertEqual(cfg['digest_issue'], '2026-10-09 08:00')
        second, seen2 = self.collect_feed(NO_DAY_AT_ALL)
        self.assertEqual(second['added'], 0)
        self.assertEqual(seen2, [])

    def test_an_issue_that_rolled_out_of_the_listing_leaves_the_list(self):
        """名单有界靠的是"它只装当前清单里的东西",所以滚出清单的要真的掉出去。

        这一条走的是 `_harvest_one` → `take` → `taken_patch` 整条线:直测
        `taken_patch` 是自己把清单递进去的,验不到"这一轮的清单有没有交回去"
        (定点注入"不交回清单"在那几条上实测红 0 条,就是这么发现的)。
        """
        slugs = [f'op-{i}' for i in range(5)]
        self.collect_feed(feed_of(*((s, '') for s in slugs)))
        self.assertEqual([u.rsplit('/', 1)[-1] for u in self.config()[_SEEN]], slugs)
        later = slugs[2:] + ['op-9']          # 前两期滚出清单、来了一期新的
        self.collect_feed(feed_of(*((s, '') for s in later)))
        self.assertEqual([u.rsplit('/', 1)[-1] for u in self.config()[_SEEN]], later)
        self.assertEqual(len(self.stored()), 6 * 3)

    def test_a_long_undated_listing_is_read_through_to_the_last_issue(self):
        """**后果先断言**:67 期全无日期,反复同步要把 201 条全收进来。

        2026-10-10 第 3 轮审查那份证据的反面 —— 当时那份已读名单按容量(60)
        淘汰,前 11 轮正常收到 198 条,第 12 轮起请求的又是已经读过的
        `opaque-000`~`005`:新增恒为 0,末期 `opaque-066` 那三条**永远**不入库,
        而 `errors=[]`、面板一直写着「还有 1 期没读,下一轮同步继续」——「再等
        一轮」是假的,第 22 轮就回到了第 11 轮的状态。67 期没越过任何已有输入
        上限(`parse_feed` 收最近 500 项),所以这不是"要求抓超出清单的存档"。
        """
        slugs = [f'opaque-{i:03d}' for i in range(67)]
        feed = feed_of(*((s, '') for s in slugs))
        rounds = 0
        while True:
            summary, seen = self.collect_feed(feed)
            self.assertEqual(summary['errors'], [])
            rounds += 1
            if not seen:
                break
            self.assertLessEqual(rounds, 12, '读不完 —— 仍然列着的已读身份被淘汰了')
        self.assertEqual(rounds, 13)        # 12 轮读完,第 13 轮一个页面都不请求
        self.assertEqual(summary['added'], 0)
        self.assertEqual(self.stored(),
                         sorted(f'https://example.org/{s}-{i}'
                                for s in slugs for i in range(3)))
        # 读完了就不该再留着"还没读完"那句提示。
        self.assertEqual(summary['partial'], [])
        self.assertEqual(len(self.config()[_SEEN]), 67)
