"""日报的停止与超预算语义:按下停止 = 这一轮到此为止;超预算 = 别再抓了,照样出报。

从 `test_daily_report.py` 拆出来 —— 那份已经顶着单文件 700 行上限。窗口/素材口径
和落盘产物仍在那一份里,两段式编排在 `test_daily_two_stage.py`。
"""
import json
import tempfile
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import daily, daily_run
from kb.web import daily_api


class DailyStopTests(unittest.TestCase):
    """按下「停止」= 这一轮到此为止;同步超预算 = 别再同步了,照样出报。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Path(temp.name) / 'daily_report.json'
        for p in (patch.object(daily_run, 'STATE_FILE', Path(temp.name) / 'last_run.json'),
                  patch.object(daily_run, 'SETTINGS_FILE', self.settings)):
            p.start(); self.addCleanup(p.stop)
        self.before = deepcopy(daily_run._state)
        self.addCleanup(lambda: daily_run._state.update(self.before))

    def run_once(self, **flags):
        daily_run._state.update({**self.before, 'running': True, 'log': [],
                                 'channels': [], **flags})
        gen = Mock(return_value={'date': '2026-09-29', 'since': '', 'until': '', 'path': '',
                                 'docs': 2, 'total': 2, 'late': 0, 'model': 'm'})
        with patch.object(daily, 'generate', gen):
            daily_run._run(None, None, False, [], daily.FIRST_DAYS)
        return gen

    def test_user_stop_does_not_spend_another_model_call(self):
        # 界面上写的是"当前阶段结束后停止",用户的预期是不再出报;继续生成等于
        # 把一份同步到一半的日报当成功交出去,还再花一次模型钱。
        gen = self.run_once(stop_requested=True)
        gen.assert_not_called()
        self.assertEqual(('stopped', False),
                         (daily_run._state['phase'], daily_run._state['running']))

    def test_running_out_of_budget_still_generates(self):
        # 超预算是"别再同步了,拿现有内容出报",生成正是它的目的。
        gen = self.run_once(over_budget=True)
        gen.assert_called_once()
        self.assertEqual('done', daily_run._state['phase'])

    def test_stop_is_refused_once_the_model_is_writing(self):
        # 生成阶段停不了:模型已经在出稿,`daily.generate()` 自己把文件写完才
        # 返回。受理下来只会承诺一件做不到的事——界面写着"这一轮不生成日报",
        # 实际照样多出一份,钱也照花(2026-09-30 审查实测)。
        daily_run._state.update({**self.before, 'running': True, 'phase': 'generate'})
        accepted, detail = daily_run.request_stop()
        self.assertFalse(accepted)
        self.assertIn('不能中途停止', detail)
        self.assertFalse(daily_run._state['stop_requested'])

    def test_stopping_mid_generation_does_not_promise_anything_it_cannot_do(self):
        # 审查者的复现形态:生成桩阻塞在模型调用上时按停止。受理与否必须和
        # 实际行为一致——这里是"拒掉 + 照常出报",而不是"受理 + 照常出报"。
        seen = {}

        def generating(**kwargs):
            seen['stop'] = daily_run.request_stop()
            return {'date': '2026-09-29', 'since': '', 'until': '', 'path': '',
                    'docs': 2, 'total': 2, 'late': 0, 'model': 'm'}

        daily_run._state.update({**self.before, 'running': True, 'log': [], 'channels': []})
        with patch.object(daily, 'generate', Mock(side_effect=generating)):
            daily_run._run(None, None, False, [], daily.FIRST_DAYS)
        self.assertFalse(seen['stop'][0])
        self.assertEqual('done', daily_run._state['phase'])
        self.assertFalse(daily_run._state['stop_requested'])

    def test_stop_while_waiting_leaves_another_task_pipeline_alone(self):
        # 日报允许在别人的管道后面排队等。这时候按停止只该结束我们自己的等待,
        # 顺手把别人启动的那一轮同步也停掉是越权。
        from kb import pipeline as pl
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': ''})
        with patch.object(pl, 'get_state', Mock(return_value={'running': True})), \
             patch.object(pl, 'request_stop', Mock()) as stop:
            accepted, _ = daily_run.request_stop()
        self.assertTrue(accepted)
        stop.assert_not_called()
        self.assertTrue(daily_run._state['stop_requested'])

    def test_stop_does_end_the_round_we_started(self):
        from kb import pipeline as pl
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': 'run-7'})
        with patch.object(pl, 'get_state', Mock(return_value={'running': True})), \
             patch.object(pl, 'request_stop', Mock()) as stop:
            self.assertEqual(True, daily_run.request_stop()[0])
        # 带上 id:停的是"我们那一轮",不是"现在在跑的那一轮"
        stop.assert_called_once_with('run-7')

    def test_stop_targets_our_run_by_id_not_whatever_runs_now(self):
        # 审查者的复现:我们那一轮在两次轮询之间跑完,另一项普通同步紧接着
        # 拿到全局管道。只记布尔所有权的话,停止会落到别人头上
        # (2026-09-30 审查实测 pipeline_stop_calls=1 落在替换后的同步上)。
        from kb import pipeline_state as ps
        state = deepcopy(ps._state)
        try:
            ps._state.update({'running': True, 'run_id': 'run-2',
                              'stop_requested': False, 'log': []})
            daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                     'pipeline_run': 'run-1'})
            self.assertEqual(True, daily_run.request_stop()[0])
            # 真的走一遍 pipeline.request_stop:别人那一轮一个字节都没动
            self.assertFalse(ps._state['stop_requested'])
            self.assertTrue(daily_run._state['stop_requested'])
        finally:
            ps._state.clear(); ps._state.update(state)

    def replaced_mid_poll(self, result=None):
        """跑一趟 `_sync_round`:我们那一轮在第一跳之前就收尾、别人顶了上来。"""
        from kb import pipeline as pl
        snapshots = [
            {'running': True, 'run_id': 'run-1', 'current_stage': 'crawl',
             'stages': {'crawl': {'status': 'running', 'detail': '抓取中'}}},
            # 我们那一轮收尾了,普通同步马上接手:running 看起来从没断过
            {'running': True, 'run_id': 'run-2', 'current_stage': 'index',
             'stages': {'index': {'status': 'error', 'detail': '别人那轮的失败'}}},
        ]
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': '', 'log': [], 'detail': ''})
        with patch.object(pl, 'start_pipeline', Mock(return_value='run-1')), \
             patch.object(pl, 'get_state', Mock(side_effect=snapshots)), \
             patch.object(pl, 'run_result', Mock(return_value=result)), \
             patch.object(pl, 'request_stop', Mock()) as stop, \
             patch.object(daily_run.time, 'sleep', Mock()), \
             patch.object(daily_run, '_wait_idle', Mock(return_value=True)):
            entry = daily_run._sync_round(daily_run.FETCH_STAGES,
                {'channel': 'rss'}, 'RSS', time.monotonic() + 600, 'rss')
        stop.assert_not_called()                      # 别人那一轮不该被我们停
        self.assertEqual('', daily_run._state['pipeline_run'])
        return entry

    def test_a_replaced_run_is_judged_by_its_own_recorded_outcome(self):
        # `_sync_channel` 只认 run id 相同的快照。换人之后既不去停它、也不拿
        # 它的阶段结果当我们的成绩单——改从管道那份按 id 存的收尾快照里取。
        entry = self.replaced_mid_poll(
            {'run_id': 'run-1', 'stopped': False, 'finished_at': '',
             'stages': {'crawl': {'status': 'error', 'detail': '我们那轮的失败'}}})
        self.assertEqual('error', entry['status'])
        self.assertIn('我们那轮的失败', entry['detail'])
        self.assertNotIn('别人那轮的失败', entry['detail'])

    def test_an_unobtainable_outcome_is_not_recorded_as_success(self):
        # 连收尾记录都取不到时**不许算成功**:那一轮的阶段状态我们一眼都没
        # 看到,成败、是否被停全都不知道,写"同步完成"是拿没人证明过的结果去
        # 记账(2026-09-30 第 3 轮审查实测这里硬编码成了 ok)。
        entry = self.replaced_mid_poll(None)
        self.assertEqual('unknown', entry['status'])
        self.assertIn('未知', entry['detail'])
        self.assertNotIn('别人那轮的失败', entry['detail'])

    def test_the_pipeline_keeps_each_finished_run_addressable_by_id(self):
        # 上面那两条靠的是管道真留了这份记录:终态在 running 放下之前按 id 存。
        from kb import pipeline_state as ps
        state, finished = deepcopy(ps._state), dict(ps._finished)
        try:
            ps._state.update({'running': True, 'run_id': 'run-42', 'log': [],
                              'stop_requested': False,
                              'stages': {s: {'status': 'ok', 'detail': '好了'}
                                         for s in ps.STAGES}})
            with patch.object(ps, '_save_last_run', Mock()):
                ps._end_run()
            got = ps.run_result('run-42')
            self.assertEqual('ok', got['stages']['crawl']['status'])
            self.assertFalse(got['stopped'])
            self.assertIsNone(ps.run_result('run-41'))     # 没跑过的查不到
            # 存的是快照,不是同一个对象:之后再起一轮不会把它改掉
            ps._state['stages']['crawl'] = {'status': 'running', 'detail': '新一轮'}
            self.assertEqual('ok', ps.run_result('run-42')['stages']['crawl']['status'])
        finally:
            ps._state.clear(); ps._state.update(state)
            ps._finished.clear(); ps._finished.update(finished)

    def test_a_failed_channel_reaches_the_report_and_the_finish_notice(self):
        # 通道失败原来只留在 _state['channels'] 里:没人读,也没写进日报,完成
        # 提示照样是一句绿色的"日报已生成"(2026-09-30 审查实测)。
        gen = self.run_once(channels=[
            {'channel': 'rss', 'label': 'RSS', 'status': 'error', 'detail': '无法连接来源'},
            {'channel': 'social', 'label': '大V观点', 'status': 'ok', 'detail': '同步完成'},
            {'channel': 'wechat_local', 'label': '公众号', 'status': 'skipped',
             'detail': '超出时间预算,未同步'}])
        issues = gen.call_args.kwargs['sync_issues']
        self.assertEqual(['RSS', '公众号'], [i['label'] for i in issues])  # ok 的不算
        self.assertEqual('done', daily_run._state['phase'])
        self.assertIn('2 项未完成(RSS、公众号)', daily_run._state['detail'])

    def sync_once(self, stages, wanted=None):
        """跑一趟 `_sync_round` 到正常收尾,stages 之外的阶段按"没选中"处理。
        `wanted` = 这一轮点名的阶段(判红只看它们),默认四段齐全。"""
        from kb import pipeline as pl
        from kb.pipeline_state import STAGES
        # 没被选中的阶段一开跑就是 skipped(见 pipeline.start_pipeline)
        full = {s: {'status': 'skipped', 'detail': ''} for s in STAGES}
        full.update(stages)
        daily_run._state.update({**self.before, 'running': True, 'phase': 'sync',
                                 'pipeline_run': '', 'log': [], 'detail': ''})
        with patch.object(pl, 'start_pipeline', Mock(return_value='run-1')), \
             patch.object(pl, 'get_state', Mock(return_value={
                 'running': False, 'run_id': 'run-1', 'current_stage': '', 'stages': full})), \
             patch.object(pl, 'request_stop', Mock()), \
             patch.object(daily_run.time, 'sleep', Mock()), \
             patch.object(daily_run, '_wait_idle', Mock(return_value=True)):
            return daily_run._sync_round(wanted or daily_run.FETCH_STAGES
                + daily_run.DIGEST_STAGES, {'channel': 'social'}, '大V观点',
                time.monotonic() + 600, 'social')

    def test_a_skipped_stage_is_not_a_successful_sync(self):
        # 2026-09-30 实测:51 个大V账号全缺 X 桥接配置,crawl 整个落 skipped
        # (「待配置 51 个」)。这里原来只数 error,于是通道报绿色的「同步完成」,
        # 日报里那一行只剩 extract 的结果 —— "一条都没抓到"连同原因一起看不见。
        entry = self.sync_once({
            'crawl': {'status': 'skipped',
                      'detail': '检查 0/51 个来源，新增 0 条，待配置 51 个（请填写 X 桥接或专用订阅链接）'},
            'export': {'status': 'ok', 'detail': '已保存 0 条到知识库'},
            'index': {'status': 'ok', 'detail': '新增/更新 0'},
            'extract': {'status': 'ok', 'detail': '没有待提取的文档'}})
        self.assertEqual('skipped', entry['status'])
        self.assertIn('待配置 51 个', entry['detail'])
        # 没选中的阶段也是 skipped,不能跟着报出来
        self.assertNotIn('projects', entry['detail'])
        self.assertNotIn('wiki', entry['detail'])

    def test_a_stage_stopped_by_the_budget_is_reported_too(self):
        entry = self.sync_once({
            'crawl': {'status': 'ok', 'detail': '检查 8/8 个来源'},
            'export': {'status': 'stopped', 'detail': '已停止，未继续执行'},
            'index': {'status': 'error', 'detail': '索引失败: 磁盘满'},
            'extract': {'status': 'stopped', 'detail': ''}})
        self.assertEqual('error', entry['status'])          # 有红就是红
        self.assertIn('index 索引失败: 磁盘满', entry['detail'])
        self.assertIn('export 已停止，未继续执行', entry['detail'])
        self.assertIn('extract 本轮未执行', entry['detail'])

    def test_a_fully_green_round_still_reads_as_done(self):
        entry = self.sync_once({s: {'status': 'ok', 'detail': f'{s} 好了'}
                                for s in daily_run.FETCH_STAGES + daily_run.DIGEST_STAGES})
        self.assertEqual(('ok', '同步完成'), (entry['status'], entry['detail']))

    def test_a_clean_round_keeps_its_plain_finish_notice(self):
        gen = self.run_once(channels=[
            {'channel': 'rss', 'label': 'RSS', 'status': 'ok', 'detail': '同步完成'}])
        self.assertEqual([], gen.call_args.kwargs['sync_issues'])
        self.assertNotIn('未完成', daily_run._state['detail'])

    def test_syncing_nothing_is_refused_at_both_layers(self):
        # 勾了"先同步"却一个来源都没选:同步阶段什么都不做、却一路报成功,
        # "同步全部并生成"静静地降级成"直接生成"(2026-09-30 审查实测)。
        with self.assertRaises(ValueError):
            daily_run.start(sync=True, channels=[])
        app = FastAPI(); app.include_router(daily_api.router)
        client = TestClient(app)
        self.assertEqual(400, client.post('/api/daily/run',
                                          json={'sync': True, 'channels': []}).status_code)
        # "直接生成"照旧:它本来就不同步,空范围是正常形态。
        with patch.object(daily_run.threading, 'Thread', Mock()):
            self.assertTrue(daily_run.start(sync=False, channels=[]))
        daily_run._state.update(self.before)

    def test_a_stopped_scheduled_round_is_not_retried(self):
        self.settings.write_text(json.dumps({'enabled': True, 'time': '07:30'}))
        self.run_once(stop_requested=True, sched_day='2026-09-29')
        s = daily_run._raw_settings()
        self.assertEqual(('2026-09-29', '2026-09-30T07:30', 0),
                         (s['last_fired'], s['next_run'], s['fail_count']))


if __name__ == '__main__':
    unittest.main()
