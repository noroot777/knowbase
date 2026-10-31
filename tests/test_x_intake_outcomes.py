"""Durable delivery owns source conclusions; unknown diagnostic kinds remain readable."""
import json
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.feeds import registry as reg, x_channels, x_feedgrab, x_intake
from kb.feeds import x_intake_blocks as blocks, x_intake_diagnostics, x_intake_store as store, x_page_state
from kb.feeds.x_cdp import CdpError, XBudgetExceeded
from kb.intake_diag import actions, sources as diagnostics
from kb.web import intake_api, intake_diag_api
from tests.test_x_intake_review_runtime import RuntimeFixture


class OutcomeTests(RuntimeFixture):
    def test_all_history_page_faults_keep_discovery_delivery_and_success(self):
        errors = [x_page_state.XBrowserError('历史页面结构无法识别'), CdpError('历史CDP响应超时')]
        for index, error in enumerate(errors):
            with self.subTest(error=type(error).__name__):
                self.old_failure()
                entry = self.entry(str(101 + index), age=1 - index / 10)
                def scan(page, handle, floor, **options):
                    if options['max_scrolls'] != 2:
                        raise error
                    return [entry], ''
                summary = self.collect_runtime(scan)
                source = reg.subscription('social', self.source['id'])
                result = x_channels._harvest['example']
                self.assertEqual((summary['added'], summary['failed_ids']), (1, []))
                self.assertEqual(summary['succeeded_ids'], [str(source['id'])])
                self.assertEqual((source['last_error'], source['failures'], source['retry_at']), ('', 0, 0))
                self.assertEqual(result['phase_errors'], {'history': str(error)})
                self.assertIn('五天历史追补：' + str(error), source['last_note'])
                self.assertEqual(x_channels.watermark(source, 'browser'), entry['x_observed_at'])
                self.assertEqual(store.delivery(source)[0], [])
                self.assertTrue(store.progress(source)['gap'])

    def test_discovery_page_faults_still_fail_and_back_off(self):
        for error in (x_page_state.XBrowserError('发现页面错误'), CdpError('发现CDP超时')):
            with self.subTest(error=type(error).__name__):
                def scan(page, handle, floor, **options):
                    if options['max_scrolls'] == 2:
                        raise error
                    return [], ''
                summary = self.collect_runtime(scan)
                source = reg.subscription('social', self.source['id'])
                self.assertEqual(summary['failed_ids'], [str(source['id'])])
                self.assertEqual(source['last_error'], str(error))
                self.assertGreater(source['retry_at'], 0)
                self.assertIn('discovery', x_channels._harvest['example']['phase_errors'])

    def test_package_reads_the_outbox_even_when_discovery_failed(self):
        self.prepare()
        entry, _ = self.accept_batch()
        intake = x_intake.Intake([self.source], self.options, budget=480)
        intake.outcome(self.source, 'discovery', error='真实发现失败')
        intake.outcome(self.source, 'history', error='独立历史失败')
        with patch.object(store, 'delivery', wraps=store.delivery) as delivery:
            result = intake.package()['example']
        delivery.assert_called_once_with(self.source)
        self.assertEqual([item['url'] for item in result['entries']], [entry['url']])
        self.assertEqual(result['error'], '真实发现失败')
        self.assertEqual(set(result['phase_errors']), {'discovery', 'history'})
        self.assertNotIn('skipped', result)
        self.assertEqual(store.delivery(self.source)[0][0]['url'], entry['url'])

    def test_three_short_discovery_rounds_still_escalate(self):
        def scan(page, handle, floor, **options):
            raise XBudgetExceeded('发现首屏外部切片用完')
        for count in range(1, 5):
            summary = self.collect_runtime(scan)
            source = reg.subscription('social', self.source['id'])
            self.assertEqual(summary['failed_ids'], [str(source['id'])] if count >= 3 else [])
            if count >= 3:
                self.assertIn(f'连续{count}轮首屏未确认', source['last_error'])
                self.assertGreater(source['retry_at'], 0)

    def test_non_discovery_errors_cannot_be_promoted_at_package_boundary(self):
        for phase in ('history', 'body', 'recovery'):
            with self.subTest(phase=phase):
                self.prepare()
                intake = x_intake.Intake([self.source], self.options, budget=480)
                intake.outcome(self.source, phase, error=phase + '错误')
                result = intake.package()['example']
                self.assertEqual(result['error'], '')
                self.assertIn(phase + '错误', result['incomplete'])

    def test_combined_feedgrab_failure_retains_discovery_ownership_and_outbox(self):
        self.settings('feedgrab')
        store.prepare([self.source], 'feedgrab', self.now)
        entry, _ = self.accept_batch()
        intake = x_intake.Intake([self.source], reg.get_settings('social'), budget=480)
        with patch.object(x_feedgrab, 'harvest', return_value={'example': dict(entries=[], error='上游扫描失败')}):
            result = intake.run()['example']
        self.assertEqual(result['error'], '上游扫描失败')
        self.assertEqual(result['phase_errors'], {'combined': '上游扫描失败'})
        self.assertEqual(result['entries'][0]['url'], entry['url'])

    def test_history_skipped_cannot_replace_the_discovery_error(self):
        self.prepare()
        intake = x_intake.Intake([self.source], self.options, budget=480)
        intake.outcome(self.source, 'discovery', error='发现失败')
        intake.outcome(self.source, 'history', skipped='历史限流')
        result = intake.package()['example']
        self.assertEqual(result['error'], '发现失败')
        self.assertIn('历史限流', result['incomplete'])


