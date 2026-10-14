"""日报那一轮的两段式编排:抓取按来源分,入库全库跑一次。

从 `test_daily_report.py` 拆出来 —— 那份加上这一组会超过单文件 700 行上限。
窗口/素材口径、落盘产物、定时与停止语义仍在那一份里。
"""
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_run


class DailyTwoStageSyncTests(unittest.TestCase):
    """同步分两段:**抓取按通道一轮一个,入库是全库的一次**。

    只有 crawl / export 认 crawl_filter;`_run_index` 全量扫 vault、`_run_extract`
    扫全库待提取队列,两个函数都不收通道参数。挨个通道各跑一遍四段,等于把同一件
    全库的活重做 N 遍(5 个通道 = 5 次全量扫描、5 次抢维护锁)。

    **合并不会放行本来不该提取的内容**:拦住大V那几类的是订阅级开关
    (`kb.extract.pending_docs` 是唯一口径,钉在 tests/test_extract_optin.py),
    跟这一轮由谁触发无关 —— 所以这里钉的是编排(谁跑几次、红算在谁头上),
    不重复钉那道闸。
    """

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        labels = {'wechat_local': '公众号', 'social': '大V观点', 'rss': 'RSS'}
        from kb import pipeline_state as ps
        for p in (patch.object(daily_run, 'STATE_FILE', Path(temp.name) / 'last_run.json'),
                  patch.object(daily_run, 'SETTINGS_FILE', Path(temp.name) / 'daily.json'),
                  patch.object(ps, '_STATE_FILE', Path(temp.name) / 'pipeline.json'),
                  patch.object(daily_run, '_channel_labels', Mock(return_value=labels))):
            p.start(); self.addCleanup(p.stop)
        self.before = deepcopy(daily_run._state)
        self.addCleanup(lambda: daily_run._state.update(self.before))

    def sync_run(self, channels=('wechat_local', 'social', 'rss'), rounds=None, **flags):
        """跑一轮带同步的 `_run`。返回每一轮管道的 (阶段清单, crawl_filter)。

        `rounds` 按轮次号(从 1 起)覆盖那一轮的阶段结果,用来注入某一轮的红。
        """
        from kb import pipeline as pl
        from kb.pipeline_state import STAGES
        calls = []

        def start(wanted, crawl_filter=None):
            calls.append((list(wanted), deepcopy(crawl_filter)))
            return f'run-{len(calls)}'

        def state():
            # 没点名的阶段一开跑就是 skipped(见 pipeline.start_pipeline)
            full = {s: {'status': 'skipped', 'detail': '未选择'} for s in STAGES}
            full.update({s: {'status': 'ok', 'detail': f'{s} 好了'} for s in calls[-1][0]})
            full.update((rounds or {}).get(len(calls), {}))
            # **形状要跟真的 `get_state()` 一致**:收尾合并读的是 planned_stages /
            # crawl_filter / export_filter,缺了它们这几条用例就碰不到合并那一步,
            # 「压根没合并」也照样全绿。
            stages, crawl_filter = calls[-1]
            return {'running': False, 'run_id': f'run-{len(calls)}',
                    'current_stage': '', 'stages': full, 'stopped': False,
                    'planned_stages': list(stages), 'log': [],
                    'started_at': f'2026-10-14 09:0{len(calls)}:00',
                    'finished_at': f'2026-10-14 09:1{len(calls)}:00',
                    'crawl_filter': crawl_filter or {'channel': 'wechat_local'},
                    'export_filter': crawl_filter or {'channel': 'wechat_local'},
                    'crawl_retry': {}}

        daily_run._state.update({**self.before, 'running': True, 'log': [],
                                 'channels': [], **flags})
        self.gen = Mock(return_value={'date': '2026-09-29', 'since': '', 'until': '',
                                      'path': '', 'docs': 2, 'total': 2, 'late': 0,
                                      'model': 'm'})
        self.stopped = Mock()
        with patch.object(pl, 'start_pipeline', Mock(side_effect=start)), \
             patch.object(pl, 'get_state', Mock(side_effect=state)), \
             patch.object(pl, 'request_stop', self.stopped), \
             patch.object(daily_run.time, 'sleep', Mock()), \
             patch.object(daily_run, '_wait_idle', Mock(return_value=True)), \
             patch.object(daily, 'generate', self.gen):
            daily_run._run(None, None, True, list(channels), daily.FIRST_DAYS)
        return calls

    def test_fetch_is_per_channel_and_the_library_stages_run_once(self):
        calls = self.sync_run()
        self.assertEqual([daily_run.FETCH_STAGES] * 3 + [daily_run.DIGEST_STAGES],
                         [stages for stages, _ in calls])
        self.assertEqual([{'channel': 'wechat_local'}, {'channel': 'social'},
                          {'channel': 'rss'}, None], [f for _, f in calls])
        # 反过来钉一边:抓取那几轮一次都没要过 index/extract(那正是原来被重做
        # N 遍的两段),入库那一轮也一次都没要过 crawl/export(不会顺手重抓)。
        self.assertEqual([], [c for c in calls[:3] if {'index', 'extract'} & set(c[0])])
        self.assertEqual([], [c for c in calls[3:] if {'crawl', 'export'} & set(c[0])])
        self.assertEqual('done', daily_run._state['phase'])

    def test_a_library_failure_is_not_blamed_on_any_channel(self):
        # 两段式之前 index/extract 跟着每个通道跑,它们的红只能记到"当时那个
        # 通道"头上 —— 磁盘满跟公众号没有半点关系,而面板和日报都照着念。
        calls = self.sync_run(rounds={4: {'index': {'status': 'error', 'detail': '磁盘满'}}})
        digest = daily_run._state['channels'][-1]
        self.assertEqual(('', daily_run.DIGEST_LABEL, 'error'),
                         (digest['channel'], digest['label'], digest['status']))
        self.assertIn('index 磁盘满', digest['detail'])
        self.assertEqual(['ok'] * 3, [c['status'] for c in daily_run._state['channels'][:3]])
        # 这一条要一路传到日报正文和完成提示里,而且只认 label(它没有通道 id)
        self.assertEqual([daily_run.DIGEST_LABEL],
                         [i['label'] for i in self.gen.call_args.kwargs['sync_issues']])
        self.assertIn(f'1 项未完成({daily_run.DIGEST_LABEL})', daily_run._state['detail'])

    def test_running_out_of_fetch_budget_still_runs_the_library_stages(self):
        # 超预算的本意是"别再抓了,拿已经抓到的内容出报"。入库那一段要是跟着跳过,
        # 这一轮导出的文件就进不了索引,而 `daily.generate` 取材自 kb.db 的
        # documents —— 抓回来的东西会整个落到这份日报外面,正好把它要保的丢了。
        calls = self.sync_run(over_budget=True)
        self.assertEqual([daily_run.DIGEST_STAGES], [stages for stages, _ in calls])
        self.assertEqual(['超出时间预算,未同步'] * 3,
                         [c['detail'] for c in daily_run._state['channels'][:3]])
        self.assertEqual('ok', daily_run._state['channels'][-1]['status'])
        # 入库那一轮的"要不要收手"判据**不认超预算**,否则它一起跑就被自己停掉
        self.stopped.assert_not_called()
        self.gen.assert_called_once()

    def test_a_user_stop_skips_the_library_stages_too(self):
        # 用户按的停止是"这一轮到此为止、也不出报",跟超预算是两件事。
        calls = self.sync_run(stop_requested=True)
        self.assertEqual([], calls)
        digest = daily_run._state['channels'][-1]
        self.assertEqual((daily_run.DIGEST_LABEL, 'skipped', '已停止,未入库'),
                         (digest['label'], digest['status'], digest['detail']))
        self.gen.assert_not_called()
        self.assertEqual('stopped', daily_run._state['phase'])

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
        self.assertEqual(['crawl', 'export', 'index', 'extract'], last['planned_stages'])
        # 范围是两个真抓过的来源,**没有**入库轮那个被自动盖上去的默认采集器
        self.assertEqual(['wechat_local', 'rss'],
                         [s['channel'] for s in pipeline.crawl_channels(last['crawl_filter'])])
        self.assertEqual(['crawl', 'export', 'index', 'extract'],
                         pipeline.retry_failed_plan())

    def test_a_clean_round_does_not_invent_a_failure_to_retry(self):
        # 反过来钉一边:全绿那一轮合并完照旧没有可重试的东西。
        from kb import pipeline
        self.sync_run(channels=['rss'])
        self.assertEqual([], pipeline.retry_failed_plan())
        self.assertEqual('ok', pipeline.load_last_run()['stages']['crawl']['status'])



