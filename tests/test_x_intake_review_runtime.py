"""Default X runtime contracts: budget identity, durable blockers and optional telemetry."""
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_account, x_article, x_browser, x_cooldown
from kb.feeds import x_intake, x_intake_blocks as blocks, x_intake_metrics, x_intake_store as store
from kb.feeds import x_page_state, x_post, x_timeline_state
from kb.feeds.x_cdp import CdpError, XBudgetExceeded
from kb.intake_diag import actions, sources as diagnostics
from tests.test_x_intake import IntakeBase, Session
from tests.x_state_fixture import page_state


class Clock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class RuntimeFixture(IntakeBase):
    def collect_runtime(self, scanner, *, channel='browser', scope=None):
        self.settings(channel)
        cool = x_cooldown.SessionCooldown(first=0, maximum=0)
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=scanner), \
             patch.object(x_cooldown, 'of', return_value=cool):
            return sync.collect('social', scope or FetchScope(force=True))

    def old_failure(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET last_success='2026-10-01T00:00:00+00:00',last_error='旧连接失败',failures=3,retry_at=42 WHERE id=?", (self.source['id'],))

    def accept_batch(self, *, stage_only=False):
        entry = self.entry()
        batch_id = uuid4().hex
        boundary = store.floor_for(self.source, self.now).isoformat()
        store.stage(self.source, [entry], phase='discovery', incomplete='', batch_id=batch_id,
                    boundary=boundary, end=self.now.isoformat())
        receipt = dict(phase='discovery', incomplete='', observed=store.observed([entry]),
                       boundary=boundary, end=self.now.isoformat(), coverage_complete=True, batch_id=batch_id)
        if not stage_only:
            store.confirm(self.source, [entry], **receipt)
        return entry, receipt


class BudgetIdentityTests(RuntimeFixture):
    def test_a_short_scheduler_wait_is_budget_not_an_account_failure(self):
        clock = Clock()
        with patch.object(x_timeline_state, 'time', clock), \
             patch.object(x_page_state, 'read', return_value=page_state(ready='loading')):
            with self.assertRaises(XBudgetExceeded):
                x_timeline_state.wait_state(object(), 'example', 4.7, '', budget_limited=True)

    def test_a_page_rendering_after_six_seconds_fits_the_minimum_slice(self):
        clock = Clock()
        def read(*args):
            return page_state(ready='complete' if clock.now >= 6 else 'loading', articles=int(clock.now >= 6))
        with patch.object(x_timeline_state, 'time', clock), patch.object(x_page_state, 'read', side_effect=read):
            state = x_timeline_state.wait_state(object(), 'example', 8, '', budget_limited=True)
        self.assertEqual(state['articles'], 1)
        self.assertGreaterEqual(clock.now, 6)

    def test_native_account_timeout_keeps_its_original_failure_contract(self):
        clock = Clock()
        with patch.object(x_timeline_state, 'time', clock), \
             patch.object(x_page_state, 'read', return_value=page_state(ready='loading')):
            with self.assertRaises(x_page_state.XBrowserError):
                x_timeline_state.wait_state(object(), 'example', 1, '')

    def test_real_core_error_is_not_reclassified_at_a_short_deadline(self):
        clock = Clock()
        with patch.object(x_timeline_state, 'time', clock), \
             patch.object(x_page_state, 'read', return_value=page_state(failed=True, dom_problem='failed')):
            with self.assertRaises(x_page_state.XBrowserError):
                x_timeline_state.wait_state(object(), 'example', 1, '', budget_limited=True)

    def test_fifty_two_six_second_pages_do_not_acquire_failures_and_backoff(self):
        for index in range(51):
            self.sub(f'Acct{index}')
        clock = Clock()
        ready_since = [0]
        post_ids = {}
        def scan(page, handle, floor, **options):
            ready_since[0] = clock.now
            x_timeline_state.wait_state(page, handle, options['deadline'], '',
                                       budget_limited=options['budget_limited'])
            identifier = post_ids.setdefault(handle, str(100 + len(post_ids)))
            return [self.entry(identifier, handle=handle)], ''
        with patch.object(x_intake, 'time', clock), patch.object(x_browser, 'time', clock), \
             patch.object(x_timeline_state, 'time', clock), patch.object(x_intake_metrics, 'time', clock), \
             patch.object(x_cooldown, 'time', clock), patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=scan), \
             patch.object(x_browser, 'navigate'), \
             patch.object(x_page_state, 'read', side_effect=lambda *args: page_state(
                 ready='complete' if clock.now - ready_since[0] >= 6 else 'loading',
                 articles=int(clock.now - ready_since[0] >= 6))):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual(summary['failed_ids'], [])
        self.assertGreater(summary['added'], 0)
        for source in reg.subscriptions('social'):
            self.assertEqual((source['failures'], source['retry_at'], source['last_error']), (0, 0, ''))

    def test_both_scan_phases_leave_accounts_unattempted_without_a_full_minimum(self):
        self.prepare()
        for phase in ('discovery', 'history'):
            with self.subTest(phase=phase):
                intake = x_intake.Intake([self.source], self.options, budget=480)
                intake.cool = x_cooldown.SessionCooldown(first=0, maximum=0)
                with patch.object(x_browser, '_harvest_one') as scan:
                    intake.scan(Session(), phase, x_intake.time.monotonic() + 7.5)
                scan.assert_not_called()
                self.assertEqual(intake.attempted, set())

    def test_deadline_identity_reaches_the_real_browser_wait(self):
        clock = Clock()
        with patch.object(x_browser, 'time', clock), patch.object(x_timeline_state, 'time', clock), \
             patch.object(x_browser, 'navigate'), \
             patch.object(x_page_state, 'read', return_value=page_state(ready='loading')):
            with self.assertRaises(XBudgetExceeded):
                x_browser._harvest_one(Session(), 'example', self.now, deadline=1)

    def test_post_and_article_cdp_budget_exceptions_keep_their_identity(self):
        page = SimpleNamespace(goto=lambda url: (_ for _ in ()).throw(XBudgetExceeded('切片截止')))
        with self.assertRaises(XBudgetExceeded):
            x_post.fetch(self.entry()['url'], page)
        with self.assertRaises(XBudgetExceeded):
            x_article.browser_fetch(page, self.entry()['url'])
        page.goto = lambda url: None
        page.eval = lambda script: (_ for _ in ()).throw(XBudgetExceeded('读取截止'))
        with self.assertRaises(XBudgetExceeded):
            x_article.browser_fetch(page, self.entry()['url'])

    def test_body_budget_does_not_increment_the_candidate_retry_ledger(self):
        with patch.object(x_post, 'complete', side_effect=XBudgetExceeded('正文切片用完')):
            summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry(truncated=True)], ''))
        self.assertEqual(summary['failed_ids'], [])
        with reg.read_db() as conn:
            task = conn.execute('SELECT * FROM x_intake_candidates').fetchone()
        self.assertEqual((task['state'], task['failures'], task['retry_at']), ('pending', 0, 0))


