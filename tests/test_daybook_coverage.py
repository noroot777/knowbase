# -*- coding: utf-8 -*-
"""**没执行过的那一步,不许把今天早些时候那笔失败结清。**

第 1 轮自由审查 R1 的形态:今天早些时候导出失败过一次,再同步一轮而采集器还没装
好 —— 那一步一个子进程都没起、报了一句 `skipped`,而「重试失败项」当场消失,刷新
也回不来(整理工具被另一个实例占着跳过那一轮同形)。来历是:覆盖范围采集器报不出
来时**按这一轮的计划范围推断**(存量采集器不必改就还是老行为),而导出的执行契约是
「扫整条来源」、全库那几步连来源都没有 —— 于是它们一律带着「整条覆盖」的推断值
收尾,照它结清就是拿**一步压根没跑**盖掉一笔真失败。

判据只有一条,落在**下一轮还补不补得到**上:**只有明确成功的目标覆盖、或者整范围
成功,才能清掉对应的旧失败;跳过、被停、覆盖范围说不出来的那几档,既不许清旧失败、
也不许凭空新建失败。** 口径在 `pipeline_retry.stage_ran` + `pipeline_daybook._coverage`,
硬约束在 `docs/decisions/retry-scope.md` 第一节。

每一族都走**三轮**:先 error、再跳过、随后真正 ok —— 只验前两轮的话,一个「跳过
之后今天之内再也结不清」的实现也全绿,而那会让这颗按钮一直催用户补一件已经做完的
事。三族各自真走产品那条路:抓取、导出(`_run_export`)、全库(`_run_projects`)。

**反过来钉三边**:①采集器自己报过的覆盖范围**不过这一道** —— 一轮里有一条订阅缺
配置时整张卡片是 `skipped`,而那一轮真抓成的那几条照旧结清(不许为了挡住跳过那一档
把整个来源重新标红);②采集器**报不出**覆盖范围而这一步确实跑成了的那一档,照旧按
计划范围结清(存量行为不许连带改掉);③今天压根没失败过时,一次跳过不许凭空记出
一笔欠账。

摆盘共用 `tests/daybook_case.py`;记账单位那一族在 `tests/test_retry_daybook.py`,
缺口与降级那一族在 `tests/test_daybook_gaps.py`。

跑:  uv run --offline python -m unittest tests.test_daybook_coverage -v
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kb import (pipeline_retry, pipeline_stages, pipeline_state,
                pipeline_wiki_stages)
from kb.collectors import wechat_local
from kb.collectors.wechat_local import WeChatLocalCollector
from kb.feeds import registry as reg
from tests.daybook_case import DaybookCase

_CHANNEL = 'wechat_local'


class SettledOnlyWhenItRanTests(DaybookCase):
    """跑没跑过 vs 红没红 —— **结清要有成功执行的证据**。"""

    # ── 摆盘:每一轮都真走产品那条路 ────────────────────────────────────────

    def failed_earlier(self, stage, channel=_CHANNEL):
        """今天早些时候这一步失败过一次。**这是前提,不是被测行为。**"""
        self._begin({'channel': channel}, stages=(stage,))
        pipeline_state._stage(stage, 'error', '摆盘:今天早些时候这一步失败了')
        pipeline_state._end_run()
        self.assertIn(stage, self.plan(), f'这一局的前提:{stage} 今天欠着')

    def export_round(self, materialize, channel=_CHANNEL):
        """真走 `_run_export` 跑一轮导出(`materialize` 由用例给)。

        **必须走 `_per_channel`**:导出那一格的覆盖范围是在那儿记下来的,把
        `_run_export` 整个打桩掉的那几局压根没有结果账,折账走的是另一条路。
        公众号那个采集器是共享实例,所以打在**类**上(`active_collector` 每次
        取到的是同一个,打实例也拦得住,打类更稳)。
        """
        self._begin({'channel': channel}, stages=('export',))
        with patch.object(WeChatLocalCollector, 'materialize', materialize):
            pipeline_stages._run_export({'channel': channel})
        pipeline_state._end_run()
        return self.stage('export')

    def skipped_export(self):
        """导出那一步**一个子进程都没起**:采集器还没装好。

        真走 `WeChatLocalCollector.materialize` 自己那条判断(只把「装好没有」这
        一问替掉),所以 `skipped` 这句话是产品自己报的。
        """
        with patch.object(wechat_local, '_backend_problem',
                          return_value='摆盘:采集器暂不可用'):
            stage = self.export_round(WeChatLocalCollector.materialize)
        self.assertEqual('skipped', stage['status'],
                         f'这一局的前提:这一轮导出是跳过收尾的({stage})')

    def ok_export(self):
        """这一次导出真跑成了。"""
        def materialize(_self):
            pipeline_state._stage('export', 'ok', '已保存 2 篇')
            return frozenset()
        stage = self.export_round(materialize)
        self.assertEqual('ok', stage['status'], '这一局的前提:这一轮导出成了')

    def projects_round(self, *, gate_busy=False, stop_in_ingest=False):
        """真走 `_run_projects`,只把子进程的返回值(和入库那一步)替掉。

        `gate_busy`:条目化脚本自带文件锁,手动实例在跑时它用返回码 3 说
        「本轮跳过」;`stop_in_ingest`:新条目入库要等维护锁,等的时候用户按了
        停止 —— 两种都是**真实的、不需要用户主动制造故障的**没执行过。
        """
        self._begin({}, stages=('projects',))
        rc = SimpleNamespace(returncode=3 if gate_busy else 0, stdout='', stderr='')
        with patch.object(pipeline_wiki_stages, '_run_export_script',
                          return_value=rc):
            if stop_in_ingest:
                from kb.pipeline_stages import _StopWhileWaiting
                with patch.object(pipeline_wiki_stages, '_ingest_pretagged_sources',
                                  side_effect=_StopWhileWaiting()):
                    pipeline_wiki_stages._run_projects()
            else:
                pipeline_wiki_stages._run_projects()
        pipeline_state._end_run()
        return self.stage('projects')

    def crawl_without_coverage(self, status, detail):
        """真走 `_run_crawl`,而采集器**报不出覆盖范围**(`fetch` 返回 `None`)。

        交一份覆盖范围是**可选能力**(契约在 `kb.collectors.SourceCollector.fetch`:
        报不出来就退回请求范围),所以这一局钉的是契约里那一档退回值 —— 现有两个
        采集器都报,而新写一个来源时不报就是默认行为。打在 `FeedCollector` **类**
        上:`active_collector('rss')` 每次新建一个实例,打自己手里那个拦不住。
        """
        from kb.collectors.feeds import FeedCollector
        scope = {'channel': 'rss'}
        self._begin(scope, stages=('crawl',))

        def fetch(_self, _scope):
            pipeline_state._stage('crawl', status, detail)
            return None

        with patch.object(FeedCollector, 'fetch', fetch):
            pipeline_stages._run_crawl(scope)
        got = self.stage('crawl')
        self.assertEqual(status, got['status'], f'这一局的前提立不起来:{got}')
        pipeline_state._end_run()

    # ── 三族各一局:先 error、再跳过、随后真正 ok ──────────────────────────

    def test_a_skipped_export_keeps_this_mornings_export_debt(self):
        """**审查 R1 实测 A**:导出根本没执行,旧导出失败却消失了。

        跳过那一轮以「已跳过」收尾、不算失败,于是它带着「整条来源都覆盖了」的推断
        值去结清 —— 今天早些时候那笔导出失败从「重试失败项」里消失,刷新也回不来,
        而那几篇文章一篇都没落盘。
        """
        self.failed_earlier('export')
        self.skipped_export()
        self.assertEqual(['export'], pipeline_retry.today_failed(),
                         '一轮压根没执行的导出把今天早些时候那笔失败结清了')
        self.assertIn('export', self.plan(), '跳过之后那颗按钮没得补了')
        self.ok_export()
        self.assertEqual([], pipeline_retry.today_failed(),
                         '导出真跑成了,这一笔该结清')
        self.assertEqual([], self.plan(), '补好的导出还在被反复重试')

    def test_a_skipped_library_step_keeps_this_mornings_debt(self):
        """**审查 R1 实测 B**:整理工具被另一个实例占着,旧整理失败同样消失。

        「另一个任务存在」不是「那个任务一定会成功」的证明 —— 这一轮一条条目都没
        条目化、也没入库。全库那几步连来源都没有,所以它们的覆盖范围一律是推断值,
        这一档尤其靠这道判据。
        """
        self.failed_earlier('projects')
        stage = self.projects_round(gate_busy=True)
        self.assertEqual('skipped', stage['status'],
                         f'这一局的前提:这一轮整理是跳过收尾的({stage})')
        self.assertEqual(['projects'], pipeline_retry.today_failed(),
                         '一轮被占用跳过的整理把今天早些时候那笔失败结清了')
        self._begin({}, stages=('projects',))
        pipeline_state._stage('projects', 'ok', '入库 3 条目')
        pipeline_state._end_run()
        self.assertEqual([], pipeline_retry.today_failed(),
                         '整理真跑成了,这一笔该结清')

    def test_a_library_step_the_stop_cut_short_keeps_this_mornings_debt(self):
        """**同一条的另一种没执行**:等维护锁的时候用户按了停止。

        那一步报的是 `stopped` —— 不算失败,可它也没执行成:新条目一条都没入库。
        「用户停了」不许当成「这一步已经补上了」。
        """
        self.failed_earlier('projects')
        stage = self.projects_round(stop_in_ingest=True)
        self.assertEqual('stopped', stage['status'],
                         f'这一局的前提:这一轮整理是停止收尾的({stage})')
        self.assertEqual(['projects'], pipeline_retry.today_failed(),
                         '一轮被停掉的整理把今天早些时候那笔失败结清了')

    def test_a_skipped_crawl_that_cannot_report_coverage_keeps_the_debt(self):
        """抓取那一族的同一条:采集器报不出覆盖范围,而这一轮是跳过收尾的。

        现有两个采集器都交得出覆盖范围(零覆盖那一档已经挡住了),所以这一局钉的是
        **契约里那一档退回值** —— 新写一个来源时它就是默认行为,那时这道判据是唯一
        挡住「跳过也算跑完」的东西。
        """
        self.failed_earlier('crawl', channel='rss')
        self.crawl_without_coverage('skipped', '摆盘:这一轮一条都没取')
        self.assertEqual(['crawl'], pipeline_retry.today_failed(),
                         '一轮跳过的抓取把今天早些时候那笔失败结清了')

    # ── 反过来钉三边 ────────────────────────────────────────────────────────

    def test_a_coverage_the_collector_reported_settles_even_on_a_skipped_card(self):
        """**反过来钉一边**:采集器报过的覆盖范围**不过「执行过没有」那一道**。

        一轮里有一条订阅缺配置时整张卡片就是 `skipped`(`sync.collect` 那一行),
        而同一轮里 A 明明真抓成了。按卡片的状态去扣住它的后果是:A 那笔旧失败在
        账上一直红到当天结束,这颗按钮每次都要去重抓一条已经抓好的订阅 —— 而用户
        要的正是「9:12 抓过的不要再抓」。`Coverage` 是执行事实,卡片只够展示
        (硬约束那条「逐来源的结论由执行器自己交回来,不许从那张合并卡片反推」)。

        **判据落在账上那一格**,不落在 `today_failed()` 上:执行端还有一道当日继承
        (`sync.still_failing` 把今天成功过的目标摘掉),它会把这里记错的那一格顺带
        遮住 —— 于是那个断言对这一条压根不敏感(实测注入后仍然全绿)。账本才是
        「今天还欠什么」的权威,两处各判一遍正是那个「说没补完 + 点了又没有可补」
        的死局;所以这里问账,再顺手钉一句「执行端也没变坏」。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.assertIn('crawl', self.plan(), '这一局的前提:A 那一条欠着')
        self.assertEqual('error', self.book_cells('crawl', 'rss')[str(self.a['id'])],
                         '这一局的前提:A 那一格在账上是红的')
        with patch.object(reg, 'configuration_error',
                          lambda s, settings: ('待配置：摆盘'
                                               if s['id'] == self.c['id'] else '')):
            # `force` 是人按的那一轮才有的那一档(判在 `kb/run_trigger.py`,
            # 「重试失败项」声明自己是手动),不带它的话 A 还在失败退避里、
            # 这一轮压根不会取它。
            got, stage = self.crawl({'channel': 'rss', 'force': True})
        self.assertIn(self.a['id'], got, '这一局的前提:这一轮真去取了 A')
        self.assertEqual('skipped', stage['status'],
                         f'这一局的前提:这一轮的卡片是跳过收尾的({stage})')
        cells = self.book_cells('crawl', 'rss')
        self.assertNotEqual('error', cells[str(self.a['id'])],
                            f'A 这一轮真抓成了,账上那一格还红着:{cells}')
        self.assertEqual([], pipeline_retry.today_failed(),
                         'A 这一轮真抓成了,它的旧失败该结清')

    def test_a_step_that_ran_settles_even_when_it_cannot_report_coverage(self):
        """**反过来钉一边**:报不出覆盖范围、而这一步真跑成了 ⇒ 照旧结清。

        只验「跳过不许结清」的话,一个「推断值一概不算」的实现也全绿 —— 而那会让
        所有不实现这个可选能力的来源今天之内再也结不清,存量行为当场反向。
        """
        self.failed_earlier('crawl', channel='rss')
        self.crawl_without_coverage('ok', '摆盘:整条通道都抓完了')
        self.assertEqual([], pipeline_retry.today_failed(),
                         '它真跑成了,按计划范围该结清这一笔')

    def test_a_skipped_step_does_not_invent_a_debt(self):
        """**反过来钉一边**:今天压根没失败过时,一次跳过不许凭空记出一笔欠账。

        不变量的另一半 —— 「不能清旧失败」和「不能凭空新建失败」要一起成立,
        不然那颗按钮会对着一件谁都没要求过的事反复催人动手。
        """
        self.skipped_export()
        self.assertEqual([], pipeline_retry.today_failed(),
                         '一次跳过凭空记出了一笔欠账')
        self.assertEqual([], self.plan(), '一次跳过凭空排出了一条重试链')

    # ── 成功回执这一侧:身份只按编号对上,一个名字都不认 ────────────────────

    def test_a_success_receipt_never_settles_the_subscription_merely_named_that_number(self):
        """**记账方向不许放宽身份**:A 抓成了,不许连带把「名字正好是 A 的编号」
        那条订阅的真失败一起结清。

        `Coverage.targets` 交回来的已经是稳定身份(订阅源报订阅 id、公众号报账号名),
        而那一侧原来又过了一遍**选源方向**的范围匹配(`reg.in_scope`:「id 相等**或**
        name 相等」)。两个方向的安全侧是反的:
        - 选源宽一点 = 多抓一条,代价是重抓一次(`in_scope` 的 docstring 就是按这一侧
          写的,那一侧照旧宽);
        - **记账宽一点 = 拿一次没发生的成功清掉另一条订阅的真失败**,而它从此不再
          进重试计划 —— 静默丢数据,重试救不回来。

        判据落在**下一轮还补不补得到**上:账上那一格、这颗按钮排不排得出抓取、以及
        真点下去它去抓的是谁。只问账本不够 —— 账上记错那一格之后,面板和执行端是
        **同一份派生**(`owed_now`),两边会一起消失、看起来一切正常。
        """
        d = reg.save_subscription('rss', {'name': str(self.a['id']),
                                          'url': 'https://d.org/feed'})
        # 第一轮只同步 D 并让它取文失败。**按编号点名**:D 的名字就是 A 的编号,
        # 裸着写这一轮会把 A 一起抓走,这一局的前提就不是「只有 D 欠着」了。
        self.crawl({'channel': 'rss', 'accounts': [reg.id_target(d['id'])]}, broken=[d])
        self.assertEqual({str(d['id']): 'error'}, self.book_cells('crawl', 'rss'),
                         '这一局的前提:账上只有 D 那一格,而且是红的')
        # 第二轮只同步 A,它真抓成了。回执里是裸着的 A 编号 —— 那个字符串同时又是
        # D 的名字。
        got, stage = self.crawl({'channel': 'rss',
                                 'accounts': [reg.id_target(self.a['id'])]})
        self.assertEqual({self.a['id']}, got, '这一局的前提:这一轮只去取了 A')
        self.assertEqual('ok', stage['status'], f'这一局的前提:A 这一轮抓成了({stage})')
        cells = self.book_cells('crawl', 'rss')
        self.assertEqual('error', cells.get(str(d['id'])),
                         f'A 的成功把 D 那笔真失败结清了:{cells}')
        self.assertEqual('ok', cells.get(str(self.a['id'])),
                         f'A 自己这一笔反而没记上:{cells}')
        self.assertIn('crawl', self.plan(), 'D 还欠着,这颗按钮却排不出抓取了')
        again, _ = self.retry_settled()
        self.assertEqual({d['id']}, again,
                         '点下去该只去补 D 这一条(空集 = 它被当成已经补完了)')


if __name__ == '__main__':
    unittest.main()
