# -*- coding: utf-8 -*-
"""范围目标的**身份**:按编号点名,还是按名字点名。

原来这是一个无类型的字符串空间:同一个 `"11"` 既可能是某条订阅的 id、也可能是
另一条订阅的**名字**,而唯一那份匹配(`reg.in_scope`)两种都认。两个方向都实测被
一起抓走:按编号点名那一侧,「只同步我点的这一条」把名字叫「11」的那条也抓了
(第 1 轮审查 R1:`accounts=["11"]`,真跑了 11 和 12 两条);按名字点名那一侧,面板
名单上只勾了名字叫「63」那一行,编号 63 的另一条也跑了(第 1 轮自由审查 R1)。

所以这里钉的是**整条链**,不是某一个按钮:

1. 匹配口径认得三种写法,**两种带类型的各自只认那一种**,而老形态照旧两种都认
   (存量历史轮次和日账里存的是裸字符串);
2. **后果**:按编号点名时真去抓的只有那一条,按名字点名时真去抓的只有同名那几条;
   裸着发那一版两条都被抓 —— 这一条反过来说明了为什么要定型,而不是只记一句
   "修好了";
3. 就绪检查、稳定身份归一、重试收窄三个同源消费方和抓取**必定是同一批**;
4. 账本那一侧只留无类型的那一份(`still_failing` 按 `str(source['id'])` 比,带前缀
   存进去等于换了个键,那一格的欠账就永远清不掉);
5. **前缀只有后端一份** —— 前端只负责把目标放进哪个字段(`names` / `ids`),一个
   前缀字面量都不许出现;两种语言各写一次是两份口径里最容易悄悄岔开的那种。

**这几条全在「选源」方向上。** 另一侧(「这一轮真跑成了谁」那份成功回执)的安全侧是
**反的**:宽一点就是拿一次没发生的成功清掉另一条订阅的真失败,它从此不进重试计划。
所以那一侧不许用这份匹配 —— 这里只钉「它拿不到采集器、也就做不成匹配」这一道结构,
后果那一半在 `tests/test_daybook_coverage.py`,口径在 `pipeline_results.cover`。
"""
from __future__ import annotations

