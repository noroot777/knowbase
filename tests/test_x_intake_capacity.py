"""Real delivery bounds and truthful window/capacity facts, without claiming a cursor."""
import json
import os
import shutil
import subprocess
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.collectors import FetchScope
from kb.feeds import registry as reg, x_channels, x_intake_capacity as capacity
from kb.feeds import x_intake_diagnostics, x_intake_store as store
from kb.intake_diag import actions
from kb.web import intake_api, intake_diag_api
from tests.test_x_intake_review_runtime import RuntimeFixture


class CapacityMathTests(unittest.TestCase):
    def test_default_browser_population_does_not_fit_a_daily_five_day_rotation(self):
        value = capacity.plan(51, 'browser')
        self.assertEqual((value['stages']['discovery']['slots'], value['stages']['history']['slots']), (30, 6))
        self.assertEqual(value['stages']['history']['rounds'], 9)
        self.assertFalse(value['stages']['history']['fits_daily_window'])
        text = capacity.warning(value)
        self.assertIn('51个来源', text)
        self.assertIn('每轮按配置6个完整账号切片', text)
        self.assertIn('至少9轮', text)
        self.assertIn('不保证五天内补齐', text)

    def test_exact_capacity_boundary_and_one_extra_source(self):
        self.assertTrue(capacity.plan(30, 'browser')['stages']['history']['fits_daily_window'])
        self.assertFalse(capacity.plan(31, 'browser')['stages']['history']['fits_daily_window'])

    def test_attach_and_browser_share_the_same_capacity_contract(self):
        self.assertEqual(capacity.plan(51, 'attach'), capacity.plan(51, 'browser'))

    def test_feedgrab_uses_its_combined_phase_and_account_quota(self):
        stage = capacity.plan(51, 'feedgrab')['stages']['combined']
        self.assertEqual((stage['slots'], stage['account_seconds'], stage['rounds']), (9, 25, 6))
        self.assertFalse(stage['fits_daily_window'])

    def test_global_deadline_reduces_later_phase_capacity(self):
        value = capacity.plan(51, 'browser', budget=100)
        self.assertEqual(value['stages']['discovery']['seconds'], 75)
        self.assertEqual(value['stages']['history']['slots'], 0)
        self.assertIn('容不下一个完整切片', capacity.warning(value))

    def test_configured_budgets_and_account_quotas_are_not_duplicate_constants(self):
        with patch.dict(os.environ, {'KB_X_HISTORY_SECONDS': '220', 'KB_X_ACCOUNT_SECONDS': '20'}):
            value = capacity.plan(51, 'browser', budget=600)
        self.assertEqual(value['stages']['history']['slots'], 11)
        self.assertTrue(value['stages']['history']['fits_daily_window'])
        self.assertNotIn('历史追补预算容量不足', capacity.warning(value))
        self.assertIn('不是断点续页', capacity.warning(value))

    def test_a_subscription_floor_narrower_than_five_days_reduces_capacity(self):
        value = capacity.plan(30, 'browser', window=timedelta(days=1))
        self.assertFalse(value['stages']['history']['fits_daily_window'])
        self.assertIn('1天窗口', capacity.warning(value))

    def test_zero_sources_or_zero_effective_window_do_not_divide_by_zero(self):
        self.assertNotIn('预算容量不足', capacity.warning(capacity.plan(0, 'browser')))
        self.assertIn('预算容量不足', capacity.warning(capacity.plan(1, 'browser', window=timedelta(0))))

    def test_only_the_real_daily_schedule_can_promise_an_automatic_opportunity(self):
        from kb import daily_run
        for values in [dict(enabled=False, sync=True, channels=['social']),
                       dict(enabled=True, sync=False, channels=['social']),
                       dict(enabled=True, sync=True, channels=['rss'])]:
            with self.subTest(values=values), patch.object(daily_run, 'load_settings', return_value=values):
                self.assertIn('未安排自动同步大V', capacity.schedule_notice())
        with patch.object(daily_run, 'load_settings', return_value=dict(enabled=True, sync=True, channels=['social'])):
            self.assertEqual(capacity.schedule_notice(), '')

    def test_unreadable_schedule_is_unknown_not_disabled(self):
        from kb import daily_run
        from kb.config import ScheduleUnreadable
        with patch.object(daily_run, 'load_settings', side_effect=ScheduleUnreadable('不可读')):
            self.assertIn('无法读取', capacity.schedule_notice())