class DailyBatchResultTests(unittest.TestCase):
    """一轮日报是 N+1 次管道运行,而「最近一次运行」只有一份。

    后跑的会把先跑的盖掉 —— 抓取失败的明细、失败名单和「重试失败项」会被最后那
    一轮入库的成功一起抹掉(`retry_failed_plan` 读的就是这一份)。两段式之前只有
    最后一个来源的结果留得下来,改成两段式之后变成**每次必然发生**。所以收尾时把
    这一批合并成一份发布(`pipeline_state.publish_batch`)。

    钉的是后果:库里那份最近运行里还有没有那条红、点重试还排不排得出计划。
    """

    def setUp(self):
        from kb import pipeline_state as ps
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(ps, '_STATE_FILE', Path(temp.name) / 'last_run.json')
        p.start(); self.addCleanup(p.stop)

    def round(self, planned, stages, **extra):
        from kb.pipeline_state import STAGES
        full = {s: {'status': 'skipped', 'detail': '未选择'} for s in STAGES}
        full.update({s: {'status': 'ok', 'detail': f'{s} 好了'} for s in planned})
        full.update(stages)
        return {'planned_stages': list(planned), 'stages': full, 'log': [],
                'started_at': '2026-10-14 09:00:00', 'finished_at': '2026-10-14 09:10:00',
                'run_id': extra.pop('run_id', 'run-x'), 'stopped': False,
                'crawl_filter': extra.pop('crawl_filter', {}),
                'export_filter': extra.pop('export_filter', {}), **extra}

    def test_a_fetch_failure_survives_the_library_round_that_follows_it(self):
        from kb import pipeline, pipeline_state as ps
        fetch = self.round(['crawl', 'export'],
                           {'crawl': {'status': 'error', 'detail': '失败 10 个',
                                      'failures': [{'title': 'X', 'reason': '连不上'}]}},
                           run_id='run-1', crawl_filter={'channel': 'rss'},
                           export_filter={'channel': 'rss'},
                           crawl_retry={'run': 'run-1', 'channels': {'rss': {'ids': [7]}}})
        digest = self.round(['index', 'extract'], {}, run_id='run-2',
                            crawl_filter={'channel': 'wechat_local'})
        ps.publish_batch([fetch, digest])
        last = pipeline.load_last_run()
        # 那条红还在,明细也在 —— 这是用户点进同步中心要看的东西
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(1, len(last['stages']['crawl']['failures']))
        # 入库那两段的成功同样记下来了,不是拿失败轮整个顶掉
        self.assertEqual(('ok', 'ok'), (last['stages']['index']['status'],
                                        last['stages']['extract']['status']))
        self.assertEqual(['crawl', 'export', 'index', 'extract'], last['planned_stages'])
        # 范围是真抓过的那个来源,不是入库轮被自动盖上去的默认采集器
        from kb import pipeline
        self.assertEqual(['rss'], [s['channel'] for s in
                                   pipeline.crawl_channels(last['crawl_filter'])])
        self.assertEqual(['rss'], [s['channel'] for s in
                                   pipeline.crawl_channels(last['export_filter'])])
        # 失败名单跟着合并后的身份走,重试才收窄得到那几条订阅
        self.assertEqual({'rss'}, set(last['crawl_retry']['channels']))
        self.assertEqual(last['run_id'], last['crawl_retry']['run'])
        # 后果:重试计划排得出来(修之前是 [],按钮灰着、点下去 409)
        self.assertEqual(['crawl', 'export', 'index', 'extract'],
                         pipeline.retry_failed_plan())

    def test_a_round_that_did_not_plan_a_stage_cannot_grey_it_out(self):
        # 反过来钉一边:入库轮没计划 crawl(落的是 skipped/未选择),不许拿它把
        # 抓取轮真实的 ok 盖成「跳过」—— 那会让面板报一轮根本没发生的跳过。
        from kb import pipeline, pipeline_state as ps
        ps.publish_batch([self.round(['crawl', 'export'], {}, run_id='run-1',
                                     crawl_filter={'channel': 'rss'}),
                          self.round(['index', 'extract'], {}, run_id='run-2')])
        last = pipeline.load_last_run()
        self.assertEqual('ok', last['stages']['crawl']['status'])
        self.assertEqual([], pipeline.retry_failed_plan())
        # 谁都没计划过的阶段照旧是「未选择」
        self.assertEqual('未选择', last['stages']['wiki']['detail'])

    def test_one_round_alone_is_left_to_the_pipeline_itself(self):
        from kb import pipeline_state as ps
        self.assertIsNone(ps.publish_batch([self.round(['crawl'], {})]))
        self.assertIsNone(ps.publish_batch([]))


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


if __name__ == '__main__':
    unittest.main()
