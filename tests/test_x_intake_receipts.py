"""Scan batches recover across rounds; diagnostic truth and body debt remain independent."""
import sqlite3
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kb.collectors import FetchScope
from kb.feeds import cdp_attach, registry as reg, sync, x_browser, x_cooldown, x_feedgrab, x_post
from kb.feeds import x_intake_batches as batches, x_intake_store as store
from kb.feeds.ws_client import WsTimeoutError
from kb.feeds.x_cdp import CdpError, CdpResponseTimeout, Chrome, Session as CdpSession, XBudgetExceeded
from kb.feeds.x_page_state import XBrowserError, XRateLimited
from tests.test_x_intake import IntakeBase, Session


class ReceiptBase(IntakeBase):
    def collect(self, scanner):
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=scanner), \
             patch.object(x_cooldown, 'of', return_value=x_cooldown.SessionCooldown(first=0, maximum=0)):
            return sync.collect('social', FetchScope(force=True))

    def seed_coverage(self, source=None):
        source = source or self.source
        store.prepare([source], 'browser', self.now)
        store.confirm(source, [], phase='history', incomplete='', observed='',
                      boundary=store.floor_for(source, self.now).isoformat(), end=self.now.isoformat(),
                      coverage_complete=True)

    def stage_batch(self, entries, *, source=None, batch_id='batch-one', when=None):
        source, when = source or self.source, when or self.now
        return store.stage(source, entries, phase='history', incomplete='等待批次收尾', batch_id=batch_id,
                           boundary=store.floor_for(source, when).isoformat(), end=when.isoformat())

    def confirm_batch(self, entries, *, source=None, batch_id='batch-one', complete=False):
        source = source or self.source
        return store.confirm(source, entries, phase='history', incomplete='' if complete else '仍有历史缺口',
                             observed=store.observed(entries), boundary=store.floor_for(source, self.now).isoformat(),
                             end=self.now.isoformat(), coverage_complete=complete, batch_id=batch_id)


