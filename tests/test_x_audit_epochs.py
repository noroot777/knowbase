"""Audit outcomes are monotonic per version and explicit recheck generation."""
from concurrent.futures import ThreadPoolExecutor
import threading
import unittest

from kb.feeds import registry as reg, x_post
from scripts import x_post_audit as audit, x_post_backfill as backfill
from tests import test_x_post_audit as fixtures
from tests.test_x_post import Page, snapshot


class DelayedPage(Page):
    def __init__(self, entered, release, text=None):
        super().__init__(snapshot(text or ''))
        self.entered, self.release, self.text = entered, release, text

    def eval(self, script):
        self.entered.set()
        if not self.release.wait(5):
            raise RuntimeError('test did not release older audit')
        if self.text is None:
            raise x_post.PostUnavailable('temporary old attempt failure')
        return super().eval(script)


class AuditEpochTests(unittest.TestCase):
    def fixture(self):
        fixture = fixtures.AuditProgressTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(body='Read\n\nhttps://\nexample.org/path')
        self.assertEqual(0, fixture.run_cli('Read https://example.org/path with ending', '--yes')[0])
        row = fixture.pending()[0]
        backfill.repair([row], Page(snapshot('Remote post was edited.')), remember=True)
        return fixture, row

    def receipt(self, row):
        return audit.load()[(row['key'], audit.version(row))][1]

    def overlap(self, row, slow_page, fast_rows, fast_page, *, apply):
        with ThreadPoolExecutor(max_workers=1) as executor:
            slow = executor.submit(backfill.repair, [row], slow_page, apply=apply, remember=True)
            try:
                self.assertTrue(slow_page.entered.wait(5))
                fast = backfill.repair(fast_rows, fast_page, apply=apply, remember=True)
                settled = self.receipt(row)
            finally:
                slow_page.release.set()
            return settled, slow.result(timeout=5), fast

    def test_older_failure_cannot_reopen_a_same_generation_retained_result(self):
        for apply in [False, True]:
            with self.subTest(apply=apply):
                fixture, row = self.fixture()
                selected = fixture.pending(apply=apply)
                page = DelayedPage(threading.Event(), threading.Event())
                settled, slow, _ = self.overlap(row, page, selected,
                                              Page(snapshot('Remote post was edited.')), apply=apply)
                self.assertEqual(1, slow['failed'])
                self.assertEqual(audit.SAFE_RECHECKS, settled['safe_rechecks'])
                self.assertEqual('blocked_whitespace', settled['retained'])
                self.assertEqual(settled, self.receipt(row))
                self.assertEqual([], fixture.pending(apply=apply))
                with reg.db() as conn:
                    self.assertEqual(row, dict(conn.execute('SELECT * FROM feed_items').fetchone()))

    def test_recheck_starts_a_persisted_generation_and_rejects_older_safe_or_failed_results(self):
        for text in [None, 'Remote post was edited.']:
            for flags in [dict(recheck=True), dict(urls=['https://x.com/Example/status/123'])]:
                with self.subTest(text=text, flags=flags):
                    fixture, row = self.fixture()
                    page = DelayedPage(threading.Event(), threading.Event(), text)
                    fast = DelayedPage(threading.Event(), threading.Event())
                    fast.release.set()
                    settled, _, _ = self.overlap(row, page, fixture.pending(**flags), fast, apply=False)
                    self.assertEqual(1, settled['generation'])
                    self.assertEqual('failed', settled['state'])
                    self.assertEqual(0, settled['safe_rechecks'])
                    self.assertEqual(settled, self.receipt(row))
                    for count in [1, 2]:
                        backfill.repair(fixture.pending(), Page(snapshot('Remote post was edited.')),
                                        remember=True)
                        self.assertEqual(count, self.receipt(row)['safe_rechecks'])
                    self.assertEqual([], fixture.pending())

    def test_positive_repair_can_still_replace_the_body_after_same_generation_retention(self):
        fixture, row = self.fixture()
        page = DelayedPage(threading.Event(), threading.Event(), row['body'] + ' with ending')
        _, slow, _ = self.overlap(row, page, fixture.pending(apply=True),
                                  Page(snapshot('Remote post was edited.')), apply=True)
        self.assertEqual(1, slow['updated'])
        with reg.db() as conn:
            current = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        self.assertEqual(row['body'] + ' with ending', current['body'])
        self.assertEqual(row['content_rev'] + 1, current['content_rev'])
        self.assertEqual('updated', self.receipt(current)['state'])
        self.assertEqual([], fixture.pending(apply=True))

    def test_late_stale_and_already_selected_work_do_not_downgrade_a_terminal_result(self):
        fixture, row = self.fixture()
        generation = audit.begin(row)
        selected = fixture.pending()
        audit.record(row, dict(state='blocked_content'), generation=generation)
        settled = self.receipt(row)
        audit.record(row, dict(state='stale'), generation=generation)
        self.assertEqual(settled, self.receipt(row))
        deferred = backfill.repair(selected, None, remember=True)
        self.assertEqual(1, deferred['deferred'])
        self.assertEqual(settled, self.receipt(row))
        self.assertEqual([], fixture.pending())

    def test_explicit_recheck_reopens_a_terminal_version_without_inheriting_safe_progress(self):
        for flags in [dict(recheck=True), dict(urls=['https://x.com/Example/status/123'])]:
            with self.subTest(flags=flags):
                fixture, row = self.fixture()
                backfill.repair([row], Page(snapshot('Remote post was edited.')), remember=True)
                self.assertEqual([], fixture.pending())
                failure = DelayedPage(threading.Event(), threading.Event())
                failure.release.set()
                backfill.repair(fixture.pending(**flags), failure, remember=True)
                receipt = self.receipt(row)
                self.assertEqual('failed', receipt['state'])
                self.assertEqual(1, receipt['generation'])
                self.assertEqual(0, receipt['safe_rechecks'])
                self.assertEqual([row], fixture.pending())
