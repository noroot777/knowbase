# -*- coding: utf-8 -*-
"""一轮里**谁欠着、欠的是哪几条**:逐来源归因,以及目标的稳定身份。

从 `tests/test_retry_scope.py` 分出来(那一份到了 700 行上限,CLAUDE.md 那条)。
那边验的是「收窄到失败那几个、也绝不漏掉它们」;这一份验的是更靠前的一问 ——
**这一轮的结论该记在谁头上、记成哪一格**:

①一轮同时抓几个来源、只有一个失败时,别的来源不许跟着被记成欠着(折账要读执行器
  逐来源交回来的结果账,不是合并之后那张阶段卡片);
②这份事实**跨得过重启**:它写在状态文件里,恢复不许把它丢掉;
③同一条订阅在「请求里写名字」和「失败名单里写编号」两种写法下必须是同一格。

判据一律落在**哪个来源的 `fetch` 被叫了几次**、**今天还欠不欠**,不落在账本里
哪一格什么颜色 —— 后者换个写法也能对上,前者是用户真正付代价的地方。

跑:  uv run --offline python -m unittest tests.test_channel_attribution -v
"""
from __future__ import annotations

from unittest.mock import patch

from kb import pipeline_daybook as daybook, pipeline_results as results
from kb import pipeline_retry, pipeline_state
from tests.retry_scope_fixture import RetryScopeBase


class ChannelAttributionTests(RetryScopeBase):
    """一轮里**只有一个来源失败**:别的来源不许跟着被记成欠着。

    2026-10-20 第 3 轮审查实测(R2):一轮同时抓公众号和 RSS,公众号报
    `ok/公众号已抓完`、RSS 只有 A 失败 —— 当日账本里公众号那一格也成了 `error`
    (说明里同时写着「公众号已抓完」和「RSS…失败1个」),点「重试失败项」把已经
    抓完的公众号又抓一遍。而用户要的正是「9:12 抓过的不要再抓」。

    根因是折账拿**合并之后那张阶段卡片**摊到每一个来源上,而合并卡片不带来源归属
    (`pipeline_segments._merge_segments` 把各段扁平并起来)。修法是执行器逐来源记
    一份结果账(`kb/pipeline_results.py`),折账读它。

    判据落在**公众号的 `fetch` 到底被叫了几次**,不是账本里哪一格什么颜色 ——
    后者换个写法也能对上,前者是用户真正付代价的地方。
    """

    def wechat(self):
        from kb.collectors import active_collector
        return active_collector('wechat_local')

    def wechat_round(self, seen, status='ok', detail='公众号已抓完'):
        """公众号那条通道的替身:记一次调用,报一个结论。"""
        from unittest.mock import patch
        return patch.object(self.wechat(), 'fetch', side_effect=lambda *a: (
            seen.append('wechat_local'), pipeline_state._stage('crawl', status, detail)))

    def ready_wechat(self):
        """公众号就绪 —— 不钉这个前提,重试会在「抓不动」那一道先被拦掉。"""
        from unittest.mock import patch
        from kb.collectors import SourceStatus
        return patch.object(self.wechat(), 'status', return_value=SourceStatus(
            online=True, ready=True, message='就绪', targets=('公众号甲',)))

    def both_channels(self):
        return {'channels': [{'channel': 'wechat_local', 'accounts': []},
                             {'channel': 'rss', 'accounts': []}]}

    def test_a_source_that_succeeded_is_not_recrawled_for_another_ones_failure(self):
        first = []
        with self.wechat_round(first):
            got, _ = self.crawl(self.both_channels(), broken=[self.a])
        self.assertEqual(['wechat_local'], first, '这一局的前提:公众号抓过一遍')
        self.assertEqual({self.a['id'], self.b['id'], self.c['id']}, got,
                         '这一局的前提:三条 RSS 都碰过,只有 A 失败')

        again = []
        with self.ready_wechat(), self.wechat_round(again):
            retried, _ = self.retry()
        self.assertEqual([], again,
                         '公众号这一轮已经抓完了,重试不许再抓它一遍')
        self.assertEqual({self.a['id']}, retried, '该补的只有失败那一条')

    def restart(self):
        """装成「服务重启过了」:进程内那几份常驻内存全丢掉,只留磁盘上那两份落盘。

        **这一步不做就验不到重启那一档** —— 活进程里「最新那一轮」是直接从内存取的
        (`last_result()`),结果账也还在模块内存里躺着;清干净之后那一问才会真的走
        恢复那条路。
        """
        daybook._pending.clear()
        daybook._lost.clear()
        results.clear()
        pipeline_state._state.update(
            running=False, started_at='', finished_at='', run_id='', results={},
            crawl_filter={}, export_filter={}, crawl_retry={},
            stages={s: {'status': '', 'detail': ''} for s in pipeline_state.STAGES})
        pipeline_state._restored = ''

    def test_a_source_that_succeeded_is_still_spared_after_a_restart(self):
        """账本写不上、服务又重启了:**这份事实在状态文件里,恢复不许把它丢掉**。

        账本是旁路(写不上只记一笔 `persist_error`,不许挡住收尾),那一档退回「最近
        一次运行」—— 而那一份的阶段卡片是各来源合并之后那一张,整步 error 会把公众号
        也算成欠着。状态文件里明明写着 `results.crawl.wechat_local=ok`,而恢复的字段
        清单里原来没有它:重启之后这份事实被自己扔了,点一次重试又把公众号抓一遍
        (第 4 轮审查 R2 实测 `restored.results=<字段缺失>`、
        `retry_wechat=['wechat_local']`)。

        判据照旧落在**公众号的 `fetch` 被叫了几次**,而不是某个字段在不在。
        """
        first = []
        with patch.object(daybook, '_write_atomic', side_effect=OSError('满盘')), \
             self.wechat_round(first):
            self.crawl(self.both_channels(), broken=[self.a])
        self.assertEqual(['wechat_local'], first, '这一局的前提:公众号抓过一遍')
        self.assertEqual({}, daybook.read(), '这一局的前提:账本一格都没写上')
        disk = pipeline_state.load_last_run() or {}
        self.assertEqual('ok', (((disk.get('results') or {}).get('crawl') or {})
                                .get('wechat_local') or {}).get('status'),
                         '这一局的前提:状态文件里写着公众号这一步成了')

        self.restart()
        again = []
        with self.ready_wechat(), self.wechat_round(again):
            retried, _ = self.retry()
        self.assertEqual([], again, '重启之后,已经抓完的公众号又被抓了一遍')
        self.assertEqual({self.a['id']}, retried, '真失败的那一条照旧要补得到')

    def test_the_source_that_actually_failed_is_still_retried(self):
        """**反过来钉一边**:真失败的那个来源照旧被重抓。

        光验「成功的不抓」的话,一个「谁都不抓」的实现照样全绿 —— 而那是漏报,
        今天欠的东西从此补不回来。
        """
        first = []
        with self.wechat_round(first, 'error', '公众号那边读不出来'):
            self.crawl(self.both_channels())
        self.assertEqual(['wechat_local'], first, '这一局的前提:公众号跑过、失败了')

        again = []
        with self.ready_wechat(), self.wechat_round(again):
            self.retry()
        self.assertEqual(['wechat_local'], again, '失败的那个来源必须补得到')


