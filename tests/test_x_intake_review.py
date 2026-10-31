"""Current-run receipts and bounded history are independent success contracts."""
import subprocess
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_cooldown, x_feedgrab, x_post
from kb.feeds import x_intake_store as store
from kb.feeds.x_cdp import CdpError
from kb.feeds.x_page_state import XBrowserError, XSessionThrottled
from tests.test_x_intake import IntakeBase, Session


class CurrentRunTests(IntakeBase):
    def seed(self, channel='browser'):
        store.prepare([self.source], channel, self.now)
        self.stage([self.entry()])
        self.assertEqual(store.progress(self.source)['gap'], '')
        with reg.db() as conn:
            conn.execute("""UPDATE subscriptions SET last_success='原有成功',last_error='上周失败',
                failures=3,retry_at=42 WHERE id=?""", (self.source['id'],))
        return self.ledger()

    def ledger(self):
        source = reg.subscription('social', self.source['id'])
        return {key: source[key] for key in ('last_success', 'last_error', 'failures', 'retry_at')}

    def collect_browser(self, scanner):
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=scanner), \
             patch.object(x_cooldown, 'of', return_value=x_cooldown.SessionCooldown(first=0, maximum=0)):
            return sync.collect('social', FetchScope(force=True))

    def collect_feedgrab(self, result):
        self.options = self.settings('feedgrab')
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_feedgrab, 'harvest', return_value={'example': result}):
            return sync.collect('social', FetchScope(force=True))

    def test_zero_reads_after_prior_coverage_never_clear_ledger_for_browser_or_attach(self):
        for channel in ('browser', 'attach'):
            for error in (CdpError, XBrowserError):
                with self.subTest(channel=channel, error=error.__name__):
                    self.options = self.settings(channel)
                    before = self.seed()
                    with patch.dict('os.environ', {'KB_X_ACCOUNT_SECONDS': '0.1'}):
                        summary = self.collect_browser(lambda *args, **kwargs: (_ for _ in ()).throw(error('切片超时')))
                    self.assertEqual(self.ledger(), before)
                    self.assertEqual(summary['succeeded_ids'], [])
                    self.assertEqual(summary['failed_ids'], [])
                    self.assertNotEqual(store.progress(self.source)['discovery'], 'complete')
                    self.assertIn('预算', store.progress(self.source)['error'])

    def test_repeated_startup_throttling_after_prior_coverage_is_not_success(self):
        before = self.seed()
        summary = self.collect_browser(lambda *args, **kwargs: (_ for _ in ()).throw(XSessionThrottled('启动请求429')))
        self.assertEqual(self.ledger(), before)
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(summary['failed_ids'], [])
        self.assertGreater(summary['x_intake']['rate_hits'], 1)
        self.assertIn('429', store.progress(self.source)['error'])

    def test_saved_outbox_can_deliver_without_claiming_a_current_remote_read(self):
        before = self.seed()
        with patch.dict('os.environ', {'KB_X_ACCOUNT_SECONDS': '0.1'}):
            summary = self.collect_browser(lambda *args, **kwargs: (_ for _ in ()).throw(CdpError('预算超时')))
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(self.ledger(), before)
        self.assertIn('预算', reg.subscription('social', self.source['id'])['last_note'])

    def test_bodyless_empty_partial_read_cannot_clear_old_failure(self):
        before = self.seed()
        summary = self.collect_browser(lambda *args, **kwargs: ([], '预算用完，尚未读到内容'))
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(self.ledger(), before)

    def test_proven_empty_timeline_is_a_real_success(self):
        self.seed()
        summary = self.collect_browser(lambda *args, **kwargs: ([], ''))
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(self.ledger()['failures'], 0)

    def test_fresh_history_receipt_can_recover_a_failed_discovery_attempt(self):
        self.seed()

        def scan(*args, **kwargs):
            if kwargs['max_scrolls'] == 2:
                raise CdpError('发现切片超时')
            return [self.entry('301')], ''

        with patch.dict('os.environ', {'KB_X_ACCOUNT_SECONDS': '0.1'}):
            summary = self.collect_browser(scan)
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(self.ledger()['last_error'], '')

    def test_history_gap_does_not_block_success_across_three_real_collects(self):
        self.seed()
        with reg.db() as conn:
            conn.execute("UPDATE x_intake_sources SET covered='',coverage_end='',gap='五天历史尚未覆盖' WHERE source_id=?",
                         (self.source['id'],))
        for round_number in range(3):
            def scan(*args, **kwargs):
                history = kwargs['max_scrolls'] != 2
                return [self.entry(str(400 + round_number * 2 + int(history)))], '五天历史仍未翻完' if history else ''

            summary = self.collect_browser(scan)
            self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
            self.assertEqual(summary['deferred'], 0)
            self.assertGreaterEqual(summary['added'], 2)
            self.assertEqual((self.ledger()['last_error'], self.ledger()['failures'], self.ledger()['retry_at']), ('', 0, 0))
            self.assertNotEqual(self.ledger()['last_success'], '原有成功')
            self.assertIn('五天历史', store.progress(self.source)['gap'])
            self.assertIn('五天历史', reg.subscription('social', self.source['id'])['last_note'])

    def test_accepted_posts_succeed_when_untrusted_quotes_only_leave_a_notice(self):
        self.seed()
        summary = self.collect_browser(lambda *args, **kwargs: ([self.entry('501')], '跳过来源无法确认的引用推'))
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(self.ledger()['last_error'], '')
        self.assertIn('引用推', reg.subscription('social', self.source['id'])['last_note'])

    def test_pending_body_still_keeps_old_failure_even_when_latest_read_succeeds(self):
        before = self.seed()
        with patch.object(x_post, 'complete', return_value=([], '详情尚未补全')):
            summary = self.collect_browser(lambda *args, **kwargs: ([self.entry('502', truncated=True)], '历史缺口'))
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(self.ledger(), before)
        self.assertEqual(store.delivery(self.source)[1], 1)
        self.assertEqual(store.pending([self.source]), [])

    def test_current_core_request_error_is_not_downgraded_to_a_history_notice(self):
        before = self.seed()

        class Timeline:
            incomplete = '当前核心请求失败'

            def finalize(self, result, bound):
                result['incomplete'] = self.incomplete

            def before_navigation(self):
                pass

        def scan(page, *args, **kwargs):
            page._x_timeline = Timeline()
            return [self.entry('503')], ''

        summary = self.collect_browser(scan)
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(self.ledger(), before)
        self.assertIn('核心请求失败', reg.subscription('social', self.source['id'])['last_note'])

    def test_feedgrab_skip_never_confirms_coverage_or_clears_a_previous_failure(self):
        before = self.seed('feedgrab')
        old = store.progress(self.source)
        summary = self.collect_feedgrab({'entries': [], 'error': '', 'skipped': '已按停止请求中断'})
        current = store.progress(self.source)
        self.assertEqual((current['covered'], current['coverage_end']), (old['covered'], old['coverage_end']))
        self.assertNotEqual(current['discovery'], 'complete')
        self.assertEqual(self.ledger(), before)
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(summary['failed_ids'], [])
        self.assertIn('停止', current['error'])

    def test_feedgrab_missing_completeness_field_is_not_coverage_evidence(self):
        self.options = self.settings('feedgrab')
        summary = self.collect_feedgrab({'entries': [], 'error': ''})
        progress = store.progress(self.source)
        self.assertEqual(progress['covered'], '')
        self.assertTrue(progress['gap'])
        self.assertEqual(summary['succeeded_ids'], [])

    def test_feedgrab_clean_explicit_completion_can_confirm_coverage(self):
        self.options = self.settings('feedgrab')
        summary = self.collect_feedgrab({'entries': [self.entry()], 'error': '', 'incomplete': ''})
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(store.progress(self.source)['gap'], '')

    def test_feedgrab_accepted_content_can_succeed_without_history_completion(self):
        self.seed('feedgrab')
        summary = self.collect_feedgrab({'entries': [self.entry('601')], 'error': '', 'incomplete': '远端历史未读完'})
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(self.ledger()['last_error'], '')
        self.assertIn('未读完', store.progress(self.source)['gap'])

    def test_feedgrab_skipped_round_does_not_poison_the_next_history_scan(self):
        self.options = self.settings('feedgrab')
        self.collect_feedgrab({'entries': [], 'error': '', 'skipped': '时间预算不足'})
        self.assertEqual(store.progress(self.source)['covered'], '')
        scanned = []

        def harvest(jobs, **kwargs):
            scanned.extend(jobs)
            return {'example': {'entries': [self.entry('602', age=96)], 'error': '', 'incomplete': ''}}

        with patch.object(reg, 'configuration_error', return_value=''), patch.object(x_feedgrab, 'harvest', side_effect=harvest):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertLessEqual(store.utc(scanned[0][1]), self.now - timedelta(days=4))


