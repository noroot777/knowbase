"""Only synthetic X/session boundaries; durable storage, sync commits and APIs are real."""
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels, x_cooldown, x_post
from kb.feeds import x_intake, x_intake_store as store
from kb.feeds.x_intake_metrics import Metrics
from kb.feeds.x_page_state import XRateLimited, XSessionThrottled
from kb.maintenance import LockBusy
from tests.test_x_channels import ChannelBase, tweet


class Session:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def open_page(self, url):
        pass


class IntakeBase(ChannelBase):
    def setUp(self):
        super().setUp()
        self.now = datetime.now(timezone.utc)
        self.source = self.sub()
        self.options = self.settings('browser')

    def entry(self, identifier='100', *, age=1, truncated=False, handle='Example'):
        raw = tweet(identifier, handle=handle, when=(self.now - timedelta(hours=age)).isoformat())
        raw['truncated'] = truncated
        return dict(x_browser._entry(raw), x_observed_at=raw['datetime'])

    def prepare(self, sources=None):
        store.prepare(sources or [self.source], 'browser', self.now)

    def stage(self, entries, source=None, *, missing='', phase='history', confirm=True):
        source = source or self.source
        bound = store.floor_for(source, self.now).isoformat()
        observed = x_browser._newest(entries, '')
        result = store.stage(source, entries, phase=phase, incomplete=missing or '等待收尾', observed=observed)
        if confirm:
            store.confirm(source, entries, phase=phase, incomplete=missing, observed=observed, boundary=bound,
                          end=self.now.isoformat(), coverage_complete=missing == '')
        return result

    def candidate(self, identifier='100'):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM x_intake_candidates WHERE post_id=?', (identifier,)).fetchone())

    def run_intake(self, scanner, sources=None, **kwargs):
        sources = sources or [self.source]
        with patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=scanner):
            return x_intake.harvest(sources, self.options, budget=480, **kwargs)


