# -*- coding: utf-8 -*-
"""「重试失败项」要补的是**今天还欠的那些**,不是「最近一次运行」里那些。

2026-10-19 的形态:09:12 一轮同步里 RSS 抓取失败,10:20 跑了一轮公众号、成功收尾
—— 后者当场把前者顶掉。于是 10:34 点「重试失败项」,它按最近那一轮的范围从头跑了
一遍,而 09:12 该补的那条一直没补上。用户要的是「同一天点重试能继承之前的状态」,
而且**只继承当日的**。

判据落在**计划、范围和下一轮真抓了谁**上,不是"账本里有没有那一格":

①今天早些时候失败的那个来源,**后面别的来源成功收尾之后仍然要补**;
②**反过来钉一边**:同一个来源后来真抓成了,就不许再抓一遍(那才是「继承」,
  不是「今天之内一律重抓」);
③**不跨天**:昨天那份账本一格都不参与 —— 跨零点之后那颗按钮照旧按「最近一次
  运行」补,不拿昨天的成功去抵今天的失败;
④账本读不出来时退回最近一次运行,**而且把这件事说出去**(今天早些时候那几笔可能
  补不到,不说的话用户以为都补上了);
⑤**没指定来源的那一轮也要记一格**(整库同步是最常见的那一轮,带来源那几局全绿时
  这个坏法一条都红不出来);
⑥**账本盖不住最近那一轮自己报出来的失败**(折账本是旁路,写不上的那一轮要是正好
  带着红,退回去只读账本就是漏报),而**整批汇总那一份不许盖回去**(它的来源清单
  是全批的),**账本比它新也不盖**(跨天那一档走的就是这条);
⑦**那颗按钮露不露脸**也得按「今天」判 —— 后端补得再对,按钮藏掉就没人点得到。

跑:  uv run --offline python -m unittest tests.test_retry_daybook -v
"""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from kb import pipeline_daybook as daybook, pipeline_retry, pipeline_state
from tests.retry_scope_fixture import RetryScopeBase