class CoverageAndNoticeTests(IntakeBase):
    def test_confirm_requires_explicit_evidence_even_when_incomplete_is_empty(self):
        self.prepare()
        entry = self.entry()
        self.stage([entry], confirm=False)
        store.confirm(self.source, [entry], phase='history', incomplete='', observed=store.observed([entry]),
                      boundary=store.floor_for(self.source, self.now).isoformat())
        self.assertEqual(store.progress(self.source)['covered'], '')
        self.assertTrue(store.progress(self.source)['gap'])

    def test_complete_flag_without_a_completeness_result_does_not_advance(self):
        self.prepare()
        store.confirm(self.source, [], phase='discovery', observed='', coverage_complete=True,
                      boundary=store.floor_for(self.source, self.now).isoformat())
        self.assertEqual(store.progress(self.source)['covered'], '')
        self.assertEqual(store.progress(self.source)['discovery'], 'partial')

    def test_rolling_floor_with_a_prior_gap_does_not_claim_content_expired(self):
        self.prepare()
        self.stage([self.entry()], missing='历史尚未覆盖')
        store.prepare([self.source], 'browser', self.now + timedelta(seconds=1))
        self.assertEqual(store.progress(self.source)['expired'], 0)
        self.assertEqual(store.progress(self.source)['outside'], '')

    def test_old_watermark_without_expired_work_does_not_claim_lost_content(self):
        with reg.db() as conn:
            reg.merge_config(self.source['id'], {'x_watermark_browser': '2026-01-01T00:00:00+00:00'}, conn=conn)
        source = reg.subscription('social', self.source['id'])
        store.prepare([source], 'browser', self.now)
        self.assertEqual(store.progress(source)['outside'], '')

    def test_expiration_notice_uses_shanghai_date_and_real_expired_count(self):
        self.now = datetime(2026, 10, 31, 20, 30, tzinfo=timezone.utc)
        store.prepare([self.source], 'browser', self.now - timedelta(hours=2))
        self.stage([self.entry(age=121, truncated=True)])
        store.prepare([self.source], 'browser', self.now)
        progress = store.progress(self.source)
        self.assertEqual(progress['expired'], 1)
        self.assertEqual(progress['outside'], '2026-10-27 以前的 1 条待办已超出自动窗口，未计为完成')
        self.assertNotIn('T', progress['outside'])
        self.assertNotIn('+00:00', progress['outside'])

    def test_stale_legacy_outside_text_is_cleared_when_nothing_expired(self):
        self.prepare()
        with reg.db() as conn:
            conn.execute("UPDATE x_intake_sources SET outside='旧机器时间戳' WHERE source_id=?", (self.source['id'],))
        self.prepare()
        self.assertEqual(store.progress(self.source)['outside'], '')


class FeedgrabTimeoutTests(IntakeBase):
    def test_timeout_message_reports_the_actual_effective_limit(self):
        for configured in (25, 0.25, None):
            effective = x_feedgrab.ACCOUNT_SECONDS if configured is None else configured
            with self.subTest(timeout=configured), patch.object(x_feedgrab.subprocess, 'run',
                    side_effect=subprocess.TimeoutExpired('feedgrab', effective)) as run:
                with self.assertRaisesRegex(x_feedgrab.FeedgrabError, f'超过 {effective:g} 秒没有返回'):
                    x_feedgrab._run('feedgrab', 'example', {}, self.root, timeout=configured)
                self.assertEqual(run.call_args.kwargs['timeout'], effective)
