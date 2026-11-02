"""Coverage authority, queue-first body service and independently observed window risks."""
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.feeds import registry as reg, x_browser, x_channels, x_cooldown, x_intake
from kb.feeds import x_intake_capacity as capacity, x_intake_metrics, x_intake_store as store
from kb.feeds import x_page_state, x_post, x_timeline_state
from kb.feeds.x_cdp import XBudgetExceeded
from kb.web import intake_api, intake_diag_api
from tests.test_x_channels import tweet
from tests.test_x_intake_review_runtime import Clock, RuntimeFixture
from tests.x_state_fixture import page_state


class BusyPage:
    def __init__(self, now, *, stalled=False):
        self.posts = [tweet(str(95000 + index), when=(now - timedelta(hours=index)).isoformat())
                      for index in range(60)]
        self.shown, self.stalled = 5, stalled

    def eval(self, script):
        if script == x_browser._COLLECT_JS:
            items = self.posts[:self.shown]
            return json.dumps(dict(articles=len(items), parsed=len(items), items=items))
        if script == x_browser._SCROLL_JS:
            if not self.stalled:
                self.shown = min(len(self.posts), self.shown + 5)
            return json.dumps(dict(bottom=False))
        return '{}'


class RealCoverageAuthorityTests(RuntimeFixture):
    def assert_incomplete_stays_incomplete(self, mode, message):
        original = x_browser._harvest_one
        reasons = []
        clock = Clock()
        def settle(*args):
            if mode == 'seconds':
                clock.now += 9
            return mode != 'stalled'
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] != 2:
                return [], '历史这一轮未完成'
            if mode == 'stalled':
                options['max_scrolls'] = 4
            entries, missing = original(BusyPage(self.now, stalled=mode == 'stalled'), handle, floor, **options)
            self.assertIsInstance(entries, x_browser.TimelineEntries)
            reasons.append(missing)
            return entries, missing
        for _ in range(3):
            with patch.object(x_browser, 'navigate'), patch.object(x_browser, '_settle', side_effect=settle), \
                 patch.object(x_page_state, 'read', return_value=page_state(articles=5)), \
                 patch.object(x_browser, 'time', clock), patch.object(x_intake, 'time', clock), \
                 patch.object(x_intake_metrics, 'time', clock), patch.object(x_cooldown, 'time', clock), \
                 patch.object(x_timeline_state, 'time', clock):
                summary = self.collect_runtime(scan)
            self.assertFalse(summary['failed_ids'], summary)
            source = reg.subscription('social', self.source['id'])
            self.assertEqual(x_channels.watermark(source, 'browser'), '')
            progress = store.progress(source)
            self.assertEqual(progress['discovery'], 'partial')
            self.assertFalse(progress['covered'])
        self.assertTrue(all(message in reason for reason in reasons), reasons)

    def test_scroll_limit_cannot_skip_the_fourteen_unread_recent_posts(self):
        self.assert_incomplete_stays_incomplete('scroll', '次滚动用完')
        self.assertEqual(reg.items('social')['total'], 10)

    def test_account_deadline_cannot_authorize_a_checkpoint(self):
        self.assert_incomplete_stays_incomplete('seconds', '秒用完')

    def test_stalled_non_bottom_page_cannot_authorize_a_checkpoint(self):
        self.assert_incomplete_stays_incomplete('stalled', '不在时间线底部')

    def test_a_forged_secondary_boolean_cannot_override_the_incomplete_fact(self):
        entries = x_browser.TimelineEntries()
        entries.append(self.entry())
        entries.checkpoint_safe = True
        self.collect_runtime(lambda *args, **kwargs: (entries, '没读到下界'))
        self.assertEqual(x_channels.watermark(reg.subscription('social', self.source['id']), 'browser'), '')


