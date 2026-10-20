"""Safe audit progress belongs to the source version, not adjacent attempt outcomes."""
import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

from kb.feeds import registry as reg, x_post
from scripts import x_post_audit as audit, x_post_backfill as backfill
from tests import test_x_post_audit as fixtures
from tests.test_x_post import Page, snapshot


class AuditRetentionTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.AuditProgressTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(body='Read\n\nhttps://\nexample.org/path')
        self.assertEqual(0, fixture.run_cli('Read https://example.org/path with ending', '--yes')[0])
        return fixture, fixture.pending()[0]

    def receipt(self, row):
        return audit.load()[(row['key'], audit.version(row))][1]

    def test_actual_backfill_safe_reads_settle_across_failures_or_different_safe_results(self):
        for middle in ['failed', 'identical']:
            for apply in [False, True]:
                with self.subTest(middle=middle, apply=apply):
                    fixture, row = self.fixture()
                    for mode in ['blocked_content', middle, 'blocked_content']:
                        pending = fixture.pending(apply=apply)
                        if not pending:
                            break
                        if mode == 'failed':
                            with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('temporary')):
                                backfill.repair(pending, None, apply=apply, remember=True)
                            self.assertEqual(1, self.receipt(row)['safe_rechecks'])
                        else:
                            text = row['body'] if mode == 'identical' else 'Remote post was edited.'
                            backfill.repair(pending, Page(snapshot(text)), apply=apply, remember=True)
                    self.assertEqual([], fixture.pending(apply=apply))
                    receipt = self.receipt(row)
                    self.assertEqual(audit.SAFE_RECHECKS, receipt['safe_rechecks'])
                    self.assertEqual('blocked_whitespace', receipt['retained'])
                    self.assertNotIn('remaining', receipt)
                    with reg.db() as conn:
                        saved = dict(conn.execute('SELECT * FROM feed_items').fetchone())
                    self.assertEqual(row, saved)
                    with patch.object(backfill.x_browser, 'browser_session') as browser, \
                         contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(0, backfill.main(['run']))
                    browser.assert_not_called()
                    self.assertEqual([row], fixture.pending(recheck=True))
                    self.assertEqual([row], fixture.pending(urls=[row['url']]))
                    text = row['body'] + ' with ending'
                    self.assertEqual(0, fixture.run_cli(text, '--yes', '--url', row['url'])[0])
                    with reg.db() as conn:
                        completed = dict(conn.execute('SELECT * FROM feed_items').fetchone())
                    self.assertEqual(text, completed['body'])
                    self.assertEqual(row['content_rev'] + 1, completed['content_rev'])
                    self.assertEqual('updated', self.receipt(completed)['state'])
                    self.assertEqual([], fixture.pending(apply=True))

    def test_safe_progress_survives_all_retry_outcomes_and_is_bounded_for_any_safe_result(self):
        for first in ['blocked_content', 'blocked_evidence', 'identical']:
            for second in ['blocked_content', 'blocked_evidence', 'identical']:
                with self.subTest(first=first, second=second):
                    fixture, row = self.fixture()
                    audit.record(row, dict(state=first))
                    for retry in ['failed', 'stale', 'deferred', 'failed']:
                        audit.record(row, dict(state=retry))
                        self.assertEqual(1, self.receipt(row)['safe_rechecks'])
                        self.assertEqual([row], fixture.pending())
                    audit.record(row, dict(state=second))
                    self.assertEqual([], fixture.pending())
                    self.assertEqual(audit.SAFE_RECHECKS, self.receipt(row)['safe_rechecks'])

    def test_old_receipt_counts_its_last_safe_result_and_new_versions_start_fresh(self):
        fixture, row = self.fixture()
        audit.record(row, dict(state='blocked_content'))
        receipt = self.receipt(row)
        receipt.pop('safe_rechecks')
        with contextlib.closing(sqlite3.connect(audit.path())) as conn, conn:
            conn.execute('UPDATE audits SET result=? WHERE item_key=? AND version=?',
                         (json.dumps(receipt), row['key'], audit.version(row)))
        audit.record(row, dict(state='failed'))
        self.assertEqual(1, self.receipt(row)['safe_rechecks'])
        audit.record(row, dict(state='identical'))
        self.assertEqual([], fixture.pending())
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET content_rev=content_rev+1')
        changed = fixture.pending()[0]
        audit.record(changed, dict(state='normalized', remaining='blocked_whitespace'))
        self.assertNotIn('safe_rechecks', self.receipt(changed))
        audit.record(changed, dict(state='identical'))
        self.assertEqual(1, self.receipt(changed)['safe_rechecks'])
        self.assertEqual([changed], fixture.pending())
        audit.record(changed, dict(state='blocked_content'))
        self.assertEqual([], fixture.pending())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, backfill.main(['scan']))
