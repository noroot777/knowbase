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
⑦**那颗按钮露不露脸**也得按「今天」判 —— 后端补得再对,按钮藏掉就没人点得到;
⑧**结清的判据是「这一次真跑过的范围覆盖了这一格」**:同一条通道里只跑了订阅 B 的
  那一轮,不许把 A 的失败也结清;反过来整条通道真跑了一遍就该把这一步的旧账清空。
  导出那一步照它自己那份范围记账(`export_filter`),不是抓取那一份;

**缺口与降级那一族在 `tests/test_daybook_gaps.py`**(写不上账本的那一轮、账本读不出来、
合不成、旧日那些内存欠账)。摆盘共用 `tests/daybook_case.py`。

跑:  uv run --offline python -m unittest tests.test_retry_daybook -v
"""
from __future__ import annotations

import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from kb import (pipeline_daybook as daybook, pipeline_retry, pipeline_stages,
                pipeline_state)
from tests.daybook_case import DaybookCase


class DaybookRetryTests(DaybookCase):
    """记账单位、归因、继承与日界 —— 账本**记对了没有**。"""

    # ── 记账单位:结清的是**这一次真跑过的范围** ────────────────────────────

    def test_a_failed_subscription_survives_another_subscription_succeeding(self):
        """**第 2 轮审查那一档**:同一条通道里先只同步 A 失败、再只同步 B 成功。

        B 那一轮压根没碰 A,不许把 A 的失败结清。记到通道那一层就够的话,RSS 那一格
        整个变成 B 的成功 —— A 从此不在计划里,而它的 `last_error` 还在。
        """
        self.crawl({'channel': 'rss', 'accounts': [self.a['name']]}, broken=[self.a])
        self.crawl({'channel': 'rss', 'accounts': [self.b['name']]})
        self.assertTrue(self.last_error(self.a), '这一局的前提:A 真的还坏着')
        self.assertIn('crawl', self.plan(), 'A 的失败被一轮只跑 B 的成功结清了')
        got, _ = self.retry()
        self.assertEqual({self.a['id']}, got, '重试没回到 A 那一条')

    def test_an_earlier_failure_in_the_same_channel_is_owed_too(self):
        """同一条通道里:c 今天早些时候失败,这一轮只点名了 a 也失败 —— **两条都补**。

        这是「只跑 B 成功」那一局的正面:上一轮点名了谁不该成为今天欠账的上限。
        记到通道那一层的话,第二轮(只点名 a)会把 c 那一笔整个盖掉。
        边界在另一边 —— **别的日子**那条历史失败不许被拖进来
        (`tests/test_retry_scope.py` 的 `test_retry_does_not_widen_beyond_last_rounds_targets`)。
        """
        self.crawl({'channel': 'rss'}, broken=[self.c])
        self.succeeded_on(self.a, 1)
        self.crawl({'channel': 'rss', 'accounts': [str(self.a['id'])]}, broken=[self.a])
        got, _ = self.retry()
        self.assertEqual({self.a['id'], self.c['id']}, got,
                         '今天欠的两条没一起补上')

    def test_running_the_whole_channel_clears_that_channels_old_debt(self):
        """**反过来钉一边**:整条通道真跑了一遍且成功 —— 这一步的旧账一笔不留。

        逐订阅那几格照旧留着的话,这颗按钮每次都去重抓一遍已经好了的订阅;而它们
        确实被覆盖过了(整通道那一轮把每一条都抓了)。
        """
        self.crawl({'channel': 'rss', 'accounts': [self.a['name']]}, broken=[self.a])
        self.crawl({'channel': 'rss'})                  # 整条通道,这次全成了
        self.assertEqual([''], list(daybook.read()['stages']['crawl']['rss']),
                         '整通道那一轮没把逐订阅那几格清掉')
        self.assertNotIn('crawl', self.plan(), '它今天已经整条抓成过了,不该再抓')

    def test_a_failed_whole_channel_round_only_owes_the_names_it_listed(self):
        """整条通道失败、而采集器留下了失败名单 ⇒ 欠的只是名单里那几个。

        B/C 在那一轮里明明成功了。把整条通道记成欠账的话,收窄只能靠名单,而名单
        一旦读不出来(采集器报不出账号级名单)就退回去重抓整条通道。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.assertEqual([str(self.a['id'])],
                         list(daybook.read()['stages']['crawl']['rss']),
                         '欠的那一格该是失败名单里那一条')
        got, _ = self.retry()
        self.assertEqual({self.a['id']}, got, '重试没收窄到名单里那一条')
        # **补上之后这颗按钮就该没得补了。** 整条通道那一格留着的话,一次成功的
        # 收窄重试结清不了它 —— 今天之内每次点都报「抓取还欠着」,而该抓的那条
        # 已经抓成了。
        self.assertNotIn('crawl', self.plan(), '收窄重试成功了,抓取那一格没结清')

    def test_a_successful_export_retry_clears_that_sources_export_debt(self):
        """**第 2 轮审查那一档**:导出按**它自己那份范围**记账。

        「只补导出」那一轮压根没有抓取范围 —— 拿抓取范围给它记账,成功就记到了空
        通道上,原来那个来源的导出 error 留在原处,下一次重试又派生出一遍
        export/index/extract(补好的那一步被反复重跑)。
        """
        self._begin({'channel': 'rss'}, stages=('crawl', 'export'))
        pipeline_state._stage('crawl', 'ok', '已抓取')
        pipeline_state._stage('export', 'error', '摆盘:磁盘暂时只读')
        pipeline_state._end_run()
        seen = []
        with patch.object(pipeline_retry, '_run_export',
                          side_effect=lambda scope: (
                              seen.append(scope),
                              pipeline_state._stage('export', 'ok', '已保存 1 条'))), \
             patch.object(pipeline_retry, '_run_index',
                          side_effect=lambda: pipeline_state._stage('index', 'ok', '索引完成')), \
             patch.object(pipeline_retry, '_run_extract',
                          side_effect=lambda: pipeline_state._stage('extract', 'ok', '提取完成')), \
             self.no_backlog():
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle()
            self.assertEqual('rss', seen[0]['channel'], '导出没按那个来源跑')
            self.assertNotIn('export', self.plan(), '补好的导出还在被反复重试')

    def test_an_export_on_one_subscription_settles_that_whole_sources_export(self):
        """**审查 R3 的另一个实例**:导出扫的是**整条来源**,所以它结清整条来源。

        `materialize()` 压根不收目标(契约在 `kb.collectors.SourceCollector`),
        所以「点名 B 的那一轮导出失败、再点名 A 的那一轮导出成功」之后,B 的那笔
        导出欠账**已经被真正跑过了** —— 留着它的话下一次重试照旧派生出
        export/index/extract,补好的导出被反复重跑。

        判据落在**今天还欠不欠导出**上,不落在账本里的键长什么样。
        """
        self.export_round([self.b['name']], broken=True)
        self.assertIn('export', self.plan(), '这一局的前提:导出欠着')
        self.export_round([self.a['name']])
        self.assertNotIn('export', self.plan(),
                         '导出扫的是整条来源,它跑成了却没结清 B 那一笔')

    def test_a_subscription_that_was_not_named_keeps_its_own_export_debt(self):
        """**反过来钉一边**:换个**来源**跑导出,结清不了这个来源的欠账。

        只验上一局的话,一个「导出成功就把所有来源的导出全清掉」的实现也全绿 ——
        而那是静默丢数据:另一个来源的导出失败从此没人补。
        """
        self.export_round([self.b['name']], broken=True)
        self.wechat_round()            # 公众号那一轮的抓取 + 导出都成功
        self.assertIn('export', self.plan(),
                      '另一个来源跑了一次导出,把 RSS 的导出欠账也清掉了')

    def test_targets_outside_the_failure_list_are_not_owed(self):
        """**审查 R2 的同来源实例**:一次请求点名几条订阅、只有一部分失败。

        不许给每个被点名的目标都套上整步那个失败结论:名单恰恰是采集器说「只有
        这几个还要重抓」,而别的那几条这一轮确实抓成了。记成失败的后果看得见 ——
        面板上那行「本次范围」会把一条好着的订阅也念成要补的。

        判据落在**「今天还欠什么」那份快照自己的范围**上(面板那行字读的就是它),
        不落在重试最终抓了谁:失败名单在后面还会收窄一次,那一道会把这个坏法遮住。
        """
        self.crawl({'channel': 'rss',
                    'accounts': [self.a['name'], self.b['name']]}, broken=[self.a])
        snap, _ = daybook.unfinished(pipeline_state.last_result())
        self.assertEqual([str(self.a['id'])],
                         snap['crawl_filter'].get('accounts'),
                         '把一条这一轮明明抓成了的订阅也记成欠着')

    def test_a_target_that_really_failed_is_still_owed(self):
        """**反过来钉一边**:名单里那几个照旧欠着。"""
        self.crawl({'channel': 'rss',
                    'accounts': [self.a['name'], self.b['name']]},
                   broken=[self.a, self.b])
        snap, _ = daybook.unfinished(pipeline_state.last_result())
        self.assertEqual({str(self.a['id']), str(self.b['id'])},
                         set(snap['crawl_filter'].get('accounts') or []),
                         '两条都失败了,却有一条不欠着')

    def test_a_source_that_reported_ok_then_blew_up_is_still_owed(self):
        """报完「好了」又抛出去 —— **那个来源照旧欠着**。

        这一道单独有用例是因为 `channel_run` 收尾那一下只兜「一次结论都没报过」:
        报过终态的那一格停在它自己那个 ok,而编排层会把**整步**记成 error。于是
        账本说这个来源好着、面板说这一步红了,这个来源一笔欠账都没有 —— 而它很
        可能已经往 vault 落了一半(漏报是静默丢数据)。
        """
        from kb.collectors.feeds import FeedCollector
        scope = {'channel': 'rss', 'accounts': []}
        self._begin(scope, stages=('crawl',))

        def fetch(_self, _scope):
            pipeline_state._stage('crawl', 'ok', '检查 3/3 个来源，新增 5 条')
            raise OSError('摆盘:记账那一步炸了')

        with patch.object(FeedCollector, 'fetch', fetch):
            try:
                pipeline_stages._run_crawl(scope)
            except OSError as e:
                # 编排层那一下照着生产来(`_run_pipeline` 的 crawl 分支)。
                pipeline_state._stage('crawl', 'error', f'抓取失败:{e}')
        pipeline_state._end_run()
        self.assertIn('crawl', self.plan(),
                      '它报完 ok 又炸了,账本却说这个来源好着')

    def test_a_source_the_stop_never_reached_is_not_owed(self):
        """按了停止、第二个来源压根没起跑 —— **它不许留下一笔欠账**。

        建账要在跑之前(那一格才一定带着覆盖范围),可**停止那一道必须在建账之前**:
        建了账就等于说它有结论,而收尾会把没报过结论的那一格记成 error。于是按了
        停止反倒给今天留下一笔欠账,下一次重试去抓一个用户明确叫停的来源 —— 而整步
        那一格是 `stopped`,按现有口径不算欠,两处当场对不上。
        """
        from kb.collectors import active_collector
        scope = {'channels': [{'channel': 'wechat_local', 'accounts': []},
                              {'channel': 'rss', 'accounts': []}]}
        self._begin(scope, stages=('crawl',))
        wechat = active_collector('wechat_local')
        def first(*_a):
            pipeline_state._stage('crawl', 'ok', '公众号抓完了')
            pipeline_state._state['stop_requested'] = True
        with patch.object(wechat, 'fetch', side_effect=first):
            pipeline_stages._run_crawl(scope)
        pipeline_state._end_run()
        self.assertNotIn('crawl', self.plan(),
                         '被停止、压根没跑的那个来源被记成欠着了')

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
        self.assertEqual('rss', pending['crawl_filter']['channel'],
                         '公众号今天抓成过了,汇总那一份不该把它也拖进来重抓')
        self.assertNotIn('channels', pending['crawl_filter'],
                         f'范围里不止 RSS 一个来源:{pending["crawl_filter"]}')
        self.assertEqual([str(self.a['id'])], pending['crawl_filter']['accounts'],
                         '欠的就是失败名单里那一条订阅')

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
        self.bump_book('crawl', f'{today} 10:20:00')
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


