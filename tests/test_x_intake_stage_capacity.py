"""Real scanner/checkpoint and full-pool rotation consequences of stage-owned budgets."""
import json
import os
import shutil
import subprocess
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb.collectors import FetchScope
from kb.feeds import registry as reg, x_browser, x_channels, x_cooldown, x_intake
from kb.feeds import x_intake_capacity as capacity, x_intake_diagnostics as diagnostics
from kb.feeds import x_intake_metrics, x_intake_store as store, x_page_state, x_post
from kb.web import intake_api, intake_diag_api
from tests.test_x_channels import tweet
from tests.test_x_intake_review_runtime import Clock, RuntimeFixture
from tests.x_state_fixture import page_state


class PostsPage:
    def __init__(self, posts):
        self.posts, self.shown = posts, 5

    def eval(self, script):
        if script == x_browser._COLLECT_JS:
            items = self.posts[:self.shown]
            return json.dumps(dict(articles=len(items), parsed=len(items), items=items))
        if script == x_browser._SCROLL_JS:
            self.shown = min(len(self.posts), self.shown + 5)
            return json.dumps(dict(bottom=self.shown == len(self.posts)))
        return '{}'


class DiscoveryCheckpointTests(RuntimeFixture):
    def run_real_scanner(self, *, marker='', rejected=False):
        posts = [tweet(str(91000 + index), when=(self.now - timedelta(hours=6 * index)).isoformat())
                 for index in range(60)]
        if rejected:
            posts.insert(0, tweet('91999', handle='Other', context='来源不明'))
        if marker:
            with reg.db() as conn:
                reg.merge_config(self.source['id'], x_channels.watermark_patch('browser', marker), conn=conn)
        original = x_browser._harvest_one
        original_entry = x_browser._entry
        def build_entry(item):
            if rejected and item['id'] == '91999':
                raise x_post.PostUnavailable('作者原文证据不足')
            return original_entry(item)
        floors, checkpoints, states = [], [], []
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] != 2:
                return [], '历史阶段尚未完成'
            floors.append(floor)
            return original(PostsPage(posts), handle, floor, **options)
        for _ in range(3):
            with patch.object(x_browser, 'navigate'), patch.object(x_browser, '_entry', side_effect=build_entry), \
                 patch.object(x_page_state, 'read', return_value=page_state(articles=5)):
                summary = self.collect_runtime(scan)
            self.assertFalse(summary['failed_ids'])
            source = reg.subscription('social', self.source['id'])
            checkpoints.append(x_channels.watermark(source, 'browser'))
            states.append(store.progress(source)['discovery'])
        return floors, checkpoints, states

    def test_first_sync_and_three_real_scans_reach_their_own_recent_boundary(self):
        floors, checkpoints, states = self.run_real_scanner()
        self.assertLess(abs((floors[0] - (self.now - timedelta(days=1))).total_seconds()), 2)
        self.assertEqual(states, ['complete'] * 3)
        self.assertTrue(all(checkpoints))
        self.assertEqual(store.utc(checkpoints[-1]), self.now)
        self.assertTrue(store.progress(self.source)['gap'])
        self.assertGreater(floors[-1], floors[0])

    def test_partial_confirmed_discovery_cannot_skip_the_unread_interval(self):
        marker = (self.now - timedelta(days=3)).isoformat()
        floors, checkpoints, states = self.run_real_scanner(marker=marker)
        self.assertLess(abs((floors[0] - store.utc(marker)).total_seconds()), 2)
        self.assertEqual(states, ['partial'] * 3)
        self.assertEqual(checkpoints, [marker] * 3)
        progress = store.progress(self.source)
        self.assertFalse(progress['covered'])
        self.assertFalse(progress['coverage_end'])
        self.assertTrue(progress['gap'])
        with reg.read_db() as conn:
            self.assertGreaterEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 10)

    def test_unknown_timeline_context_cannot_authorize_a_partial_checkpoint(self):
        marker = (self.now - timedelta(days=3)).isoformat()
        _, checkpoints, states = self.run_real_scanner(marker=marker, rejected=True)
        self.assertEqual(checkpoints, [marker] * 3)
        self.assertEqual(states, ['partial'] * 3)

    def test_an_unconfirmed_batch_cannot_advance_a_checkpoint(self):
        entry = self.entry()
        with patch.object(store, 'confirm', return_value=False):
            summary = self.collect_runtime(lambda *args, **options: ([entry], ''))
        self.assertFalse(summary['succeeded_ids'])
        self.assertFalse(x_channels.watermark(reg.subscription('social', self.source['id']), 'browser'))
        self.assertTrue(store.progress(self.source)['unconfirmed'])


