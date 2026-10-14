"""日报那一轮的两段式编排:抓取按来源分,入库全库跑一次。

从 `test_daily_report.py` 拆出来 —— 那份加上这一组会超过单文件 700 行上限。
窗口/素材口径、落盘产物、定时与停止语义仍在那一份里。
"""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from kb import daily, daily_run
from kb import pipeline_state as ps


def finish_round(planned, crawl_filter, batch='', results=None, run_id='run-1',
                 started='2026-10-14 09:00:00'):
    """照 `start_pipeline` 的样子摆一轮管道运行,再**走真的收尾**(`_end_run`)。

    两组用例共用一份摆盘:批次账本是由 `_end_run` 喂的,各写一份的话两边的前提
    会各漂各的。含 `start_pipeline` 那个"没指定来源就盖默认采集器"的兜底 ——
    全库那一轮的 `crawl_filter` 现实中就带着一个跟它无关的来源名。
    """
    from kb.collectors import active_collector
    cf = deepcopy(crawl_filter or {})
    cf['channel'] = active_collector(cf.get('channel')).name
    full = {st: {'status': 'skipped', 'detail': '未选择'} for st in ps.STAGES}
    full.update({st: {'status': 'ok', 'detail': f'{st} 好了'} for st in planned})
    full.update(results or {})
    ps._state.update({
        'running': True, 'stop_requested': False, 'stopped': False,
        'run_id': run_id, 'log': [], 'current_stage': '',
        'started_at': started, 'finished_at': '',
        'crawl_filter': cf, 'export_filter': deepcopy(cf),
        'run_kind': 'sync', 'batch': batch, 'crawl_retry': {},
        'planned_stages': [st for st in ps.STAGES if st in planned],
        'stages': full})
    ps._end_run()
    return run_id


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
        for p in (patch.object(daily_run, 'STATE_FILE', Path(temp.name) / 'last_run.json'),
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
            ps.close_batch()
        self.addCleanup(restore)

    def sync_run(self, channels=('wechat_local', 'social', 'rss'), rounds=None, **flags):
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

        def start(wanted, crawl_filter=None, batch=''):
            self.batches.append(batch)
            calls.append((list(wanted), deepcopy(crawl_filter)))
            run = f'run-{len(calls)}'
            # 照 `start_pipeline` 的样子摆一轮(含它那个"没指定来源就盖默认采集器"
            # 的兜底),把阶段结果摆成终态,再走真的收尾。
            return finish_round(wanted, crawl_filter, batch,
                                (rounds or {}).get(len(calls)), run,
                                f'2026-10-14 09:0{len(calls)}:00')

        daily_run._state.update({**self.before, 'running': True, 'log': [],
                                 'channels': [], **flags})
        self.gen = Mock(return_value={'date': '2026-09-29', 'since': '', 'until': '',
                                      'path': '', 'docs': 2, 'total': 2, 'late': 0,
                                      'model': 'm'})
        self.stopped = Mock()
        with patch.object(pl, 'start_pipeline', Mock(side_effect=start)), \
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

    def test_every_round_runs_under_one_batch_and_the_batch_is_closed(self):
        """这几轮必须是**同一个业务批次**,而且走完要关掉。

        「最近一次运行」只有一个位子,而一轮日报是 N+1 次运行。把它们归成一个
        批次之后,每一轮收尾都由结果存储层把整批合并着落盘(不变量本身钉在
        `DailyBatchResultTests`,那边验的是盘上那份)。这里只钉接线:批次身份
        传到了每一轮,而且**不许漏关** —— 开着的批次会把下一轮独立运行算进一个
        早就结束的批次。
        """
        from kb import pipeline_state as ps
        self.sync_run()
        self.assertEqual(4, len(self.batches))
        self.assertEqual(1, len(set(self.batches)), self.batches)
        self.assertTrue(self.batches[0], '一轮都没带上批次身份')
        self.assertEqual('', ps._batch_id, '走完没关批次')

    def test_a_crash_inside_the_sync_block_still_closes_the_batch(self):
        # 半路抛出来时 `close_batch` 那一句走不到,只有 finally 那次关得掉。开着的
        # 批次会把下一轮独立运行算进一个早就结束的批次。
        from kb import pipeline_state as ps
        with patch.object(daily_run, '_sync_round',
                          Mock(side_effect=RuntimeError('摆盘:同步那一步炸了'))):
            self.sync_run()
        self.assertEqual('failed', daily_run._state['phase'])
        self.assertEqual('', ps._batch_id, '抛出来之后批次还开着')

    def test_a_refused_publish_is_said_out_loud(self):
        # 批次没发布出去不是日报的失败(素材都在库里,照常出报),但那句话要跟到
        # 完成提示里:咽下去的后果是同步中心看不到这一轮的失败、也点不了重试,
        # 而日报这边一句「已生成」看着一切正常。
        from kb import pipeline as pl
        with patch.object(pl, 'close_batch', Mock(return_value='盘上那份是更新的一轮')):
            self.sync_run()
        self.assertIn('盘上那份是更新的一轮', daily_run._state['detail'])

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
    这一批合并成一份(`pipeline_state._publish_locked`),而且**在子轮收尾的同一步里
    就写进去**(`_publish_in_batch`)。

    钉的是后果:库里那份最近运行里还有没有那条红、点重试还排不排得出计划。
    """

    def setUp(self):
        from kb import pipeline_state as ps
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(ps, '_STATE_FILE', Path(temp.name) / 'last_run.json')
        p.start(); self.addCleanup(p.stop)
        # `_state` / `_finished` 是模块级常驻的:这几条要往里摆终态,**走的时候
        # 一定要还回去**,否则同跑时会把别的用例带红,而单跑永远是绿的。
        before_state, before_done = deepcopy(ps._state), deepcopy(ps._finished)
        def restore():
            ps._state.clear(); ps._state.update(before_state)
            ps._finished.clear(); ps._finished.update(before_done)
        self.addCleanup(restore)

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
        ps._publish_locked([fetch, digest])
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
        ps._publish_locked([self.round(['crawl', 'export'], {}, run_id='run-1',
                                     crawl_filter={'channel': 'rss'}),
                          self.round(['index', 'extract'], {}, run_id='run-2')])
        last = pipeline.load_last_run()
        self.assertEqual('ok', last['stages']['crawl']['status'])
        self.assertEqual([], pipeline.retry_failed_plan())
        # 谁都没计划过的阶段照旧是「未选择」
        self.assertEqual('未选择', last['stages']['wiki']['detail'])

    def test_the_first_round_of_a_batch_lands_as_itself(self):
        """一轮单独的时候不走合并 —— 它自己就是整批。

        这条原来钉在 `publish_batch` 的「少于两轮回 None」上,那个公开出口已经
        收掉了(留着等于给人留一条"事后再聚合"的路);契约搬到
        `_publish_in_batch` 的头一轮分支。判据是**合并留下的痕迹**:合过的那份
        detail 前面会挂来源名(`_scope_name`),没合过的就是阶段自己那句话。
        """
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        self.fresh_process()
        last = ps.load_last_run()
        self.assertEqual(('run-1', '失败 1 个'),
                         (last['run_id'], last['stages']['crawl']['detail']))
        self.assertEqual('sync', last['run_kind'])

    def test_a_takeover_leaves_a_record_complete_enough_to_merge(self):
        """没亲眼看到自己那一轮收尾、照 id 取回来的那份,也要能合进去。

        它和自观测那份现在是同一个契约(`pipeline_state.run_payload`)。缺
        `planned_stages` 的那一版里,这一轮真实的抓取失败在合并时被整个过滤掉,
        而 `_sync_round` 原来那道守卫拿 `run_id == run` 当"是不是自观测"的判据
        —— 两者的 run_id 本来就相同,等于没拦住。
        """
        from kb import pipeline, pipeline_state as ps
        ps._state.update({'run_id': 'run-9', 'started_at': 'A', 'finished_at': 'B',
                          'run_kind': 'sync', 'planned_stages': ['crawl', 'export'],
                          'crawl_filter': {'channel': 'rss'},
                          'export_filter': {'channel': 'rss'},
                          'crawl_retry': {'run': 'run-9', 'channels': {'rss': {'ids': [7]}}},
                          'stopped': False, 'log': []})
        ps._state['stages'] = {s: {'status': 'skipped', 'detail': '未选择'}
                               for s in ps.STAGES}
        ps._state['stages']['crawl'] = {'status': 'error', 'detail': '失败 10 个'}
        ps._state['stages']['export'] = {'status': 'ok', 'detail': ''}
        # **走真的收尾**,不是自己往 `_finished` 里摆一份:那样injection 打在
        # `_end_run` 上这条用例一声不吭(2026-10-14 复审第 2 轮实测,红 0 条)。
        ps._state['running'] = True
        ps._end_run()
        fetched = pipeline.run_result('run-9')
        # 契约:后备记录带着计划、来源范围和失败名单,不是一份只有 stages 的壳
        for key in ('planned_stages', 'crawl_filter', 'export_filter', 'crawl_retry'):
            self.assertIn(key, fetched, key)
        ps._publish_locked([fetched, self.round(['index', 'extract'], {}, run_id='run-10')])
        last = pipeline.load_last_run()
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(['crawl', 'export', 'index', 'extract'],
                         pipeline.retry_failed_plan())

    def test_every_failing_source_keeps_its_own_detail(self):
        """同一个阶段在几个来源都失败时,合并不许只留一个。

        只挑最重的那一个对象,等于把另一个来源的原因和失败名单整个扔掉 —— 面板上
        「来源:RSS + 网页专栏」却只列得出一条明细。
        """
        from kb import pipeline, pipeline_state as ps
        def failing(channel, title):
            return self.round(['crawl', 'export'],
                              {'crawl': {'status': 'error', 'detail': f'{channel} 失败 1 个',
                                         'failures': [{'title': title, 'reason': '连不上'}]}},
                              run_id=f'run-{channel}', crawl_filter={'channel': channel},
                              export_filter={'channel': channel})
        ps._publish_locked([failing('rss', 'RSS 源'), failing('web', '网页源'),
                          self.round(['index', 'extract'], {}, run_id='run-z')])
        crawl = pipeline.load_last_run()['stages']['crawl']
        self.assertEqual({'RSS 源', '网页源'}, {f['title'] for f in crawl['failures']})
        self.assertIn('rss 失败 1 个', crawl['detail'])
        self.assertIn('web 失败 1 个', crawl['detail'])
        # 两个来源都留在范围里,重试才两个都重抓
        self.assertEqual(['rss', 'web'], [s['channel'] for s in
                                          pipeline.crawl_channels(
                                              pipeline.load_last_run()['crawl_filter'])])

    def test_a_newer_finished_run_is_not_replaced_by_this_batch(self):
        """期间另一轮独立运行已经收尾时,这一批没有资格当「最近一次运行」。

        覆盖掉等于把一次刚失败的手动同步改写成成功,它的「重试失败项」跟着消失。
        一把锁只防同时写,答不了"我有没有资格当最新"。
        """
        from kb import pipeline, pipeline_state as ps
        ps._state.update({'run_id': 'run-manual', 'started_at': 'C', 'finished_at': 'D',
                          'run_kind': 'sync', 'planned_stages': ['index'],
                          'crawl_filter': {}, 'export_filter': {}, 'crawl_retry': {},
                          'stopped': False, 'log': []})
        ps._state['stages'] = {s: {'status': 'skipped', 'detail': '未选择'}
                               for s in ps.STAGES}
        ps._state['stages']['index'] = {'status': 'error', 'detail': '索引失败'}
        ps._save_last_run()
        with self.assertRaises(ps.PublishRefused) as caught:
            ps._publish_locked([self.round(['crawl', 'export'], {}, run_id='run-1',
                                         crawl_filter={'channel': 'rss'}),
                              self.round(['index', 'extract'], {}, run_id='run-2')])
        self.assertIn('run-manual', caught.exception.reason)
        # 那一轮手动同步的失败原样留着,重试照旧排得出来
        last = pipeline.load_last_run()
        self.assertEqual(('run-manual', 'error'),
                         (last['run_id'], last['stages']['index']['status']))
        self.assertIn('index', pipeline.retry_failed_plan())

    def fresh_process(self):
        """装成"另一个进程":内存里那份权威记录归零,只剩磁盘。"""
        for name in ('_last_result', '_last_result_file'):
            pa = patch.object(ps, name, None); pa.start(); self.addCleanup(pa.stop)

    def test_a_finished_sub_round_never_leaves_only_itself_on_disk(self):
        """进程在子轮收尾之后、日报收尾之前退出,前几轮的失败也不许没了。

        这是第 3 轮「结果保全」那一条**没修全**的部分。原来每个子轮先把自己那份
        写进唯一的状态文件,再由日报稍后合并发布 —— 中间那一刻盘上只有刚收尾的
        那一轮(入库轮的 crawl 落「本次不执行」),进程这时退出(重启、OOM、
        被杀),重启后同步中心只看到「同步已完成」,既没有 RSS 抓取失败,也没有
        「重试失败项」。**窗口有多窄都不行**:那正是"写了一份会删掉前序失败的
        局部结果,然后指望还活着的调用方稍后修好"。

        所以钉的不是"下一轮起跑前看得到",而是**每个子轮收尾之后盘上就是全的**。
        """
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {
                         'status': 'error', 'detail': '失败 1 个',
                         'failures': [{'title': '某订阅', 'reason': '连不上'}]}})
        finish_round(['index', 'extract'], None, batch, run_id='run-2',
                     started='2026-10-14 09:30:00')
        # **只读磁盘**:日报那一句收尾还没跑到,进程就是在这一刻没的
        on_disk = json.loads(ps._STATE_FILE.read_text(encoding='utf-8'))
        self.assertEqual('error', on_disk['stages']['crawl']['status'])
        self.assertEqual(['某订阅'],
                         [f['title'] for f in on_disk['stages']['crawl']['failures']])
        self.assertEqual(['crawl', 'export', 'index', 'extract'],
                         on_disk['planned_stages'])
        self.assertEqual(['rss'], [sc['channel'] for sc in
                                   pipeline.run_scopes(on_disk)])
        # 后果:重启之后那个新进程看到的还是这一份,重试照旧排得出计划
        self.fresh_process()
        self.assertEqual('error', pipeline.load_last_run()['stages']['crawl']['status'])
        self.assertIn('crawl', pipeline.retry_failed_plan())

    def test_start_pipeline_puts_the_batch_id_where_the_ledger_looks(self):
        """`start_pipeline` 要把批次身份**原子地**放进 `_state`。

        账本认的是 `_state['batch']`(见 `_publish_in_batch`),所以这一格丢了的话
        整批退化成"每轮只写自己",而上面那条用例是自己摆盘的、看不见这一段真代码
        (注入「丢掉 batch」时它红 0 条)。放进 `_state` 而不是事后标,是因为一轮
        运行完全可能比调用方的下一句还快。
        """
        from kb import pipeline
        with patch.object(pipeline.threading, 'Thread') as thread:
            run = pipeline.start_pipeline(['index'], batch='batch-9')
        self.addCleanup(ps._state.update, {'running': False})
        self.assertTrue(run)
        self.assertEqual('batch-9', ps._state['batch'])
        thread.assert_called_once()
        # 反过来钉一边:不传就是空串,**而且不许继承上一轮的**
        ps._state['running'] = False
        with patch.object(pipeline.threading, 'Thread'):
            pipeline.start_pipeline(['index'])
        self.assertEqual('', ps._state['batch'])

    def test_a_refused_batch_remembers_why_for_close_batch(self):
        """被拒的原因要留到 `close_batch` 手里,不许咽下去。

        咽下去的后果:同步中心显示的是那一轮手动同步,而日报那边一句「已生成」
        看着一切正常,没人知道这一轮日报的同步结果压根没进去。
        """
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        # 期间用户自己点了一轮同步,它比我们这一批新
        finish_round(['index'], None, '', run_id='run-manual',
                     results={'index': {'status': 'error', 'detail': '索引失败'}},
                     started='2026-10-14 10:00:00')
        finish_round(['index', 'extract'], None, batch, run_id='run-2',
                     started='2026-10-14 10:10:00')
        issue = ps.close_batch()
        self.assertIn('run-manual', issue)
        # 退回只写自己那一份:**我们这一轮确实是此刻最新的**,但不许把前几轮一起
        # 合进去盖掉人家那一轮
        self.fresh_process()
        last = ps.load_last_run()
        self.assertEqual(('run-2', ['index', 'extract']),
                         (last['run_id'], last['planned_stages']))

    def test_a_run_outside_the_batch_is_not_merged_into_it(self):
        # 反过来钉一边:不带批次身份的那一轮(手动同步、重试)就该只写它自己 ——
        # 合进一个别人的批次等于把早先那几轮的结果说成"刚刚这一轮的成绩"。
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        finish_round(['index'], None, '', run_id='run-own',
                     started='2026-10-14 10:00:00')
        self.fresh_process()
        last = pipeline.load_last_run()
        self.assertEqual(('run-own', ['index']), (last['run_id'], last['planned_stages']))
        self.assertEqual('未选择', last['stages']['crawl']['detail'])

    def test_a_half_written_state_file_cannot_eat_the_previous_result(self):
        """落盘写到一半退出,旧那份要一字未动。

        就地 `write_text` 的话盘上剩的是半截 JSON:读的那端只能当"读不出",而
        被它截掉的旧结果也没了 —— 两份一起丢。写同目录的临时文件再原子替换,
        断在中途只会留下一个临时文件,正式那份一字未动。

        注入的是**写到一半就断**(写进去半截、然后抛) —— 这才是进程死在写盘
        中间的形状。让它"写完半截正常返回"证明不了什么:那样后面那次原子替换
        照旧会把这份残缺品摆上台。
        """
        from kb import pipeline
        finish_round(['crawl'], {'channel': 'rss'}, run_id='run-good',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        good = ps._STATE_FILE.read_text(encoding='utf-8')
        real = Path.write_text

        def half(self_path, data, *a, **kw):
            real(self_path, data[:len(data) // 2], *a, **kw)
            raise OSError('写到一半断了')

        with patch.object(Path, 'write_text', half):
            finish_round(['index'], None, run_id='run-cut')
        self.assertEqual(good, ps._STATE_FILE.read_text(encoding='utf-8'),
                         '旧那份被半截写坏了')
        self.fresh_process()
        self.assertEqual('run-good', pipeline.load_last_run()['run_id'])

    def test_two_same_named_subscriptions_both_keep_their_row(self):
        """两条订阅同名时,两条失败都要留下。**显示用的标题不是身份。**

        按 (标题, 原因) 去重的后果:两个来源各有一条叫「每日速览」的订阅、都是
        「连不上」,面板上只剩一行、计数也少一个 —— 看上去就是"只有一个来源出了
        问题",而另一个来源那条红从此没人知道。同名同因在同一个来源里也会发生
        (两条都叫「每日速览」),所以这里一条都不并。
        """
        from kb import pipeline, pipeline_state as ps
        def failing(channel, titles):
            return self.round(['crawl', 'export'],
                              {'crawl': {'status': 'error', 'detail': '失败 1 个',
                                         'failures': [{'title': t, 'reason': '连不上'}
                                                      for t in titles]}},
                              run_id=f'run-{channel}', crawl_filter={'channel': channel},
                              export_filter={'channel': channel})
        ps._publish_locked([failing('rss', ['每日速览', '每日速览']),
                          failing('web', ['每日速览']),
                          self.round(['index', 'extract'], {}, run_id='run-z')])
        crawl = pipeline.load_last_run()['stages']['crawl']
        self.assertEqual(3, len(crawl['failures']),
                         f'同名同因被并掉了:{crawl["failures"]}')
        # detail 同理:两个来源各写一句「失败 1 个」,照文本去重就成了一句,
        # 而卡片上写着来源是两个。按来源挂名之后两句都在、还看得出谁是谁。
        self.assertIn('RSS：失败 1 个', crawl['detail'])
        self.assertIn('失败 1 个；', crawl['detail'])
        self.assertEqual(2, crawl['detail'].count('失败 1 个'), crawl['detail'])

    def test_a_library_round_adds_no_source_name_to_its_own_detail(self):
        # 反过来钉一边:全库那一轮不属于任何来源,它那句话前面不许挂来源名
        # (挂上去的话,`start_pipeline` 盖的那个默认采集器会被念成"公众号:索引
        # 失败" —— 磁盘满跟公众号没有半点关系)。
        from kb import pipeline, pipeline_state as ps
        ps._publish_locked([self.round(['crawl', 'export'], {}, run_id='run-1',
                                     crawl_filter={'channel': 'rss'}),
                          self.round(['index', 'extract'],
                                     {'index': {'status': 'error', 'detail': '磁盘满'}},
                                     run_id='run-2',
                                     crawl_filter={'channel': 'wechat_local'})])
        self.assertEqual('磁盘满', pipeline.load_last_run()['stages']['index']['detail'])

    def test_a_newer_run_blocks_this_batch_even_when_its_own_write_failed(self):
        """那一轮手动同步**没落盘成功**,照样是最新的一轮。

        落盘会失败(满盘、目录没了),而那一下是**吞掉**的 —— 本来也不该因为写不进
        状态文件就判这一轮失败。错的是拿磁盘当"有没有更新的一轮"的判据:那一轮
        明明跑完了、结论还在内存里,磁盘上却还是这一批的某一轮,于是这一批
        "有资格当最新",把人家刚刚的 index 失败改写成成功、「重试失败项」跟着
        消失,而日报那边一句「已生成」看着一切正常。
        """
        from kb import pipeline, pipeline_state as ps
        ps._state.update({'run_id': 'run-manual', 'started_at': 'C', 'finished_at': 'D',
                          'run_kind': 'sync', 'planned_stages': ['index'],
                          'crawl_filter': {}, 'export_filter': {}, 'crawl_retry': {},
                          'stopped': False, 'log': []})
        ps._state['stages'] = {s: {'status': 'skipped', 'detail': '未选择'}
                               for s in ps.STAGES}
        ps._state['stages']['index'] = {'status': 'error', 'detail': '索引失败'}
        with patch.object(Path, 'write_text', Mock(side_effect=OSError('磁盘满'))):
            ps._save_last_run()
        self.assertFalse(ps._STATE_FILE.exists(), '这一局的前提:磁盘上压根没有它')
        with self.assertRaises(ps.PublishRefused) as caught:
            ps._publish_locked([self.round(['crawl', 'export'], {}, run_id='run-1',
                                         crawl_filter={'channel': 'rss'}),
                              self.round(['index', 'extract'], {}, run_id='run-2')])
        self.assertIn('run-manual', caught.exception.reason)
        # 面板和重试读的是同一份,而且是那一轮真实的失败
        last = pipeline.load_last_run()
        self.assertEqual(('run-manual', 'error'),
                         (last['run_id'], last['stages']['index']['status']))
        self.assertIn('index', pipeline.retry_failed_plan())
        # 写不进去也**不许一声不吭**:这一轮的日志里要说清结论只活在内存里
        self.assertTrue(any('没能写进磁盘' in line for line in last['log']), last['log'])

    def test_an_unreadable_last_run_is_not_a_green_light(self):
        """读不出盘上那份 = **不知道**,不是"没有更新的一轮"。

        刚重启的进程手里没有权威身份,只能问磁盘;而一份读不出来的状态既可能是
        「没有更新的运行」也可能是「有、而且比我们新」。放行就又回到把人家的失败
        改写成成功那一档,所以**失败关闭**。
        """
        from kb import pipeline_state as ps
        batch = [self.round(['crawl', 'export'],
                            {'crawl': {'status': 'error', 'detail': '失败 10 个'}},
                            run_id='run-1', crawl_filter={'channel': 'rss'}),
                 self.round(['index', 'extract'], {}, run_id='run-2')]
        broken = '{"run_id": "run-'
        ps._STATE_FILE.write_text(broken, encoding='utf-8')
        with self.assertRaises(ps.PublishRefused) as caught:
            ps._publish_locked(deepcopy(batch))
        self.assertIn('读不出', caught.exception.reason)
        self.assertEqual(broken, ps._STATE_FILE.read_text(encoding='utf-8'),
                         '拒绝发布就不许动它')
        # 反过来钉一边:**文件压根不存在**是合法的头一轮,照旧发布得出去 ——
        # 一律失败关闭会让新装的库永远发布不了第一批
        ps._STATE_FILE.unlink()
        published = ps._publish_locked(deepcopy(batch))
        self.assertEqual('error', published['stages']['crawl']['status'])

    def test_a_write_failure_is_not_a_silent_success(self):
        # 静默吞掉的后果:同步中心看不到这一轮的失败、也点不了重试,而日报那边
        # 一句「已生成」看着一切正常。
        from kb import pipeline_state as ps
        with patch.object(Path, 'write_text', Mock(side_effect=OSError('磁盘满'))), \
             self.assertRaises(ps.PublishRefused) as caught:
            ps._publish_locked([self.round(['crawl', 'export'], {}, run_id='run-1',
                                         crawl_filter={'channel': 'rss'}),
                              self.round(['index', 'extract'], {}, run_id='run-2')])
        self.assertIn('磁盘满', caught.exception.reason)


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
