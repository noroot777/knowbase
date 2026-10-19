# -*- coding: utf-8 -*-
"""拦路的东西还拦着时,「重试失败项」拒绝起跑(见 `pipeline_retry.crawl_blocked`)。

2026-10-19 的形态:一个占着浏览器 profile 的 X 登录窗口把大V那一轮整批堵死
(51/51 失败),用户点「重试失败项」—— 窗口还开着,于是这一轮原地又红一遍,**而且
那份一模一样的失败成了「最近一次运行」**。计划和范围都从最近那一轮派生,所以这一下
真正的代价不是白跑,是**拿一轮白跑换掉了手上那份诊断**。

所以这一份的判据落在两件事上,不是"调了某个函数":

①**拒绝之后那一轮的留档一字未动** —— 这是这道闸唯一的理由,也是它和"让它跑完
  再看"的全部区别;
②**拦不住就别拦** —— 有一个来源抓得动、或者压根问不出来时照旧起跑,否则一个来源
  的故障会把同一轮里本该重试的 index / extract 一起扣下。

跑:  uv run --offline python -m unittest tests.test_retry_blocked -v
"""
from __future__ import annotations

import unittest
from copy import deepcopy
from unittest.mock import patch

from kb import pipeline_retry, pipeline_state
from kb.collectors import SourceStatus
from kb.collectors.feeds import FeedCollector
from kb.feeds import registry as reg, sync
from tests.retry_scope_fixture import RetryScopeBase

_BLOCKED = SourceStatus(online=True, ready=False,
                        message='X 登录窗口开着,它占着抓取要用的浏览器',
                        hint='在那个窗口里登录完就关掉它')
_READY = SourceStatus(online=True, ready=True, message='已启用 3 个来源')


