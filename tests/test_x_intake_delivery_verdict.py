"""A failed source can atomically deliver confirmed bodies without becoming successful."""
import json
import sqlite3
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.collectors import FetchScope
from kb.feeds import registry as reg, source_failure, sync, x_browser, x_channels, x_cooldown, x_feedgrab
from kb.feeds import x_intake, x_intake_metrics, x_intake_store as store, x_page_state, x_timeline_state
from kb.feeds.x_cdp import CdpError, CdpResponseTimeout
from kb.web import intake_api, intake_diag_api
from tests.test_x_intake_review_runtime import Clock, RuntimeFixture
from tests.x_state_fixture import page_state


class DeliveryVerdictTests(RuntimeFixture):
    def count_items(self):
        with reg.read_db() as conn:
            return conn.execute('SELECT count(*) FROM feed_items').fetchone()[0]

    def failed_result(self):
        self.prepare()
        entry, _ = self.accept_batch()
        intake = x_intake.Intake([self.source], self.options, budget=480)
        intake.outcome(self.source, 'discovery', error='发现失败但成品可投递')
        x_channels._harvest.update(example=intake.package()['example'])
        return entry, x_channels.take(self.source, self.options)

    def test_discovery_browser_and_cdp_faults_deliver_each_history_batch_and_still_fail(self):
        errors = [x_page_state.XBrowserError('发现页面结构异常'), CdpError('发现CDP故障'),
                  CdpResponseTimeout('发现CDP响应超时')]
        for count, error in enumerate(errors, start=1):
            with self.subTest(error=type(error).__name__):
                entry = self.entry(str(200 + count))
                def scan(page, handle, floor, **options):
                    if options['max_scrolls'] == 2:
                        raise error
                    return [entry], ''
                summary = self.collect_runtime(scan)
                source = reg.subscription('social', self.source['id'])
                self.assertEqual((summary['added'], summary['failed_ids'], summary['succeeded_ids']),
                                 (1, [str(source['id'])], []))
                self.assertEqual(summary['deferred'], 0)
                self.assertEqual(len(summary['errors']), 1)
                self.assertEqual(source['last_error'], str(error))
                self.assertEqual(source['failures'], count)
                self.assertGreater(source['retry_at'], 0)
                self.assertEqual(source['last_success'], '')
                self.assertEqual(x_channels.watermark(source, 'browser'), '')
                self.assertEqual((self.count_items(), store.delivery(source)[0]), (count, []))

    def test_four_short_discovery_rounds_deliver_even_after_the_third_round_escalation(self):
        original, seconds = x_browser._harvest_one, x_intake.seconds
        clock, round_number = Clock(), [0]
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] == 2:
                return original(page, handle, floor, **options)
            options['on_ready'](0.001)
            return [self.entry(str(300 + round_number[0]))], ''
        def limit(name, default):
            return {'KB_X_DISCOVERY_SECONDS': 8.5, 'KB_X_ACCOUNT_SECONDS': 20}.get(name, seconds(name, default))
        with patch.object(x_intake, 'time', clock), patch.object(x_browser, 'time', clock), \
             patch.object(x_timeline_state, 'time', clock), patch.object(x_intake_metrics, 'time', clock), \
             patch.object(x_cooldown, 'time', clock), patch.object(x_intake, 'seconds', side_effect=limit), \
             patch.object(x_browser, 'navigate'), patch.object(x_page_state, 'read', return_value=page_state()):
            for count in range(1, 5):
                round_number[0] = count
                summary = self.collect_runtime(scan)
                source = reg.subscription('social', self.source['id'])
                self.assertEqual(summary['added'], 1)
                self.assertEqual(self.count_items(), count)
                self.assertEqual(store.delivery(source)[0], [])
                if count >= 3:
                    self.assertEqual(summary['failed_ids'], [str(source['id'])])
                    self.assertEqual(summary['succeeded_ids'], [])
                    self.assertEqual(source['failures'], count - 2)
                    self.assertIn(f'连续{count}轮首屏未确认', source['last_error'])
                    self.assertGreater(source['retry_at'], 0)
        self.assertEqual(json.loads(store.progress(source)['screen_waits'])['discovery']['count'], 4)

    def test_combined_feedgrab_error_delivers_a_prior_confirmed_outbox(self):
        self.settings('feedgrab')
        store.prepare([self.source], 'feedgrab', self.now)
        self.accept_batch()
        with patch.object(x_feedgrab, 'harvest', return_value={'example': dict(entries=[], error='上游扫描失败')}):
            summary = self.collect_runtime(lambda *args, **kwargs: None, channel='feedgrab')
        self.assertEqual((summary['added'], summary['failed_ids'], summary['succeeded_ids']),
                         (1, [str(self.source['id'])], []))
        self.assertEqual((self.count_items(), store.delivery(self.source)[0]), (1, []))
        self.assertEqual(reg.subscription('social', self.source['id'])['last_error'], '上游扫描失败')

    def test_failure_accounting_fault_rolls_back_items_and_outbox_acknowledgement(self):
        self.old_failure()
        _, taken = self.failed_result()
        with patch.object(source_failure, 'write', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                sync._save_result(self.source, taken, FetchScope(force=True))
        self.assertEqual((self.count_items(), len(store.delivery(self.source)[0])), (0, 1))
        source = reg.subscription('social', self.source['id'])
        self.assertEqual((source['last_error'], source['failures'], source['retry_at']), ('旧连接失败', 3, 42))

    def test_item_write_failure_does_not_consume_outbox_or_update_source_failure(self):
        _, taken = self.failed_result()
        with patch.object(reg, 'put_items', side_effect=RuntimeError('保存内容失败')):
            with self.assertRaises(RuntimeError):
                sync._save_result(self.source, taken, FetchScope(force=True))
        self.assertEqual((self.count_items(), len(store.delivery(self.source)[0])), (0, 1))
        self.assertEqual(reg.subscription('social', self.source['id'])['failures'], 0)

    def test_repointed_source_gets_neither_old_content_nor_old_failure(self):
        _, taken = self.failed_result()
        current = reg.save_subscription('social', {'url': 'https://x.com/newauthor'}, self.source['id'])
        self.assertIsNone(sync._save_result(self.source, taken, FetchScope(force=True)))
        self.assertEqual(self.count_items(), 0)
        self.assertEqual(reg.subscription('social', current['id'])['last_error'], '')

    def test_failed_delivery_does_not_advance_watermark_or_success_even_with_a_patch(self):
        self.old_failure()
        _, taken = self.failed_result()
        taken[1]['config_patch'] = x_channels.watermark_patch('browser', self.now.isoformat())
        self.assertEqual(sync._save_result(self.source, taken, FetchScope(force=True)), 1)
        source = reg.subscription('social', self.source['id'])
        self.assertEqual(source['last_success'], '2026-10-01T00:00:00+00:00')
        self.assertEqual(x_channels.watermark(source, 'browser'), '')
        self.assertEqual((source['last_error'], source['failures']), ('发现失败但成品可投递', 4))

    def test_empty_automatic_failure_and_legacy_error_still_raise_without_saving(self):
        for automatic, entries in [(True, []), (False, [self.entry()])]:
            with self.subTest(automatic=automatic):
                x_channels._harvest.update(example=dict(entries=entries, error='真实失败', x_intake=automatic))
                with self.assertRaises(x_channels.XCollectionError):
                    x_channels.take(self.source, self.options)

    def test_same_confirmed_entry_is_acknowledged_once_across_failed_rounds(self):
        self.prepare()
        entry, _ = self.accept_batch()
        x_channels.reset()
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] == 2:
                raise x_page_state.XBrowserError('发现读取失败')
            return [entry], ''
        first, second = self.collect_runtime(scan), self.collect_runtime(scan)
        self.assertEqual((first['added'], second['added'], self.count_items()), (1, 0, 1))
        self.assertEqual(store.delivery(self.source)[0], [])
        self.assertEqual(reg.subscription('social', self.source['id'])['failures'], 2)

    def test_failure_summary_and_notice_are_each_reported_once_after_delivery(self):
        original = x_intake.Intake.package
        def package(intake):
            result = original(intake)
            result['example'].update(note='一条不可访问内容', unavailable=[dict(url='https://x.com/example/status/99')],
                                     unavailable_count=1)
            return result
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] == 2:
                raise x_page_state.XBrowserError('发现阶段失败')
            return [self.entry()], ''
        with patch.object(x_intake.Intake, 'package', package):
            summary = self.collect_runtime(scan)
        self.assertEqual(len(summary['errors']), 1)
        self.assertEqual(len(summary['notices']), 1)
        self.assertEqual(summary['notices'][0]['count'], 1)
        self.assertEqual((summary['failed_ids'], summary['succeeded_ids'], summary['deferred']),
                         ([str(self.source['id'])], [], 0))
        self.assertEqual((summary['added'], self.count_items()), (1, 1))

    def test_api_items_show_delivered_content_while_source_keeps_its_failure(self):
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] == 2:
                raise x_page_state.XBrowserError('发现阶段仍失败')
            return [self.entry()], ''
        self.collect_runtime(scan)
        app = FastAPI()
        app.include_router(intake_api.router)
        app.include_router(intake_diag_api.router)
        x_channels.reset()
        with patch.object(reg, 'configuration_error', return_value=''), TestClient(app) as client:
            overview = client.get('/api/intake/social')
            items = client.get('/api/intake/social/items')
            care = client.get('/api/intake-care?kind=social')
        self.assertEqual((overview.status_code, items.status_code, care.status_code), (200, 200, 200))
        self.assertEqual(overview.json()['sources'][0]['last_error'], '发现阶段仍失败')
        self.assertIn(self.entry()['url'], json.dumps(items.json()))
        self.assertEqual(self.count_items(), 1)
        self.assertEqual(store.delivery(self.source)[0], [])

    def test_existing_failure_only_policy_preserves_cap_notice_and_retry_contract(self):
        summary = dict(errors=[], failed_ids=[])
        error = x_channels.XCollectionError(dict(error='故障' * 1000, note='不可用提示', unavailable=[dict(url='synthetic')]))
        self.assertTrue(sync._save_failure(self.source, error, summary))
        source = reg.subscription('social', self.source['id'])
        self.assertEqual(len(source['last_error']), sync.FAILURE_CAP)
        self.assertEqual((source['failures'], summary['failed_ids']), (1, [str(source['id'])]))
        self.assertEqual(summary['notices'][0]['reason'], '不可用提示')
        self.assertGreater(source['retry_at'], 0)
