"""Phase-owned blockers, replayable observations and qualified first-screen timeouts."""
import fcntl
import json
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import maintenance
from kb.collectors import FetchScope
from kb.feeds import registry as reg, x_account, x_browser, x_channels, x_cooldown, x_intake
from kb.feeds import x_intake_blocks as blocks, x_intake_metrics, x_intake_store as store
from kb.feeds import x_page_state, x_post, x_timeline_state
from kb.feeds.x_cdp import XBudgetExceeded
from kb.intake_diag import sources as diagnostics
from kb.web import intake_api, intake_diag_api
from tests.test_x_intake import Session
from tests.test_x_intake_review_runtime import Clock, RuntimeFixture
from tests.x_state_fixture import page_state


class PhaseDeliveryTests(RuntimeFixture):
    def observe(self, phase, error=None):
        event = blocks.describe(error or x_page_state.XSessionThrottled(phase + '启动429'), phase)
        store.set_block(self.source, event)
        return event

    def receipt(self, phase, *, confirm=True):
        batch_id = uuid4().hex
        entries = [self.entry()]
        proof = dict(phase=phase, incomplete='', observed=store.observed(entries),
                     boundary=store.floor_for(self.source, self.now).isoformat(),
                     end=self.now.isoformat(), batch_id=batch_id)
        store.stage(self.source, entries, **proof)
        if confirm:
            store.confirm(self.source, entries, **proof, coverage_complete=True)
        return entries, proof

    @contextmanager
    def blocked_observation_write(self):
        real = store.set_block
        def locked(source, event):
            lockfile = maintenance.DATA_DIR / maintenance.LOCK_NAME
            lockfile.parent.mkdir(parents=True, exist_ok=True)
            with lockfile.open('a+') as held:
                fcntl.flock(held, fcntl.LOCK_EX)
                try:
                    return real(source, event)
                finally:
                    fcntl.flock(held, fcntl.LOCK_UN)
        with patch.object(store, 'set_block', side_effect=locked):
            yield

    def test_body_throttle_does_not_veto_discovery_success_or_its_watermark(self):
        self.old_failure()
        for error in (x_page_state.XSessionThrottled('正文启动429'),
                      x_page_state.XRateLimited('正文内容429'),
                      x_page_state.XAccessBlocked('正文验证墙'),
                      x_post.PostSessionUnavailable('正文会话断开')):
            with self.subTest(error=type(error).__name__):
                with reg.db() as conn:
                    conn.execute('UPDATE x_intake_candidates SET retry_at=0')
                with patch.object(x_post, 'fetch', side_effect=error):
                    summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry(truncated=True)], ''))
                source = reg.subscription('social', self.source['id'])
                self.assertEqual(summary['succeeded_ids'], [str(source['id'])])
                self.assertEqual((source['last_error'], source['failures'], source['retry_at']), ('', 0, 0))
                self.assertTrue(x_channels.watermark(source, 'browser'))
                self.assertNotIn('discovery', store.progress(source)['blocks'])
                self.assertIn('body', store.progress(source)['blocks'])
                problems = diagnostics.problems('social')
                self.assertFalse(any(item['problem'] in ('connect', 'x_intake_throttled', 'x_intake_rate',
                                                        'x_intake_challenge', 'x_intake_session') for item in problems))
                self.assertIn(str(error), next(item['evidence'] for item in problems if item['problem'] == 'x_intake_body'))

    def test_history_block_does_not_veto_confirmed_discovery(self):
        self.old_failure()
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] != 2:
                raise x_page_state.XRateLimited('仅历史访问429')
            return [self.entry()], ''
        summary = self.collect_runtime(scan)
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(set(store.progress(self.source)['blocks']), {'history'})
        self.assertTrue(x_channels.watermark(reg.subscription('social', self.source['id']), 'browser'))

    def test_all_history_blocker_types_respect_discovery_ownership(self):
        errors = [x_page_state.XSessionThrottled('历史启动429'), x_page_state.XRateLimited('历史访问429'),
                  x_page_state.XAccessBlocked('历史验证墙'), x_account.XAccountUnavailable('account', 'suspended'),
                  x_account.XAccountUnavailable('login', 'login'), x_post.PostSessionUnavailable('历史会话断开')]
        for error in errors:
            with self.subTest(error=type(error).__name__):
                self.old_failure()
                def scan(page, handle, floor, **options):
                    if options['max_scrolls'] != 2:
                        raise error
                    return [self.entry()], ''
                summary = self.collect_runtime(scan)
                source = reg.subscription('social', self.source['id'])
                self.assertEqual(summary['succeeded_ids'], [str(source['id'])])
                self.assertEqual((source['last_error'], source['failures'], source['retry_at']), ('', 0, 0))
                self.assertNotIn('discovery', store.progress(source)['blocks'])
                self.assertEqual(store.progress(source)['blocks']['history']['reason'], str(error))
                self.assertTrue(x_channels.watermark(source, 'browser'))

    def test_each_phase_has_its_own_observation_and_receipt(self):
        self.prepare()
        events = {phase: self.observe(phase) for phase in blocks.PHASES}
        self.receipt('discovery')
        remaining = store.progress(self.source)['blocks']
        self.assertEqual(remaining, {phase: events[phase] for phase in ('history', 'body')})
        self.receipt('history')
        self.assertEqual(store.progress(self.source)['blocks'], {'body': events['body']})

    def test_combined_feedgrab_receipt_clears_only_both_timeline_phases(self):
        self.prepare()
        for phase in blocks.PHASES:
            self.observe(phase)
        self.receipt('combined')
        self.assertEqual(set(store.progress(self.source)['blocks']), {'body'})

    def test_old_receipt_cannot_clear_a_later_observation_in_its_own_phase(self):
        self.prepare()
        self.observe('discovery')
        entries, proof = self.receipt('discovery', confirm=False)
        later = self.observe('discovery', x_page_state.XAccessBlocked('迟到发现验证墙'))
        store.confirm(self.source, entries, **proof, coverage_complete=True)
        self.assertEqual(store.progress(self.source)['blocks']['discovery'], later)

    def test_discovery_recovery_does_not_clear_body_until_body_actually_finishes(self):
        with patch.object(x_post, 'fetch', side_effect=x_page_state.XSessionThrottled('旧正文429')):
            self.collect_runtime(lambda *args, **kwargs: ([self.entry(truncated=True)], ''))
        self.collect_runtime(lambda *args, **kwargs: ([self.entry('101')], ''))
        self.assertIn('body', store.progress(self.source)['blocks'])
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_candidates SET retry_at=0')
        with patch.object(x_post, 'fetch', return_value='补全后可确认的真实正文内容'):
            summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry('101')], ''))
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertNotIn('body', store.progress(self.source)['blocks'])
        self.assertEqual(store.delivery(self.source)[1], 0)

    def test_legacy_single_block_is_conservatively_a_discovery_observation(self):
        self.prepare()
        old = blocks.describe(x_page_state.XAccessBlocked('旧版未知阶段验证墙'))
        old.pop('phase')
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_sources SET block=?', (json.dumps(old),))
        self.assertEqual(store.progress(self.source)['blocks'], {'discovery': old})
        self.receipt('history')
        self.assertIn('discovery', store.progress(self.source)['blocks'])

    def test_real_maintenance_lock_cannot_drop_a_challenge_from_results_or_api(self):
        error = x_page_state.XAccessBlocked('维护锁期间实际观察到验证墙')
        with self.blocked_observation_write(), self.assertLogs('kb.feeds.x_intake', 'WARNING'):
            summary = self.collect_runtime(lambda *args, **kwargs: (_ for _ in ()).throw(error))
        self.assertTrue(summary['x_observations'])
        with reg.read_db() as conn:
            self.assertFalse(blocks.active(conn.execute('SELECT block FROM x_intake_sources').fetchone()['block']))
        x_channels.reset()
        app = FastAPI()
        app.include_router(intake_api.router)
        app.include_router(intake_diag_api.router)
        with patch.object(reg, 'configuration_error', return_value=''), TestClient(app) as client:
            response = client.get('/api/intake/social')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['sources'][0]['x_intake']['blocks']['discovery']['reason'], str(error))
            problems = client.get('/api/intake-care?kind=social').json()['sources']
        self.assertTrue(any(item['problem'] == 'x_intake_challenge' and item['evidence'] == str(error) for item in problems))

    def test_even_error_results_deliver_the_only_observation_copy(self):
        with self.blocked_observation_write(), self.assertLogs('kb.feeds.x_intake', 'WARNING'):
            summary = self.collect_runtime(lambda *args, **kwargs: (_ for _ in ()).throw(x_post.PostSessionUnavailable('未落盘会话故障')))
        self.assertTrue(summary['x_observations'])
        self.assertIn('session', {item['kind'] for item in store.progress(self.source)['blocks'].values()})

    def test_body_observation_under_lock_is_visible_without_changing_business_outcome(self):
        self.old_failure()
        with self.blocked_observation_write(), self.assertLogs('kb.feeds.x_intake', 'WARNING'), \
             patch.object(x_post, 'fetch', side_effect=x_page_state.XSessionThrottled('未落盘正文429')):
            summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry(truncated=True)], ''))
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertIn('未落盘正文429', next(item['evidence'] for item in diagnostics.problems('social')
                                      if item['problem'] == 'x_intake_body'))

    def test_fallback_survives_an_unrelated_run_but_not_a_real_recovery(self):
        with self.blocked_observation_write(), self.assertLogs('kb.feeds.x_intake', 'WARNING'):
            self.collect_runtime(lambda *args, **kwargs: (_ for _ in ()).throw(x_page_state.XAccessBlocked('需要人工验证')))
        other = self.sub('Other')
        self.collect_runtime(lambda *args, **kwargs: ([self.entry(handle='Other')], ''),
                             scope=FetchScope(force=True, targets=(reg.id_target(other['id']),)))
        with reg.db() as conn:
            for _ in range(31):
                conn.execute("INSERT INTO feed_runs(kind,started_at,finished_at,summary) VALUES('social',?,?,'{}')",
                             (reg.now(), reg.now()))
        self.assertTrue(store.progress(self.source)['blocks'])
        self.collect_runtime(lambda *args, **kwargs: ([self.entry()], ''),
                             scope=FetchScope(force=True, targets=(reg.id_target(self.source['id']),)))
        x_channels.reset()
        self.assertFalse(store.progress(self.source)['blocks'])
        self.assertFalse(any(item['problem'] == 'x_intake_challenge' for item in diagnostics.problems('social')))

    def test_fallback_cannot_follow_a_repointed_source_or_another_channel(self):
        with self.blocked_observation_write(), self.assertLogs('kb.feeds.x_intake', 'WARNING'):
            self.collect_runtime(lambda *args, **kwargs: (_ for _ in ()).throw(x_page_state.XAccessBlocked('旧身份验证墙')))
        source = reg.subscription('social', self.source['id'])
        store.statuses([source], 'feedgrab')
        self.assertNotIn('x_intake', source)
        source = reg.save_subscription('social', {'url': 'https://x.com/NewAuthor'}, self.source['id'])
        store.statuses([source], 'browser')
        self.assertNotIn('x_intake', source)


