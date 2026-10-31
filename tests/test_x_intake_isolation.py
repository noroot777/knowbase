"""Account-local outcomes, read-only contention, bounded notices and local delivery."""
import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_cooldown, x_feedgrab
from kb.feeds import x_intake, x_intake_store as store
from kb.feeds.x_intake_metrics import Metrics
from kb.feeds.x_page_state import XAccessBlocked, XRateLimited
from kb.intake_diag import actions, sources as diagnostics
from tests.test_x_channels import tweet
from tests.test_x_intake import IntakeBase, Session


class IsolationFixture(IntakeBase):
    def collect(self, scanner, *, session=None):
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=session or Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=scanner), \
             patch.object(x_cooldown, 'of', side_effect=lambda *args: x_cooldown.SessionCooldown(first=0, maximum=0)):
            return sync.collect('social', FetchScope(force=True))

    def failures(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET last_success='旧成功',last_error='旧连接失败',failures=2,retry_at=42 WHERE kind='social'")


class AccountOutcomeTests(IsolationFixture):
    def test_rate_limit_after_healthy_read_does_not_contaminate_healthy_ledger(self):
        other = self.sub('Other')
        self.failures()

        def scan(page, handle, floor, **kwargs):
            if handle == 'other':
                raise XRateLimited('另一个账号被限流')
            return [self.entry()], ''

        summary = self.collect(scan)
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        healthy = reg.subscription('social', self.source['id'])
        self.assertEqual((healthy['last_error'], healthy['failures'], healthy['retry_at']), ('', 0, 0))
        self.assertNotIn('另一个账号', healthy['last_note'])
        self.assertIn('另一个账号', summary['message'])
        self.assertEqual(reg.subscription('social', other['id'])['last_success'], '旧成功')

    def test_access_block_after_healthy_read_keeps_session_stop_but_not_false_account_debt(self):
        self.sub('Other')
        third = self.sub('Third')
        visited = []

        def scan(page, handle, floor, **kwargs):
            visited.append(handle)
            if handle == 'other':
                raise XAccessBlocked('另一个账号要求验证')
            return [self.entry()], ''

        summary = self.collect(scan)
        self.assertEqual(visited, ['example', 'other'])
        self.assertIn(str(self.source['id']), summary['succeeded_ids'])
        self.assertNotIn('另一个账号', reg.subscription('social', third['id'])['last_note'])
        self.assertEqual(store.latest_metrics()['blocked'], '另一个账号要求验证')

    def test_pending_expiry_does_not_block_current_account_success(self):
        self.prepare()
        self.stage([self.entry(age=119, truncated=True)])
        store.prepare([self.source], 'browser', self.now + timedelta(hours=2))
        summary = self.collect(lambda *args, **kwargs: ([self.entry('200')], ''))
        self.assertIn(str(self.source['id']), summary['succeeded_ids'])
        self.assertEqual(summary['added'], 1)


class ReadIsolationTests(IsolationFixture):
    def test_collector_and_panel_reads_do_not_request_writer_lock(self):
        self.prepare()
        self.stage([self.entry(truncated=True)])
        store.save_metrics(reg.now(), {'channel': 'browser'})
        writer = sqlite3.connect(reg.DB_PATH, timeout=0)
        self.addCleanup(writer.close)
        writer.execute('BEGIN IMMEDIATE')
        self.assertTrue(store.progress(self.source))
        self.assertEqual(len(store.pending([self.source])), 1)
        self.assertEqual(store.delivery(self.source)[1], 1)
        current = [dict(self.source)]
        store.statuses(current)
        self.assertEqual(current[0]['x_intake']['pending'], 1)
        self.assertEqual(store.latest_metrics()['channel'], 'browser')
        with reg.read_db(timeout=0) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("UPDATE subscriptions SET last_note='forbidden'")

    def test_twenty_four_concurrent_overviews_all_return_200(self):
        self.prepare()
        self.stage([self.entry()])
        with patch.object(reg, 'configuration_error', return_value=''), ThreadPoolExecutor(max_workers=24) as pool:
            responses = list(pool.map(lambda index: self.client.get('/api/intake/social'), range(24)))
        self.assertEqual([response.status_code for response in responses], [200] * 24)
        self.assertTrue(all(response.json()['sources'][0]['x_intake']['discovery'] == 'complete' for response in responses))

    def test_transient_prepare_progress_lock_only_defers_that_source(self):
        other = self.sub('Other')
        self.failures()
        original = store.progress
        injected = False

        def progress(source):
            nonlocal injected
            if source['id'] == self.source['id'] and not injected:
                injected = True
                raise sqlite3.OperationalError('database is locked')
            return original(source)

        with patch.object(store, 'progress', side_effect=progress):
            summary = self.collect(lambda page, handle, floor, **kwargs: ([self.entry('200', handle=handle)], ''))
        self.assertEqual(summary['succeeded_ids'], [str(other['id'])])
        self.assertEqual(summary['failed_ids'], [])
        self.assertEqual(reg.subscription('social', self.source['id'])['failures'], 2)
        self.assertEqual(reg.subscription('social', other['id'])['failures'], 0)

    def test_browser_sort_and_recheck_progress_failures_are_account_local(self):
        other = self.sub('Other')
        self.prepare([self.source, other])
        for fail_call in (1, 2):
            with self.subTest(fail_call=fail_call):
                intake = x_intake.Intake([self.source, other], self.options, budget=480)
                intake.cool = x_cooldown.SessionCooldown(first=0, maximum=0)
                original, seen = store.progress, 0

                def progress(source):
                    nonlocal seen
                    if source['id'] == self.source['id']:
                        seen += 1
                        if seen == fail_call:
                            raise sqlite3.OperationalError('database is locked')
                    return original(source)

                with patch.object(store, 'progress', side_effect=progress), \
                     patch.object(x_browser, '_harvest_one', side_effect=lambda page, handle, floor, **kw: ([self.entry('200', handle=handle)], '')):
                    intake.scan(Session(), 'discovery', intake.metrics.network_deadline)
                self.assertEqual(set(intake.local), {self.source['id']})
                self.assertIn(other['id'], intake.read)

    def test_feedgrab_sort_fault_does_not_abort_healthy_source(self):
        other = self.sub('Other')
        self.options = self.settings('feedgrab')
        original = store.progress

        def progress(source):
            if source['id'] == self.source['id']:
                raise sqlite3.OperationalError('database is locked')
            return original(source)

        def harvest(jobs, **kwargs):
            handle = jobs[0][0]
            return {handle: dict(entries=[self.entry('200', handle=handle)], error='', incomplete='')}

        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(store, 'progress', side_effect=progress), patch.object(x_feedgrab, 'harvest', side_effect=harvest):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual(summary['succeeded_ids'], [str(other['id'])])
        self.assertEqual(summary['added'], 1)

    def test_body_queue_read_fault_isolated_for_browser_and_feedgrab(self):
        other = self.sub('Other')
        for channel in ('browser', 'feedgrab'):
            with self.subTest(channel=channel):
                self.options = self.settings(channel)
                intake = x_intake.Intake([self.source, other], self.options, budget=480)
                self.prepare([self.source, other])
                self.stage([self.entry('201', handle='Other', truncated=True)], source=other)
                original = store.pending

                def pending(sources, **kwargs):
                    if sources[0]['id'] == self.source['id']:
                        raise sqlite3.OperationalError('database is locked')
                    return original(sources, **kwargs)

                with patch.object(store, 'pending', side_effect=pending):
                    jobs = list(intake.pending_jobs())
                self.assertEqual({source['id'] for source, task in jobs}, {other['id']})
                self.assertEqual(set(intake.local), {self.source['id']})


class ExpirationRetentionTests(IsolationFixture):
    def expire_one(self):
        self.prepare()
        self.stage([self.entry(age=119, truncated=True)])
        future = self.now + timedelta(hours=2)
        store.prepare([self.source], 'browser', future)
        return future

    def test_expiry_notice_is_a_24_hour_window_not_a_lifetime_counter(self):
        future = self.expire_one()
        self.assertEqual(store.progress(self.source)['expired'], 1)
        store.prepare([self.source], 'browser', future + timedelta(hours=23))
        self.assertEqual(store.progress(self.source)['expired'], 1)
        store.prepare([self.source], 'browser', future + timedelta(hours=25))
        self.assertEqual(store.progress(self.source)['expired'], 0)
        self.assertEqual(store.progress(self.source)['outside'], '')
        self.assertEqual(self.candidate()['state'], 'expired')

    def test_panel_expiry_window_decays_without_another_sync(self):
        self.expire_one()
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_expirations SET expired_at=?', ((self.now - timedelta(hours=25)).isoformat(),))
        sources = [dict(self.source)]
        store.statuses(sources)
        self.assertEqual(sources[0]['x_intake']['expired'], 0)
        self.assertEqual(sources[0]['x_intake']['outside'], '')

    def test_retention_removes_old_terminal_receipts_but_not_feed_items_or_ready_outbox(self):
        self.prepare()
        self.stage([self.entry('100'), self.entry('101', truncated=True)])
        reg.put_items(self.source, [self.entry('300')])
        store.prepare([self.source], 'browser', self.now + timedelta(days=10))
        self.assertEqual(self.candidate('100')['state'], 'ready')
        self.assertEqual(len(store.delivery(self.source)[0]), 1)
        with reg.db() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM x_intake_candidates WHERE post_id='101'").fetchone()[0], 0)
            self.assertEqual(conn.execute("SELECT count(*) FROM x_intake_links WHERE post_id='101'").fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 1)

    def test_deleted_subscription_and_old_batch_members_are_pruned(self):
        self.prepare()
        store.stage(self.source, [self.entry()], phase='history', incomplete='',
                    batch_id='old', end=self.now.isoformat())
        with reg.db() as conn:
            conn.execute('DELETE FROM subscriptions WHERE id=?', (self.source['id'],))
        other = self.sub('Other')
        store.prepare([other], 'browser', self.now + timedelta(days=10))
        with reg.db() as conn:
            for table in ('x_intake_links', 'x_intake_batches', 'x_intake_batch_posts', 'x_intake_expirations', 'x_intake_candidates'):
                self.assertEqual(conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], 0, table)