class WindowAndOutboxTests(IntakeBase):
    def test_five_day_boundary_is_frozen_and_inclusive(self):
        self.prepare()
        inside = self.entry(age=120)
        outside = self.entry('101', age=120.01)
        exact = self.now - timedelta(days=5)
        inside['published'] = exact.isoformat()
        inside['x_observed_at'] = exact.isoformat()
        self.stage([inside, outside])
        entries, pending, _, _ = store.delivery(self.source)
        self.assertEqual([entry['url'] for entry in entries], [inside['url']])
        self.assertEqual(pending, 0)
        self.assertEqual(store.utc(store.progress(self.source)['floor']), exact)

    def test_old_watermark_does_not_expand_five_day_history(self):
        with reg.db() as conn:
            reg.merge_config(self.source['id'], {'x_watermark_browser': '2026-01-01T00:00:00+00:00'}, conn=conn)
        source = reg.subscriptions('social')[0]
        floors = []

        def scan(page, handle, floor, **kwargs):
            floors.append(floor)
            return [self.entry()], ''

        self.run_intake(scan, [source])
        self.assertTrue(all(floor >= self.now - timedelta(days=5, seconds=2) for floor in floors))
        self.assertEqual(floors[0], floors[-1])

    def test_expired_work_is_not_success_or_deletion(self):
        self.prepare()
        preview = self.entry(age=119, truncated=True)
        self.stage([preview])
        reg.put_items(self.source, [self.entry('200', age=200)])
        store.prepare([self.source], 'browser', self.now + timedelta(hours=2))
        self.assertEqual(self.candidate()['state'], 'expired')
        self.assertEqual(store.pending([self.source]), [])
        self.assertEqual(store.delivery(self.source)[3], 1)
        with reg.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 1)

    def test_preview_never_reaches_feed_items_and_queue_survives_restart(self):
        self.prepare()
        self.stage([self.entry(truncated=True)])
        x_channels.reset()
        self.assertEqual(store.delivery(self.source)[:2], ([], 1))
        self.assertEqual(len(store.pending([self.source])), 1)
        with reg.db() as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM feed_items').fetchone()[0], 0)

    def test_late_response_confirmation_is_required_for_delivery(self):
        self.prepare()
        self.stage([self.entry()], confirm=False)
        self.assertEqual(store.delivery(self.source)[0], [])
        self.assertTrue(store.progress(self.source)['gap'])
        self.stage([self.entry()])
        self.assertEqual(len(store.delivery(self.source)[0]), 1)

    def test_failed_save_rolls_back_acknowledgement_and_watermark(self):
        self.prepare()
        self.stage([self.entry()])
        entries = store.delivery(self.source)[0]
        meta = dict(etag='', modified='', resolved_url='', x_intake=True,
                    config_patch={'x_watermark_browser': self.now.isoformat()})
        with patch.object(reg, 'put_items', side_effect=sqlite3.OperationalError('database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                sync._save_result(self.source, (entries, meta), FetchScope())
        self.assertEqual(len(store.delivery(self.source)[0]), 1)
        self.assertEqual(x_channels.watermark(reg.subscriptions('social')[0], 'browser'), '')
        sync._save_result(self.source, (entries, meta), FetchScope())
        self.assertEqual(store.delivery(self.source)[0], [])

    def test_revision_change_cannot_ack_or_stage_old_account(self):
        self.prepare()
        self.stage([self.entry()])
        with reg.db() as conn:
            conn.execute('UPDATE subscriptions SET rev=rev+1 WHERE id=?', (self.source['id'],))
        self.assertIsNone(store.stage(self.source, [self.entry('101')], phase='history', incomplete=''))
        self.assertIsNone(sync._save_result(self.source, (store.delivery(self.source)[0],
                         dict(etag='', modified='', resolved_url='', x_intake=True)), FetchScope()))
        fresh = reg.subscriptions('social')[0]
        self.prepare([fresh])
        self.assertEqual(store.delivery(fresh)[0], [])

    def test_subscription_specific_floor_is_preserved(self):
        boundary = self.now - timedelta(hours=3)
        self.source['history_floor'] = boundary.isoformat()
        with reg.db() as conn:
            conn.execute('UPDATE subscriptions SET history_floor=? WHERE id=?',
                         (boundary.isoformat(), self.source['id']))
        self.prepare()
        self.stage([self.entry(age=4), self.entry('101', age=2)])
        self.assertEqual(len(store.delivery(self.source)[0]), 1)
        self.assertEqual(store.utc(store.progress(self.source)['floor']), boundary)

    def test_expired_inflight_body_cannot_be_resurrected_by_late_completion(self):
        self.prepare()
        preview = self.entry(age=119, truncated=True)
        self.stage([preview])
        source, task = store.pending([self.source])[0]
        store.prepare([self.source], 'browser', self.now + timedelta(hours=2))
        store.finish(source, task, [x_post.merge(preview, 'a thought with a complete expanded body')], [], '')
        self.assertEqual(self.candidate()['state'], 'expired')

    def test_old_coverage_does_not_hide_a_gap_after_days_of_downtime(self):
        old_run = self.now - timedelta(days=3)
        store.prepare([self.source], 'browser', old_run)
        entry = self.entry(age=73)
        boundary = store.progress(self.source)['floor']
        store.stage(self.source, [entry], phase='history', incomplete='等待确认')
        store.confirm(self.source, [entry], phase='history', incomplete='', observed=store.observed([entry]),
                      boundary=boundary, end=old_run.isoformat(), coverage_complete=True)
        phases = []

        def scan(page, handle, floor, **kwargs):
            phases.append(kwargs['max_scrolls'])
            return [self.entry()], '发现阶段尚未翻完' if kwargs['max_scrolls'] == 2 else '尚未翻过缺失的两天'

        result = self.run_intake(scan)
        self.assertEqual(phases, [2, x_browser.MAX_SCROLLS])
        self.assertTrue(store.progress(self.source)['gap'])
        self.assertIn('缺失的两天', result['example']['incomplete'])


class BodyProofTests(IntakeBase):
    def test_completed_body_is_reused_before_detail_on_later_round(self):
        self.prepare()
        preview = self.entry(truncated=True)
        self.stage([preview])
        source, task = store.pending([self.source])[0]
        full = x_post.merge(preview, 'a thought with the complete expanded body')
        store.finish(source, task, [full], [], '')
        reg.put_items(self.source, [full])
        with patch.object(x_post, 'complete', side_effect=AssertionError('must reuse')):
            result = self.run_intake(lambda *args, **kwargs: ([preview], ''))
        self.assertEqual(result['example']['entries'][0]['body'], full['body'])
        self.assertEqual(store.pending([self.source]), [])

    def test_legacy_or_forged_body_is_not_early_reuse(self):
        self.prepare()
        preview = self.entry(truncated=True)
        legacy = dict(self.entry(), x_post_meta='')
        reg.put_items(self.source, [legacy])
        self.stage([preview])
        with reg.db() as conn:
            self.assertIsNone(store.existing(conn, preview))
        full = x_post.merge(preview, 'a thought with a safely verified complete body')
        source, task = store.pending([self.source])[0]
        store.finish(source, task, [full], [], '')
        self.assertEqual(self.candidate()['state'], 'repair')
        self.assertEqual(store.delivery(self.source)[:2], ([], 1))

    def test_permanent_exclusion_prevents_completion(self):
        self.prepare()
        preview = self.entry(truncated=True)
        with reg.db() as conn:
            conn.execute('INSERT INTO feed_item_exclusions VALUES(?,?,?,?,?)',
                         (reg.item_key('social', preview['url']), 'social', preview['url'], '', reg.now()))
        self.stage([preview])
        self.assertEqual(self.candidate()['state'], 'excluded')
        self.assertEqual(store.pending([self.source]), [])

    def test_body_failure_does_not_erase_discovery_or_penalize_subscription(self):
        self.prepare()
        self.stage([self.entry(truncated=True)])
        source, task = store.pending([self.source])[0]
        before = reg.subscriptions('social')[0]
        store.finish(source, task, [], [], '详情加载超时')
        self.assertEqual(self.candidate()['failures'], 1)
        self.assertTrue(self.candidate()['retry_at'] > time.time())
        after = reg.subscriptions('social')[0]
        for field in ('failures', 'last_error', 'retry_at', 'last_success'):
            self.assertEqual(before[field], after[field])
        self.assertFalse(store.progress(self.source)['gap'])

    def test_verified_unavailable_does_not_requeue_on_rescan(self):
        self.prepare()
        preview = self.entry(truncated=True)
        self.stage([preview])
        source, task = store.pending([self.source])[0]
        store.finish(source, task, [], [dict(url=preview['url'], reason='明确不存在')], '明确不存在')
        self.stage([preview])
        self.assertEqual(self.candidate()['state'], 'unavailable')
        self.assertEqual(len(store.delivery(self.source)[2]), 1)

    def test_shared_post_identity_is_a_single_body_job(self):
        other = self.sub('Other')
        self.prepare([self.source, other])
        preview = self.entry(truncated=True)
        self.stage([preview])
        self.stage([preview], other)
        self.assertEqual(len(store.pending([self.source, other])), 1)
        source, task = store.pending([self.source, other])[0]
        store.finish(source, task, [x_post.merge(preview, 'a thought with expanded text')], [], '')
        self.assertEqual(len(store.delivery(other)[0]), 1)


class SchedulingTests(IntakeBase):
    def test_all_accounts_discovered_before_any_body_navigation(self):
        sources = [self.source, self.sub('Other'), self.sub('Third')]
        calls = []

        def scan(page, handle, floor, **kwargs):
            calls.append(('scan', handle))
            return [self.entry(str(100 + len(calls)), handle=handle, truncated=True)], ''

        def body(entries, **kwargs):
            calls.append(('body', entries[0]['url']))
            return [x_post.merge(entries[0], 'a thought complete expanded body')], ''

        with patch.object(x_post, 'complete', side_effect=body):
            self.run_intake(scan, sources)
        first_body = next(index for index, call in enumerate(calls) if call[0] == 'body')
        self.assertGreaterEqual(first_body, len(sources))
        self.assertEqual({call[1] for call in calls[:3]}, {'example', 'other', 'third'})

    def test_body_jobs_round_robin_accounts(self):
        other = self.sub('Other')
        self.prepare([self.source, other])
        self.stage([self.entry(str(100 + index), truncated=True) for index in range(3)])
        self.stage([self.entry('200', handle='Other', truncated=True)], other)
        ordered = store.pending([self.source, other])
        self.assertEqual([source['id'] for source, _ in ordered[:2]], [self.source['id'], other['id']])

    def test_cancel_does_not_launch_browser_or_advance_progress(self):
        with patch.object(x_browser, 'browser_session', side_effect=AssertionError('must not launch')):
            results = x_intake.harvest([self.source], self.options, budget=480, should_stop=lambda: True)
        self.assertIn('停止', results['example']['skipped'])
        self.assertEqual(store.progress(self.source)['discovery'], 'unattempted')

    def test_small_global_budget_does_not_launch_useless_work(self):
        with patch.object(x_browser, '_harvest_one', side_effect=AssertionError('no budget')), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''):
            results = x_intake.harvest([self.source], self.options, budget=1)
        self.assertIn('未轮到', results['example']['skipped'])
        self.assertEqual(store.latest_metrics()['counts']['accounts_attempted'], 0)

    def test_startup_throttling_retries_finitely_and_keeps_shared_gate(self):
        calls = []

        def scan(*args, **kwargs):
            calls.append(True)
            if len(calls) == 1:
                raise XSessionThrottled('启动请求 429')
            return [self.entry()], ''

        with patch.object(x_cooldown, 'of', return_value=x_cooldown.SessionCooldown(first=0.001, maximum=0.002)):
            self.run_intake(scan)
        report = store.latest_metrics()
        self.assertEqual(report['rate_hits'], 1)
        self.assertGreater(report['phases']['discovery']['cooldown_seconds'], 0)
        self.assertLess(report['phases']['discovery']['cooldown_seconds'], report['phases']['discovery']['wall_seconds'])

    def test_insufficient_cooldown_budget_yields_without_sleep(self):
        intake = x_intake.Intake([self.source], self.options, budget=10)
        intake.cool = x_cooldown.SessionCooldown(first=45)
        intake.cool.throttled()
        with patch.object(intake.cool, 'wait', side_effect=AssertionError('must yield')):
            self.assertFalse(intake.wait(time.monotonic() + 4, 'body'))

    def test_content_rate_limit_stops_batch_without_unattempted_failures(self):
        other = self.sub('Other')
        result = self.run_intake(lambda *args, **kwargs: (_ for _ in ()).throw(XRateLimited('内容源429')), [self.source, other])
        self.assertTrue(all(not value.get('error') for value in result.values()))
        self.assertIn('429', result['example']['skipped'])
        self.assertNotIn('429', result['other']['skipped'])
        self.assertIn('429', store.latest_metrics()['blocked'])
        self.assertEqual(store.latest_metrics()['counts']['accounts_unattempted'], 1)

    def test_typed_local_write_failure_preserves_account_backoff(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET failures=3,last_error='旧错误',retry_at=42 WHERE id=?", (self.source['id'],))
        with patch.object(store, 'stage', side_effect=LockBusy('busy')):
            result = self.run_intake(lambda *args, **kwargs: ([self.entry()], ''))
        self.assertFalse(result['example']['error'])
        self.assertTrue(result['example']['local_error'])
        after = reg.subscriptions('social')[0]
        self.assertEqual((after['failures'], after['last_error'], after['retry_at']), (3, '旧错误', 42))

    def test_oldest_scan_attempt_is_prioritized_independently_of_body_failure(self):
        other = self.sub('Other')
        self.prepare([self.source, other])
        store.attempt(self.source, 'discovery')
        order = []

        def scan(page, handle, *args, **kwargs):
            order.append(handle)
            return [], ''

        self.run_intake(scan, [self.source, other])
        self.assertEqual(order[0], 'other')


class IntegrationTests(IntakeBase):
    def test_partial_save_keeps_old_success_failure_and_backoff(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET failures=2,last_error='旧错',last_success='旧成功',retry_at=42 WHERE id=?",
                         (self.source['id'],))
        self.prepare()
        self.stage([self.entry()])
        meta = dict(etag='', modified='', resolved_url='', x_intake=True, x_pending=True, note='正文待补',
                    config_patch={'x_watermark_browser': self.now.isoformat()})
        sync._save_result(self.source, (store.delivery(self.source)[0], meta), FetchScope())
        source = reg.subscriptions('social')[0]
        self.assertEqual((source['failures'], source['last_error'], source['last_success'], source['retry_at']),
                         (2, '旧错', '旧成功', 42))
        self.assertEqual(source['last_note'], '正文待补')
        self.assertTrue(x_channels.watermark(source, 'browser'))

    def test_api_exposes_persistent_status_and_timings_after_cache_reset(self):
        with patch.object(x_post, 'complete', return_value=([], '详情未补到')):
            self.run_intake(lambda *args, **kwargs: ([self.entry(truncated=True)], ''), should_stop=lambda: False)
        x_channels.reset()
        response = self.client.get('/api/intake/social')
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertGreater(data['sources'][0]['x_intake']['pending'], 0)
        self.assertFalse(data['x_intake']['native_cursor_verified'])
        self.assertIn('discovery', data['x_intake']['phases'])

    def test_ranged_collection_stays_on_existing_path_without_watermark(self):
        with patch.object(x_browser, 'harvest', return_value={'example': dict(entries=[], error='', watermark='wrong')}), \
             patch.object(x_intake, 'harvest', side_effect=AssertionError('automatic only')):
            x_channels.prefetch([self.source], self.options, since='2026-10-01')
        self.assertNotIn('config_patch', x_channels.take(self.source, self.options)[1])

    def test_api_does_not_relabel_metrics_from_another_channel(self):
        self.run_intake(lambda *args, **kwargs: ([self.entry()], ''))
        self.assertEqual(self.client.get('/api/intake/social').json()['x_intake']['channel'], 'browser')
        self.settings('feedgrab')
        self.assertIsNone(self.client.get('/api/intake/social').json()['x_intake'])

    def test_explicit_range_collect_does_not_adopt_previous_automatic_metrics(self):
        self.run_intake(lambda *args, **kwargs: ([self.entry()], ''))
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'harvest', return_value={'example': dict(entries=[], error='')}):
            summary = sync.collect('social', FetchScope(since=(self.now - timedelta(days=1)).date().isoformat(),
                                                       until=self.now.date().isoformat(), force=True))
        self.assertNotIn('x_intake', summary)
        data = self.client.get('/api/intake/social').json()
        self.assertTrue(data['x_intake'])
        self.assertNotIn('x_intake', data['last_run']['summary'])

    def test_real_collect_pending_body_does_not_block_discovery_success(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET failures=2,last_error='旧错',last_success='旧成功' WHERE id=?",
                         (self.source['id'],))
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', return_value=([self.entry(truncated=True)], '')), \
             patch.object(x_post, 'complete', return_value=([], '正文未补到')):
            summary = sync.collect('social', FetchScope(force=True))
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(summary['failed_ids'], [])
        self.assertEqual(summary['deferred'], 0)
        self.assertTrue(summary['x_intake'])
        source = reg.subscriptions('social')[0]
        self.assertNotEqual(source['last_success'], '旧成功')
        self.assertEqual((source['last_error'], source['failures']), ('', 0))
        self.assertEqual(store.delivery(self.source)[1], 1)

    def test_local_failure_keeps_old_error_and_exposes_current_note_after_cache_reset(self):
        with reg.db() as conn:
            conn.execute("UPDATE subscriptions SET failures=2,last_error='旧错',last_success='旧成功' WHERE id=?",
                         (self.source['id'],))
        with patch.object(reg, 'configuration_error', return_value=''), \
             patch.object(x_browser, 'browser_session', return_value=Session()), \
             patch.object(x_browser, 'release_login_window', return_value=''), \
             patch.object(x_browser, '_harvest_one', side_effect=LockBusy('本机候选写入被占用')):
            summary = sync.collect('social', FetchScope(force=True))
        x_channels.reset()
        response = self.client.get('/api/intake/social')
        self.assertEqual(response.status_code, 200)
        source = response.json()['sources'][0]
        self.assertEqual((source['last_success'], source['last_error'], source['failures']), ('旧成功', '旧错', 2))
        self.assertIn('未计入账号连接失败', source['last_note'])
        self.assertEqual(summary['failed_ids'], [])
        self.assertTrue(summary['local_error'])
        self.assertTrue(response.json()['x_intake']['local_errors'])

    def test_late_core_error_cannot_confirm_earlier_candidates(self):
        other = self.sub('Other')
        retained = []

        class Timeline:
            def finalize(self, result, bound):
                retained.append(result)

            def before_navigation(self):
                for result in retained:
                    result.clear()
                    result.update(entries=[], error='迟到内容源429')

        def scan(page, handle, *args, **kwargs):
            page._x_timeline = Timeline()
            return [self.entry(handle=handle)], ''

        result = self.run_intake(scan, [self.source, other])
        self.assertTrue(all(not item['entries'] for item in result.values()))
        self.assertEqual(store.delivery(self.source)[0], [])
        self.assertTrue(store.progress(self.source)['gap'])

    def test_feedgrab_uses_same_window_outbox_and_budget_without_article_in_scanner(self):
        self.options = self.settings('feedgrab')
        options = []

        def harvest(jobs, **kwargs):
            options.append((jobs, kwargs))
            return {'example': dict(entries=[self.entry()], error='', incomplete='')}

        with patch('kb.feeds.x_feedgrab.harvest', side_effect=harvest):
            result = x_intake.harvest([self.source], self.options, budget=480)
        self.assertEqual(len(result['example']['entries']), 1)
        self.assertFalse(options[0][1]['complete_articles'])
        self.assertLessEqual(options[0][1]['budget'], 240)
        self.assertGreaterEqual(store.utc(options[0][0][0][1]), self.now - timedelta(days=5, seconds=1))
        self.assertFalse(store.latest_metrics()['timeline_phases_separable'])

    def test_expired_network_deadline_still_commits_certified_outbox(self):
        self.prepare()
        self.stage([self.entry()])
        meta = dict(etag='', modified='', resolved_url='', x_intake=True, x_deadline=time.monotonic() - 1)
        sync._save_result(self.source, (store.delivery(self.source)[0], meta), FetchScope())
        self.assertEqual(store.delivery(self.source)[0], [])
        self.assertEqual(len(reg.items('social')['items']), 1)

    def test_telemetry_counts_wait_as_subset_not_an_extra_stage(self):
        metrics = Metrics(10)
        with metrics.phase('discovery'):
            metrics.data['phases']['discovery'] = dict(cooldown_seconds=0.001)
        report = metrics.finish()
        self.assertNotIn('network_seconds', report)
        self.assertLess(report['elapsed_seconds'], 1)
        self.assertNotIn('cooldown', report['phases'])
