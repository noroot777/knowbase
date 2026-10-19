"""日报那一轮的两段式编排:抓取按来源分,入库全库跑一次。

从 `test_daily_report.py` 拆出来 —— 那份加上这一组会超过单文件 700 行上限。
窗口/素材口径、落盘产物、定时与停止语义仍在那一份里;结果的发布与保全
(整批合并、写失败的退路、资格冲突)在 `tests/test_batch_results.py`。
"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_library, daily_run, daily_sync
from kb import daily_state
from kb import pipeline_batch as pb
from kb import pipeline_state as ps
from tests.pipeline_round_fixture import finish_round, no_extract_backlog


class DailyTwoStageSyncTests(unittest.TestCase):
    """同步分两段:**抓取按通道一轮一个,入库是全库的一次**。

    只有 crawl / export 认 crawl_filter;`_run_index` 全量扫 vault、`_run_extract`
    扫全库待提取队列,两个函数都不收通道参数。挨个通道各跑一遍四段,等于把同一件
    全库的活重做 N 遍(5 个通道 = 5 次全量扫描、5 次抢维护锁)。

    **合并不会放行本来不该提取的内容**:拦住大V那几类的是订阅级开关
    (`kb.extract.pending_docs` 是唯一口径,钉在 tests/test_extract_optin.py),
    跟这一轮由谁触发无关 —— 所以这里钉的是编排(谁跑几次、红算在谁头上),
    不重复钉那道闸。

    出报之后还跟着**第三段**:整理图鉴与 Wiki(`daily_library.LIBRARY_STAGES`),
    跑在同一个批次里。所以这里的"几轮"是 N+2,成绩单的最后一项也不再是入库那一项
    —— **按 label 取**(`scored`),不按位置。本文件里的 digest 一律指入库那一段。
    """

    def scored(self, label):
        """成绩单上那一项(按 label 找)。**位置会变**:尾巴上又多了整理那一项,
        按 `[-1]` 取的话这几条断言会悄悄挪到另一项上去验,而用例照样是绿的。"""
        got = [c for c in daily_run._state['channels'] if c['label'] == label]
        self.assertEqual(1, len(got), f'{label} 这一项有 {len(got)} 条')
        return got[0]

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        labels = {'wechat_local': '公众号', 'social': '大V观点', 'rss': 'RSS'}
        for p in (patch.object(daily_state, 'STATE_FILE', Path(temp.name) / 'last_run.json'),
                  patch.object(daily_run, 'SETTINGS_FILE', Path(temp.name) / 'daily.json'),
                  patch.object(ps, '_STATE_FILE', Path(temp.name) / 'pipeline.json'),
                  patch.object(daily_run, '_channel_labels', Mock(return_value=labels))):
            p.start(); self.addCleanup(p.stop)
        self.before = deepcopy(daily_run._state)
        self.addCleanup(lambda: daily_run._state.update(self.before))
        # `ps._state` / `ps._finished` 是模块级常驻的,下面要走真的收尾往里写东西,
        # **走的时候一定要还回去**:不还的话同跑时会把别的用例带红,而单跑永远绿。
        keep_state, keep_done = deepcopy(ps._state), deepcopy(ps._finished)
        def restore():
            ps._state.clear(); ps._state.update(keep_state)
            ps._finished.clear(); ps._finished.update(keep_done)
            pb.close_batch()
        self.addCleanup(restore)

    def sync_run(self, channels=('wechat_local', 'social', 'rss'), rounds=None,
                 gen=None, **flags):
        """跑一轮带同步的 `_run`。返回每一轮管道的 (阶段清单, crawl_filter)。

        `rounds` 按轮次号(从 1 起)覆盖那一轮的阶段结果,用来注入某一轮的红。

        打桩只打到 `start_pipeline` 这一层:**收尾(`_end_run`)和落盘都是真的**。
        批次账本由 `_end_run` 喂,打掉它的话这一轮压根不会落盘,「有没有真的
        合并发布」这件事就没有判据了(而用例照样全绿)。
        """
        from kb import pipeline as pl
        calls = []

        # `batch` 要原样传到 `start_pipeline`:每一轮收尾时由结果存储层把整批
        # 合并着落盘,靠的就是这一格(见 `pipeline_state._publish_in_batch`)。
        self.batches = []

        def start(wanted, crawl_filter=None, batch='', refused=None):
            self.batches.append(batch)
            calls.append((list(wanted), deepcopy(crawl_filter)))
            run = f'run-{len(calls)}'
            # 照 `start_pipeline` 的样子摆一轮(含它那个"没指定来源就盖默认采集器"
            # 的兜底),把阶段结果摆成终态,再走真的收尾。
            return finish_round(wanted, crawl_filter, batch,
                                (rounds or {}).get(len(calls)), run,
                                f'2026-10-14 09:0{len(calls)}:00')

        # **摆盘要过 `fresh_round()`**:上一条用例留下的那个 `done` 会把这一轮
        # 整轮冻住(结论冻结,见 `daily_state._set`)—— 同跑时红、单跑全绿。
        daily_run._state.update({**self.before, **daily_state.fresh_round(),
                                 'running': True, **flags})
        self.gen = gen or Mock(return_value={
            'date': '2026-09-29', 'since': '', 'until': '', 'path': '',
            'docs': 2, 'total': 2, 'late': 0, 'model': 'm'})
        self.stopped = Mock()
        with patch.object(pl, 'start_pipeline', Mock(side_effect=start)), \
             patch.object(pl, 'request_stop', self.stopped), \
             patch.object(daily_run.time, 'sleep', Mock()), \
             patch.object(daily_sync, '_wait_idle', Mock(return_value=True)), \
             patch.object(daily, 'generate', self.gen):
            daily_run._run(None, None, True, list(channels), daily.FIRST_DAYS)
        return calls

    def test_fetch_is_per_channel_and_the_digest_stages_run_once(self):
        calls = self.sync_run()
        self.assertEqual([daily_run.FETCH_STAGES] * 3 + [daily_run.DIGEST_STAGES,
                                                         daily_library.LIBRARY_STAGES],
                         [stages for stages, _ in calls])
        self.assertEqual([{'channel': 'wechat_local'}, {'channel': 'social'},
                          {'channel': 'rss'}, None, None], [f for _, f in calls])
        # 反过来钉一边:抓取那几轮一次都没要过 index/extract(那正是原来被重做
        # N 遍的两段),后面那两轮全库的活也一次都没要过 crawl/export(不会顺手重抓)。
        self.assertEqual([], [c for c in calls[:3] if {'index', 'extract'} & set(c[0])])
        self.assertEqual([], [c for c in calls[3:] if {'crawl', 'export'} & set(c[0])])
        self.assertEqual('done', daily_run._state['phase'])

    def test_a_digest_failure_is_not_blamed_on_any_channel(self):
        # 两段式之前 index/extract 跟着每个通道跑,它们的红只能记到"当时那个
        # 通道"头上 —— 磁盘满跟公众号没有半点关系,而面板和日报都照着念。
        calls = self.sync_run(rounds={4: {'index': {'status': 'error', 'detail': '磁盘满'}}})
        digest = self.scored(daily_run.DIGEST_LABEL)
        self.assertEqual(('', 'error'), (digest['channel'], digest['status']))
        self.assertIn('index 磁盘满', digest['detail'])
        self.assertEqual(['ok'] * 3, [c['status'] for c in daily_run._state['channels'][:3]])
        # 这一条要一路传到日报正文和完成提示里,而且只认 label(它没有通道 id)
        self.assertEqual([daily_run.DIGEST_LABEL],
                         [i['label'] for i in self.gen.call_args.kwargs['sync_issues']])
        self.assertIn(f'1 项未完成({daily_run.DIGEST_LABEL})', daily_run._state['detail'])

    def test_every_round_runs_under_one_batch_and_the_batch_is_closed(self):
        """这几轮必须是**同一个业务批次**,而且走完要关掉。

        「最近一次运行」只有一个位子,而一轮日报是 N+1 次运行。把它们归成一个
        批次之后,每一轮收尾都由结果存储层把整批合并着落盘(不变量本身钉在
        `DailyBatchResultTests`,那边验的是盘上那份)。这里只钉接线:批次身份
        传到了每一轮,而且**不许漏关** —— 开着的批次会把下一轮独立运行算进一个
        早就结束的批次。
        """
        from kb import pipeline_batch as pb
        self.sync_run()
        self.assertEqual(5, len(self.batches), '抓取 3 轮 + 入库 1 轮 + 整理 1 轮')
        self.assertEqual(1, len(set(self.batches)), self.batches)
        self.assertTrue(self.batches[0], '一轮都没带上批次身份')
        self.assertEqual('', pb._batch_id, '走完没关批次')

    def test_a_crash_inside_the_sync_block_still_closes_the_batch(self):
        # 半路抛出来时 `close_batch` 那一句走不到,只有 finally 那次关得掉。开着的
        # 批次会把下一轮独立运行算进一个早就结束的批次。
        from kb import pipeline_batch as pb
        with patch.object(daily_run, '_sync_round',
                          Mock(side_effect=RuntimeError('摆盘:同步那一步炸了'))):
            self.sync_run()
        self.assertEqual('failed', daily_run._state['phase'])
        self.assertEqual('', pb._batch_id, '抛出来之后批次还开着')

    def test_a_data_dir_that_cannot_be_written_is_not_called_a_busy_pipeline(self):
        """起跑被拒时,日报念的是**真实原因**,不是一律「被其它任务占用」。

        没起跑有两种:管道真被占着(等一下就好),或者这一轮的持久化边界落不下来
        —— 后者**等不好**,得去看磁盘空间和数据目录权限。而日报每一段(各来源那
        几轮 + 入库那一轮)都会照它各报一次,整屏都在劝用户等,真实原因只留在管道
        日志里。判据落在成绩单上那句 detail 上,不是「有没有把 refused 传下去」。
        """
        daily_run._state.update({**self.before, 'running': True, 'log': []})
        with patch.dict(ps._state, {'running': False}), \
             patch.object(ps._open, '_write_atomic',
                          side_effect=OSError('No space left on device')), \
             patch.object(daily_sync, '_wait_idle', Mock(return_value=True)):
            entry = daily_run._sync_round(['crawl'], {'channel': 'rss'}, 'RSS', 0)
        self.assertEqual('skipped', entry['status'])
        self.assertIn('数据目录写不进去', entry['detail'])
        self.assertNotIn('被其它任务占用', entry['detail'])

    def test_a_pipeline_that_is_really_busy_still_says_it_is_busy(self):
        """反过来钉一边:真被别人占着时念的还是「在运行中」,没把两档混成一句。"""
        daily_run._state.update({**self.before, 'running': True, 'log': []})
        with patch.dict(ps._state, {'running': True}), \
             patch.object(daily_sync, '_wait_idle', Mock(return_value=True)):
            entry = daily_run._sync_round(['crawl'], {'channel': 'rss'}, 'RSS', 0)
        self.assertEqual('skipped', entry['status'])
        self.assertIn('运行中', entry['detail'])
        self.assertNotIn('数据目录', entry['detail'])

    def test_a_refused_publish_is_said_out_loud(self):
        # 批次没发布出去不是日报的失败(素材都在库里,照常出报),但那句话要跟到
        # 完成提示里:咽下去的后果是同步中心看不到这一轮的失败、也点不了重试,
        # 而日报这边一句「已生成」看着一切正常。
        from kb import pipeline as pl
        with patch.object(pl, 'close_batch', Mock(return_value='盘上那份是更新的一轮')):
            self.sync_run()
        self.assertIn('盘上那份是更新的一轮', daily_run._state['detail'])

    def test_running_out_of_fetch_budget_still_runs_the_digest_stages(self):
        # 超预算的本意是"别再抓了,拿已经抓到的内容出报"。入库那一段要是跟着跳过,
        # 这一轮导出的文件就进不了索引,而 `daily.generate` 取材自 kb.db 的
        # documents —— 抓回来的东西会整个落到这份日报外面,正好把它要保的丢了。
        calls = self.sync_run(over_budget=True)
        # 整理那一段也不认超预算(它压根不抓),所以它照样在
        self.assertEqual([daily_run.DIGEST_STAGES, daily_library.LIBRARY_STAGES],
                         [stages for stages, _ in calls])
        self.assertEqual(['超出时间预算,未同步'] * 3,
                         [c['detail'] for c in daily_run._state['channels'][:3]])
        self.assertEqual('ok', self.scored(daily_run.DIGEST_LABEL)['status'])
        # 入库那一轮的"要不要收手"判据**不认超预算**,否则它一起跑就被自己停掉
        self.stopped.assert_not_called()
        self.gen.assert_called_once()

    def test_a_user_stop_skips_the_digest_stages_too(self):
        # 用户按的停止是"这一轮到此为止、也不出报",跟超预算是两件事。
        calls = self.sync_run(stop_requested=True)
        self.assertEqual([], calls)
        digest = self.scored(daily_run.DIGEST_LABEL)
        self.assertEqual(('skipped', '已停止,未入库'),
                         (digest['status'], digest['detail']))
        # 停止是对整轮说的:后面那段整理也跳过,而且在成绩单上留一项说清楚
        self.assertEqual('skipped', self.scored(daily_library.LIBRARY_LABEL)['status'])
        self.gen.assert_not_called()
        self.assertEqual('stopped', daily_run._state['phase'])

    def test_the_tail_does_not_rewrite_a_failed_report(self):
        """出报失败 + 后面那段整理跑成功 = **结论还是失败**(审查 R1)。

        共用一份 phase/detail 的后果:整理那一轮复用同一个 `_sync_round`,它一路
        `_set('sync', …)`,把 `failed` 改写成 `sync` —— 而前端只在
        `phase === 'failed'` 时留失败横幅,于是一次**根本没出来**的日报被报成
        「日报已生成」(实测终态 `phase='sync', detail='整理工具与 Wiki:同步中',
        error='Claude 503', result=None`)。
        """
        calls = self.sync_run(gen=Mock(side_effect=RuntimeError('Claude 503')))
        self.assertEqual(daily_library.LIBRARY_STAGES, calls[-1][0],
                         '整理那一段压根没跑,这一条就没在验它改不改写结论')
        self.assertEqual('failed', daily_run._state['phase'])
        self.assertIn('Claude 503', daily_run._state['detail'])
        self.assertIsNone(daily_run._state['result'])
        self.assertTrue(daily_run._state['settled_at'], '结论落下的时刻没记')
        # 两件事分开记:整理自己那一格照常有进度和终态
        self.assertEqual('done', daily_run._state['library']['phase'])
        self.assertIn(daily_library.LIBRARY_LABEL, daily_run._state['library']['detail'])

    def test_a_clean_report_keeps_its_closing_line_through_the_tail(self):
        """反过来钉一边:成功那一档的收尾文案也不许被整理那一段改写 —— 改写之后
        `amend_done`(只认 `done`)连批次发布失败那句都补不进去。"""
        self.sync_run()
        self.assertEqual('done', daily_run._state['phase'])
        self.assertIn('日报已生成', daily_run._state['detail'])
        self.assertNotIn('同步中', daily_run._state['detail'])

    def test_a_fetch_failure_still_reaches_the_pipeline_panel_and_retry(self):
        """这一条钉的是**接线**:`_run` 跑完真的把这一批合并发布了。

        不接的话,库里那份「最近一次运行」就是最后那轮入库的「同步已完成」——
        面板上看不到 RSS 抓失败,「重试失败项」也排不出计划(点下去 409)。
        """
        from kb import pipeline
        self.sync_run(channels=['wechat_local', 'rss'], rounds={
            2: {'crawl': {'status': 'error', 'detail': '失败 10 个'}}})
        last = pipeline.load_last_run()
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(['crawl', 'export', 'index', 'extract', 'projects', 'wiki'],
                         last['planned_stages'])
        # 范围是两个真抓过的来源,**没有**入库轮那个被自动盖上去的默认采集器
        self.assertEqual(['wechat_local', 'rss'],
                         [s['channel'] for s in pipeline.crawl_channels(last['crawl_filter'])])
        self.assertEqual(['crawl', 'export', 'index', 'extract'],
                         pipeline.retry_failed_plan())

    def test_a_clean_round_does_not_invent_a_failure_to_retry(self):
        """反过来钉一边:全绿那一轮合并完照旧没有可重试的东西。

        **提取积压要打掉**(口径在 `no_extract_backlog`):不打的话这一条验的是
        开发机上那一刻的积压数,而要验的那件事压根没被验到。
        """
        from kb import pipeline
        self.sync_run(channels=['rss'])
        with no_extract_backlog():
            self.assertEqual([], pipeline.retry_failed_plan())
        self.assertEqual('ok', pipeline.load_last_run()['stages']['crawl']['status'])



class RunScopeTests(unittest.TestCase):
    """「这一轮作用在哪几个来源上」只有一份口径(`pipeline_stages.run_scopes`)。

    `start_pipeline` 一律会往 `crawl_filter` 里盖一个默认采集器名,所以只跑
    index/extract 的那一轮照着 filter 念,会把公众号说成正在同步、还会把订阅
    写保护错加在它头上。前端那一份同口径在 `pipe_channels.js` 的 `pipeRunScopes`。
    """

    def test_a_library_round_belongs_to_no_source(self):
        from kb import pipeline
        self.assertEqual([], pipeline.run_scopes(
            {'planned_stages': ['index', 'extract'],
             'crawl_filter': {'channel': 'wechat_local'}}))

    def test_a_fetch_round_still_reports_its_sources(self):
        from kb import pipeline
        self.assertEqual(['rss'], [s['channel'] for s in pipeline.run_scopes(
            {'planned_stages': ['crawl', 'export'], 'crawl_filter': {'channel': 'rss'}})])
        # 存量记录没有 planned_stages,照老样子摊平 —— 一律判成「不按来源」会让
        # 订阅写保护对所有存量记录静默失效
        self.assertEqual(['rss'], [s['channel'] for s in pipeline.run_scopes(
            {'crawl_filter': {'channel': 'rss'}})])


class VerdictFreezeTests(unittest.TestCase):
    """结论一落下就是终态(`daily_state.SETTLED`),冻的是**这一轮**。

    守卫待在 `_set` 里而不是各调用点:出报之后那一段复用同一个 `_sync_round`,
    拦在调用点就是「下一个复用它的人再踩一遍」(审查 R1)。
    """

    def setUp(self):
        self.before = deepcopy(daily_state._state)
        self.addCleanup(lambda: daily_state._state.update(self.before))

    def test_a_settled_verdict_is_not_rewritten(self):
        daily_state._state.update(phase='failed', detail='日报生成失败: Claude 503')
        daily_state._set('sync', '整理工具与 Wiki:同步中')
        self.assertEqual(('failed', '日报生成失败: Claude 503'),
                         (daily_state._state['phase'], daily_state._state['detail']))

    def test_the_tail_progress_has_a_slot_of_its_own(self):
        """它不是"什么都不写":整理那一段的进度要看得见,只是不许写在日报那一格。"""
        daily_state._state.update(phase='done', detail='日报已生成:12 条素材')
        daily_state.library_progress('sync', '整理工具与 Wiki:同步中')
        self.assertEqual('done', daily_state._state['phase'])
        self.assertEqual('整理工具与 Wiki:同步中',
                         daily_state._state['library']['detail'])

    def test_a_new_round_starts_from_scratch(self):
        """**反过来钉一边,而且冻错边比漏冻更难看**:面板会永久停在上一轮的结论上,
        新的一轮跑着而页面一个字不动。新一轮由 `start()` 直接重设 `_state`(不过
        `_set`),`settled_at` 和整理那一格也跟着归零。"""
        daily_state._state.update(
            running=False, phase='failed', detail='日报生成失败: Claude 503',
            settled_at='2026-10-19 09:40:00',
            library={'phase': 'done', 'detail': '上一轮整理完了'})
        with patch.object(daily_run, 'threading', Mock()), \
             patch.object(daily_run, 'load_settings',
                          Mock(return_value={'channels': ['rss'], 'first_days': 1})), \
             patch.object(daily_run, '_channel_labels',
                          Mock(return_value={'rss': 'RSS'})):
            self.assertTrue(daily_run.start(sync=True, channels=['rss']))
        self.assertEqual('starting', daily_state._state['phase'])
        self.assertEqual('', daily_state._state['settled_at'], 'settled_at 没归零')
        self.assertEqual({'phase': '', 'detail': ''}, daily_state._state['library'])
        daily_state._set('generate', '正在生成日报')
        self.assertEqual('generate', daily_state._state['phase'],
                         '新一轮被上一轮的结论冻住了')


if __name__ == '__main__':
    unittest.main()