class LegacyBookKeyTests(DaybookCase):
    """账本里**读进来的那几个键**也要归一到稳定 id,不只是这一轮新写的那几个。

    升级前那一版按订阅的显示名记账,这一版按稳定 id 记。只归一新写入那一侧的后果是
    同一条订阅同时留下两格:旧的 `A:error` 和新的 `63:ok`。今天还欠抓取的结论来自
    旧的 A,而真去重试又被「这条订阅后来成功过」挡住 —— 页面说「4/4 完成」,又说
    「今天还有 1 个步骤没补上」,点下去回一句「没有要补的来源」,一个点不动也消不掉
    的死局(第 4 轮审查 R3 实测 `after.stages.crawl.rss.keys()=['A','63']`、
    `next_started=False`)。
    """

    def test_a_debt_recorded_under_the_old_display_name_is_settled_by_the_id(self):
        """按显示名记下的那一笔欠账,补成功之后要能被编号结清。"""
        self.crawl({'channel': 'rss', 'accounts': [self.a['name']]}, broken=[self.a])
        self.rename_book_key('crawl', 'rss', str(self.a['id']), self.a['name'])
        self.assertEqual(['crawl'], pipeline_retry.today_failed(),
                         '这一局的前提:今天欠着抓取这一步')

        got, _ = self.retry_settled()
        self.assertEqual({self.a['id']}, got, '重试抓的就是它')
        cells = daybook.read()['stages']['crawl']['rss']
        self.assertEqual([str(self.a['id'])], list(cells),
                         f'同一条订阅记成了两格:{list(cells)}')
        # **判据是后果**:今天不欠了,而且这颗按钮此刻压根没有可补的。
        self.assertEqual([], pipeline_retry.today_failed(),
                         '按名字记的那一格还红着 —— 补成功了却还说欠着')
        with self.no_backlog():
            said = self.refuse()
        self.assertIn('没有失败项', said, said)

    def test_an_old_key_that_matches_nothing_keeps_its_debt(self):
        """**反过来钉一边**:翻不出稳定身份的旧键**原样留着**,不许从账上摘掉。

        那条订阅可能刚被删掉、或者刚改了名,也可能一个名字匹配上好几条(有歧义)。
        归不出来就并不进去 —— 从账上摘掉是静默丢数据:今天欠的那一笔从此查不出来,
        而漏报补不回来;留着顶多多抓一次。
        """
        self.crawl({'channel': 'rss', 'accounts': [self.a['name']]}, broken=[self.a])
        self.rename_book_key('crawl', 'rss', str(self.a['id']), '早就删掉的那条')
        # 再点名跑一条**别的**订阅:这一轮会把 rss 那一格重折一遍(归一就发生在
        # 那时候),而它不覆盖上面那个键 —— 整条通道跑一遍才会清空旧账。
        self.crawl({'channel': 'rss', 'accounts': [self.b['name']]})
        cells = daybook.read()['stages']['crawl']['rss']
        self.assertIn('早就删掉的那条', cells,
                      f'翻不出身份的旧键被摘掉了:{list(cells)}')
        self.assertIn('crawl', self.plan(), '它那一笔欠账也跟着没了')


if __name__ == '__main__':
    unittest.main()