class BatchRecoveryTests(ReceiptBase):
    def test_batch_confirmation_uses_persistent_members_not_mutable_entries(self):
        self.prepare()
        entries = [self.entry()]
        self.stage_batch(entries)
        entries.clear()
        self.assertTrue(self.confirm_batch(entries))
        self.assertEqual(len(store.delivery(self.source)[0]), 1)
        self.assertEqual(store.progress(self.source)['unconfirmed'], 0)

    def test_confirmation_does_not_approve_a_different_batch(self):
        self.prepare()
        self.stage_batch([self.entry()], batch_id='first')
        self.stage_batch([self.entry('101')], batch_id='second')
        self.confirm_batch([], batch_id='first')
        self.assertEqual([store.post_id(entry) for entry in store.delivery(self.source)[0]], ['100'])
        self.assertEqual(store.progress(self.source)['unconfirmed'], 1)

    def test_wrong_identity_cannot_confirm_old_batch(self):
        self.prepare()
        self.stage_batch([self.entry()])
        with reg.db() as conn:
            conn.execute('UPDATE subscriptions SET rev=rev+1 WHERE id=?', (self.source['id'],))
        self.assertFalse(self.confirm_batch([]))
        self.assertEqual(store.delivery(self.source)[0], [])

    def test_candidate_and_batch_membership_rollback_together(self):
        self.prepare()
        with patch.object(batches, 'add', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.stage_batch([self.entry()])
        with reg.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM x_intake_batches').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM x_intake_candidates').fetchone()[0], 0)
            self.assertEqual(conn.execute('SELECT count(*) FROM x_intake_links').fetchone()[0], 0)

    def test_unconfirmed_ready_is_visible_and_expires_without_delivery(self):
        old_run = self.now - timedelta(hours=2)
        store.prepare([self.source], 'browser', old_run)
        self.stage_batch([self.entry(age=121)], when=old_run)
        self.assertEqual(store.progress(self.source)['unconfirmed'], 1)
        payload = self.client.get('/api/intake/social').json()
        self.assertEqual(payload['sources'][0]['x_intake']['unconfirmed'], 1)
        store.prepare([self.source], 'browser', self.now)
        self.assertEqual(self.candidate()['state'], 'expired')
        self.assertEqual(store.progress(self.source)['unconfirmed'], 0)
        self.assertEqual(store.progress(self.source)['expired'], 1)
        self.assertEqual(store.delivery(self.source)[0], [])
        with reg.db() as conn:
            self.assertEqual(conn.execute('SELECT state FROM x_intake_batches').fetchone()[0], 'expired')

    def test_legacy_unbatched_ready_is_still_detected_and_recovered(self):
        self.seed_coverage()
        self.stage_batch([self.entry(age=72)])
        with reg.db() as conn:
            conn.execute('DELETE FROM x_intake_batch_posts')
            conn.execute('DELETE FROM x_intake_batches')
        phases = []

        def scan(*args, **kwargs):
            phases.append(kwargs['max_scrolls'])
            return [], ''

        summary = self.collect(scan)
        self.assertEqual(len(phases), 2)
        self.assertEqual(summary['added'], 1)
        self.assertEqual(store.progress(self.source)['unconfirmed'], 0)

    def test_expiring_one_unconfirmed_link_preserves_another_confirmed_outbox(self):
        other = self.sub('Other')
        old_run = self.now - timedelta(hours=2)
        store.prepare([self.source, other], 'browser', old_run)
        entries = [self.entry(age=121)]
        self.stage_batch(entries, when=old_run, batch_id='confirmed')
        self.stage_batch(entries, source=other, when=old_run, batch_id='unconfirmed')
        self.confirm_batch([], batch_id='confirmed')
        store.prepare([self.source, other], 'browser', self.now)
        self.assertEqual(self.candidate()['state'], 'ready')
        self.assertEqual(len(store.delivery(self.source)[0]), 1)
        self.assertEqual(store.delivery(other)[0], [])
        self.assertEqual(store.delivery(other)[3], 1)
        self.assertEqual(store.progress(other)['expired'], 1)
        self.assertEqual(store.progress(other)['unconfirmed'], 0)

    def test_late_rate_limit_recovers_three_day_old_ready_after_restart(self):
        for channel in ('browser', 'attach'):
            with self.subTest(channel=channel):
                self.options = self.settings(channel)
                self.seed_coverage()

                class LateRateLimit:
                    incomplete = ''

                    def finalize(self, result, bound):
                        self.result = result

                    def before_navigation(self):
                        self.result.clear()
                        self.result.update(entries=[], error='会话收尾429')
                        raise XRateLimited('会话收尾429')

                identifier = '701' if channel == 'browser' else '702'

                def scan(page, handle, floor, **kwargs):
                    if kwargs['max_scrolls'] == 2:
                        return [], '发现尚未读完'
                    page._x_timeline = LateRateLimit()
                    return [self.entry(identifier, age=72)], ''

                summary = self.collect(scan)
                self.assertEqual(summary['added'], 0)
                self.assertEqual(store.progress(self.source)['unconfirmed'], 1)
                self.assertEqual(self.candidate(identifier)['state'], 'ready')
                from kb.feeds import x_channels
                x_channels.reset()
                phases = []

                def recover(*args, **kwargs):
                    phases.append(kwargs['max_scrolls'])
                    return [], ''

                recovered = self.collect(recover)
                self.assertEqual(len(phases), 2)
                self.assertEqual(recovered['added'], 1)
                self.assertEqual(store.progress(self.source)['unconfirmed'], 0)
                self.assertEqual(recovered['succeeded_ids'], [str(self.source['id'])])

    def test_unconfirmed_sources_have_history_priority(self):
        other = self.sub('Other')
        store.prepare([self.source, other], 'browser', self.now)
        self.stage_batch([self.entry(handle='Other', age=72)], source=other)
        with reg.db() as conn:
            conn.execute('UPDATE x_intake_sources SET history_at=999 WHERE source_id=?', (other['id'],))
        history = []

        def scan(page, handle, floor, **kwargs):
            if kwargs['max_scrolls'] != 2:
                history.append(handle)
            return [], ''

        self.collect(scan)
        self.assertEqual(history[0], 'other')

    def test_partial_history_cannot_recover_an_unseen_old_candidate(self):
        self.seed_coverage()
        self.stage_batch([self.entry(age=72)])
        summary = self.collect(lambda *args, **kwargs: ([], '历史未读完'))
        self.assertEqual(summary['added'], 0)
        self.assertEqual(store.progress(self.source)['unconfirmed'], 1)
        self.assertEqual(summary['succeeded_ids'], [])

    def test_feedgrab_full_window_receipt_recovers_old_ready(self):
        self.options = self.settings('feedgrab')
        store.prepare([self.source], 'feedgrab', self.now)
        self.stage_batch([self.entry(age=72)])
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_feedgrab, 'harvest', return_value={'example': {'entries': [], 'error': '', 'incomplete': ''}}):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual(summary['added'], 1)
        self.assertEqual(store.progress(self.source)['unconfirmed'], 0)