class DurableBlockTests(RuntimeFixture):
    def test_all_typed_blockers_survive_restart_and_reach_diagnostics(self):
        cases = [(x_page_state.XSessionThrottled('启动429'), 'throttled'),
                 (x_page_state.XRateLimited('内容429'), 'rate'),
                 (x_page_state.XAccessBlocked('安全验证原话'), 'challenge'),
                 (x_account.XAccountUnavailable('example', 'suspended'), 'account'),
                 (x_account.XAccountUnavailable('example', 'login'), 'login'),
                 (x_post.PostSessionUnavailable('会话中断原话'), 'session')]
        for error, kind in cases:
            with self.subTest(kind=kind):
                self.collect_runtime(lambda *args, **kwargs: (_ for _ in ()).throw(error))
                store.statuses([source := reg.subscription('social', self.source['id'])])
                self.assertEqual(source['x_intake']['block']['kind'], kind)
                problem = next(item for item in diagnostics.problems('social') if item['problem'] == f'x_intake_{kind}')
                self.assertEqual(problem['evidence'], str(error))
                plan = actions._source_scope(problem)
                self.assertNotIn('extract', plan['stages'])
                self.assertEqual(plan['source_id'], self.source['id'])
                self.assertEqual(plan['scope_sig'], problem['sig'])

    def test_security_challenge_does_not_turn_into_a_silent_history_wait(self):
        self.collect_runtime(lambda *args, **kwargs: (_ for _ in ()).throw(x_page_state.XAccessBlocked('安全验证原话')))
        problems = diagnostics.problems('social')
        challenge = next(item for item in problems if item['problem'] == 'x_intake_challenge')
        self.assertIn('反复同步不会', challenge['advice'])
        self.assertTrue(any(item['problem'] == 'x_intake_history' for item in problems))

    def test_other_accounts_do_not_inherit_an_observed_blocker(self):
        other = self.sub('Other')
        def scan(page, handle, floor, **kwargs):
            if handle == 'other':
                raise x_page_state.XAccessBlocked('只有Other的验证墙')
            return [self.entry()], ''
        summary = self.collect_runtime(scan)
        self.assertIn(str(self.source['id']), summary['succeeded_ids'])
        states = [reg.subscription('social', identifier) for identifier in (self.source['id'], other['id'])]
        store.statuses(states)
        self.assertFalse(states[0]['x_intake']['block'])
        self.assertEqual(states[1]['x_intake']['block']['kind'], 'challenge')

    def test_confirming_an_earlier_batch_cannot_clear_a_later_blocker(self):
        self.prepare()
        store.set_block(self.source, blocks.describe(x_page_state.XSessionThrottled('旧阻断')))
        entry, receipt = self.accept_batch(stage_only=True)
        newer = blocks.describe(x_page_state.XAccessBlocked('迟到验证墙'))
        store.set_block(self.source, newer)
        store.confirm(self.source, [entry], **receipt)
        self.assertEqual(store.progress(self.source)['block'], newer)
        self.accept_batch()
        self.assertFalse(store.progress(self.source)['block'])

    def test_prepare_and_unusable_empty_receipts_do_not_erase_a_blocker(self):
        self.prepare()
        block = blocks.describe(x_page_state.XRateLimited('内容限流'))
        store.set_block(self.source, block)
        self.prepare()
        batch_id = uuid4().hex
        store.stage(self.source, [], phase='discovery', incomplete='未读到', batch_id=batch_id)
        store.confirm(self.source, [], phase='discovery', incomplete='未读到', observed='',
                      boundary=store.floor_for(self.source, self.now).isoformat(), batch_id=batch_id)
        self.assertEqual(store.progress(self.source)['block'], block)

    def test_a_real_success_clears_an_older_block_without_relogin(self):
        self.collect_runtime(lambda *args, **kwargs: (_ for _ in ()).throw(x_page_state.XSessionThrottled('启动限流')))
        summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry()], ''))
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertFalse(store.progress(self.source)['block'])
        self.assertFalse(any(item['problem'] == 'x_intake_throttled' for item in diagnostics.problems('social')))

    def test_body_boot_throttle_reports_a_typed_problem_without_consuming_its_queue(self):
        with patch.object(x_post, 'fetch', side_effect=x_page_state.XSessionThrottled('正文启动429')):
            self.collect_runtime(lambda *args, **kwargs: ([self.entry(truncated=True)], ''))
        self.assertEqual(store.progress(self.source)['block']['kind'], 'throttled')
        self.assertEqual(store.delivery(self.source)[1], 1)

    def test_a_late_timeline_block_is_attributed_before_packaging(self):
        self.prepare()
        intake = x_intake.Intake([self.source], self.options, budget=480)
        intake.timelines[self.source['id']] = [('discovery', SimpleNamespace(blocker=x_page_state.XAccessBlocked('收尾验证墙')))]
        result = intake.package()['example']
        self.assertTrue(result['pending'])
        self.assertEqual(store.progress(self.source)['block']['reason'], '收尾验证墙')