class DeliveryAndDiagnosticsTests(IsolationFixture):
    def test_network_budget_reserves_30_seconds_for_tail(self):
        metrics = Metrics(480)
        self.assertEqual(metrics.deadline - metrics.network_deadline, 30)
        self.assertEqual(metrics.data['finalize_reserve_seconds'], 30)
        self.assertGreater(Metrics(1).network_deadline, time.monotonic())

    def test_slow_close_past_network_and_total_deadline_still_delivers_certified_products(self):
        offset = 0
        clock = time.monotonic

        class SlowClose(Session):
            def __exit__(session, *args):
                nonlocal offset
                offset += 500
                return False

        with patch('time.monotonic', side_effect=lambda: clock() + offset):
            summary = self.collect(lambda *args, **kwargs: ([self.entry()], ''), session=SlowClose())
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(store.delivery(self.source)[0], [])
        self.assertGreater(summary['x_intake']['overrun_seconds'], 0)

    def test_structured_body_and_history_are_not_generic_partial_or_connection_failures(self):
        self.prepare()
        store.attempt(self.source, 'history')
        self.stage([self.entry(truncated=True)], missing='历史尚未翻完')
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET last_note='未知文案不能决定分类' WHERE id=?", (self.source['id'],))
        with patch.object(reg, 'configuration_error', return_value=''):
            problems = diagnostics.problems('social')
        self.assertEqual({problem['problem'] for problem in problems}, {'x_intake_body', 'x_intake_history'})
        body = next(problem for problem in problems if problem['problem'] == 'x_intake_body')
        plan = actions._source_scope(body)
        self.assertEqual(plan['title'], '检查独立正文队列')
        self.assertIn('不跳过单条正文冷却', plan['detail'])
        self.assertNotIn('按原同步下界', body['advice'])
        self.assertNotIn('extract', plan['stages'])

    def test_unknown_automatic_note_does_not_create_a_problem(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET last_note='别人的限流原话或未知说明' WHERE id=?", (self.source['id'],))
        with patch.object(reg, 'configuration_error', return_value=''):
            self.assertEqual(diagnostics.problems('social'), [])

    def test_unconfirmed_and_expired_have_different_advice_and_expired_cannot_retry(self):
        self.prepare()
        self.stage([self.entry()], confirm=False)
        with patch.object(reg, 'configuration_error', return_value=''):
            problem = next(problem for problem in diagnostics.problems('social') if problem['problem'] == 'x_intake_unconfirmed')
        self.assertIn('优先重扫', problem['advice'])
        with reg.db() as conn:
            conn.execute("UPDATE x_intake_candidates SET state='expired'")
            conn.execute('INSERT INTO x_intake_expirations VALUES(?,?,?,?)',
                         (self.source['id'], store.identity(self.source), '100', self.now.isoformat()))
        with patch.object(reg, 'configuration_error', return_value=''):
            problem = next(problem for problem in diagnostics.problems('social') if problem['problem'] == 'x_intake_expired')
        self.assertFalse(problem['actionable'])
        self.assertIn('指定历史区间', problem['blocked'])

    def test_non_x_notes_keep_the_existing_diagnostic_contract(self):
        source = reg.save_subscription('rss', {'name': 'RSS', 'url': 'https://example.test/feed'})
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET last_note='没有读到上次的位置' WHERE id=?", (source['id'],))
        with patch.object(reg, 'configuration_error', return_value=''):
            self.assertEqual(diagnostics.problems('rss')[0]['problem'], 'coverage')


class EffectiveLimitsTests(IsolationFixture):
    def harvest(self, *, deadline, scrolls, clock):
        entry = tweet('100', when=self.now.isoformat())
        snapshot = json.dumps(dict(articles=1, parsed=1, items=[entry]))

        class Timeline:
            incomplete = ''

            def eval(session, script):
                return json.dumps({'bottom': False}) if script == x_browser._SCROLL_JS else snapshot

            def check(session):
                pass

        with patch.object(x_browser, 'navigate'), patch.object(x_browser, '_wait_state', return_value={}), \
             patch.object(x_browser, 'TimelineState', return_value=Timeline()), patch.object(x_browser, '_settle', return_value=True), \
             patch('time.monotonic', side_effect=clock):
            return x_browser._harvest_one(Session(), 'Example', self.now - timedelta(days=5),
                                          deadline=deadline, max_scrolls=scrolls)

    def test_fractional_time_limit_uses_effective_call_budget(self):
        entries, reason = self.harvest(deadline=100.25, scrolls=2, clock=[100, 101])
        self.assertEqual(len(entries), 1)
        self.assertIn('0.25 秒', reason)
        self.assertNotIn('50 秒', reason)

    def test_scroll_exhaustion_reports_actual_two_scrolls(self):
        entries, reason = self.harvest(deadline=104.7, scrolls=2, clock=lambda: 100)
        self.assertEqual(len(entries), 1)
        self.assertIn('2 次滚动', reason)
        self.assertNotIn('40', reason)

    def test_default_call_reports_effective_module_cap(self):
        entries, reason = self.harvest(deadline=1000, scrolls=None, clock=[100, 151])
        self.assertEqual(len(entries), 1)
        self.assertIn('50 秒', reason)