import ast
import inspect
import re
import tempfile
import textwrap
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

    def test_a_name_target_matches_only_the_subscription_called_that(self):
        """按名字点名:只认名字。编号正好是那个数字的另一条**不许**被捎上。

        这一条是面板名单那一侧的后果(名单只给名字):只勾了名字叫「63」那一行,
        编号 63 的另一条也跑了 —— 用户明明只选了一条(第 1 轮自由审查 R1 实测,
        请求 `accounts=["63"]`,真跑了 63 和 64 两条)。
        """
        want = (reg.name_target(self.b['name']),)
        self.assertTrue(reg.in_scope(self.b, want))
        self.assertFalse(reg.in_scope(self.a, want), '编号正好是那个名字的订阅被捎上了')

    def test_a_name_group_runs_every_subscription_sharing_that_name(self):
        """**反过来钉一边**:同名那几条照旧一起跑 —— 名单上那一行代表的就是它们。

        收窄成「只跑其中某一条」的话,面板上勾「同名博客」会漏掉另一条,而页面上
        看不出少跑了谁。
        """
        twin = reg.save_subscription('rss', {'name': self.a['name'],
                                             'url': 'https://twin.example/feed'})
        want = (reg.name_target(self.a['name']),)
        self.assertTrue(reg.in_scope(self.a, want))
        self.assertTrue(reg.in_scope(twin, want))
        self.assertFalse(reg.in_scope(self.b, want))

    def test_a_subscription_literally_named_like_a_token_is_reachable_by_name(self):
        """真有订阅叫 `id:9`:**按编号点名不认它,按名字点名才认它**。

        原来这一档是「两边都算」,理由写的是「选源方向宁可宽」。可**「宽」的额度只在
        「判不准」那一档上** —— 类型写明了就不是判不准,那是用户已经做出的精确选择,
        多抓一条就是跑了他明确没选的那一条。现在两种写法各有自己的入口,「点名它」
        这件事不再需要靠放宽编号那一档来表达。
        """
        odd = reg.save_subscription('rss', {'name': reg.id_target(9),
                                            'url': 'https://odd.example/feed'})
        self.assertFalse(reg.in_scope(odd, (reg.id_target(9),)),
                         '按编号点名却命中了一条名字长得像编号的订阅')
        self.assertTrue(reg.in_scope(odd, (reg.name_target(reg.id_target(9)),)),
                        '名字长得像另一种写法时,按名字点名就点不到它了')

    def test_the_accounting_side_cannot_run_this_matcher_at_all(self):
        """**另一侧是结构钉住的**:记账那一步拿不到采集器,也就做不成名字匹配。

        归一要先有采集器(`pipeline_results._stable` 第一件事就是取它的
        `TargetIdentity`),所以「这条路上不许有模糊匹配」靠的是**签名里没有它**,
        不是靠谁记得别写。拿掉这一道之后的实测形态:A 编号 63 名字「A」、B 编号 64
        名字「63」,先只同步 B 并让取文失败、再只同步 A 并成功,账本从
        `{"64":"error"}` 变成 `{"64":"ok","63":"ok"}`,B 的 `last_error` 还在原处而
        重试计划已经空了(后果那一半钉在 `tests/test_daybook_coverage.py`)。

        **读的是 AST 不是正文**:`cover` 的 docstring 里就写着这两个名字(它讲的正是
        为什么不许用),按字符串找会永远红。
        """
        from kb import pipeline_results
        self.assertNotIn('collector',
                         inspect.signature(pipeline_results.cover).parameters,
                         '记账那一步又收上采集器了,模糊匹配就又做得成了')
        tree = ast.parse(textwrap.dedent(inspect.getsource(pipeline_results.cover)))
        used = ({n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
                | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)})
        self.assertEqual(set(), used & {'_stable', 'in_scope', 'stable_targets'},
                         '成功回执这条路上又出现了范围匹配')

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

    def test_readiness_also_reads_the_name_form_of_a_target(self):
        """就绪检查和抓取**必定是同一批**:名称目标那一档也得认。

        认不出来的后果是用**一批订阅的就绪去替另一批放行**:面板名单上勾一条,就绪
        却按整条通道算 —— 别人配好了不证明这一条配好了,而那一条必定再失败一次。
        """
        status = self.collector.status(targets=(reg.name_target(self.b['name']),))
        self.assertEqual((self.b['name'],), status.targets)
        self.assertIn('这一轮要抓的 1 个来源', status.message)

    def test_stable_targets_normalises_an_id_target_to_the_bare_id(self):
        self.assertEqual([str(self.a['id'])],
                         self.collector.stable_targets((reg.id_target(self.a['id']),)))

    def test_an_unmatched_target_is_kept_without_its_type_prefix(self):
        """删掉/改名的那几条要留在账上,**而且留的是无类型那一份**。

        账本是个无类型字符串空间(`still_failing` 按 `str(source['id'])` 比),带前缀
        存进去等于换了个键:同一条订阅留下两格,页面于是既说这一步还欠着、又说没有
        可补的来源。**两种写法都要摘** —— 只摘编号那一种时,名称目标会原样变成账本
        键,而那一侧根本不会红(名字匹配得上的都归到 id 上去了,红不出来的是「刚被
        改了名」那一档)。
        """
        self.assertEqual(['9999'], self.collector.stable_targets((reg.id_target(9999),)))
        self.assertEqual(['刚改过名'],
                         self.collector.stable_targets((reg.name_target('刚改过名'),)))

    def test_retry_narrowing_hands_back_id_targets(self):
        note = {'targets': [str(self.a['id'])], 'at': ''}
        self.assertEqual([reg.id_target(self.a['id'])], self.collector.retry_targets(note))

    def test_a_retry_round_only_refetches_the_subscription_it_named(self):
        """**后果**:重试那一侧也不许把同数字名字的那条捎上。"""
        note = {'targets': [str(self.a['id'])], 'at': ''}
        self.assertEqual([self.a['id']],
                         self.touched(tuple(self.collector.retry_targets(note))))

    # ── 4. 前缀只有后端一份 ───────────────────────────────────────────────────
    def test_the_front_end_never_writes_a_target_prefix_itself(self):
        """前端**一个前缀字面量都不许有**:类型由它放进哪个字段决定。

        原来它自己拼 `id:`,后端一份、前端一份 —— 两份口径里最容易悄悄岔开的那种
        (改了后端常量,前端那一份照旧发老前缀,于是每个目标都匹配不上、一条都不抓)。
        现在请求里名字和编号分两个字段(`names` / `ids`),前端只决定放哪边,拼前缀
        是后端采集器的事(`kb.collectors.TargetTyping`)。真实页面那一侧由
        `tests/pipe_entry_probe.mjs` 实点。
        """
        text = JS.read_text(encoding='utf-8')
        for prefix in (reg.ID_PREFIX, reg.NAME_PREFIX):
            self.assertNotRegex(text, r'[\'"`]' + re.escape(prefix),
                                f'前端又自己拼起 {prefix} 了')
        self.assertIn('pipeTargetFields', text, '分字段那一步不见了')
        self.assertRegex(text, r'\{ channel: id, \.\.\.pipeTargetFields',
                         '请求体又把两种身份混进一个名单里了')


if __name__ == '__main__':
    unittest.main()