class BlockedGateTests(RetryScopeBase):
    """抓不动的时候拒绝起跑,而且什么都不改。"""

    def blocked(self, status=_BLOCKED):
        return patch.object(FeedCollector, 'status', lambda self: status)

    def test_a_still_blocked_source_refuses_to_start(self):
        """拦路的还拦着 → 拒绝,一个请求都不发(`refuse` 自己钉着这一条)。"""
        self.crawl({'channel': 'rss'}, broken=[self.a])
        with self.blocked():
            msg = self.refuse()
        self.assertIn('抓不动', msg)

    def test_the_refusal_leaves_the_previous_rounds_record_untouched(self):
        """**这道闸唯一的理由**:拒绝之后上一轮的结论、失败名单、导出范围一字未动。

        真让它跑完的话,这一份留档会被一轮一模一样的失败顶掉 —— 而下一次重试的
        计划和范围都从最近那一轮派生,于是那份诊断再也找不回来。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        before = deepcopy(pipeline_state.load_last_run() or {})
        self.assertTrue(before.get('run_id'), '这一局的前提:上一轮真的留了档')
        self.assertEqual([str(self.a['id'])],
                         pipeline_state.retry_notes(before)['rss']['targets'],
                         '这一局的前提:那份失败名单在留档里')
        with self.blocked():
            self.refuse()
        self.assertEqual(before, pipeline_state.load_last_run() or {},
                         '拒绝起跑却动了上一轮的留档')

    def test_the_refusal_says_which_source_and_what_to_do(self):
        """那句话要带**来源名、原因和下一步** —— 用户据此决定做什么。

        笼统一句「现在不能重试」会让人反复点那颗按钮,而真正要做的事(去关掉那个
        窗口)一个字都没说。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        with self.blocked():
            msg = self.refuse()
        self.assertIn('RSS', msg.upper(), '没说是哪个来源')
        self.assertIn('占着抓取要用的浏览器', msg, '没说原因')
        self.assertIn('关掉它', msg, '没说下一步')

    def test_a_status_probe_that_blows_up_lets_the_retry_through(self):
        """反过来钉一边:**问不出来一律放行**。

        `status()` 要起子进程、读订阅库、碰文件系统,它自己炸掉跟「能不能抓」是两
        件事。拦住整轮会把同一轮里本该重试的 index / extract 一起带走;而漏拦一次
        只是白抓一轮。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])

        def boom(self):
            raise RuntimeError('订阅库打不开')
        with patch.object(FeedCollector, 'status', boom):
            got, stage = self.retry()
        self.assertEqual({self.a['id']}, got, '问不出来就该照旧抓那一条')

    def test_the_gate_stays_out_of_the_way_when_nothing_is_crawled(self):
        """反过来钉一边:这一轮不抓东西时这道闸不参与。

        上一轮只有 index 红了,来源就算待配置也和这次重试无关 —— 拦下来等于让一个
        无关的故障把索引和提取一起扣住。
        """
        self.crawl({'channel': 'rss'}, also_red=('index',))
        with self.blocked(), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, f'不抓东西的那一轮被拦住了:{msg}')
            self._wait_idle('重试那一轮没有收尾')
        self.assertNotIn('crawl', msg, '这一轮压根不该排抓取')
        self.assertIn('index', msg, '该重试的索引被一个无关的来源故障扣住了')
        self.assertNotEqual('skipped', self.stage('index')['status'])


class BlockedProbeCostTests(unittest.TestCase):
    """「有一个抓得动就不拦」那条,连带它省掉的那几次探测。"""

    def test_one_usable_source_is_enough_and_stops_the_probing(self):
        """一个抓得动就当场返回空串:那一轮有正事可做,别拿一个来源的故障扣下它。

        **而且就此不再问下去** —— 公众号那一下要起采集器 CLI,冷缓存时几十秒,
        而那个答案已经改变不了结论了。
        """
        asked: list[str] = []

        def collector(channel):
            asked.append(channel)
            return type('C', (), {'status': lambda _s: (
                _READY if channel == 'rss' else _BLOCKED)})()
        with patch.object(pipeline_retry, 'active_collector', collector):
            got = pipeline_retry.crawl_blocked(
                [{'channel': 'social'}, {'channel': 'rss'}, {'channel': 'gzh'}])
        self.assertEqual('', got, '有一个抓得动就不该拦')
        self.assertEqual(['social', 'rss'], asked, 'rss 之后还在往下探测')

    def test_every_blocked_source_gets_named(self):
        """全都抓不动时,每一个都要在那句话里出现 —— 只说第一个的话,用户关掉那个
        窗口再点一次,又被第二个拦住,而第二条原因这才第一次露面。"""
        def collector(channel):
            return type('C', (), {'status': lambda _s: SourceStatus(
                online=True, ready=False, message=f'{channel} 没配好')})()
        with patch.object(pipeline_retry, 'active_collector', collector):
            got = pipeline_retry.crawl_blocked(
                [{'channel': 'social'}, {'channel': 'gzh'}])
        self.assertIn('social 没配好', got)
        self.assertIn('gzh 没配好', got)


class CarriedWithinTheDayTests(RetryScopeBase):
    """当日继承走完整条链:今天抓成过的来源,这颗按钮整个摘掉它。

    收窄那一级的判据在 `tests/test_retry_scope.py`(`still_failing`),这里验的是
    **端到端的后果**:计划里连抓取都不排,而且用户看到的是一句说得清的话。
    """

    def test_a_source_that_already_succeeded_today_is_not_crawled_again(self):
        """09:12 抓成了、10:22 整批失败、10:34 点重试 → **一个抓取请求都不发**。

        这一轮照旧会去跑提取(临时 vault 里有积压),所以判据不是"拒绝起跑",而是
        **抓取这一步压根没进计划**、而且那张卡片上写着为什么。
        """
        self.crawl({'channel': 'rss'})
        row = reg.subscription('rss', self.a['id'])
        self.assertTrue(sync._succeeded_today(row['last_success']),
                        '这一局的前提:今天前面那一轮抓成了')
        # 让 index 也红:这一轮的计划里还有别的阶段,于是真的会起跑 —— 否则这一局
        # 的红绿就押在「共用的 kb.db 里恰好有没有提取积压」上(见 `no_backlog`)。
        self.crawl({'channel': 'rss', 'force': True},
                   broken=[self.a, self.b, self.c], also_red=('index',))
        self.assertEqual('error', self.stage()['status'],
                         '这一局的前提:后面那一轮整批失败了')

        got, stage = self.retry()
        self.assertEqual(set(), got, '今天已经抓成过的来源又被抓了一遍')
        self.assertEqual('skipped', stage['status'])
        self.assertEqual(pipeline_retry.RETRY_NOTHING, stage['detail'],
                         '一张绿色的「检查 0/0 个来源」和"这个按钮坏了"长得一模一样')


if __name__ == '__main__':
    unittest.main()
