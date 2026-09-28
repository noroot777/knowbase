"""Cooperative stop preserves completed work and never releases a live writer."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from unittest import TestCase, main
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from kb import pipeline, pipeline_retry, pipeline_state as state
from kb.web import pipeline_api


class PipelineStopTests(TestCase):
    def setUp(self):
        self.saved = deepcopy(state._state)
        self.temp = TemporaryDirectory()
        self.file_patch = patch.object(state, '_STATE_FILE', Path(self.temp.name) / 'state.json')
        self.file_patch.start()
        state._state.update(running=True, stop_requested=False, stopped=False,
                            run_kind='', planned_stages=None,
                            current_stage='', started_at='2026-09-26 09:00:00',
                            finished_at='', crawl_filter={}, log=[],
                            stages={name: {'status': 'pending', 'detail': ''} for name in state.STAGES})

    def tearDown(self):
        self.file_patch.stop()
        self.temp.cleanup()
        state._state.clear()
        state._state.update(self.saved)

    def test_stop_endpoint_is_idempotent_and_reports_idle_conflict(self):
        app = FastAPI()
        app.include_router(pipeline_api.router)
        client = TestClient(app)
        self.assertEqual(200, client.post('/api/pipeline/stop').status_code)
        self.assertEqual(200, client.post('/api/pipeline/stop').status_code)
        self.assertEqual(1, len(state._state['log']))
        state._state['running'] = False
        self.assertEqual(409, client.post('/api/pipeline/stop').status_code)

    def test_light_status_skips_source_and_returns_a_snapshot(self):
        with patch.object(pipeline, 'source_status') as source:
            result = pipeline_api.pipeline_status(source=False)
            source.assert_not_called()
        self.assertTrue(result['stop_supported'])
        state._stage('crawl', 'ok', '新文章 3 篇')
        self.assertEqual('pending', result['stages']['crawl']['status'])

    def test_stop_after_current_stage_skips_later_work_and_persists_result(self):
        def crawl(_):
            state._stage('crawl', 'ok', '新增 3 篇')
            state.request_stop()
        with patch.object(pipeline, '_run_crawl', side_effect=crawl), \
             patch.object(pipeline, '_run_export') as export:
            pipeline._run_pipeline(['crawl', 'export'])
            export.assert_not_called()
        self.assertFalse(state._state['running'])
        last = state.load_last_run()
        self.assertTrue(last['stopped'])
        self.assertEqual('ok', last['stages']['crawl']['status'])
        self.assertEqual('stopped', last['stages']['export']['status'])
        self.assertTrue(last['log'])
        self.assertEqual('2026-09-26 09:00:00', last['started_at'])

    def test_parallel_extraction_finishes_before_becoming_idle(self):
        entered, release = Event(), Event()
        def extract(_, stats):
            entered.set()
            if not release.wait(3):
                raise AssertionError('test worker was never released')
            stats['ok'] = 1
        def crawl(_):
            self.assertTrue(entered.wait(2))
            state._stage('crawl', 'ok', '采集完成')
            state.request_stop()
        with patch.object(pipeline, '_extract_pass', side_effect=extract), \
             patch.object(pipeline, '_run_crawl', side_effect=crawl), \
             patch.object(pipeline, '_run_export') as export:
            runner = Thread(target=pipeline._run_pipeline, args=(['crawl','extract','export'],))
            runner.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(state._state['running'])
                self.assertFalse(pipeline.start_pipeline(['index']))
            finally:
                release.set()
                runner.join(3)
            self.assertFalse(runner.is_alive())
            self.assertFalse(state._state['running'])
            export.assert_not_called()
            self.assertIn('存量已提取 1', state._state['stages']['extract']['detail'])

    def test_unexpected_error_also_joins_parallel_writer(self):
        entered, release = Event(), Event()
        errors = []
        def extract(_, stats):
            entered.set()
            release.wait(3)
        def crawl(_):
            entered.wait(2)
            raise RuntimeError('test failure')
        def run():
            try:
                pipeline._run_pipeline(['crawl','extract'])
            except RuntimeError as exc:
                errors.append(str(exc))
        with patch.object(pipeline, '_extract_pass', side_effect=extract), \
             patch.object(pipeline, '_run_crawl', side_effect=crawl):
            runner = Thread(target=run)
            runner.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertTrue(state._state['running'])
            finally:
                release.set()
                runner.join(3)
        self.assertEqual(['test failure'], errors)
        self.assertFalse(state._state['running'])
        self.assertEqual('error', state._state['stages']['extract']['status'])

    def test_retry_obeys_stop_between_steps(self):
        def index():
            state._stage('index', 'ok', '索引完成')
            state.request_stop()
        with patch.object(pipeline_retry, '_run_index', side_effect=index), \
             patch.object(pipeline_retry, '_run_extract') as extract:
            pipeline_retry._run_retry_failed(['index','extract'])
            extract.assert_not_called()
        self.assertTrue(state.load_last_run()['stopped'])

    def test_new_run_clears_previous_stop_flag(self):
        state._state.update(running=False, stop_requested=True, stopped=True)
        with patch.object(pipeline.threading, 'Thread'):
            self.assertTrue(pipeline.start_pipeline(['index']))
        self.assertFalse(state._state['stop_requested'])
        self.assertFalse(state._state['stopped'])

    def test_previous_failures_allow_new_sync_with_new_scope(self):
        for restored in (False, True):
            with self.subTest(restored=restored):
                state._state.update(running=False, crawl_filter={
                    'channel': 'wechat_oa', 'accounts': ['上次公众号'],
                    'since_date': '2026-09-01', 'until_date': '2026-09-02'})
                state._stage('extract', 'error', '失败 1')
                state._save_last_run()
                if restored:
                    state._state['stages'] = {}
                with patch.object(pipeline.threading, 'Thread') as thread:
                    response = pipeline_api.pipeline_run(pipeline_api.PipelineRunRequest(
                        channel='wechat_oa', accounts=['本次公众号'],
                        since_date='2026-09-27', until_date='2026-09-28'))
                    self.assertEqual({'ok': True}, response)
                    thread.return_value.start.assert_called_once()
                self.assertEqual(['本次公众号'], state._state['crawl_filter']['accounts'])
                self.assertEqual('2026-09-27', state._state['crawl_filter']['since_date'])
                self.assertTrue(state._state['running'])

    def test_failure_during_live_run_still_blocks_concurrent_sync(self):
        state._stage('extract', 'error', '失败 1')
        state._stage('crawl', 'running', '抓取仍在执行')
        with patch.object(pipeline.threading, 'Thread') as thread:
            with self.assertRaises(HTTPException) as error:
                pipeline_api.pipeline_run(pipeline_api.PipelineRunRequest(channel='wechat_oa'))
            self.assertEqual(409, error.exception.status_code)
            self.assertIn('运行中', error.exception.detail)
            thread.assert_not_called()

    def test_sync_plan_survives_stop_and_is_an_independent_status_snapshot(self):
        for selected in (None, ['index', 'extract']):
            with self.subTest(selected=selected):
                state._state.update(running=False, run_kind='retry', planned_stages=['wiki'])
                with patch.object(pipeline.threading, 'Thread'):
                    self.assertTrue(pipeline.start_pipeline(selected))
                planned = list(state.STAGES) if selected is None else selected
                status = pipeline_api.pipeline_status(source=False)
                self.assertEqual('sync', status['run_kind'])
                self.assertEqual(planned, status['planned_stages'])
                status['planned_stages'].clear()
                self.assertEqual(planned, state._state['planned_stages'])
                state._stage(planned[0], 'ok', '完成')
                state.request_stop()
                state._end_run()
                last = pipeline_api.pipeline_status(source=False)['last_run']
                self.assertEqual(planned, last['planned_stages'])
                self.assertEqual('sync', last['run_kind'])
                self.assertTrue(last['stopped'])

    def test_retry_persists_its_own_plan_and_counts_refill_in_export_once(self):
        cases = [(['extract'], ['extract']),
                 (['refill', 'index', 'extract'], ['export', 'index', 'extract']),
                 (['export', 'refill', 'index', 'extract'], ['export', 'index', 'extract'])]
        for plan, expected in cases:
            with self.subTest(plan=plan):
                state._state['running'] = False
                with patch.object(pipeline_retry, 'retry_failed_plan', return_value=plan), \
                     patch.object(pipeline_retry, 'load_last_run', return_value={'crawl_filter': {'channel': 'wechat_oa'}}), \
                     patch.object(pipeline_retry.threading, 'Thread'):
                    started, _ = pipeline_retry.start_retry_failed()
                self.assertTrue(started)
                self.assertEqual(expected, pipeline_api.pipeline_status(source=False)['planned_stages'])
                for name in expected:
                    state._stage(name, 'ok', '重试完成')
                state._end_run()
                last = pipeline_api.pipeline_status(source=False)['last_run']
                self.assertEqual('retry', last['run_kind'])
                self.assertEqual(expected, last['planned_stages'])
                self.assertEqual(len(expected), sum(s['status'] == 'ok' for s in last['stages'].values()))


if __name__ == '__main__':
    main()
