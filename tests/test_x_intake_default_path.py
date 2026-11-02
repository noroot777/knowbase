"""Behavioral invariants follow the actual default entry, not the obsolete bulk stub."""
import json
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import skipUnless
from unittest.mock import patch

from kb.feeds import registry as reg, x_browser, x_channels, x_intake, x_page_state, x_post
from kb.feeds import x_intake_store as store
from tests.test_x_channels import tweet
from tests.test_x_intake_review_runtime import RuntimeFixture


class DefaultPathContracts(RuntimeFixture):
    @skipUnless(shutil.which('node'), 'JavaScript runtime required for status contract')
    def test_browser_status_discriminates_empty_and_typed_block_records(self):
        script = (Path(__file__).resolve().parents[1] / 'kb/static/js/x_intake_status.js').read_text()
        script += '''
const esc = text => text;
const absent = [undefined, null, {}].map(block => xIntakeStatus({x_intake: {discovery: 'complete', block}}));
const present = Object.fromEntries(['throttled', 'rate', 'challenge', 'account', 'login', 'session'].map(kind =>
  [kind, xIntakeStatus({x_intake: {discovery: 'partial', block: {kind, reason: '原话'}}})]));
console.log(JSON.stringify({absent, present}));
'''
        result = subprocess.run(['node', '-e', script], capture_output=True, text=True, check=True, timeout=10)
        statuses = json.loads(result.stdout)
        for status in statuses['absent']:
            self.assertIn('最新发现已完成', status)
            self.assertNotIn('访问受阻', status)
        for kind, label in dict(throttled='启动限流', rate='访问限流', challenge='安全验证',
                                account='账号不可用', login='登录失效', session='会话不可用').items():
            self.assertIn(label, statuses['present'][kind])

    def test_default_collect_uses_the_incremental_entry_and_current_scanner_signature(self):
        calls = []
        def scan(page, handle, floor, **kwargs):
            calls.append(kwargs)
            return [self.entry()], ''
        with patch.object(x_browser, 'harvest', side_effect=AssertionError('不得调用旧整批入口')):
            summary = self.collect_runtime(scan)
        self.assertEqual(summary['added'], 1)
        self.assertTrue(calls)
        self.assertTrue(all({'deadline', 'max_scrolls', 'should_stop', 'on_ready', 'precise'} <= set(call) for call in calls))

    def test_incomplete_discovery_saves_content_without_advancing_its_watermark(self):
        before = (self.now - timedelta(hours=4)).isoformat()
        with reg.db() as conn:
            reg.merge_config(self.source['id'], {'x_watermark_browser': before}, conn=conn)
        summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry()], '时间线未到底'))
        self.assertEqual(summary['added'], 1)
        source = reg.subscription('social', self.source['id'])
        self.assertEqual(x_channels.watermark(source, 'browser'), before)
        self.assertIn('未到底', source['last_note'])

    def test_folded_posts_still_expand_on_the_matching_post_before_delivery(self):
        entry = self.entry(truncated=True)
        body = entry['body'] + '\n匹配详情页展开后的完整正文'
        with patch.object(x_post, 'fetch', return_value=body) as detail:
            summary = self.collect_runtime(lambda *args, **kwargs: ([entry], ''))
        self.assertEqual(summary['added'], 1)
        detail.assert_called_once()
        self.assertEqual(detail.call_args.args[0], entry['url'])
        self.assertEqual(reg.items('social')['items'][0]['body'], body)

    def test_bodyless_quote_still_cannot_impersonate_the_quoted_author(self):
        raw = tweet('200', when=self.now.isoformat(), text='')
        raw['quoted'] = True
        entries = x_browser._select([raw], self.now - timedelta(days=1), 'example')
        self.assertEqual(entries, [])
        self.assertEqual(entries.skipped_quotes, 1)
        summary = self.collect_runtime(lambda *args, **kwargs: (entries, ''))
        self.assertEqual(summary['added'], 0)
        self.assertEqual(reg.items('social')['total'], 0)

    def test_source_identity_change_still_invalidates_inflight_content(self):
        changed = False
        def scan(*args, **kwargs):
            nonlocal changed
            if not changed:
                saved = reg.save_subscription('social', {'name': 'Other', 'url': 'https://x.com/Other'},
                                              self.source['id'])
                self.assertEqual(saved['id'], self.source['id'])
                changed = True
            return [self.entry()], ''
        summary = self.collect_runtime(scan)
        self.assertEqual(summary['added'], 0)
        self.assertEqual(reg.items('social')['total'], 0)
        self.assertEqual(reg.subscription('social', self.source['id'])['url'], 'https://x.com/other')

    def test_attach_channel_uses_the_same_incremental_proofs_and_delivery(self):
        summary = self.collect_runtime(lambda *args, **kwargs: ([self.entry()], ''), channel='attach')
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertEqual(store.progress(self.source)['discovery'], 'complete')

    def test_a_late_content_limit_keeps_the_batch_unconfirmed_and_diagnosable(self):
        error = x_page_state.XRateLimited('迟到核心429')
        def finalize(result, boundary):
            result.clear()
            result.update(entries=[], error=str(error))
        def scan(page, handle, floor, **kwargs):
            page._x_timeline = SimpleNamespace(finalize=finalize, before_navigation=lambda: None,
                                                blocker=error, incomplete=str(error))
            return [self.entry()], ''
        summary = self.collect_runtime(scan)
        self.assertEqual(summary['added'], 0)
        self.assertEqual(summary['succeeded_ids'], [])
        self.assertEqual(store.progress(self.source)['unconfirmed'], 1)
        self.assertEqual(store.progress(self.source)['block']['kind'], 'rate')

    def test_boot_throttle_retries_the_source_and_a_success_clears_its_block(self):
        attempts = []
        def scan(*args, **kwargs):
            attempts.append(True)
            if len(attempts) == 1:
                raise x_page_state.XSessionThrottled('首次启动限流')
            return [self.entry()], ''
        summary = self.collect_runtime(scan)
        self.assertGreaterEqual(len(attempts), 2)
        self.assertEqual(summary['added'], 1)
        self.assertEqual(summary['failed_ids'], [])
        self.assertEqual(summary['succeeded_ids'], [str(self.source['id'])])
        self.assertFalse(store.progress(self.source)['block'])

    def test_explicit_date_entry_is_still_independent_of_the_automatic_implementation(self):
        with patch.object(x_browser, 'harvest', return_value={'example': dict(entries=[], error='', watermark='wrong')}), \
             patch.object(x_intake, 'harvest', side_effect=AssertionError('日期区间不得走自动入口')):
            x_channels.prefetch([self.source], self.options, since='2026-10-01')
        self.assertNotIn('config_patch', x_channels.take(self.source, self.options)[1])