class TargetIdentityTests(RetryScopeBase):
    """同一条订阅,**请求里写名字、失败名单里写编号,记的必须是同一格**。

    2026-10-20 第 3 轮审查实测(R3):初次按页面契约用 `accounts=["A"]` 抓并失败,
    修好后点一次重试 —— 重试按失败名单改成 `accounts=["63"]`、实际抓的就是 63、
    四个计划步骤全成功;而当日账本里 `rss["A"].status="error"` 和
    `rss["63"].status="ok"` 并存,于是 `today_failed()` 还是 `["crawl"]`。页面同时
    显示「4/4 完成」和「今天还有 1 个步骤没补上」,点下去后端回一句「今天已经抓
    成功、没有要补的来源」—— 一个点不动也消不掉的死局。

    修法是记账的键一律归一到稳定 id(`collectors.TargetIdentity`),而覆盖范围由
    执行器交回来(`kb/pipeline_results.py`)。
    """

    def test_a_name_and_an_id_for_the_same_subscription_settle_as_one(self):
        self.crawl({'channel': 'rss', 'accounts': [self.a['name']]}, broken=[self.a])
        self.assertEqual(['crawl'], pipeline_retry.today_failed(),
                         '这一局的前提:今天欠着这一步')

        got, _ = self.retry_settled()
        self.assertEqual({self.a['id']}, got, '重试抓的就是它')
        # **判据是后果**:今天不欠了,所以那颗按钮没有可补的、页面也不再说欠着。
        self.assertEqual([], pipeline_retry.today_failed(),
                         '按名字记的那一格还红着 —— 同一条订阅被记成了两格')
        # 而且这颗按钮此刻压根没有可补的 —— 原来的死局正是「页面说还欠着、点下去
        # 却被拒」。两句话各说各的:这里该是「没有失败项」,不是「今天已经抓成功、
        # 没有要补的来源」(后者说明账上还留着一格红、只是被收窄掉了)。
        with self.no_backlog():
            said = self.refuse()
        self.assertIn('没有失败项', said, said)

    def test_a_subscription_that_is_still_broken_stays_owed(self):
        """**反过来钉一边**:没修好的那条照旧欠着,照旧补得到。"""
        self.crawl({'channel': 'rss', 'accounts': [self.a['name']]}, broken=[self.a])
        got, _ = self.retry_settled(broken=[self.a])
        self.assertEqual({self.a['id']}, got)
        self.assertEqual(['crawl'], pipeline_retry.today_failed(),
                         '还在失败的那条不许从账上消失')