class CapacityDeliveryTests(RuntimeFixture):
    def api(self):
        app = FastAPI()
        app.include_router(intake_api.router)
        app.include_router(intake_diag_api.router)
        return TestClient(app)

    def test_discovery_reaches_a_three_day_old_watermark_and_delivers_a_two_day_old_post(self):
        marker = self.now - timedelta(days=3)
        with reg.db() as conn:
            reg.merge_config(self.source['id'], x_channels.watermark_patch('browser', marker.isoformat()), conn=conn)
        seen = []
        entry = self.entry('6101', age=48)
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] == 2:
                seen.append(floor)
                return [entry] if floor < store.utc(entry['x_observed_at']) else [], ''
            return [], '历史阶段未读完'
        summary = self.collect_runtime(scan)
        self.assertEqual((summary['added'], summary['failed_ids']), (1, []))
        self.assertEqual(seen, [marker - timedelta(microseconds=1)])
        with reg.read_db() as conn:
            self.assertEqual(conn.execute('SELECT url FROM feed_items').fetchone()[0], entry['url'])

    def test_a_recent_subscription_floor_and_future_watermark_still_bound_discovery(self):
        with reg.db() as conn:
            conn.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                         ((self.now - timedelta(hours=36)).isoformat(), self.source['id']))
        source = reg.subscription('social', self.source['id'])
        seen = []
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] == 2:
                seen.append(floor)
            return [], ''
        self.run_intake(scan, [source])
        self.assertLess(abs((seen[0] - (self.now - timedelta(hours=36))).total_seconds()), 1)
        source['config'] = json.dumps(x_channels.watermark_patch('browser', (self.now + timedelta(days=1)).isoformat()))
        before = self.now
        self.run_intake(scan, [source])
        self.assertGreaterEqual(seen[-1] + timedelta(microseconds=1), before)
        self.assertLess((seen[-1] - before).total_seconds(), 2)

    def test_partial_rescans_never_become_coverage_and_keep_their_warning_after_cache_reset(self):
        def scan(page, handle, floor, **options):
            return [self.entry('6201', age=60)], '尚未读到五天下界'
        self.collect_runtime(scan)
        prior = store.progress(self.source)
        self.assertFalse(prior['covered'])
        self.assertFalse(prior['coverage_end'])
        x_channels.reset()
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(capacity, 'schedule_notice', return_value=''), self.api() as client:
            response = client.get('/api/intake/social')
            care = client.get('/api/intake-care?kind=social')
        self.assertEqual((response.status_code, care.status_code), (200, 200))
        state = response.json()['sources'][0]['x_intake']
        self.assertIn('不是断点续页', state['coverage_warning'])
        issue = next(row for row in care.json()['sources'] if row['problem'] == 'x_intake_history')
        self.assertIn('不保证五天内补齐', issue['advice'])
        self.assertIn('不是断点续页', issue['evidence'])
        self.assertEqual(store.progress(self.source)['covered'], prior['covered'])

    def test_capacity_counts_the_whole_enabled_automatic_population_after_a_targeted_run(self):
        for index in range(50):
            self.sub(f'Capacity{index}')
        self.collect_runtime(lambda *args, **options: ([], '未读完'),
                             scope=FetchScope(force=True, targets=(reg.id_target(self.source['id']),)))
        self.sub('PausedCapacity', enabled=False)
        self.sub('RssCapacity', feed_url='https://example.test/rss')
        with patch.object(capacity, 'schedule_notice', return_value=''), \
             patch.object(reg, 'configuration_error', return_value=''), self.api() as client:
            overview = client.get('/api/intake/social')
            care = client.get('/api/intake-care?kind=social')
        self.assertEqual((overview.status_code, care.status_code), (200, 200))
        source = next(row for row in overview.json()['sources'] if row['id'] == self.source['id'])
        state = source['x_intake']
        self.assertEqual(state['window_capacity']['sources'], 51)
        self.assertIn('至少9轮', state['coverage_warning'])
        self.assertIn('每日一次无法在5天窗口内轮遍', state['coverage_warning'])
        problem = next(row for row in care.json()['sources'] if row['source_id'] == self.source['id'] and row['problem'] == 'x_intake_history')
        scope = actions.scope_of('social', [], problem=problem)
        self.assertEqual(scope['source_id'], self.source['id'])
        self.assertEqual(scope['scope_sig'], problem['sig'])
        self.assertNotIn('extract', scope['stages'])
        excluded = {row['name']: row for row in overview.json()['sources']}
        self.assertNotIn('x_intake', excluded['PausedCapacity'])
        self.assertNotIn('x_intake', excluded['RssCapacity'])

    def test_a_completed_coverage_receipt_does_not_get_relabelled_as_current_debt(self):
        self.collect_runtime(lambda *args, **options: ([self.entry('6301')], ''))
        sources = reg.subscriptions('social')
        with patch.object(capacity, 'schedule_notice', return_value='日报未启用'):
            store.statuses(sources)
        self.assertFalse(sources[0]['x_intake']['gap'])
        self.assertNotIn('coverage_warning', sources[0]['x_intake'])
        self.assertFalse(any(issue['kind'] == 'x_intake_history' for issue in sources[0]['x_intake']['issues']))

    def test_status_and_capacity_calculation_do_not_create_a_progress_receipt(self):
        source = reg.subscriptions('social')[0]
        with patch.object(capacity, 'schedule_notice', return_value='日报未安排自动同步大V'):
            store.statuses([source])
        self.assertEqual(source['x_intake']['discovery'], 'unattempted')
        self.assertIn('未安排自动同步', source['x_intake']['coverage_warning'])
        with reg.read_db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM x_intake_sources').fetchone()[0], 0)


@unittest.skipUnless(shutil.which('node'), 'Node is required for the real status renderer')
class CapacityRendererTests(unittest.TestCase):
    def test_capacity_warning_is_rendered_and_escaped(self):
        path = Path(__file__).resolve().parents[1] / 'kb/static/js/x_intake_status.js'
        script = "const fs=require('fs');global.esc=s=>s.replaceAll('<','&lt;');eval(fs.readFileSync(process.argv[1],'utf8'));"
        script += "const html=xIntakeStatus({x_intake:{discovery:'partial',coverage_warning:'预算容量不足<51，不保证五天内补齐'}});"
        script += "if(!html.includes('预算容量不足&lt;51，不保证五天内补齐')||html.includes('预算容量不足<51'))process.exit(1);"
        result = subprocess.run(['node', '-e', script, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_history_advice_never_promises_an_unverified_resume_cursor(self):
        self.assertEqual(x_intake_diagnostics.PROBLEMS['x_intake_history']['advice'], capacity.RESCAN_NOTICE)
        self.assertIn('从顶部', capacity.RESCAN_NOTICE)
        self.assertIn('超窗后', capacity.RESCAN_NOTICE)
