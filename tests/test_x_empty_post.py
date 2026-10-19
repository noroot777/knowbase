"""Zero authored text is a proved terminal audit result, never an empty write."""
import contextlib
import io
import time
from unittest.mock import patch

from kb.feeds import registry as reg, x_article, x_post
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

    def seed(self, text, context=''):
        reg.put_items(self.source, [backfill_entry(tweet('123', text=text, context=context))])
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def row(self, key):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone())

    def test_each_loaded_media_kind_has_a_distinct_terminal_state(self):
        for kind in ['photo', 'video', 'quote', 'link', 'unspecified']:
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

    def test_repost_attribution_only_is_empty_before_and_after_whitespace_normalization(self):
        row = self.seed('', context='向阳乔木 转推了')
        self.assertEqual('Example Person 的动态', row['title'])
        self.assertEqual('> 向阳乔木 转推了\n\n', row['body'])
        for body in [row['body'], row['body'].rstrip()]:
            with self.subTest(body=body):
                with reg.db() as conn:
                    conn.execute('UPDATE feed_items SET body=? WHERE key=?', (body, row['key']))
                stored = self.row(row['key'])
                stats = backfill.repair([stored], Page(empty()), apply=True)
                self.assertEqual(1, stats['empty'])
                self.assertEqual(0, stats['blocked_length'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(stored, self.row(row['key']))

    def test_repost_attribution_does_not_hide_real_authored_blockquotes(self):
        row = self.seed('> keep my own words', context='向阳乔木 转推了')
        stats = backfill.repair([row], Page(empty()), apply=True)
        self.assertEqual(0, stats['empty'])
        self.assertEqual(1, stats['blocked_length'])
        self.assertEqual(row, self.row(row['key']))

    def test_authored_blockquote_without_context_is_not_mistaken_for_attribution(self):
        row = self.seed('> keep my own words')
        stats = backfill.repair([row], Page(empty()), apply=True)
        self.assertEqual(1, stats['blocked_length'])
        self.assertEqual(row, self.row(row['key']))

    def test_cli_succeeds_for_proved_empty_but_keeps_unknown_empty_failure(self):
        row = self.seed('', context='向阳乔木 转推了')
        for snap, code, phrase in [(empty(), 0, '已确认无自写文字 1 条'),
                                   (snapshot(''), 1, '失败 1 条')]:
            with self.subTest(snap=snap), patch.object(x_post, 'SECONDS', 0.05), \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(Page(snap))), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(code, backfill.main(['run', '--yes']))
            self.assertIn(phrase, output.getvalue())
            self.assertEqual(row, self.row(row['key']))

    def test_empty_proof_is_an_audited_terminal_skip_not_a_completed_folded_preview(self):
        unavailable = []
        entries, note = x_post.complete([preview()], session=Page(empty()), unavailable=unavailable)
        self.assertEqual([], entries)
        self.assertEqual('', note)
        self.assertEqual([dict(url=URL, reason='详情页已确认无自写文字(photo)', empty_kind='photo')],
                         unavailable)

    def test_no_authored_text_does_not_override_positive_article_evidence(self):
        entry = dict(preview(), **{x_article.HINT: 'https://x.com/i/article/999'})
        article = dict(title='Full article', body='The entire article, including the ending.')
        for detail in [article, None]:
            unavailable = []
            with self.subTest(detail=detail), patch.object(x_article, 'API', 'https://article.test'), \
                 patch.object(x_article, 'fetch', return_value=detail):
                kept, note = x_post.complete([entry], session=Page(empty('link')),
                                              unavailable=unavailable)
            self.assertEqual([], unavailable)
            if detail:
                self.assertEqual('', note)
                self.assertEqual(1, len(kept))
                self.assertIn(article['body'], kept[0]['body'])
                self.assertNotIn(x_post.HINT, kept[0])
            else:
                self.assertEqual([], kept)
                self.assertIn('下次同步', note)

    def test_expired_budget_does_not_approve_empty_without_a_second_read(self):
        page = Page(empty())
        with self.assertRaises(x_post.PostUnavailable):
            x_post.fetch(URL, page, deadline=time.monotonic() - 1)
        self.assertEqual(0, page.reads)