class DaybookRetryTests(RetryScopeBase):
    """今天那本账,和它派生出来的计划与范围。"""

    def wechat_round(self, status='ok', detail='公众号这一轮好了', at=''):
        """真走收尾摆一轮**另一个来源**的运行(日报那一轮就是这个形状)。"""
        with patch.object(pipeline_state, '_now',
                          return_value=at or pipeline_state._now()):
            self._begin({'channel': 'wechat_local'}, stages=('crawl', 'export'))
            pipeline_state._stage('crawl', status, detail)
            pipeline_state._stage('export', status, detail)
            pipeline_state._end_run()

    def plain_round(self, status='error', detail='摆盘:这一轮红了', at=''):
        """**整库同步**那一轮(面板上一个来源都没点、`crawl_filter` 是空的)。

        这是最常见的那一轮,所以它必须有自己的一局:带来源那几局全绿时,「没指定
        来源的那一轮一格都不记」这个坏法一条都红不出来。
        """
        with patch.object(pipeline_state, '_now',
                          return_value=at or pipeline_state._now()):
            self._begin({}, stages=('crawl', 'export'))
            pipeline_state._stage('crawl', status, detail)
            pipeline_state._stage('export', status, detail)
            pipeline_state._end_run()

    def plan(self):
        with self.no_backlog():
            return pipeline_retry.retry_failed_plan()

    def test_a_whole_library_sync_gets_a_cell_too(self):
        """**没指定来源的那一轮也要记下来** —— 它的那一格键是空串。

        整库同步的 `crawl_filter` 是空的,摊出来的范围 `channel` 也是空的。把它当成
        「没有来源可挂」丢掉的话,`stages` 这一层照旧多出 crawl/export 两个空字典,
        于是账本看着「今天跑过」、每一步却都合成成「未选择」——**这颗按钮对最常见
        的那一轮回一句「上次运行没有失败项」**,那一轮的红一个入口都没有了。
        """
        self.plain_round()
        cells = daybook.read()['stages']
        self.assertEqual([''], list(cells['crawl']), '整库那一轮的抓取没记下来')
        plan = self.plan()
        self.assertIn('crawl', plan, '它的抓取失败被当成「未选择」了')
        self.assertIn('export', plan, '它的导出失败也被当成「未选择」了')
        pending, _ = daybook.unfinished(pipeline_state.last_result())
        self.assertEqual({}, pending['crawl_filter'],
                         '空串那一格不许进范围 —— 空范围才等于「照默认采集器全抓」')

    def test_the_book_cannot_hide_the_latest_rounds_own_failure(self):
        """**账本盖不住最近那一轮自己报出来的失败。**

        折账本是旁路,写不上只记一笔 `persist_error` 就过去了(它不许挡住收尾)——
        可它写不上的那一轮要是正好带着红,账本里就只剩之前那几轮的样子,而它们多半
        是绿的:这颗按钮于是回一句「没有失败项」,那一轮的红**一个入口都没有了**。
        退回去只读账本是漏报,而漏报是静默丢数据。
        """
        today = daybook._day()
        self.wechat_round(at=f'{today} 09:00:00')
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            self.plain_round(at=f'{today} 10:20:00')
        self.assertEqual(['wechat_local'], list(daybook.read()['stages']['crawl']),
                         '这一局的前提:账本里只有公众号那一轮,没有刚才那一轮')
        plan = self.plan()
        self.assertIn('crawl', plan, '最近那一轮的抓取失败被账本盖住了')
        self.assertIn('export', plan, '它的导出失败也被盖住了')

    def test_the_button_is_still_offered_after_an_all_green_round(self):
        """**今天还欠着时那颗按钮要露得出来** —— 面板的显示判据也得是「今天」。

        它原来按「最近一轮有没有红」显示。09:12 抓 RSS 失败、10:20 一轮公众号全绿
        收尾之后,最近那一轮一个红都没有 —— 按钮当场藏掉,于是 09:12 那条**连点的
        机会都没有**,后端这一侧补得再对也到不了。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a], also_red=['export'])
        self.wechat_round()
        latest = pipeline_state.last_result()
        self.assertEqual([], [n for n in pipeline_state.STAGES
                              if pipeline_retry.stage_failed((latest['stages'] or {}).get(n))],
                         '这一局的前提:最近那一轮一个红都没有')
        owed = pipeline_retry.today_failed()
        self.assertIn('crawl', owed, '今天早些时候那条抓取没算进来')
        self.assertIn('export', owed, '它那条导出也没算进来')

    def test_what_the_button_offers_falls_back_with_the_book(self):
        """反过来钉一边:账本空着时它就是「最近一次运行」那几步,不多也不少。

        只会多显示、不会少显示 —— 多显示那一档点下去照旧按真实计划跑,少显示那一档
        是用户再也碰不到的红。
        """
        daybook._book_file().unlink(missing_ok=True)
        self._begin({'channel': 'rss'}, stages=('crawl', 'export'))
        pipeline_state._stage('crawl', 'ok', '好了')
        pipeline_state._stage('export', 'error', '摆盘:导出红了')
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            pipeline_state._end_run()
        self.assertEqual({}, daybook.read(), '这一局的前提:账本是空的')
        self.assertEqual(['export'], pipeline_retry.today_failed(),
                         '账本空着时该原样报最近那一轮的那几步')

    def test_a_broken_synthesis_does_not_take_down_the_status_endpoint(self):
        """合成那一步炸了,这颗按钮的显示判据只退回存量行为。

        `/api/pipeline/status` 是面板的命脉 —— 连「管道在不在跑」都从它读
        (`./kb.sh` 杀进程前那道保护就是),一个只读字段不该让那一问变成 500。
        """
        self._begin({'channel': 'rss'}, stages=('crawl', 'export'))
        pipeline_state._stage('crawl', 'ok', '好了')
        pipeline_state._stage('export', 'error', '摆盘:导出红了')
        pipeline_state._end_run()
        with patch.object(daybook, 'unfinished', side_effect=RuntimeError('合成炸了')):
            self.assertEqual(['export'], pipeline_retry.today_failed(),
                             '炸了也该退回最近那一轮,不许把异常抛给状态接口')

    def test_a_batch_aggregate_is_not_smeared_over_the_other_sources(self):
        """最近那一份是**整批汇总**时,不许拿它盖回账本 —— 它的来源清单是全批的。

        汇总继承最后那个子轮的身份,而账本里那几格就是那一轮自己写的,所以按身份
        认得出来、不用盖。盖的话一批里 RSS 失败 + 公众号成功合成一个 error,再按
        合并后的来源清单摊开,公众号那一格也成了 error —— 继承当场反向,下一次重试
        去重抓一个今天已经抓成了的来源。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.wechat_round()
        latest = deepcopy(pipeline_state.last_result())
        latest['stages']['crawl'] = {'status': 'error',
                                     'detail': '整批合起来:1 个来源失败'}
        latest['crawl_filter'] = {'channel': 'rss', 'channels': [
            {'channel': 'rss', 'accounts': []},
            {'channel': 'wechat_local', 'accounts': []}]}
        pending, _ = daybook.unfinished(latest)
        self.assertEqual({'channel': 'rss', 'accounts': []}, pending['crawl_filter'],
                         '公众号今天抓成过了,汇总那一份不该把它也拖进来重抓')

    def test_an_older_round_does_not_come_back_over_a_newer_book(self):
        """**反过来钉一边**:账本比「最近一次运行」新时,不许拿旧那一轮的红盖回去。

        跑着的时候 `last_result()` 回落的是磁盘上**上一轮**那份(口径在
        `pipeline-history.md`),而账本里可能已经有更新的一格。按它盖回去的话今天
        之内那个来源再也变不回绿色 —— 「继承」当场反向;跨天那一档走的也是这条
        (昨晚那一轮比今天任何一格都旧,它的红不该搬进今天)。
        """
        today = daybook._day()
        self.crawl({'channel': 'rss'}, broken=[self.a])
        stale = deepcopy(pipeline_state.last_result())
        stale['finished_at'] = f'{today} 09:00:00'
        self.crawl({'channel': 'rss'})                  # 同一个来源,后来抓成了
        raw = json.loads(daybook._book_file().read_text(encoding='utf-8'))
        raw['stages']['crawl']['rss']['at'] = f'{today} 10:20:00'
        daybook._book_file().write_text(json.dumps(raw, ensure_ascii=False),
                                        encoding='utf-8')
        pending, _ = daybook.unfinished(stale)
        self.assertNotEqual('error', pending['stages']['crawl']['status'],
                            '账本里那一格更新,旧那一轮的红不该盖回去')
        self.assertNotIn('crawl', self.plan(), '它今天已经抓成过了,不该再抓')

    def test_an_earlier_failure_survives_another_sources_success(self):
        """**这一条就是 10:34 那一次。** 早些时候 RSS 抓取/导出红了,之后一轮公众号
        成功收尾 —— 那一轮压根没碰 RSS,不许把它那两条红盖掉。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a], also_red=['export'])
        self.wechat_round()
        self.assertEqual('wechat_local',
                         (pipeline_state.load_last_run() or {})['crawl_filter']['channel'],
                         '这一局的前提:最近那一轮是公众号')
        plan = self.plan()
        self.assertIn('crawl', plan, '今天早些时候那条 RSS 抓取失败被顶掉了')
        self.assertIn('export', plan, '它那条导出失败也被顶掉了')

    def test_the_retry_really_goes_back_to_that_subscription(self):
        """判据落在**真抓了谁**上:范围也要跟着从账本来,不是从最近那一轮。"""
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.wechat_round()
        got, _ = self.retry()
        self.assertEqual({self.a['id']}, got, '重试没去抓今天早些时候失败的那一条')

    def test_a_later_success_on_the_same_source_is_inherited(self):
        """**反过来钉一边**:同一个来源后来真抓成了,就不许再抓一遍。

        这才是「继承」。账本那几格要是取**最重**的那一档而不是**最后**那一档,
        今天之内它再也变不回绿色,这颗按钮每次都去重抓一遍已经好了的来源。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.crawl({'channel': 'rss'})                  # 同一个来源,这次全成了
        self.wechat_round()
        self.assertNotIn('crawl', self.plan(), '它今天已经抓成过了,不该再抓')

    def test_a_stage_nobody_ran_today_is_not_a_failure(self):
        """反过来钉一边:今天压根没跑过的那几步是「未选择」,不是「失败」。

        账本里没有那一格时合成成失败的话,每次重试都把整条链从头跑一遍 —— 那正是
        10:34 那一次的形状(用户看到的就是「又从头跑了」)。链式规则本来就会让
        抓取之后跟上 index/extract(新抓回来的东西要入库),所以判据钉在**那一格
        合成成什么**和**链尾那两步在不在**上。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        pending, _ = daybook.unfinished(pipeline_state.last_result())
        for step in ('index', 'extract', 'projects', 'wiki'):
            self.assertEqual({'status': 'skipped', 'detail': '未选择'},
                             pending['stages'][step], f'{step} 今天没跑过')
        plan = self.plan()
        for step in ('projects', 'wiki'):
            self.assertNotIn(step, plan, f'{step} 在链尾、不牵连别人,不该排进来')

    def test_yesterdays_book_does_not_count(self):
        """**不跨天**:把账本改成昨天的,它一格都不参与,退回最近一次运行那一份。"""
        self.crawl({'channel': 'rss'}, broken=[self.a], also_red=['export'])
        self.wechat_round()
        raw = json.loads(daybook._book_file().read_text(encoding='utf-8'))
        raw['day'] = '2000-01-01'
        daybook._book_file().write_text(json.dumps(raw, ensure_ascii=False),
                                        encoding='utf-8')
        self.assertEqual({}, daybook.read(), '换了一天就该当空账本')
        self.assertEqual([], self.plan(),
                         '昨天的账本不该把今天的计划撑起来(最近那一轮是全绿的公众号)')

    def test_an_unreadable_book_falls_back_and_says_so(self):
        """账本读不出来:退回最近一次运行,**而且那句话要跟着起跑消息出去**。

        不说的话用户以为今天欠的都补上了,而这一次只按最近那一轮补。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        pending, note = daybook.unfinished(pipeline_state.last_result())
        self.assertEqual('', note, '这一局的前提:正常时没有降级说明')
        with patch.object(daybook, 'read', side_effect=OSError('账本读不动')):
            pending, note = daybook.unfinished(pipeline_state.last_result())
            self.assertIn('账本读不动', note, '降级原因没带出来')
            self.assertEqual('rss', pending['crawl_filter']['channel'],
                             '退回的那一份该是最近一次运行')
            got, _ = self.retry()
        self.assertEqual({self.a['id']}, got, '退回之后照旧要补那一条')

    def test_the_book_write_does_not_hold_up_the_round(self):
        """反过来钉一边:账本写不上**不许把一轮已经做完的同步变成「永远进行中」**。

        它和另两份留档一样是旁路(口径在 `pipeline_state._end_run` 那段)。写不上的
        代价只是退回存量行为:那颗按钮按最近一次运行来补。
        """
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')):
            self.crawl({'channel': 'rss'}, broken=[self.a])
        self.assertFalse(pipeline_state._state['running'], '账本写炸了,收尾也得完成')
        self.assertIn('OSError', pipeline_state.persist_error(), '是什么坏了要如实记')
        self.assertTrue(any('当日账本' in line
                            for line in pipeline_state.get_state()['log']),
                        f'日志里要说清哪一份没写上:{pipeline_state.get_state()["log"]}')

    def test_the_whole_library_steps_are_not_pinned_on_a_source(self):
        """全库那几步不属于任何来源:它们只占一格,而且跟抓哪个来源无关。

        挂到来源上的话,日报那一轮(入库段不带来源)会把 index 的红记在公众号头上,
        而下一次只抓 RSS 的重试就看不到它了。
        """
        self.crawl({'channel': 'rss'}, also_red=('index',))
        cells = daybook.read()['stages']
        self.assertEqual([''], list(cells['index']), 'index 那一格不该挂在来源上')
        self.assertIn('index', self.plan())


if __name__ == '__main__':
    unittest.main()
