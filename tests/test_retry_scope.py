# -*- coding: utf-8 -*-
"""「重试失败项」的范围:只抓上一轮失败的那几个,也绝不漏掉它们。

背景(2026-10-09 用户报告):大V观点同步时一个账号地址填错导致那一条失败,改好
之后点「重试失败项」—— 51 个账号的时间线全被重新读了一遍。两个**各自独立**的
缺口叠出来的:

①那一轮的 crawl 范围是**原样继承**上一轮的 `crawl_filter`,而上一轮是"同步
  大V观点(全部)",继承来的 `accounts` 是空的 —— 而空的含义正好是"全部"。
②`FetchScope.force` 的注释从一开始就写着"手动重试可跳过来源退避",却压根没人
  传过它;而失败会给那条订阅记一个 `retry_at`(最短 1800 秒,见
  `sync._save_failure`),于是失败后半小时内点这颗按钮,唯一该抓的那条反而被
  记成 `deferred` 跳过。

合起来这颗按钮的实际行为是「该抓的不抓、不该抓的全抓」。而收窄本身有两个反向的
坑,各有一组用例钉着:

- **收窄过头**(第 1 轮审查实测):按"现在 `last_error` 还在不在"筛,会把用户刚
  改正过地址的那条当成"已经恢复"漏掉 —— 改址时 `save_subscription` 的 reset 会
  把 `last_error` 连同退避一起清空。那正是这颗按钮唯一的用法,修完地址点它,那个
  账号一条都不抓。
- **收窄到别人头上**(同轮审查实测):拿"合并之后有没有逐项失败"当一个全局开关,
  RSS 的一条逐项失败会让**所有**来源都按失败项收窄,而整批失败的那个来源一份名单
  都没留下、被收窄成空集。

所以下面每一条的判据都是**哪些订阅真的被抓了**(数 `fetch_source` 收到的订阅 id、
以及浏览器预取收到的账号名单),不是"有没有打上标志"—— 只验标志的话,一个把标志
传下去却没人认它的实现也能全绿。
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from kb import pipeline_retry, pipeline_segments, pipeline_stages
from kb import pipeline_state
from kb.feeds import registry as reg, sync
from kb.feeds.fetching import FeedError
from kb.collectors.feeds import FeedCollector
from tests.retry_scope_fixture import RetryScopeBase


class RetryScopeTests(RetryScopeBase):
    """单来源:只抓失败的那几条,而且一条不漏。"""

    def test_retry_touches_only_the_failed_subscription(self):
        """一条地址错了 → 重试只抓它,另两条一次请求都没发出去。

        红的就是用户报告里那件事:继承来的空 accounts = 全部,三条全被重抓。
        """
        first, stage = self.crawl({'channel': 'rss'}, broken=[self.a])
        self.assertEqual({self.a['id'], self.b['id'], self.c['id']}, first)
        self.assertEqual('error', stage['status'])

        got, stage = self.retry()
        self.assertEqual({self.a['id']}, got,
                         '重试应当只碰上一轮失败的那一条,别的订阅一次请求都不该发')
        self.assertEqual('', self.last_error(self.a), '它真的被重抓到了')
        self.assertIn('检查 1/1 个来源', stage['detail'])

    def test_a_corrected_address_is_still_retried(self):
        """**第 1 轮审查那条拦验收**:改正地址之后点重试,必须抓那条改好的。

        这正是用户那一轮的形态 —— 他先把地址改对了,然后点这颗按钮。而改链接时
        `save_subscription` 的 reset 会把 `last_error` 连同退避一起清空,照"现在
        还失败着吗"筛的实现在这里一个请求都不发:界面显示「已跳过」,原来的失败
        提示和重试按钮一起消失,那个账号从此谁也不去抓(审查实测的末态是预取名单
        `[]`、抓取名单 `[]`、crawl `skipped`)。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        reg.save_subscription('rss', {'name': 'A', 'url': 'https://a-fixed.org/feed'},
                              self.a['id'])
        self.assertEqual('', self.last_error(self.a),
                         '这一局的前提:改链接确实把失败状态清空了')

        got, stage = self.retry()
        self.assertEqual({self.a['id']}, got,
                         '改正过地址的那条必须被重抓,别的订阅照旧不碰')
        self.assertNotEqual('skipped', stage['status'])
        self.assertTrue(any('a-fixed.org' in u for u in self.urls(self.a)),
                        '要按**改好之后**那个地址抓,而不是只是"碰过它"')

    def test_a_subscription_that_worked_before_and_broke_now_is_retried(self):
        """上一轮还好着、这一轮才坏的那条,照样要重抓。

        它身上留着一个**旧的** `last_success`,而名单里记的是这一轮的时刻 ——
        两个时间戳必须是同一套格式才比得出先后(`reg.now()` 的 UTC ISO 和
        `pipeline_state._now()` 的本地空格格式混用时,ISO 那个在同一天里恒大于
        空格那个,于是"它后来成功过"一律答成"是",整份名单当场被摘空)。
        """
        self.crawl({'channel': 'rss'})
        self.assertTrue(reg.subscription('rss', self.a['id'])['last_success'],
                        '这一局的前提:它确实成功过一次')
        self.crawl({'channel': 'rss', 'force': True}, broken=[self.a])

        got, _ = self.retry()
        self.assertEqual({self.a['id']}, got, '"以前成功过"不等于"现在不用重试"')

    def test_retry_reaches_a_subscription_still_inside_its_backoff(self):
        """失败刚发生、退避还没过,重试照样抓它。

        红的是 `deferred 1` —— 没有 `force` 时这颗按钮在失败后的半小时里
        什么都不做,而那半小时正是用户改完地址就点它的那段时间。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.assertGreater(reg.subscription('rss', self.a['id'])['retry_at'],
                           time.time(), '这一局的前提:退避确实还没过')

        got, stage = self.retry()
        self.assertEqual({self.a['id']}, got, '退避不该把手动重试挡在外面')
        self.assertNotIn('等待重试', stage['detail'])

    def test_retry_does_not_widen_beyond_last_rounds_targets(self):
        """上一轮只同步了 a,另有一条历史失败的 c —— 重试不许把 c 一起拖进来。

        「重试失败项」重试的是**上一轮**的失败,不是把库里所有失败订阅扫一遍。
        """
        self.crawl({'channel': 'rss'}, broken=[self.c])          # c 留下历史失败
        self.crawl({'channel': 'rss', 'accounts': [str(self.a['id'])]},
                   broken=[self.a])
        self.assertTrue(self.last_error(self.c) and self.last_error(self.a))

        got, _ = self.retry()
        self.assertEqual({self.a['id']}, got, '范围要和上一轮点名的目标取交集')

    def test_plain_sync_is_never_narrowed_to_failed_sources(self):
        """反过来钉一边:普通同步照旧抓全部,不许按失败名单收窄。

        收窄只属于重试那一轮 —— 漏进常规路径的话,一条失败过的订阅会让以后每一轮
        同步都只抓它自己。第二轮带 `force` 是为了把两件事分开:force 只放开退避
        (不然失败的 a 会被它自己的 `retry_at` 挡住,这条用例就看不出收窄了没有)。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        got, stage = self.crawl({'channel': 'rss', 'force': True})
        self.assertEqual({self.a['id'], self.b['id'], self.c['id']}, got)
        self.assertIn('检查 3/3 个来源', stage['detail'])

    def test_everything_already_fixed_refuses_and_says_where_to_go(self):
        """失败的那条后来已经成功同步过了:一个请求都不发,而且要说清为什么。

        这一档和上面"改过地址"那条**必须分得开**:改过地址的那条
        `last_success` 被 reset 清成了空串(它还没验证过),而这一条是真的成功过
        —— 判据是"那一轮之后有没有成功同步过",不是"现在还有没有错误字符串"。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.succeeded_later(self.a)

        msg = self.refuse()
        self.assertEqual(pipeline_retry.RETRY_NOTHING, msg)
        self.assertIn('我的订阅', msg, '要指到真能重抓某一条的那个入口')

    def test_nothing_to_crawl_never_falls_back_to_the_default_source(self):
        """"一个都不用抓"不能用空范围表达:crawl 压根不该进计划。

        `crawl_channels` 对"没指定来源"会回落到默认采集器(公众号),真让一个
        空范围的 crawl 跑起来,抓的是另一个来源。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.succeeded_later(self.a)
        self.assertEqual([], pipeline_retry.retry_crawl_scopes(
            pipeline_state.load_last_run() or {}))
        self.assertNotIn('crawl', pipeline_retry.retry_failed_plan(nothing_to_crawl=True))
        with patch.object(pipeline_retry, '_run_crawl') as crawl:
            self.refuse()
        crawl.assert_not_called()

    def test_the_crawl_card_explains_an_empty_retry_round(self):
        """还有别的阶段要重试时,crawl 那张卡片要说清它为什么没跑。

        一张写着「本次无需重试」的灰卡片和"这个按钮坏了"长得一模一样 ——
        而上一轮 crawl 明明是红的。
        """
        # 让 index 也红:这一轮的计划里还有别的阶段,于是真的会起跑。走产品自己那条路
        # (`also_red` → `_stage` → `_end_run`),**两头都不手改**:重试读的是空闲时认
        # 内存的那一份(`pipeline_state.last_result`),只改磁盘文件这一局在摆局那一步
        # 就不成立;而手改内存又绕开了收尾,摆出来的跟真跑一轮留下的不是同一个东西。
        self.crawl({'channel': 'rss'}, broken=[self.a], also_red=('index',))
        self.succeeded_later(self.a)
        with patch.object(pipeline_retry, '_run_crawl') as crawl, \
             patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, _ = pipeline_retry.start_retry_failed()
            self.assertTrue(ok)
            self._wait_idle()
        crawl.assert_not_called()
        self.assertIn('已成功同步', self.stage()['detail'])