class DiagnosticAndDebtTests(ReceiptBase):
    def test_real_page_errors_remain_failures_even_after_slice_expires(self):
        for error_type in (XBrowserError, CdpError):
            with self.subTest(error_type=error_type.__name__):
                message = '@example 页面结构看不懂了，X 可能改版了；页面没有加载完'

                def scan(*args, **kwargs):
                    time.sleep(max(0, kwargs['deadline'] - time.monotonic()) + 0.01)
                    raise error_type(message)

                with patch.dict('os.environ', {'KB_X_ACCOUNT_SECONDS': '0.1'}):
                    summary = self.collect(scan)
                self.assertEqual(summary['failed_ids'], [str(self.source['id'])])
                self.assertEqual(store.progress(self.source)['error'], message)
                source = reg.subscription('social', self.source['id'])
                self.assertIn(message, source['last_error'])
                self.assertGreater(source['retry_at'], time.time())

    def test_explicit_budget_error_preserves_original_without_penalizing_account(self):
        message = '当前账号切片预算到点：原始诊断'
        summary = self.collect(lambda *args, **kwargs: (_ for _ in ()).throw(XBudgetExceeded(message)))
        self.assertEqual(summary['failed_ids'], [])
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(store.progress(self.source)['error'], message)

    def test_body_debt_does_not_block_success_across_three_rounds(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET last_error='上周连接失败',failures=3,retry_at=42 WHERE id=?",
                         (self.source['id'],))
        with patch.object(x_post, 'complete', return_value=([], '正文待下轮补齐')):
            for index in range(3):
                summary = self.collect(lambda *args, **kwargs: ([self.entry(str(800 + index), truncated=True)], ''))
                self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
                source = reg.subscription('social', self.source['id'])
                self.assertEqual((source['last_error'], source['failures'], source['retry_at']), ('', 0, 0))
                self.assertEqual(store.delivery(self.source)[1], index + 1)


class DeadlineTypeTests(IntakeBase):
    def session(self, error):
        session = CdpSession()
        session._post = Mock()
        session._take = Mock(side_effect=error)
        return session

    def test_explicit_deadline_exhaustion_never_sends_a_command(self):
        session = self.session(CdpError('not reached'))
        session._x_deadline = time.monotonic() - 1
        with self.assertRaises(XBudgetExceeded):
            session.call('Runtime.evaluate')
        session._post.assert_not_called()

    def test_response_timeout_only_becomes_budget_when_budget_bound_the_call(self):
        for budget in (False, True):
            with self.subTest(budget=budget):
                session = self.session(CdpResponseTimeout('原始响应超时'))
                if budget:
                    session._x_deadline = time.monotonic() + 0.1
                with self.assertRaises(CdpError) as caught:
                    session.call('Runtime.evaluate', timeout=30)
                self.assertEqual(isinstance(caught.exception, XBudgetExceeded), budget)
                self.assertEqual(str(caught.exception), '原始响应超时')

    def test_protocol_error_is_never_relabelled_by_a_budget(self):
        session = self.session(CdpError('Runtime.evaluate 失败:execution context missing'))
        session._x_deadline = time.monotonic() + 0.1
        with self.assertRaises(CdpError) as caught:
            session.call('Runtime.evaluate')
        self.assertNotIsInstance(caught.exception, XBudgetExceeded)

    def test_pipe_and_attached_read_timeouts_are_structured(self):
        chrome = Chrome.__new__(Chrome)
        chrome._buf = b''
        with self.assertRaises(CdpResponseTimeout):
            chrome._take(time.monotonic() - 1)
        attached = cdp_attach.Attached.__new__(cdp_attach.Attached)
        attached._ws = SimpleNamespace(recv=Mock(side_effect=WsTimeoutError('响应超时')))
        with self.assertRaises(CdpResponseTimeout):
            attached._take(time.monotonic())
