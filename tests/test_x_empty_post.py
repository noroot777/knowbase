"""Zero authored text is a proved terminal audit result, never an empty write."""
import contextlib
import io
import time
from unittest.mock import patch

from kb.feeds import registry as reg, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_post import BODY, URL, Page, preview, snapshot
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_post_backfill import backfill_entry


def empty(kind='photo', **extra):
    return dict(snapshot(''), ready='complete', empty_kind=kind, **extra)


class EmptyPostTests(ChannelBase):
    def setUp(self):
        super().setUp()
        fast = patch.object(x_post, 'STEP', 0.0)
        fast.start()
        self.addCleanup(fast.stop)
        self.source = self.sub()

    def seed(self, text):
        reg.put_items(self.source, [backfill_entry(tweet('123', text=text))])
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def row(self, key):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone())

    def test_each_loaded_media_kind_has_a_distinct_terminal_state(self):
        for kind in ['photo', 'video', 'quote', 'link']:
            page = Page(empty(kind))
            with self.subTest(kind=kind), self.assertRaises(x_post.PostEmpty) as caught:
                x_post.fetch(URL, page)
            self.assertEqual(kind, caught.exception.kind)
            self.assertEqual(2, page.reads)

    def test_unproved_empty_loading_wrong_post_and_folded_text_remain_retryable(self):
        for snap in [snapshot(''), empty('unknown'), dict(empty(), ready='loading'),
                     dict(empty(), at='999'), dict(empty(), id='999'),
                     dict(empty(), truncated=True)]:
            with self.subTest(snap=snap), patch.object(x_post, 'SECONDS', 0.003):
                with self.assertRaises(x_post.PostUnavailable) as caught:
                    x_post.fetch(URL, Page(snap))
            self.assertNotIsInstance(caught.exception, x_post.PostEmpty)

    def test_transient_or_changing_empty_evidence_does_not_hide_late_authored_text(self):
        for snaps in [(empty(), snapshot(), snapshot()),
                      (empty('photo'), empty('video'), snapshot(), snapshot())]:
            page = Page(*snaps)
            self.assertEqual(BODY, x_post.fetch(URL, page))

    def test_confirmed_empty_preserves_every_stored_column_even_with_yes(self):
        row = self.seed('')
        stats = backfill.repair([row], Page(empty()), apply=True)
        self.assertEqual(1, stats['empty'])
        self.assertEqual(0, stats['failed'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual('confirmed_empty', stats['results'][0]['reason'])
        self.assertEqual('photo', stats['results'][0]['empty_kind'])
        self.assertEqual(row, self.row(row['key']))

    def test_remote_empty_never_replaces_or_approves_nonempty_stored_text(self):
        row = self.seed('keep this existing text')
        stats = backfill.repair([row], Page(empty()), apply=True)
        self.assertEqual(0, stats['empty'])
        self.assertEqual(1, stats['blocked_length'])
        self.assertEqual('remote_empty_stored_text', stats['results'][0]['reason'])
        self.assertEqual(row, self.row(row['key']))

    def test_cli_succeeds_for_proved_empty_but_keeps_unknown_empty_failure(self):
        self.seed('')
        for snap, code, phrase in [(empty(), 0, '已确认无自写文字 1 条'),
                                   (snapshot(''), 1, '失败 1 条')]:
            with self.subTest(snap=snap), patch.object(x_post, 'SECONDS', 0.05), \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(Page(snap))), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(code, backfill.main(['run', '--yes']))
            self.assertIn(phrase, output.getvalue())

    def test_empty_proof_cannot_be_written_as_a_completed_folded_preview(self):
        entries, note = x_post.complete([preview()], session=Page(empty()))
        self.assertEqual([], entries)
        self.assertIn('下次同步', note)

    def test_expired_budget_does_not_approve_empty_without_a_second_read(self):
        page = Page(empty())
        with self.assertRaises(x_post.PostUnavailable):
            x_post.fetch(URL, page, deadline=time.monotonic() - 1)
        self.assertEqual(0, page.reads)
