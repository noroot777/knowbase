"""Audit checkpoints survive bounded reruns without hiding changed rows or retry debt."""
import contextlib
import io
import json
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, x_post, x_post_meta
from scripts import x_post_audit as audit, x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_post import Page, snapshot
from tests.test_x_post_backfill import backfill_entry


class AuditProgressTests(ChannelBase):
    def setUp(self):
        super().setUp()
        self.source = self.sub()
        fast = patch.object(x_post, 'STEP', 0)
        fast.start()
        self.addCleanup(fast.stop)

    def seed(self, identity='123', body='A complete sentence.'):
        item = backfill_entry(tweet(identity, text=body))
        reg.put_items(self.source, [item])
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE url=?', (item['url'],)).fetchone())

    def pending(self, **kwargs):
        with reg.db() as conn:
            return backfill.candidates(conn, **kwargs)

    def run_cli(self, body='A complete sentence.', *flags):
        page = Page(snapshot(body))
        with contextlib.redirect_stdout(io.StringIO()), patch.object(
                backfill.x_browser, 'browser_session', return_value=contextlib.nullcontext(page)):
            code = backfill.main(['run', *flags])
        return code, page

    def test_small_cli_batches_resume_from_new_rows_until_the_whole_set_is_audited(self):
        rows = [self.seed(str(identity)) for identity in range(123, 127)]
        visited = []
        for _ in rows:
            target = self.pending(limit=1)[0]
            identity = target['url'].rsplit('/', 1)[1]
            page = Page(dict(snapshot(target['body']), at=identity, id=identity))
            with contextlib.redirect_stdout(io.StringIO()), patch.object(
                    backfill.x_browser, 'browser_session', return_value=contextlib.nullcontext(page)):
                self.assertEqual(0, backfill.main(['run', '--limit', '1']))
            visited.extend(page.urls)
        self.assertEqual({row['url'] for row in rows}, set(visited))
        self.assertEqual(len(rows), len(visited))
        self.assertEqual([], self.pending())
        self.assertEqual([], self.pending(all_posts=True))
        with patch.object(backfill.x_browser, 'browser_session') as browser, \
             contextlib.redirect_stdout(io.StringIO()):
            report = self.root / 'summary.json'
            report.write_text('stale report')
            self.assertEqual(0, backfill.main(['run', '--report', str(report)]))
        browser.assert_not_called()
        self.assertEqual(0, json.loads(report.read_text())['candidates'])

    def test_scan_reads_but_does_not_create_audit_state_and_explicit_recheck_is_available(self):
        row = self.seed()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, backfill.main(['scan']))
        self.assertFalse(audit.path().exists())
        self.assertEqual(0, self.run_cli()[0])
        self.assertTrue(audit.path().exists())
        with reg.db() as conn:
            self.assertEqual(row, dict(conn.execute('SELECT * FROM feed_items').fetchone()))
        self.assertEqual([], self.pending())
        self.assertEqual([row], self.pending(recheck=True))
        self.assertEqual([row], self.pending(urls=[row['url']]))
        self.assertEqual(0, self.run_cli('A complete sentence.', '--recheck')[0])
        with patch.object(audit, 'POLICY', 'new-policy'):
            self.assertEqual([row], self.pending())

    def test_probe_findings_can_still_be_applied_and_updated_version_is_not_reopened(self):
        row = self.seed(body='Read https://\nexample.org/path')
        detail = 'Read https://example.org/path'
        self.assertEqual(0, self.run_cli(detail)[0])
        self.assertEqual([], self.pending())
        self.assertEqual([row], self.pending(apply=True))
        self.assertEqual(0, self.run_cli(detail, '--yes')[0])
        self.assertEqual([], self.pending(apply=True))
        with reg.db() as conn:
            current = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        self.assertEqual(detail, current['body'])
        self.assertEqual(row['content_rev'] + 1, current['content_rev'])
        self.assertEqual('updated', audit.load()[(row['key'], audit.version(current))][1]['state'])

    def test_body_hash_and_revision_each_invalidate_progress_but_translation_does_not(self):
        row = self.seed()
        audit.record(row, dict(state='identical'))
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='译文',translated_at='now'")
        self.assertEqual([], self.pending())
        for statement in ["body=body||' changed'", "content_rev=content_rev+1",
                          "content_hash='different'"]:
            with self.subTest(statement=statement):
                with reg.db() as conn:
                    conn.execute('UPDATE feed_items SET ' + statement)
                    current = dict(conn.execute('SELECT * FROM feed_items').fetchone())
                    stamped = x_post_meta.stamp(
                        current, 'post', '', author_text=x_post_meta.AuthorText(current['body'], 'detail'))
                    conn.execute('UPDATE feed_items SET x_post_meta=? WHERE key=?',
                                 (stamped[x_post_meta.FIELD], row['key']))
                changed = self.pending()
                self.assertEqual(1, len(changed))
                audit.record(changed[0], dict(state='identical'))
                self.assertEqual([], self.pending())

    def test_body_change_without_new_provenance_is_excluded_instead_of_reusing_a_receipt(self):
        row = self.seed()
        audit.record(row, dict(state='identical'))
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body=body||' changed'")
        excluded = []
        self.assertEqual([], self.pending(excluded=excluded))
        self.assertEqual('invalid_provenance', excluded[0]['reason'])
        self.assertEqual('skipped_ambiguous', excluded[0]['state'])
        pending = self.pending(resolve_legacy=True)
        self.assertEqual(1, len(pending))
        self.assertEqual(row['key'], pending[0]['key'])
        self.assertNotEqual(row['body'], pending[0]['body'])

    def test_failures_wait_behind_new_rows_and_retry_in_oldest_attempt_order(self):
        for identity in range(123, 126):
            self.seed(str(identity))
        rows = self.pending()
        for checked, row in enumerate(rows[:2], 1):
            with patch.object(audit.time, 'time', return_value=checked):
                audit.record(row, dict(state='failed', error='temporary'))
        self.assertEqual(rows[2], self.pending(limit=1)[0])
        audit.record(rows[2], dict(state='identical'))
        self.assertEqual(rows[:2], self.pending())
        with patch.object(audit.time, 'time', return_value=3):
            audit.record(rows[0], dict(state='failed', error='temporary again'))
        self.assertEqual([rows[1], rows[0]], self.pending())

    def test_receipt_is_durable_before_progress_callback_and_not_written_for_unstarted_rows(self):
        rows = [self.seed(), self.seed('124')]
        with self.assertRaisesRegex(RuntimeError, 'interrupted'):
            backfill.repair(rows, Page(snapshot(rows[0]['body'])), remember=True,
                            progress=lambda *_: (_ for _ in ()).throw(RuntimeError('interrupted')))
        self.assertEqual([rows[1]], self.pending())
        self.assertEqual(1, len(audit.load()))
        backfill.repair([rows[1]], None, remember=True, should_stop=lambda: True)
        self.assertEqual([rows[1]], self.pending())
        with patch.object(x_post, 'fetch', side_effect=x_post.PostHalted('stop')):
            backfill.repair([rows[1]], None, remember=True)
        self.assertEqual([rows[1]], self.pending())

    def test_blocked_findings_are_remembered_without_calling_them_repaired(self):
        row = self.seed(body='First\n\nSecond')
        stats = backfill.repair([row], Page(snapshot('First Second')), remember=True, apply=True)
        self.assertEqual(1, stats['blocked_whitespace'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual([], self.pending(apply=True))
        self.assertEqual([row], self.pending(recheck=True))
        receipt = audit.load()[(row['key'], audit.version(row))][1]
        self.assertEqual('blocked_whitespace', receipt['state'])

    def test_concurrent_replacement_and_stale_cas_do_not_mark_the_new_version_audited(self):
        row = self.seed(body='Original')
        replacement = dict(backfill.entry_of(row), body='Newer', title='Newer')
        replacement.pop(x_post.HINT)
        replacement = x_post_meta.stamp(replacement, 'post', '')
        self.assertTrue(grab_store.replace(row, replacement))
        stats = backfill.repair([row], Page(snapshot('Original with ending')), apply=True, remember=True)
        self.assertEqual(1, stats['stale'])
        pending = self.pending(apply=True)
        self.assertEqual(1, len(pending))
        self.assertEqual('Newer', pending[0]['body'])
        self.assertNotIn((row['key'], audit.version(pending[0])), audit.load())

    def test_retryable_fetch_failure_is_recorded_and_never_claims_completion(self):
        row = self.seed()
        with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('temporary')):
            stats = backfill.repair([row], None, remember=True, apply=True)
        self.assertEqual(1, stats['failed'])
        self.assertEqual([row], self.pending())
        self.assertEqual('temporary', audit.load()[(row['key'], audit.version(row))][1]['error'])

    def test_a_concurrent_change_after_our_success_is_not_given_our_audit_receipt(self):
        row = self.seed(body='Original')
        replace = grab_store.replace

        def replace_then_change(seen, complete):
            accepted = replace(seen, complete)
            self.assertTrue(accepted)
            with reg.db() as conn:
                current = dict(conn.execute('SELECT * FROM feed_items').fetchone())
            replacement = x_post_meta.stamp(dict(complete, body='Concurrent new content'), 'post', '')
            self.assertTrue(replace(current, replacement))
            return accepted

        with patch.object(grab_store, 'replace', side_effect=replace_then_change):
            stats = backfill.repair([row], Page(snapshot('Original with ending')),
                                    apply=True, remember=True)
        self.assertEqual(1, stats['updated'])
        pending = self.pending()
        self.assertEqual(1, len(pending))
        self.assertEqual('Concurrent new content', pending[0]['body'])

    def test_a_stop_during_sidecar_creation_does_not_prevent_the_next_run(self):
        row = self.seed()
        audit.path().touch()
        self.assertEqual([row], self.pending())
        self.assertEqual(0, self.run_cli()[0])
        self.assertEqual([], self.pending())

    def test_partial_write_remains_retryable_across_failures_and_blocked_reruns_until_resolved(self):
        row = self.seed(body='Read\n\nhttps://\nexample.org/path')
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='旧译文',translated_at='old',"
                         "path='social/old.md',exported_hash=content_hash")
            row = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        detail = 'Read https://example.org/path with ending'
        self.assertEqual(0, self.run_cli(detail, '--yes')[0])
        pending = self.pending(apply=True)
        self.assertEqual(1, len(pending))
        current = pending[0]
        self.assertEqual('Read\n\nhttps://example.org/path', current['body'])
        self.assertEqual(row['content_rev'] + 1, current['content_rev'])
        self.assertEqual('', current['body_zh'])
        self.assertEqual('', current['translated_at'])
        self.assertEqual(row['path'], current['path'])
        self.assertEqual(row['exported_hash'], current['exported_hash'])
        self.assertEqual('blocked_whitespace', audit.load()[
            (row['key'], audit.version(current))][1]['remaining'])
        fresh = self.seed('124')
        self.assertEqual([fresh, current], self.pending())
        audit.record(fresh, dict(state='identical'))
        audit.record(current, dict(state='identical'))
        self.assertEqual([current], self.pending())
        with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('temporary')):
            backfill.repair([current], None, remember=True)
        for _ in range(2):
            self.assertEqual(0, self.run_cli(detail, '--yes')[0])
            self.assertEqual([current], self.pending())
            self.assertEqual([current], self.pending(apply=True))
            receipt = audit.load()[(row['key'], audit.version(current))][1]
            self.assertEqual('blocked_whitespace', receipt['state'])
            self.assertEqual('blocked_whitespace', receipt['remaining'])
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, backfill.main(['scan', '--url', row['url']]))
        self.assertIn('仍需补查', output.getvalue())
        self.assertIn('remaining', output.getvalue())
        self.assertEqual(0, self.run_cli('Read\n\nhttps://example.org/path with ending', '--yes')[0])
        self.assertEqual([], self.pending(apply=True))
        with reg.db() as conn:
            saved = dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (row['key'],)).fetchone())
        self.assertEqual('Read\n\nhttps://example.org/path with ending', saved['body'])
        self.assertEqual(row['content_rev'] + 2, saved['content_rev'])
        self.assertNotIn('remaining', audit.load()[(row['key'], audit.version(saved))][1])

    def test_partial_probe_retries_after_fresh_rows_without_changing_the_content(self):
        row = self.seed(body='Read\n\nhttps://\nexample.org/path')
        self.assertEqual(0, self.run_cli('Read https://example.org/path with ending')[0])
        fresh = self.seed('124')
        self.assertEqual([fresh, row], self.pending())
        self.assertEqual([fresh, row], self.pending(apply=True))
        with reg.db() as conn:
            self.assertEqual(row, dict(conn.execute('SELECT * FROM feed_items WHERE key=?',
                                                   (row['key'],)).fetchone()))

    def test_scan_exposes_current_receipts_after_probe_without_filtering_or_writing(self):
        rows = [self.seed('123', 'Read https://\nexample.org/path'),
                self.seed('124', 'First\n\nSecond'), self.seed('125'), self.seed('126')]
        for row, detail in zip(rows[:3], ['Read https://example.org/path', 'First Second',
                                          'A complete sentence.']):
            with patch.object(x_post, 'fetch', return_value=detail):
                backfill.repair([row], None, remember=True)
        audit.record(rows[3], dict(state='failed', error='temporary failure'))
        receipt_bytes = audit.path().read_bytes()
        for flags in [[], ['--recheck']]:
            with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(
                    backfill.x_browser, 'browser_session') as browser:
                self.assertEqual(0, backfill.main(['scan', *flags]))
            browser.assert_not_called()
            for row in rows:
                self.assertIn(row['url'], output.getvalue())
            for label in ['可写回', 'normalized', '已扣留', 'blocked_whitespace', 'identical',
                          '失败待重试', 'temporary failure']:
                self.assertIn(label, output.getvalue())
        self.assertEqual(receipt_bytes, audit.path().read_bytes())
        with reg.db() as conn:
            self.assertEqual(rows, [dict(conn.execute('SELECT * FROM feed_items WHERE key=?',
                                                    (row['key'],)).fetchone()) for row in rows])
        self.assertEqual([rows[0], rows[3]], self.pending(apply=True))

    def test_scan_keeps_url_and_limit_scope_and_does_not_reuse_a_changed_versions_receipt(self):
        row = self.seed()
        other = self.seed('124')
        audit.record(row, dict(state='blocked_content', reason='old conclusion'))
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET content_rev=content_rev+1 WHERE key=?', (row['key'],))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, backfill.main(['scan', '--url', row['url'], '--limit', '1']))
        self.assertIn(row['url'], output.getvalue())
        self.assertIn('未核对', output.getvalue())
        self.assertNotIn(other['url'], output.getvalue())
        self.assertNotIn('old conclusion', output.getvalue())
