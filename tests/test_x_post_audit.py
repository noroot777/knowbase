"""Audit checkpoints survive bounded reruns without hiding changed rows or retry debt."""
import contextlib
import io
import json
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, x_post
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
                changed = self.pending()
                self.assertEqual(1, len(changed))
                audit.record(changed[0], dict(state='identical'))
                self.assertEqual([], self.pending())

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
            self.assertTrue(replace(current, dict(complete, body='Concurrent new content')))
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