class OptionalMetricsTests(RuntimeFixture):
    def assert_metrics_failure_isolated(self, error):
        self.old_failure()
        with patch.object(store, 'save_metrics', side_effect=error), self.assertLogs('kb.feeds.x_intake', 'WARNING') as log:
            summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry()], ''))
        self.assertIn('不改变来源结论', log.output[0])
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(summary['failed_ids'], [])
        source = reg.subscription('social', self.source['id'])
        self.assertEqual((source['last_error'], source['failures'], source['retry_at']), ('', 0, 0))
        self.assertNotEqual(source['last_success'], '2026-10-01T00:00:00+00:00')

    def test_sqlite_busy_in_statistics_cannot_veto_content_or_success(self):
        self.assert_metrics_failure_isolated(sqlite3.OperationalError('database is locked'))

    def test_an_actual_competing_sqlite_writer_only_blocks_the_statistics_insert(self):
        save = store.save_metrics
        def competing_writer(*args):
            connection = sqlite3.connect(reg.DB_PATH, isolation_level=None)
            try:
                connection.execute('BEGIN IMMEDIATE')
                save(*args)
            finally:
                connection.rollback()
                connection.close()
        with patch.object(store, 'save_metrics', side_effect=competing_writer), \
             self.assertLogs('kb.feeds.x_intake', 'WARNING'):
            summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry()], ''))
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(summary['failed_ids'], [])

    def test_telemetry_serialization_fault_cannot_veto_content_or_success(self):
        self.assert_metrics_failure_isolated(ValueError('统计序列化失败'))

    def test_metrics_failure_preserves_unread_and_genuine_failure_conclusions(self):
        self.sub('Other')
        def scan(page, handle, floor, **kwargs):
            raise CdpError('真实DOM故障') if handle == 'example' else XBudgetExceeded('预算不足')
        with patch.object(store, 'save_metrics', side_effect=sqlite3.OperationalError('database is locked')), \
             self.assertLogs('kb.feeds.x_intake', 'WARNING'):
            summary = self.collect_runtime(scan)
        self.assertEqual(summary['failed_ids'], [str(self.source['id'])])
        self.assertEqual(summary['skipped'], 1)
        self.assertIn('真实DOM故障', reg.subscription('social', self.source['id'])['last_error'])