class StructuredCapacityTests(RuntimeFixture):
    def test_disclaimer_and_schedule_text_alone_cannot_create_a_problem(self):
        for discovery in ('unattempted', 'partial', 'complete'):
            with self.subTest(discovery=discovery):
                state = dict(discovery=discovery, coverage_warning=capacity.RESCAN_NOTICE, capacity_limited=False)
                self.assertFalse(diagnostics.issues(state))

    def test_capacity_problems_require_an_actual_attempt_and_a_structured_fact(self):
        for discovery in ('unattempted', 'partial', 'complete'):
            with self.subTest(discovery=discovery):
                state = dict(discovery=discovery, capacity_limited=True, coverage_warning='真实容量不足')
                issues = diagnostics.issues(state)
                self.assertEqual(len(issues), int(discovery != 'unattempted'))
                if issues:
                    self.assertEqual(issues[0]['evidence'], '真实容量不足')

    def test_real_gap_and_history_attempt_remain_visible_without_capacity_failure(self):
        state = dict(discovery='unattempted', history_at=1, gap='历史确实未读完', capacity_limited=False)
        self.assertEqual(diagnostics.issues(state)[0]['evidence'], '历史确实未读完')

    def test_full_pool_api_does_not_manufacture_untouched_source_failures(self):
        for index in range(50):
            self.sub(f'Pool{index}')
        self.collect_runtime(lambda *args, **options: ([], '真实缺口'),
                             scope=FetchScope(force=True, targets=(reg.id_target(self.source['id']),)))
        app = FastAPI()
        app.include_router(intake_api.router)
        app.include_router(intake_diag_api.router)
        with patch.dict(os.environ, {'KB_X_HISTORY_SECONDS': '120'}), \
             patch.object(capacity, 'schedule_notice', return_value='日报未安排自动同步'), \
             patch.object(reg, 'configuration_error', return_value=''), TestClient(app) as client:
            overview = client.get('/api/intake/social')
            care = client.get('/api/intake-care?kind=social')
        self.assertEqual((overview.status_code, care.status_code), (200, 200))
        histories = [row for row in care.json()['sources'] if row['problem'] == 'x_intake_history']
        self.assertEqual([row['source_id'] for row in histories], [self.source['id']])
        untouched = [row for row in overview.json()['sources'] if row['id'] != self.source['id']]
        self.assertTrue(all(row['x_intake']['capacity_limited'] and not row['x_intake']['issues'] for row in untouched))
        with reg.read_db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM x_intake_sources').fetchone()[0], 1)


class RotationBudgetTests(RuntimeFixture):
    def test_full_51_source_history_rotation_delivers_two_day_posts_within_five_runs(self):
        for index in range(50):
            self.sub(f'Rotate{index}')
        history_seen = set()
        for day in range(5):
            current = self.now + timedelta(days=day)
            class Snapshot(datetime):
                @classmethod
                def now(cls, tz=None):
                    return current.astimezone(tz) if tz else current.replace(tzinfo=None)
            clock, calls = Clock(), dict(discovery=0, history=0)
            def scan(page, handle, floor, **options):
                phase = 'discovery' if options['max_scrolls'] == 2 else 'history'
                calls[phase] += 1
                self.assertGreaterEqual(options['deadline'] - clock.now + 0.00001, options['account_seconds'])
                clock.now += options['account_seconds']
                options['on_ready'](0.001)
                if phase == 'discovery':
                    return [], '发现阶段未完成'
                history_seen.add(handle)
                index = 50 if handle == 'example' else int(handle.removeprefix('rotate'))
                stamp = (current - timedelta(days=2)).isoformat()
                return [dict(x_browser._entry(tweet(str(92000 + index), handle=handle, when=stamp)), x_observed_at=stamp)], ''
            with patch.object(x_intake, 'datetime', Snapshot), patch.object(x_intake, 'time', clock), \
                 patch.object(x_intake_metrics, 'time', clock), patch.object(x_cooldown, 'time', clock):
                summary = self.collect_runtime(scan)
            self.assertEqual(calls, dict(discovery=13, history=11))
            self.assertLessEqual(clock.now, 445)
            self.assertFalse(summary['failed_ids'])
        self.assertEqual(len(history_seen), 51)
        with reg.read_db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 51)

    def test_startup_time_shrinks_discovery_before_it_takes_history_and_body_reserves(self):
        for index in range(50):
            self.sub(f'Startup{index}')
        intake = x_intake.Intake(reg.subscriptions('social'), self.options, budget=480)
        with patch.object(x_intake.time, 'monotonic', return_value=intake.metrics.started + 15):
            limits = intake.budgets()
        self.assertEqual(limits, dict(discovery=90, history=220, body=120))

    def test_configured_reader_is_used_for_both_estimate_and_actual_limits(self):
        def read(name, default):
            return {'KB_X_HISTORY_SECONDS': 80, 'KB_X_ACCOUNT_SECONDS': 10}.get(name, default)
        value = capacity.plan(51, 'browser', reader=read)
        self.assertEqual(value['phase_seconds']['history'], 80)
        self.assertEqual(value['stages']['history']['slots'], 8)
        self.assertTrue(capacity.limited(value))

    def test_explicit_tight_budget_does_not_get_silently_overridden(self):
        with patch.dict(os.environ, {'KB_X_HISTORY_SECONDS': '120', 'KB_X_DISCOVERY_SECONDS': '240'}):
            value = capacity.plan(51, 'browser')
        self.assertEqual(value['stages']['history']['slots'], 6)
        self.assertTrue(capacity.limited(value))
        self.assertIn('至少9轮', capacity.warning(value))
        self.assertNotIn(capacity.RESCAN_NOTICE, capacity.warning(value))


@unittest.skipUnless(shutil.which('node'), 'Node is required for the real status renderer')
class StructuredRendererTests(unittest.TestCase):
    def test_only_a_boolean_capacity_fact_can_display_a_capacity_warning(self):
        path = Path(__file__).resolve().parents[1] / 'kb/static/js/x_intake_status.js'
        script = "const fs=require('fs');global.esc=value=>value;eval(fs.readFileSync(process.argv[1],'utf8'));"
        script += "for(const value of [undefined,false,'true']){if(xIntakeStatus({x_intake:{discovery:'unattempted',capacity_limited:value,coverage_warning:'FALSE_POSITIVE'}}).includes('FALSE_POSITIVE'))process.exit(1)};"
        script += "if(!xIntakeStatus({x_intake:{discovery:'partial',capacity_limited:true,coverage_warning:'REAL_RISK'}}).includes('REAL_RISK'))process.exit(2);"
        result = subprocess.run(['node', '-e', script, str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