class PriorityAllocationTests(unittest.TestCase):
    def test_discovery_keeps_its_window_floor_as_the_population_grows(self):
        for count in range(1, 201):
            with self.subTest(count=count):
                value = capacity.plan(count, 'browser')
                stage = value['stages']['discovery']
                self.assertGreaterEqual(stage['slots'], min(30, (count + 4) // 5))
                self.assertLessEqual(sum(value['phase_seconds'].values()), 445)
                self.assertEqual(value['phase_seconds']['body'], 120)
                if count <= 150:
                    self.assertTrue(stage['fits_daily_window'])
        for count in (76, 80, 100, 101):
            value = capacity.plan(count, 'browser')
            self.assertFalse(value['stages']['history']['fits_daily_window'])
            self.assertTrue(capacity.limited(value))

    def test_known_body_queue_demand_is_not_a_one_item_reservation(self):
        for pending, expected in ((0, 0), (1, 25), (4, 100), (8, 120), (100, 120)):
            with self.subTest(pending=pending):
                value = capacity.plan(51, 'browser', body_pending=pending)
                self.assertEqual(value['phase_seconds']['body'], expected)
                self.assertTrue(value['stages']['discovery']['fits_daily_window'])

    def test_history_can_use_only_what_remains_after_body_service(self):
        value = capacity.plan(80, 'browser')
        self.assertEqual(value['phase_seconds'], dict(discovery=128, history=197, body=120))
        known = capacity.plan(80, 'browser', remaining=317, discovery_done=True)
        self.assertEqual(known['phase_seconds'], dict(discovery=0, history=197, body=120))

    def test_final_body_allocation_uses_actual_queue_and_remaining_time(self):
        value = capacity.plan(51, 'browser', remaining=110, body_pending=2,
                              discovery_done=True, history_done=True)
        self.assertEqual(value['phase_seconds'], dict(discovery=0, history=0, body=50))

    def test_feedgrab_reserves_body_service_and_respects_a_short_remainder(self):
        self.assertEqual(capacity.plan(51, 'feedgrab', body_pending=8)['phase_seconds']['body'], 120)
        value = capacity.plan(51, 'feedgrab', remaining=70, discovery_done=True, history_done=True, body_pending=8)
        self.assertEqual(value['phase_seconds'], dict(discovery=0, history=0, body=70))


class RealBodyThroughputTests(RuntimeFixture):
    def test_eight_expiring_previews_are_delivered_in_two_full_pool_runs(self):
        for index in range(50):
            self.sub(f'BodyPool{index}')
        self.prepare(reg.subscriptions('social'))
        self.stage([self.entry(str(96000 + index), age=90, truncated=True) for index in range(8)])
        allocations = []
        for day in range(2):
            current, clock = self.now + timedelta(days=day), Clock()
            class Snapshot(datetime):
                @classmethod
                def now(cls, tz=None):
                    return current.astimezone(tz) if tz else current.replace(tzinfo=None)
            def scan(page, handle, floor, **options):
                clock.now += options['account_seconds']
                options['on_ready'](0.001)
                return [], '最新发现未完成' if options['max_scrolls'] == 2 else ''
            def expand(entries, *, deadline, **options):
                if deadline - clock.now < 25:
                    raise XBudgetExceeded('正文剩余不足完整切片')
                clock.now += 25
                return [x_post.merge(entry, entry['body'] + '\n匹配详情页的完整正文') for entry in entries], ''
            with patch.object(x_intake, 'datetime', Snapshot), patch.object(x_intake, 'time', clock), \
                 patch.object(x_intake_metrics, 'time', clock), patch.object(x_cooldown, 'time', clock), \
                 patch.object(x_post, 'complete', side_effect=expand):
                summary = self.collect_runtime(scan)
            self.assertEqual(summary['added'], 4)
            self.assertFalse(summary['failed_ids'])
            self.assertLessEqual(clock.now, 445)
            allocations.append(store.latest_metrics('browser')['phase_allocations']['body'])
        self.assertEqual([row['body_pending'] for row in allocations], [8, 4])
        self.assertEqual([row['phase_seconds']['body'] for row in allocations], [120, 100])
        self.assertEqual(reg.items('social')['total'], 8)
        self.assertFalse(store.pending([self.source]))
        self.assertFalse(store.delivery(self.source)[0])
        self.assertEqual(store.progress(self.source)['expired'], 0)


class IndependentObservationTests(RuntimeFixture):
    def test_default_capacity_changes_risk_when_only_actual_history_age_changes(self):
        for index in range(50):
            self.sub(f'Observed{index}')
        self.collect_runtime(lambda *args, **kwargs: ([], '真实历史缺口'))
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_sources SET history_at=? WHERE source_id=?',
                         ((self.now - timedelta(days=6)).timestamp(), self.source['id']))
        app = FastAPI()
        app.include_router(intake_api.router)
        app.include_router(intake_diag_api.router)
        def read(client):
            response = client.get('/api/intake/social')
            self.assertEqual(response.status_code, 200)
            return next(row['x_intake'] for row in response.json()['sources'] if row['id'] == self.source['id'])
        with patch.object(capacity, 'schedule_notice', return_value=''), \
             patch.object(reg, 'configuration_error', return_value=''), TestClient(app) as client:
            old = read(client)
            self.assertTrue(all(stage['fits_daily_window'] for stage in old['window_capacity']['stages'].values()))
            self.assertTrue(old['capacity_limited'])
            self.assertIn('距上次实际尝试已6.0天', old['coverage_warning'])
            self.assertIn(old['coverage_warning'], client.get('/api/intake-care?kind=social').text)
            with reg.db() as conn:
                conn.execute('UPDATE x_intake_sources SET history_at=? WHERE source_id=?',
                             (self.now.timestamp(), self.source['id']))
            x_channels.reset()
            recent = read(client)
            self.assertFalse(recent['capacity_limited'])
            self.assertEqual(old['window_capacity']['phase_seconds'], recent['window_capacity']['phase_seconds'])

    def test_never_attempted_history_is_a_fact_not_a_budget_identity(self):
        value = capacity.observe(capacity.plan(51, 'browser'),
                                 dict(discovery='complete', gap='历史没轮到', scan_at=self.now.timestamp()), self.now)
        self.assertTrue(capacity.limited(value))
        self.assertIn('历史追补尚无实际尝试', capacity.warning(value))

    def test_unattempted_source_cannot_inherit_an_observed_risk(self):
        value = capacity.observe(capacity.plan(51, 'browser'), dict(discovery='unattempted'), self.now)
        self.assertFalse(capacity.limited(value))

    def test_actual_body_backlog_can_warn_with_every_scan_forecast_green(self):
        value = capacity.observe(capacity.plan(51, 'browser'), dict(discovery='partial', pending=25), self.now)
        self.assertTrue(all(stage['fits_daily_window'] for stage in value['stages'].values()))
        self.assertTrue(capacity.limited(value))
        self.assertIn('实际积压25条', capacity.warning(value))
