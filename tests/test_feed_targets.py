# -*- coding: utf-8 -*-
"""范围目标的**身份**:按编号点名,还是按名字点名。

原来这是一个无类型的字符串空间:同一个 `"11"` 既可能是某条订阅的 id、也可能是
另一条订阅的**名字**,而唯一那份匹配(`reg.in_scope`)两种都认。于是「只同步我点
的这一条」会把名字叫「11」的那条一起抓走 —— 请求里明明只有一条
(第 1 轮审查 R1 实测:`accounts=["11"]`,真跑了 11 和 12 两条)。

所以这里钉的是**整条链**,不是某一个按钮:

1. 匹配口径认得三种写法,**而且老形态照旧认**(存量历史轮次和日账里存的是裸 id);
2. **后果**:按编号点名时,真去抓的只有那一条;裸着发那一版两条都被抓 —— 这一条
   反过来说明了为什么要定型,而不是只记一句"修好了";
3. 就绪检查、稳定身份归一、重试收窄三个同源消费方和抓取**必定是同一批**;
4. 账本那一侧只留裸 id(`still_failing` 按 `str(source['id'])` 比,带前缀存进去
   等于换了个键,那一格的欠账就永远清不掉);
5. 前端那份字面量和后端常量一致 —— 前缀在两种语言里各写一次,是两份口径里最容易
   悄悄岔开的那种。
"""
from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kb import maintenance
from kb.collectors import FetchScope
from kb.collectors.feeds import FeedCollector
from kb.feeds import fetching, registry as reg, sync

JS = Path(__file__).resolve().parent.parent / 'kb' / 'static' / 'js' / 'pipe_setup.js'


class TargetIdentityTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for mock in (
            patch.object(reg, 'DB_PATH', Path(temp.name) / 'feeds.db'),
            patch.object(maintenance, 'DATA_DIR', Path(temp.name)),
            patch.object(sync, '_state', {'stop_requested': False}),
            patch.object(sync, '_stage'),
            patch.object(fetching.socket, 'getaddrinfo',
                         side_effect=AssertionError('不许连网')),
        ):
            mock.start()
            self.addCleanup(mock.stop)
        # 先建 A 拿到它的编号,再把 B 的**名字**摆成那个编号 —— 这就是碰撞现场
        self.a = reg.save_subscription('rss', {'name': '某博客', 'url': 'https://a.example/feed'})
        self.b = reg.save_subscription('rss', {'name': str(self.a['id']),
                                               'url': 'https://b.example/feed'})
        self.collector = FeedCollector('rss')

    # ── 1. 匹配口径 ────────────────────────────────────────────────────────────
    def test_an_id_target_matches_only_that_subscription(self):
        want = (reg.id_target(self.a['id']),)
        self.assertTrue(reg.in_scope(self.a, want))
        self.assertFalse(reg.in_scope(self.b, want), '名字正好是那个编号的订阅被捎上了')

    def test_a_bare_target_still_matches_both_ways(self):
        """**老形态照旧认**:存量历史轮次、日账名单里存的就是裸 id。

        这一条同时是上一条的**反面**:裸着发时两条都算在内 —— 定型要解决的正是
        「分不清」,不是把老数据判成不匹配(那是静默漏抓)。
        """
        want = (str(self.a['id']),)
        self.assertTrue(reg.in_scope(self.a, want))
        self.assertTrue(reg.in_scope(self.b, want))

    def test_names_and_the_empty_scope_are_untouched(self):
        self.assertTrue(reg.in_scope(self.a, ('某博客',)))
        self.assertFalse(reg.in_scope(self.b, ('某博客',)))
        for source in (self.a, self.b):
            self.assertTrue(reg.in_scope(source, ()), '空范围就是全都在')

    def test_a_subscription_literally_named_like_a_token_stays_on_the_safe_side(self):
        """真有订阅叫 `id:9` 时两边都算:多抓一条顶多重抓一次,漏抓是静默丢数据。"""
        odd = reg.save_subscription('rss', {'name': reg.id_target(9),
                                            'url': 'https://odd.example/feed'})
        self.assertTrue(reg.in_scope(odd, (reg.id_target(9),)))

    # ── 2. 后果:真去抓的是哪几条 ───────────────────────────────────────────────
    def touched(self, targets):
        seen = []

        def fetch(source, settings, **kwargs):
            seen.append(source['id'])
            return [], dict(etag='', modified='', resolved_url=source['url'])

        with patch.object(sync, 'fetch_source', side_effect=fetch):
            sync.collect('rss', FetchScope(targets=targets, force=True))
        return sorted(seen)

    def test_naming_one_subscription_by_id_fetches_only_that_one(self):
        self.assertEqual([self.a['id']], self.touched((reg.id_target(self.a['id']),)))

    def test_the_bare_form_is_exactly_what_used_to_fetch_both(self):
        """**反过来钉一边**(也是事故本身):裸着发,两条都被抓。"""
        self.assertEqual(sorted([self.a['id'], self.b['id']]),
                         self.touched((str(self.a['id']),)))

    # ── 3. 三个同源消费方 ──────────────────────────────────────────────────────
    def test_readiness_counts_the_same_single_subscription(self):
        status = self.collector.status(targets=(reg.id_target(self.a['id']),))
        self.assertEqual(('某博客',), status.targets)
        self.assertIn('这一轮要抓的 1 个来源', status.message)

    def test_stable_targets_normalises_an_id_target_to_the_bare_id(self):
        self.assertEqual([str(self.a['id'])],
                         self.collector.stable_targets((reg.id_target(self.a['id']),)))

    def test_an_unmatched_id_target_is_kept_as_a_bare_id(self):
        """删掉/改名的那几条要留在账上,**而且留的是裸 id**(账本是纯 id 空间)。"""
        self.assertEqual(['9999'], self.collector.stable_targets((reg.id_target(9999),)))

    def test_retry_narrowing_hands_back_id_targets(self):
        note = {'targets': [str(self.a['id'])], 'at': ''}
        self.assertEqual([reg.id_target(self.a['id'])], self.collector.retry_targets(note))

    def test_a_retry_round_only_refetches_the_subscription_it_named(self):
        """**后果**:重试那一侧也不许把同数字名字的那条捎上。"""
        note = {'targets': [str(self.a['id'])], 'at': ''}
        self.assertEqual([self.a['id']],
                         self.touched(tuple(self.collector.retry_targets(note))))

    # ── 4. 两种语言里那一份前缀 ────────────────────────────────────────────────
    def test_the_front_end_writes_the_same_prefix_as_the_backend(self):
        found = re.search(r"const PIPE_ID_TARGET = '([^']*)';", JS.read_text(encoding='utf-8'))
        self.assertIsNotNone(found, '前端那份前缀不见了或改了写法')
        self.assertEqual(reg.ID_PREFIX, found.group(1))


if __name__ == '__main__':
    unittest.main()
