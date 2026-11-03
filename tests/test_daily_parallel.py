"""日报那一轮的整段同步(`kb.daily_sync._sync_sources`):各来源同时跑的那一轮管道怎么盯。

管道本身打桩(`start_pipeline` / `get_state` 由脚本推进,每一跳 `time.sleep` 走一步),
**类型计划是真的**(`pipeline_flow`):超预算时「结束本类型」和开门落在哪几类上、
成绩单怎么读「没等它」,判据都落在那份计划和成绩单上。硬约束在
`docs/decisions/sync-schedule.md`。
"""
from __future__ import annotations

import time
import unittest
from copy import deepcopy
from unittest.mock import Mock, patch

from kb import daily_state, daily_sync
from kb import pipeline as pl
from kb import pipeline_flow as flow
from kb import pipeline_results as live_results
from kb import pipeline_state as ps

BATCH = 'b-1'
RUN = 'run-1'
LABELS = {'a': '甲', 'b': '乙'}
FETCH, DIGEST = ['crawl', 'export'], ['index', 'extract']


def _cell(status='ok', detail='好了'):
    return {'planned': [], 'covered': [], 'whole': True, 'reported': False,
            'status': status, 'detail': detail}


class SyncSourcesTest(unittest.TestCase):
    def setUp(self):
        before = deepcopy(daily_state._state)
        self.addCleanup(lambda: daily_state._state.update(before))
        daily_state._state.update(stop_requested=False, over_budget=False, pipeline_run='',
                                  log=[], phase='sync', detail='')
        state = patch.dict(ps._state, {'running': True, 'run_id': RUN, 'batch': BATCH,
                                       'stop_requested': False, 'log': []})
        state.start()
        self.addCleanup(state.stop)
        flow._plans.clear()
        self.addCleanup(flow._plans.clear)
        live_results.clear()
        self.addCleanup(live_results.clear)
        with patch.object(flow, '_labels', return_value=LABELS):
            flow.open_daily(BATCH, ['a', 'b'], library=False)
        self.snap = {'run_id': RUN, 'running': True, 'current_stage': 'crawl',
                     'stages': {s: {'status': 'running', 'detail': ''} for s in FETCH + DIGEST}}
        self.steps, self.stop = [], Mock()

    def plan(self):
        return flow._plans[BATCH]

    def go(self, deadline, budget=3600.0):
        def tick(_):
            if self.steps:
                self.steps.pop(0)()

        def start(wanted, crawl_filter=None, batch='', refused=None):
            # 管道起跑:两路进门,a 还在抓、b 已经在导出,门在等
            for key, step in (('src:a', 'crawl'), ('src:b', 'export')):
                flow.enter(BATCH, key)
                flow.advance(BATCH, key, step)
            flow.gate(BATCH, 'wait')
            return RUN
        with patch.object(daily_sync, '_wait_idle', Mock(return_value=True)), \
                patch.object(pl, 'start_pipeline', Mock(side_effect=start)), \
                patch.object(pl, 'get_state', Mock(side_effect=lambda: deepcopy(self.snap))), \
                patch.object(pl, 'request_stop', self.stop), \
                patch.object(pl, 'run_result', Mock(return_value=None)), \
                patch.object(daily_sync.time, 'sleep', Mock(side_effect=tick)):
            return daily_sync._sync_sources(['a', 'b'], {'channels': []}, LABELS, FETCH,
                                            DIGEST, '入库处理', deadline, budget, batch=BATCH)

    def ingest(self, late=('src:a',)):
        """管道那边:门开了,入库处理跑起来。"""
        flow.gate(BATCH, 'go' if late else 'open', late)
        self.plan()['times']['ingest'] = {'started_at': '2026-11-03 09:30:00', 'finished_at': None}
        self.snap['current_stage'] = 'index'

    def ingest_done(self):
        self.plan()['done'].append('ingest')
        for s in DIGEST:
            self.snap['stages'][s] = {'status': 'ok', 'detail': f'{s} 好了'}
        for st in FETCH:
            live_results._cells.setdefault(st, {})['b'] = _cell()
        live_results._cells.setdefault('crawl', {})['a'] = _cell()

    def test_over_budget_cuts_the_crawling_source_opens_the_gate_and_hands_in_early(self):
        seen = {}

        def after_first_poll():
            seen['cuts'] = sorted(self.plan()['cuts'])
            seen['gate'] = self.plan()['gate']
            self.ingest()
        self.steps = [after_first_poll, self.ingest_done]
        entries = self.go(deadline=time.monotonic() - 1)
        # 只砍还在抓的那一路;已经在导出的那一路导完(提前结束它等于丢掉抓回来的东西)
        self.assertEqual({'cuts': ['src:a'], 'gate': 'go'}, seen)
        self.assertTrue(daily_state._state['over_budget'])
        got = {e['label']: (e['status'], e['detail']) for e in entries}
        self.assertEqual(('skipped', daily_sync.LATE), got['甲'])
        self.assertEqual(('ok', '同步完成'), got['乙'])
        self.assertEqual('ok', got['入库处理'][0])
        self.assertEqual(['甲', '乙', '入库处理'], [e['label'] for e in entries])
        # 入库处理一走完就交卷:没等的那一路还在后台,不为它停这一轮
        self.stop.assert_not_called()

    def test_within_budget_nothing_is_cut_and_the_gate_is_left_alone(self):
        """反过来钉一边:没超预算时一下都不砍、门也不替用户开。"""
        seen = {}

        def look():
            seen['cuts'], seen['gate'] = dict(self.plan()['cuts']), self.plan()['gate']
            self.ingest(late=())
        def both_done():
            self.ingest_done()
            live_results._cells['export']['a'] = _cell()     # 等到了:a 的导出也跑完了
        self.steps = [look, both_done]
        entries = self.go(deadline=time.monotonic() + 3600)
        self.assertEqual({'cuts': {}, 'gate': 'wait'}, seen)
        self.assertFalse(daily_state._state['over_budget'])
        self.assertEqual(['ok', 'ok', 'ok'], [e['status'] for e in entries])

    def test_a_late_source_that_failed_still_says_so(self):
        """没等的那一路在门开之前已经红了:红照说,不被「没等它」盖掉。"""
        def fail_a():
            self.ingest()
            self.ingest_done()
            live_results._cells['crawl']['a'] = _cell('error', '登录过期')
        self.steps = [fail_a]
        entries = self.go(deadline=time.monotonic() + 3600)
        first = entries[0]
        self.assertEqual('error', first['status'])
        self.assertIn('crawl 登录过期', first['detail'])
        self.assertIn(daily_sync.LATE, first['detail'])

    def test_the_digest_budget_stops_the_round(self):
        """入库处理自己那份保底预算超了:停这一轮(没有它的话日报一直等下去)。"""
        def stuck():
            self.ingest(late=())
            self.snap['current_stage'] = 'extract'
        self.steps = [stuck, lambda: None, lambda: self.snap.update(running=False)]
        self.go(deadline=time.monotonic() - 1, budget=-10)
        self.stop.assert_called_once_with(RUN)


class SourceEntryTest(unittest.TestCase):
    def test_a_cell_still_running_is_not_graded_as_synced(self):
        """那一格还没落定(导出还在补正文、翻译):不知道它最后成没成,不许念「同步完成」。"""
        results = {'crawl': {'a': _cell()}, 'export': {'a': _cell('running', '')}}
        entry = daily_sync._source_entry('a', '甲', FETCH, results, late=False)
        self.assertEqual('unknown', entry['status'])
        self.assertIn('export 还没跑完', entry['detail'])
        # 反过来钉一边:两格都落定了就是同步完成
        results['export']['a'] = _cell()
        entry = daily_sync._source_entry('a', '甲', FETCH, results, late=False)
        self.assertEqual(('ok', '同步完成'), (entry['status'], entry['detail']))
