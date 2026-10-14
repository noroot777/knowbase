# -*- coding: utf-8 -*-
"""管道运行结果的发布与保全:一轮日报 N+1 次运行,而「最近一次运行」只有一个位子。

从 `test_daily_two_stage.py` 拆出来 —— 那份加上这一组会超过单文件 700 行上限。
编排(谁跑几次、红算在谁头上)、范围口径仍在那一份里;摆盘共用
`tests/pipeline_round_fixture.py`。
"""
import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from kb import pipeline_state as ps
from tests.pipeline_round_fixture import RoundStore, finish_round


class DailyBatchResultTests(RoundStore):
    """一轮日报是 N+1 次管道运行,而「最近一次运行」只有一份。

    后跑的会把先跑的盖掉 —— 抓取失败的明细、失败名单和「重试失败项」会被最后那
    一轮入库的成功一起抹掉(`retry_failed_plan` 读的就是这一份)。两段式之前只有
    最后一个来源的结果留得下来,改成两段式之后变成**每次必然发生**。所以收尾时把
    这一批合并成一份(`pipeline_merge._merge_runs`),而且**在子轮收尾的同一步里
    就写进去**(`_publish_in_batch`)。

    钉的是后果:库里那份最近运行里还有没有那条红、点重试还排不排得出计划。
    """

    def test_a_fetch_failure_survives_the_library_round_that_follows_it(self):
        from kb import pipeline
        self.run_batch(
            self.spec(['crawl', 'export'], run_id='run-1',
                      crawl_filter={'channel': 'rss'},
                      crawl_retry={'run': 'run-1', 'channels': {'rss': {'ids': [7]}}},
                      results={'crawl': {'status': 'error', 'detail': '失败 10 个',
                                         'failures': [{'title': 'X', 'reason': '连不上'}]}}),
            self.spec(['index', 'extract'], run_id='run-2'))
        last = pipeline.last_result()
        # 那条红还在,明细也在 —— 这是用户点进同步中心要看的东西
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(1, len(last['stages']['crawl']['failures']))
        # 入库那两段的成功同样记下来了,不是拿失败轮整个顶掉
        self.assertEqual(('ok', 'ok'), (last['stages']['index']['status'],
                                        last['stages']['extract']['status']))
        self.assertEqual(['crawl', 'export', 'index', 'extract'], last['planned_stages'])
        # 范围是真抓过的那个来源,不是入库轮被自动盖上去的默认采集器
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
        from kb import pipeline
        self.run_batch(self.spec(['crawl', 'export'], run_id='run-1',
                                 crawl_filter={'channel': 'rss'}),
                       self.spec(['index', 'extract'], run_id='run-2'))
        last = pipeline.last_result()
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
        self.run_batch(self.spec(['crawl'], run_id='run-1',
                                 crawl_filter={'channel': 'rss'},
                                 results={'crawl': {'status': 'error',
                                                    'detail': '失败 1 个'}}))
        self.fresh_process()
        last = ps.load_last_run()
        self.assertEqual(('run-1', '失败 1 个'),
                         (last['run_id'], last['stages']['crawl']['detail']))
        self.assertEqual('sync', last['run_kind'])

    def test_the_ledger_does_not_need_daily_to_witness_a_round(self):
        """账本由 `_end_run` 喂,所以"没亲眼看到自己那一轮收尾"也照样进账。

        日报那边的轮询完全可能错过自己那一轮(跑得比下一跳还快,接替者已经顶上)。
        它照 id 取回来的那份(`run_result`)和自观测那份是同一个契约
        (`run_payload`)—— 缺 `planned_stages` 的那一版里,这一轮真实的抓取失败
        在合并时被整个过滤掉。现在合并压根不经过日报,这里钉两件事:那份记录
        仍然完整(日报要靠它打分),而整批的合并不依赖谁看见了什么。
        """
        from kb import pipeline
        self.run_batch(
            self.spec(['crawl', 'export'], run_id='run-9',
                      crawl_filter={'channel': 'rss'},
                      crawl_retry={'run': 'run-9', 'channels': {'rss': {'ids': [7]}}},
                      results={'crawl': {'status': 'error', 'detail': '失败 10 个'}}),
            self.spec(['index', 'extract'], run_id='run-10'))
        fetched = pipeline.run_result('run-9')
        for key in ('planned_stages', 'crawl_filter', 'export_filter', 'crawl_retry'):
            self.assertIn(key, fetched, key)
        self.assertEqual('error', fetched['stages']['crawl']['status'])
        last = pipeline.last_result()
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(['crawl', 'export', 'index', 'extract'],
                         pipeline.retry_failed_plan())

    def test_every_failing_source_keeps_its_own_detail(self):
        """同一个阶段在几个来源都失败时,合并不许只留一个。

        只挑最重的那一个对象,等于把另一个来源的原因和失败名单整个扔掉 —— 面板上
        「来源:RSS + 网页专栏」却只列得出一条明细。
        """
        from kb import pipeline
        def failing(channel, title):
            return self.spec(['crawl', 'export'], run_id=f'run-{channel}',
                             crawl_filter={'channel': channel},
                             results={'crawl': {
                                 'status': 'error', 'detail': f'{channel} 失败 1 个',
                                 'failures': [{'title': title, 'reason': '连不上'}]}})
        self.run_batch(failing('rss', 'RSS 源'), failing('web', '网页源'),
                       self.spec(['index', 'extract'], run_id='run-z'))
        crawl = pipeline.last_result()['stages']['crawl']
        self.assertEqual({'RSS 源', '网页源'}, {f['title'] for f in crawl['failures']})
        self.assertIn('rss 失败 1 个', crawl['detail'])
        self.assertIn('web 失败 1 个', crawl['detail'])
        # 两个来源都留在范围里,重试才两个都重抓
        self.assertEqual(['rss', 'web'], [s['channel'] for s in
                                          pipeline.crawl_channels(
                                              pipeline.last_result()['crawl_filter'])])

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

    def test_a_new_batch_does_not_show_the_previous_batch_as_latest(self):
        """新一批的头一轮收尾之后,权威位上不许还挂着上一批那份汇总。

        整批那份汇总是**进程内的权威**(落盘成不成功都照旧当权威),所以每一次
        收尾都得重设它 —— 口径收在 `_save_own` 里。漏掉头一轮那一次的后果:新一批
        的抓取段明明跑完了,面板显示的仍是上一批的结论(上一批的阶段、上一批那条
        失败),「重试失败项」也跟着去抓上一批那几个来源 —— 而这两件事都没有任何
        报错,因为磁盘上那一份是对的、只有内存里那份权威是旧的。
        反过来钉一边:同一批之内汇总**就是**权威,那一档在
        `test_a_fetch_failure_survives_the_library_round_that_follows_it` 钉着。
        """
        from kb import pipeline
        first = ps.open_batch()
        finish_round(['crawl', 'export'], {'channel': 'rss'}, first, run_id='old-1',
                     results={'crawl': {
                         'status': 'error', 'detail': '失败 1 个',
                         'failures': [{'title': '上一批那条', 'reason': '连不上'}]}})
        finish_round(['index', 'extract'], None, first, run_id='old-2',
                     started='2026-10-14 09:30:00')
        self.assertEqual('', ps.close_batch())
        second = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, second, run_id='new-1',
                     started='2026-10-15 09:00:00')
        last = pipeline.last_result()
        self.assertEqual(('new-1', 'ok'),
                         (last['run_id'], last['stages']['crawl']['status']))
        self.assertNotIn('上一批那条', json.dumps(last, ensure_ascii=False))
        self.assertEqual([], pipeline.retry_failed_plan())

    def test_a_half_written_state_file_cannot_eat_the_previous_result(self):
        """落盘写到一半退出,旧那份要一字未动。

        就地改写正式文件的话,盘上剩的是半截 JSON:读的那端只能当"读不出",而
        被它截掉的旧结果也没了 —— 两份一起丢。写同目录的临时文件再原子替换,
        断在中途只会留下一个临时文件(而且那个也收掉了),正式那份一字未动。

        注入的是**写到一半就断**(写进去半截、然后抛) —— 这才是进程死在写盘
        中间的形状。让它"写完半截正常返回"证明不了什么:那样后面那次原子替换
        照旧会把这份残缺品摆上台。**注入点钉在 `Path.write_bytes`**:原子替换
        那一步(`pipeline_persist._write_atomic`)写的是临时文件,钉在别的写法上
        等于注入压根没落在写盘那一步,一注就"正常写完"。
        """
        from kb import pipeline
        finish_round(['crawl'], {'channel': 'rss'}, run_id='run-good',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        good = ps._STATE_FILE.read_text(encoding='utf-8')
        real = Path.write_bytes

        def half(self_path, data, *a, **kw):
            real(self_path, data[:len(data) // 2], *a, **kw)
            raise OSError('写到一半断了')

        with patch.object(Path, 'write_bytes', half):
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
        from kb import pipeline
        def failing(channel, titles):
            return self.spec(['crawl', 'export'], run_id=f'run-{channel}',
                             crawl_filter={'channel': channel},
                             results={'crawl': {
                                 'status': 'error', 'detail': '失败 1 个',
                                 'failures': [{'title': t, 'reason': '连不上'}
                                              for t in titles]}})
        self.run_batch(failing('rss', ['每日速览', '每日速览']),
                       failing('web', ['每日速览']),
                       self.spec(['index', 'extract'], run_id='run-z'))
        crawl = pipeline.last_result()['stages']['crawl']
        self.assertEqual(3, len(crawl['failures']),
                         f'同名同因被并掉了:{crawl["failures"]}')
        # detail 同理:两个来源各写一句「失败 1 个」,照文本去重就成了一句,
        # 而卡片上写着来源是两个。按来源挂名之后两句都在、还看得出谁是谁。
        self.assertIn('RSS：失败 1 个', crawl['detail'])
        self.assertIn('失败 1 个；', crawl['detail'])
        self.assertEqual(2, crawl['detail'].count('失败 1 个'), crawl['detail'])

    def test_truncated_failure_details_keep_the_real_total(self):
        """明细截断到 50 条时,**真实总数要留下**。

        只截不记的后果:面板照着数组长度写「问题明细 · 50 项」,而真实失败数可能
        是三百 —— 用户看到的是一个看着完整的清单,余下那些连"还有"都不知道。
        上限本身是刻意的(状态体不能膨胀),重试名单另存,不从这个展示数组反推。
        """
        from kb import pipeline
        def failing(channel, count):
            return self.spec(['crawl', 'export'], run_id=f'run-{channel}',
                             crawl_filter={'channel': channel},
                             results={'crawl': {
                                 'status': 'error', 'detail': f'失败 {count} 个',
                                 'failures': [{'title': f'{channel}-{i}',
                                               'reason': '连不上'}
                                              for i in range(count)]}})
        self.run_batch(failing('rss', 40), failing('web', 40),
                       self.spec(['index', 'extract'], run_id='run-z'))
        crawl = pipeline.last_result()['stages']['crawl']
        self.assertEqual(ps._FAILURES_KEEP, len(crawl['failures']))
        self.assertEqual(80, crawl['failures_total'])
        # 反过来钉一边:没截断的那一档不许带这一格(带了面板就会写一句
        # 「仅列前 N 条」,而其实一条都没少)
        self.run_batch(failing('rss', 3),
                       self.spec(['index', 'extract'], run_id='run-y'))
        self.assertNotIn('failures_total',
                         pipeline.last_result()['stages']['crawl'])

    def test_each_truncation_layer_keeps_its_own_total(self):
        """失败明细有三处上限,**每一处都要自己留总数**。

        只在整批合并那一层算的话,单来源、单轮次那一档(没有分段、也没有第二轮
        可合)照旧只剩一个看着完整的「50 项」—— 而真实失败数可能是三百。
        """
        many = [{'title': f't{i}', 'reason': '连不上'} for i in range(60)]
        ps._state['stages'] = {st: {'status': 'skipped', 'detail': ''}
                               for st in ps.STAGES}
        ps.clear_segments()
        self.addCleanup(ps.clear_segments)
        # ① 单阶段自己那一次
        ps._stage('crawl', 'error', '失败 60 个', failures=many)
        entry = ps._state['stages']['crawl']
        self.assertEqual((ps._FAILURES_KEEP, 60),
                         (len(entry['failures']), entry['failures_total']))
        # ② 多来源分段:两段各 40,合起来 80
        ps._state['stages']['crawl'] = {'status': 'skipped', 'detail': ''}
        for label, part in (('RSS', many[:40]), ('网页专栏', many[20:60])):
            with ps.stage_segment('crawl', label):
                ps._stage('crawl', 'error', '失败 40 个', failures=part)
        entry = ps._state['stages']['crawl']
        self.assertEqual((ps._FAILURES_KEEP, 80),
                         (len(entry['failures']), entry['failures_total']))
        # 反过来钉一边:两段加起来没超上限时不许带这一格
        ps.clear_segments()
        ps._state['stages']['crawl'] = {'status': 'skipped', 'detail': ''}
        for label in ('RSS', '网页专栏'):
            with ps.stage_segment('crawl', label):
                ps._stage('crawl', 'error', '失败 3 个', failures=many[:3])
        self.assertNotIn('failures_total', ps._state['stages']['crawl'])

    def test_a_half_written_aggregate_cannot_eat_the_previous_batch_result(self):
        """汇总那一次写到一半断掉,**这一批上一轮的汇总要一字未动**。

        第 5 轮实测:汇总那条路原来是就地改写正式文件,正式文件被写进半截 JSON、
        进程立刻退出 —— 新进程读出来是 null,连之前已经落盘的那份有效汇总一起
        没了(重启后页面像从没同步过)。写出口有两个,只把普通那一条改成原子
        替换等于没改:正常日报从第二个子轮起走的就是汇总这一条。
        注入点同上一条,钉在 `Path.write_bytes`(原子替换写的是临时文件)。
        """
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {
                         'status': 'error', 'detail': '失败 1 个',
                         'failures': [{'title': '某订阅', 'reason': '连不上'}]}})
        kept = ps._STATE_FILE.read_text(encoding='utf-8')
        real = Path.write_bytes

        def half(self_path, data, *a, **kw):
            real(self_path, data[:len(data) // 2], *a, **kw)
            raise OSError('写到一半断了')

        with patch.object(Path, 'write_bytes', half):
            finish_round(['index', 'extract'], None, batch, run_id='run-2',
                         started='2026-10-14 09:30:00')
        self.assertEqual(kept, ps._STATE_FILE.read_text(encoding='utf-8'),
                         '上一轮那份汇总被半截写坏了')
        self.fresh_process()
        last = pipeline.load_last_run()
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertIn('crawl', pipeline.retry_failed_plan())

    def test_a_library_round_adds_no_source_name_to_its_own_detail(self):
        # 反过来钉一边:全库那一轮不属于任何来源,它那句话前面不许挂来源名
        # (挂上去的话,`start_pipeline` 盖的那个默认采集器会被念成"公众号:索引
        # 失败" —— 磁盘满跟公众号没有半点关系)。
        from kb import pipeline
        self.run_batch(self.spec(['crawl', 'export'], run_id='run-1',
                                 crawl_filter={'channel': 'rss'}),
                       self.spec(['index', 'extract'], run_id='run-2',
                                 results={'index': {'status': 'error',
                                                    'detail': '磁盘满'}}))
        self.assertEqual('磁盘满', pipeline.last_result()['stages']['index']['detail'])

    def test_a_newer_run_blocks_this_batch_even_when_its_own_write_failed(self):
        """那一轮手动同步**没落盘成功**,照样是最新的一轮。

        落盘会失败(满盘、目录没了),而那一下是**吞掉**的 —— 本来也不该因为写不进
        状态文件就判这一轮失败。错的是拿磁盘当"有没有更新的一轮"的判据:那一轮
        明明跑完了、结论还在内存里,磁盘上却还是这一批的某一轮,于是这一批
        "有资格当最新",把前几轮一起合进去盖掉人家刚刚那条 index 失败。
        """
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1')
        # 期间用户自己点了一轮同步,它失败了、而且连状态文件都没写进去
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            finish_round(['index'], None, '', run_id='run-manual',
                         results={'index': {'status': 'error', 'detail': '索引失败'}},
                         started='2026-10-14 10:00:00')
        finish_round(['index', 'extract'], None, batch, run_id='run-2',
                     started='2026-10-14 10:10:00')
        issue = ps.close_batch()
        self.assertIn('run-manual', issue)
        # 文案只说已经发生的事:**不许承诺"显示的是那一轮"** —— 紧接着的退路会把
        # 当前子轮写进去,于是显示的其实是刚结束的这一轮(第 5 轮实测文案不实)。
        self.assertIn('最后收尾的那一轮', issue)
        self.assertNotIn('显示的是那一轮', issue)
        # 整批没有被合并发布:显示和重试都只认最后收尾的那一轮
        self.fresh_process()
        last = pipeline.load_last_run()
        self.assertEqual(('run-2', ['index', 'extract']),
                         (last['run_id'], last['planned_stages']))

    def test_an_unreadable_last_run_is_not_a_green_light(self):
        """手里没有权威身份时只能问磁盘,而**读不出来就失败关闭**。

        一份读不出来的状态既可能是「没有更新的运行」也可能是「有、而且比我们新」,
        放行就又回到把人家的失败改写成成功那一档。

        这一档是**冗余带**:正常跑的时候这一批的头一轮已经记下权威身份,走不到
        这里。它守的是"手里没有身份"这件事本身(数据目录换了、权威被清掉),所以
        这条用例也是把权威清掉来走它 —— 冗余带不删,但要说清它是冗余带。
        """
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 10 个'}})
        ps._STATE_FILE.write_text('{"run_id": "run-', encoding='utf-8')
        with patch.object(ps, '_last_settled', ''), \
             patch.object(ps, '_last_settled_file', None):
            finish_round(['index', 'extract'], None, batch, run_id='run-2',
                         started='2026-10-14 09:30:00')
        self.assertIn('读不出', ps.close_batch())
        # 没资格就不合并:盘上写的是最后收尾的那一轮自己(坏文件本来也读不出来,
        # 留着它才是真的什么都没有)
        self.fresh_process()
        self.assertEqual(['index', 'extract'],
                         pipeline.load_last_run()['planned_stages'])

    def test_a_missing_last_run_still_lets_the_batch_publish(self):
        # 反过来钉一边:**文件压根不存在**是合法的头一轮,照旧合并发布得出去 ——
        # 一律失败关闭会让新装的库永远发布不了第一批。
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 10 个'}})
        ps._STATE_FILE.unlink()
        with patch.object(ps, '_last_settled', ''), \
             patch.object(ps, '_last_settled_file', None):
            finish_round(['index', 'extract'], None, batch, run_id='run-2',
                         started='2026-10-14 09:30:00')
        self.assertEqual('', ps.close_batch())
        self.fresh_process()
        last = pipeline.load_last_run()
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(['crawl', 'export', 'index', 'extract'], last['planned_stages'])

    def test_a_transient_write_failure_cannot_demote_the_batch(self):
        """汇总写盘失败一次,整批的失败不许被降成当前子轮的局部成功。

        第 5 轮实测:只让汇总那一次写抛 OSError、后面立刻恢复 —— 退路却去写了
        **当前子轮**(入库那一轮的 crawl 是「未选择」),于是 RSS 那条抓取失败在
        内存里和磁盘上一起没了:同步中心显示「同步已完成」,重试入口也没有。
        写成不成功只回答"重启之后读不读得到",答不了"本进程这一批有没有出过那条
        失败" —— 所以整批那份照旧当权威,盘上旧那份原样留着。
        """
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {
                         'status': 'error', 'detail': '失败 1 个',
                         'failures': [{'title': '某订阅', 'reason': '连不上'}]}})
        kept = ps._STATE_FILE.read_text(encoding='utf-8')
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            finish_round(['index', 'extract'], None, batch, run_id='run-2',
                         started='2026-10-14 09:30:00')
        # 当前读取:显示和重试读的那一份是**整批**
        last = pipeline.last_result()
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(['某订阅'],
                         [f['title'] for f in last['stages']['crawl']['failures']])
        self.assertEqual(['crawl', 'export', 'index', 'extract'], last['planned_stages'])
        self.assertEqual(['crawl', 'export', 'index', 'extract'],
                         pipeline.retry_failed_plan())
        # 盘上那份一字未动(这一批上一轮的汇总,同样带着那条失败)
        self.assertEqual(kept, ps._STATE_FILE.read_text(encoding='utf-8'))
        # 降级要说出来:一格结构化字段(面板直接显示)+ 一行日志 + 日报的提示
        self.assertIn('没能写进磁盘', last['persist_issue'])
        self.assertTrue(any('没能写进磁盘' in line for line in last['log']), last['log'])
        self.assertIn('没能写进磁盘', ps.close_batch())
        # 重启之后:盘上那份(这一批上一轮的汇总)原样在,那条失败一个字没丢
        self.fresh_process()
        restarted = pipeline.load_last_run()
        self.assertEqual('error', restarted['stages']['crawl']['status'])
        # **而重试不许静默拿它的范围去跑。** 这一档正是"状态文件写失败、历史写
        # 成功":历史里那一轮(run-2)更新,但历史记录没有重试派生字段 —— 恢复只
        # 证得出"有更新的一轮",证不出它的范围。如实报失证并拦住,不许退回盘上
        # 那一轮的范围(那样导出会摘掉失败那个来源、面板报「已保存 0 条」,见
        # `docs/decisions/retry-scope.md`)。失败原因本身没丢:盘上那份和历史里
        # 那条都还在。
        # 恢复是懒做的(`_restore_once`),先读一次权威触发它 —— 面板那条路也是
        # 这个顺序。重启之后屏幕上是历史里更新的那一轮(入库那一轮),而不是盘上
        # 这份更旧的汇总:**不许把更旧的那一份摆成「最新」**。
        self.assertEqual('run-2', pipeline.last_result()['run_id'])
        self.assertEqual('scope', pipeline.restore_gap())
        self.assertEqual([], pipeline.retry_failed_plan())

    def test_a_later_successful_publish_clears_the_warning(self):
        """中间一轮写失败、后面写成功了,就不许还挂着「没写进」。

        第 5 轮实测:当前状态明明已经写进去了(新进程都读得到整批),日报的完成
        提示里却还带着那句旧抱怨。原因留在运行日志里,"当前仍未保存"这句话不行。
        """
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                     results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            finish_round(['crawl', 'export'], {'channel': 'web'}, batch, run_id='run-2',
                         started='2026-10-14 09:10:00')
        finish_round(['index', 'extract'], None, batch, run_id='run-3',
                     started='2026-10-14 09:20:00')
        self.assertEqual('', ps.close_batch())
        self.fresh_process()
        last = pipeline.load_last_run()
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertEqual(['crawl', 'export', 'index', 'extract'], last['planned_stages'])
        self.assertNotIn('persist_issue', last)
        # **"当前仍未保存"撤掉,那次失败本身要留着**:子轮快照是落盘之前拍的,
        # 下一轮合并从账本重新拼日志 —— 只把那行字追加到当轮结果里的话,这条
        # 历史在后面一轮成功之后就没了(第 6 轮实测找不到),而我们对外说的是
        # "原因留在运行日志里"。
        self.assertTrue(any('没能写进磁盘' in line for line in last['log']), last['log'])

    def test_the_first_round_write_failure_also_stays_in_the_final_log(self):
        """**头一轮**写失败那条,后面写成功之后同样要留着。

        这一批的头一轮走的是普通保存(`_save_last_run`,整批只有它自己时写自己那份
        就是全的),不是汇总那条路。所以"把保存事件记进批次账本"这件事不能只做在
        汇总那一侧:第 1 轮实测注入首轮写失败、入库那一轮正常落盘,抓取失败和重试
        计划都对、当前那句警告也正确撤掉了,但最终日志里找不到首轮那次失败 ——
        而决策文档和上一条用例都说"原因留在运行日志里"。日志也远没到 80 行上限。
        """
        from kb import pipeline
        batch = ps.open_batch()
        self.addCleanup(ps.close_batch)
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('首轮磁盘满'))):
            finish_round(['crawl', 'export'], {'channel': 'rss'}, batch, run_id='run-1',
                         results={'crawl': {'status': 'error', 'detail': '失败 1 个'}})
        finish_round(['index', 'extract'], None, batch, run_id='run-2',
                     started='2026-10-14 09:20:00')
        self.assertEqual('', ps.close_batch())
        self.fresh_process()
        last = pipeline.load_last_run()
        # 先把这一批本来的后果钉住:首轮那条抓取失败和重试入口都在
        self.assertEqual('error', last['stages']['crawl']['status'])
        self.assertNotIn('persist_issue', last)
        self.assertTrue(any('首轮磁盘满' in line for line in last['log']), last['log'])
        self.assertLess(len(last['log']), 80, last['log'])

    def test_a_plain_write_failure_is_reported_as_a_persistence_issue(self):
        """不在批次里的那一轮写不进去,也要有结构化的一格给面板用。

        只写运行日志的话,得展开日志才知道"这一轮重启后会退回旧记录",而面板上
        那句收尾文案仍是一句干净的「同步完成」。
        """
        with patch.object(ps, '_write_state', Mock(side_effect=OSError('磁盘满'))):
            finish_round(['index'], None, run_id='run-own',
                         results={'index': {'status': 'error', 'detail': '索引失败'}})
        last = ps.last_result()
        self.assertIn('磁盘满', last['persist_issue'])
        self.assertTrue(any('磁盘满' in line for line in last['log']), last['log'])
        self.assertEqual('error', last['stages']['index']['status'])
        # 反过来钉一边:写成功的那一轮不许带这一格(带了就是挂一句假警报)
        finish_round(['index'], None, run_id='run-ok',
                     started='2026-10-14 10:00:00')
        self.assertNotIn('persist_issue', ps.last_result())


if __name__ == '__main__':
    unittest.main()