class UnknownKindTests(RuntimeFixture):
    def app(self):
        app = FastAPI()
        app.include_router(intake_api.router)
        app.include_router(intake_diag_api.router)
        return app

    def write_block(self, phase='discovery', *, legacy=False):
        self.prepare()
        block = blocks.describe(x_page_state.XAccessBlocked('未知阻断的完整原话'), phase)
        block['kind'] = 'captcha_v2'
        value = block if legacy else {phase: block}
        if legacy:
            block.pop('phase')
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_sources SET block=? WHERE source_id=?',
                         (json.dumps(value), self.source['id']))

    def assert_unknown_row(self, client):
        self.assertEqual(client.get('/api/intake/social').status_code, 200)
        response = client.get('/api/intake-care?kind=social')
        self.assertEqual(response.status_code, 200)
        row = next(item for item in response.json()['sources'] if item['problem'] == 'x_intake_captcha_v2')
        self.assertEqual(row['label'], 'x_intake_captcha_v2')
        self.assertEqual(row['advice'], '')
        self.assertEqual(row['evidence'], '未知阻断的完整原话')
        self.assertEqual(row['source_id'], self.source['id'])
        plan = actions.scope_of('social', [], problem=row)
        self.assertEqual(plan['source_id'], self.source['id'])
        self.assertNotIn('extract', plan['stages'])
        return row

    def test_unknown_phase_kinds_do_not_break_real_apis_or_source_scope(self):
        for phase in ('discovery', 'history'):
            with self.subTest(phase=phase):
                self.write_block(phase)
                with patch.object(reg, 'configuration_error', return_value=''), TestClient(self.app()) as client:
                    row = self.assert_unknown_row(client)
                self.assertTrue(row['id'].endswith(':' + phase))
                self.assertEqual(row['step'], 0 if phase == 'discovery' else 1)

    def test_unknown_legacy_kind_keeps_the_original_evidence(self):
        self.write_block(legacy=True)
        x_channels.reset()
        with patch.object(reg, 'configuration_error', return_value=''), TestClient(self.app()) as client:
            self.assert_unknown_row(client)

    def test_known_kinds_keep_the_owner_metadata_and_advice(self):
        self.prepare()
        for kind in ('throttled', 'rate', 'challenge', 'account', 'login', 'session'):
            with self.subTest(kind=kind):
                block = blocks.describe(x_page_state.XAccessBlocked('已知阻断原话'))
                block['kind'] = kind
                store.set_block(self.source, block)
                with patch.object(reg, 'configuration_error', return_value=''):
                    row = next(item for item in diagnostics.problems('social') if item['problem'] == 'x_intake_' + kind)
                for key, value in x_intake_diagnostics.PROBLEMS['x_intake_' + kind].items():
                    self.assertEqual(row[key], value)
                self.assertEqual(row['evidence'], '已知阻断原话')

    def test_unknown_nonretryable_issue_has_no_invented_advice(self):
        self.prepare()
        issue = dict(kind='x_intake_future', evidence='未知不可重试事实', retryable=False)
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_intake_diagnostics, 'issues', return_value=[issue]):
            row = diagnostics.problems('social')[0]
        self.assertFalse(row['actionable'])
        self.assertEqual((row['label'], row['advice'], row['blocked']), ('x_intake_future', '', ''))