class ScreenTimeoutTests(RuntimeFixture):
    @contextmanager
    def clocked(self):
        clock = Clock()
        with patch.object(x_intake, 'time', clock), patch.object(x_browser, 'time', clock), \
             patch.object(x_timeline_state, 'time', clock), patch.object(x_intake_metrics, 'time', clock), \
             patch.object(x_cooldown, 'time', clock), patch.object(x_browser, 'navigate'), \
             patch.object(x_page_state, 'read', return_value=page_state()):
            yield clock

    def test_full_discovery_and_history_allow_the_unknown_page_failure_exit(self):
        for seconds in (8, 20):
            with self.subTest(seconds=seconds), self.clocked() as clock:
                with self.assertRaises(x_page_state.XBrowserError) as caught:
                    x_browser._harvest_one(Session(), 'example', self.now, deadline=clock.now + seconds,
                                           account_seconds=seconds, budget_limited=False)
                self.assertNotIsInstance(caught.exception, XBudgetExceeded)
                self.assertIn('未能判出原因', str(caught.exception))

    def test_a_truly_shortened_history_slice_is_budget_not_an_account_failure(self):
        with self.clocked() as clock:
            with self.assertRaises(XBudgetExceeded):
                x_browser._harvest_one(Session(), 'example', self.now, deadline=clock.now + 8.5,
                                       account_seconds=20, budget_limited=True)

    def test_fair_share_shortening_keeps_the_configured_quota_identity(self):
        self.prepare()
        options_seen = []
        def scan(page, handle, floor, **options):
            options_seen.append(options)
            raise XBudgetExceeded('公平分配缩短首屏等待')
        with self.clocked(), patch.object(x_browser, '_harvest_one', side_effect=scan), \
             patch.object(x_intake, 'seconds', return_value=20):
            intake = x_intake.Intake([self.source], self.options, budget=480)
            intake.cool = x_cooldown.SessionCooldown(first=0, maximum=0)
            intake.scan(Session(), 'discovery', 10)
        self.assertEqual(options_seen[0]['account_seconds'], 20)
        self.assertTrue(options_seen[0]['budget_limited'])
        self.assertFalse(intake.results)

    def test_default_collect_qualifies_a_full_unknown_page_as_an_actual_failure(self):
        original = x_browser._harvest_one
        with self.clocked():
            summary = self.collect_runtime(original)
        self.assertEqual(summary['failed_ids'], [str(self.source['id'])])
        source = reg.subscription('social', self.source['id'])
        self.assertEqual(source['failures'], 1)
        self.assertGreater(source['retry_at'], 0)
        self.assertIn('未能判出原因', source['last_error'])

    def test_four_short_history_rounds_escalate_only_history_and_keep_delivering(self):
        original, seconds = x_browser._harvest_one, x_intake.seconds
        discovered = []
        def scan(page, handle, floor, **options):
            if options['max_scrolls'] == 2:
                options['on_ready'](0.01)
                entry = self.entry(str(101 + len(discovered)), age=1 - len(discovered) / 10)
                discovered.append(entry)
                return [entry], ''
            return original(page, handle, floor, **options)
        def limit(name, default):
            return 8.5 if name == 'KB_X_HISTORY_SECONDS' else seconds(name, default)
        self.old_failure()
        with self.clocked(), patch.object(x_intake, 'seconds', side_effect=limit):
            for count in range(1, 5):
                summary = self.collect_runtime(scan)
                source = reg.subscription('social', self.source['id'])
                self.assertEqual(summary['failed_ids'], [])
                self.assertEqual(summary['added'], 1)
                self.assertEqual(summary['succeeded_ids'], [str(source['id'])])
                self.assertEqual((source['last_error'], source['failures'], source['retry_at']), ('', 0, 0))
                self.assertEqual(store.delivery(source)[0], [])
                self.assertEqual(x_channels.watermark(source, 'browser'), discovered[-1]['x_observed_at'])
                self.assertIn('五天历史追补', source['last_note'])
                if count >= 3:
                    self.assertIn(f'连续{count}轮首屏未确认', source['last_note'])
                    self.assertIn('history', x_channels._harvest['example']['phase_errors'])
        source = reg.subscription('social', self.source['id'])
        self.assertEqual(json.loads(store.progress(source)['screen_waits'])['history']['count'], 4)
        with reg.read_db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 4)

    def test_wait_counter_is_once_per_round_and_resets_only_its_confirmed_phase(self):
        self.prepare()
        self.assertEqual(store.screen_wait(self.source, 'discovery', 'round1'), 1)
        self.assertEqual(store.screen_wait(self.source, 'discovery', 'round1'), 1)
        self.assertEqual(store.screen_wait(self.source, 'discovery', 'round2'), 2)
        self.assertEqual(store.screen_wait(self.source, 'history', 'round2'), 1)
        self.accept_batch()
        self.assertEqual(store.screen_wait(self.source, 'discovery', 'round3'), 1)
        self.assertEqual(store.screen_wait(self.source, 'history', 'round3'), 2)

    def test_old_database_adds_the_screen_ledger_without_losing_phase_receipts(self):
        self.prepare()
        self.accept_batch()
        with reg.db() as conn:
            conn.execute('ALTER TABLE x_intake_sources DROP COLUMN screen_waits')
        self.assertEqual(store.screen_wait(self.source, 'discovery', 'round1'), 1)
        self.assertTrue(store.progress(self.source)['latest'])

    @skipUnless(shutil.which('node'), 'JavaScript runtime required for phase rendering')
    def test_phase_specific_client_labels_do_not_turn_empty_records_into_blocks(self):
        script = (Path(__file__).resolve().parents[1] / 'kb/static/js/x_intake_status.js').read_text()
        script += '''
const esc = value => value;
const render = blocks => xIntakeStatus({x_intake: {discovery: 'complete', blocks}});
console.log(JSON.stringify([render({}), render({body: {kind: 'throttled'}}),
  render({history: {kind: 'challenge'}}), render({discovery: {kind: 'rate'}})]));
'''
        result = subprocess.run(['node', '-e', script], text=True, capture_output=True, check=True, timeout=10)
        rendered = json.loads(result.stdout)
        self.assertNotIn('访问受阻', rendered[0])
        self.assertIn('正文补全：启动限流', rendered[1])
        self.assertIn('历史追补：安全验证', rendered[2])
        self.assertIn('最新发现：访问限流', rendered[3])