class SourceLevelFailureTests(RetryScopeBase):
    """来源级整轮故障:一条订阅都没被碰到,这时该重抓整个来源。

    这一档和「失败项已经修好」在 `subscriptions` 那边**长得一模一样**(都是一个
    `last_error` 都没有),所以判据不能是订阅行上的状态 —— 分得开它们的是这个来源
    这一轮**有没有留下失败名单**:整批失败时 `collect` 整个抛出去,那时一条订阅都
    还没被碰到,也就没有名单可言(见 `pipeline_segments.note_retry_targets`)。
    """

    def crawl_blowing_up(self, crawl_filter):
        """让 `collect` 整个抛出去:一条订阅都没碰到,也就没有名单。"""
        self._begin(crawl_filter)
        with patch.object(sync, '_prefetch_x',
                          side_effect=RuntimeError('登录态读取器崩了')):
            with self.assertRaises(RuntimeError):
                pipeline_stages._run_crawl(crawl_filter)
        pipeline_state._end_run()
        return self.stage()

    def test_a_source_level_blowup_is_retried_in_full(self):
        stage = self.crawl_blowing_up({'channel': 'rss'})
        self.assertEqual('error', stage['status'])
        self.assertEqual({}, pipeline_state.retry_notes(
            pipeline_state.load_last_run() or {}), '这一局的前提:没有留下名单')
        self.assertFalse(any(self.last_error(s) for s in (self.a, self.b, self.c)),
                         '这一局的前提:一条订阅都没被碰到')

        got, _ = self.retry()
        self.assertEqual({self.a['id'], self.b['id'], self.c['id']}, got,
                         '整轮都没抓到时该重抓整个来源,而不是收窄成空集')

    def test_a_per_subscription_failure_still_narrows(self):
        """反过来钉一边:留下了名单的那一轮照旧收窄。

        两条用例合起来才说明判据是"名单在不在",不是"一律收窄"或"一律不收窄"。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self.assertEqual(
            [str(self.a['id'])],
            pipeline_state.retry_notes(
                pipeline_state.load_last_run() or {})['rss']['targets'])
        got, _ = self.retry()
        self.assertEqual({self.a['id']}, got)


class StillFailingTests(RetryScopeBase):
    """那道「现在还要不要重抓」的判据,直接测(见 `sync.still_failing`)。

    三种情况必须分得开,而只看 `last_error` 的实现把前两种混成了一种。
    """

    def note(self, *sources):
        return {'targets': [str(s['id']) for s in sources], 'at': reg.now()}

    def test_a_cleared_error_does_not_mean_recovered(self):
        """改过地址(reset 清空了 last_error 和 last_success)→ 仍要重抓。"""
        self.crawl({'channel': 'rss'}, broken=[self.a])
        note = self.note(self.a)
        reg.save_subscription('rss', {'name': 'A', 'url': 'https://a-fixed.org/feed'},
                              self.a['id'])
        row = reg.subscription('rss', self.a['id'])
        self.assertEqual(('', ''), (row['last_error'], row['last_success']),
                         '这一局的前提:reset 把两个都清空了')
        self.assertEqual([str(self.a['id'])], sync.still_failing('rss', note))

    def test_a_later_success_takes_it_off_the_list(self):
        """名单之后真的成功同步过 → 不必再抓一遍。"""
        self.crawl({'channel': 'rss'}, broken=[self.a])
        note = self.note(self.a)
        self.succeeded_later(self.a)
        self.assertEqual([], sync.still_failing('rss', note))

    def test_an_older_success_keeps_it_on_the_list(self):
        """成功是在名单**之前**(以前好着、这一轮才坏)→ 照样要抓。"""
        self.crawl({'channel': 'rss'})
        self.crawl({'channel': 'rss', 'force': True}, broken=[self.a])
        note = self.note(self.a)
        self.assertTrue(reg.subscription('rss', self.a['id'])['last_success'])
        self.assertEqual([str(self.a['id'])], sync.still_failing('rss', note))

    def test_a_gone_or_disabled_subscription_drops_out(self):
        """被删掉、停用的不进范围:没有可抓的东西。"""
        note = self.note(self.a, self.b, self.c)
        reg.save_subscription('rss', {'enabled': False}, self.b['id'])
        reg.delete_subscription('rss', self.c['id'])
        self.assertEqual([str(self.a['id'])], sync.still_failing('rss', note))


class RetryNoteContractTests(RetryScopeBase):
    """名单那条链的契约:谁写、写不写、什么时候不算数。"""

    def test_a_clean_round_still_leaves_an_empty_list(self):
        """一条都没失败也要留下名单 —— 那份空名单本身就是一句话。

        「扫完了、没有要重抓的」和「压根没留下名单(整批失败)」是两件相反的事,
        不留空名单的话两者就再也分不开了。
        """
        self.crawl({'channel': 'rss'})
        notes = pipeline_state.retry_notes(pipeline_state.load_last_run() or {})
        self.assertEqual({'rss'}, set(notes))
        self.assertEqual([], notes['rss']['targets'])

    def test_a_note_from_an_earlier_run_does_not_count(self):
        """名单带着自己那一轮的身份:上一轮的名单不许拿来收窄这一轮。

        整批失败那一轮一份名单都没留下,而 `_state` 里还躺着**更早**那一轮的名单
        —— 它会连同这一轮的状态一起落盘,照它收窄就是按上一轮的失败去抓。
        """
        self.crawl({'channel': 'rss'}, broken=[self.a])
        stale = pipeline_state._state['crawl_retry']
        self.assertEqual([str(self.a['id'])], stale['channels']['rss']['targets'])

        self.crawl_run_id_changes_but_nobody_notes()
        run = pipeline_state.load_last_run() or {}
        self.assertEqual(stale['channels'], run['crawl_retry']['channels'],
                         '这一局的前提:陈旧名单真的被一起落盘了')
        self.assertEqual({}, pipeline_state.retry_notes(run))

    def crawl_run_id_changes_but_nobody_notes(self):
        """换一轮运行,而这一轮谁也没写名单(来源级故障的形状)。"""
        self._begin({'channel': 'rss'})
        pipeline_state._stage('crawl', 'error', '登录态读取器崩了')
        pipeline_state._end_run()

    def test_a_new_run_starts_its_own_list(self):
        """换届就重开一份,不跟上一轮的名单混在一起。"""
        self.crawl({'channel': 'rss'}, broken=[self.a])
        self._begin({'channel': 'rss'})
        pipeline_segments.note_retry_targets('rss', [self.b['id']], reg.now())
        note = pipeline_state._state['crawl_retry']
        self.assertEqual(pipeline_state._state['run_id'], note['run'])
        self.assertEqual([str(self.b['id'])], note['channels']['rss']['targets'])

    def test_a_source_that_cannot_narrow_keeps_its_original_range(self):
        """公众号报不出账号级的失败名单 —— 连名单硬塞给它也按原范围重跑。

        它的失败是文章级的(`title` 是文章标题、「推送归属待核实」这类句子,见
        `wechat_local._report`),而 `--targets` 是账号级的,压根对不上;正文重试
        本来就由采集器自己的 `article_fetch_state` 退避管。
        """
        from kb.collectors import active_collector, retry_narrower
        self.assertIsNone(retry_narrower(active_collector('wechat_local')))
        self.assertIsNotNone(retry_narrower(active_collector('rss')))

        prev = {'run_id': 'run-x',
                'crawl_filter': {'channel': 'wechat_local', 'accounts': ['号A']},
                'crawl_retry': {'run': 'run-x', 'channels': {
                    'wechat_local': {'targets': ['号A'], 'at': reg.now()}}}}
        self.assertEqual(
            [{'channel': 'wechat_local', 'accounts': ['号A'], 'retry': 'all'}],
            pipeline_retry.retry_crawl_scopes(prev))


class StartupIsolationTests(RetryScopeBase):
    """造范围这一步的故障,不许扩散到跟它无关的阶段上。

    这颗按钮的射程远不止抓取:上一轮只有索引失败、这会儿订阅库又恰好打不开
    (锁着、权限、路径没挂上)时,启动路径无条件先去问一遍各来源的失败名单,
    那个异常就一路冒到 HTTP 层 —— 用户收到的是一句「Internal Server Error」,
    索引和后面的提取一步都没起跑(2026-10-09 第 2 轮审查实测)。

    判据一律是**后果**:哪几个阶段真的跑起来了、哪几个来源真的被抓了。
    """

    def broken_db(self):
        """把订阅库换成一个**目录**:`reg.db()` 那句 connect 当场抛
        `OperationalError: unable to open database file`。

        不能"指到一个不存在的目录" —— `reg.db()` 自己会 `mkdir(parents=True)`,
        那样它照样连得上一个空库。
        """
        path = self.root / 'broken-feeds.db'
        path.mkdir()
        return patch.object(reg, 'DB_PATH', path)

    def index_only_round(self):
        """摆一轮「crawl 全成功、只有 index 失败」的已完成运行。"""
        crawl_filter = {'channel': 'rss', 'accounts': []}
        self._begin(crawl_filter, stages=('crawl', 'index'))
        with patch.object(sync, 'fetch_source',
                          side_effect=self._fetcher(set(), (), 'p1')):
            pipeline_stages._run_crawl(crawl_filter)
        self.assertEqual('ok', self.stage()['status'], '这一局的前提:抓取成功')
        pipeline_state._stage('index', 'error', '索引失败')
        pipeline_state._end_run()
        self.assertEqual(['index', 'extract'], pipeline_retry.retry_failed_plan(),
                         '这一局的前提:计划里压根没有 crawl/export')

    def test_an_index_only_retry_survives_an_unusable_feeds_db(self):
        """**后果那一条**:复现审查那份证据(HTTP 500、索引压根没起跑)。

        它要**两道同时失效**才红(实测:退回被审那一版红在这里,单退任一道时
        另一道兜住了它)—— 两道各自的契约由下面两条分别钉着,这一条钉的是
        "合起来真的把那件事挡住了"。
        """
        self.index_only_round()
        ran: list[str] = []
        with self.broken_db(), \
             patch.object(pipeline_retry, '_run_crawl',
                          side_effect=AssertionError('crawl 不在这一轮的计划里')), \
             patch.object(pipeline_retry, '_run_export',
                          side_effect=AssertionError('export 不在这一轮的计划里')), \
             patch.object(pipeline_retry, '_run_index',
                          side_effect=lambda: ran.append('index')), \
             patch.object(pipeline_retry, '_run_extract',
                          side_effect=lambda: ran.append('extract')), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            # 抛出来的话 HTTP 层就是 500 —— 这一句本身就是那条断言
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')
        self.assertEqual(['index', 'extract'], ran,
                         '索引和提取一步都没起跑,而故障在一个它们不碰的库上')

    def test_the_plan_decides_before_any_target_lookup(self):
        """crawl 不在计划里 → 一次名单查询都不该发生。

        **这一道和采集器无关**,所以它必须自己有一条用例:下面那条"空名单不开库"
        是 feeds 这个采集器自己的早返回,换一个无条件读自己库的采集器就不成立了;
        而顺序这一道管的是"压根不去问",对谁都成立。

        判据是**问了几次**,不是"抛没抛异常" —— `retry_crawl_scopes` 那道
        `except Exception` 把 AssertionError 也一并吞掉(它正是为了"别把整次重试
        带走"),拿一个会被吞掉的异常当判据,这条用例对顺序压根不敏感。
        """
        self.index_only_round()
        asked: list[dict] = []
        with patch.object(FeedCollector, 'retry_targets', autospec=True,
                          side_effect=lambda _self, note: asked.append(note) or []), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')
        self.assertEqual([], asked,
                         '这一轮压根不重抓,却先去问了各来源的失败名单')

    def test_an_empty_note_never_opens_the_subscription_db(self):
        """那个来源这一轮扫完了、没有要重抓的 → 答案只能是空集,别去开库。

        这是第三道:上面那条管的是"crawl 不在计划里",这条管的是"在计划里、
        而这个来源没有要重抓的"。
        """
        with patch.object(reg, 'subscriptions',
                          side_effect=AssertionError('名单是空的,不该读订阅库')):
            self.assertEqual([], sync.still_failing('rss', {'targets': [],
                                                            'at': reg.now()}))

    def test_a_broken_db_falls_back_to_the_original_range_for_that_source(self):
        """名单问不出来 = 这个来源收窄不了,按原范围重跑,**别把整次重试带走**。

        故障只圈在这一个来源里:认名字和问名单两件事都在那道 try 里面
        (问名单要读这个来源自己的订阅库)。
        """
        self.crawl({'channel': 'rss', 'accounts': []}, broken=[self.a])
        note = pipeline_state.retry_notes(pipeline_state.load_last_run() or {})
        self.assertEqual([str(self.a['id'])], note['rss']['targets'],
                         '这一局的前提:名单里有那条失败的订阅')

        seen: list[dict] = []
        with self.broken_db(), \
             patch.object(pipeline_retry, '_run_crawl',
                          side_effect=lambda f=None: seen.append(f)), \
             patch.object(pipeline_retry, '_run_export'), \
             patch.object(pipeline_retry, '_run_index'), \
             patch.object(pipeline_retry, '_run_extract'), \
             patch.object(pipeline_retry, '_run_projects'), \
             patch.object(pipeline_retry, '_run_wiki'):
            ok, msg = pipeline_retry.start_retry_failed()
            self.assertTrue(ok, msg)
            self._wait_idle('重试那一轮没有收尾')
        self.assertEqual(1, len(seen), '抓取这一步没起跑')
        self.assertEqual([{'channel': 'rss', 'accounts': []}],
                         pipeline_stages.crawl_channels(seen[0]),
                         '收窄不了就该按原范围重跑,不是收成空集')


if __name__ == '__main__':
    unittest.main()
