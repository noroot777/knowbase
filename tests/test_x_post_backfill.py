"""Stored-preview recovery is opt-in, versioned, and does not trust ellipsis as proof."""
import contextlib
import io
import json
import time
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, sync, translate, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_post import BODY, Page, snapshot

SHORT = 'Review every app…'
FULL = SHORT[:-1] + ' and ask before disconnecting anything.\n\nThe full ending.'


class BackfillTests(ChannelBase):
    def setUp(self):
        super().setUp()
        fast = patch.object(x_post, 'STEP', 0.0)
        fast.start()
        self.addCleanup(fast.stop)
        self.source = self.sub()

    def seed(self, text=SHORT, identity='123', context=''):
        item = tweet(identity, text=text, context=context)
        reg.put_items(self.source, [backfill_entry(item)])
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE url=?', (item['url'],)).fetchone())

    def row(self, key):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (key,)).fetchone())

    def test_scan_is_only_a_candidate_list_and_opens_no_browser(self):
        self.seed()
        self.seed('complete sentence', '124')
        with patch.object(backfill.x_browser, 'browser_session') as browser, \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, backfill.main(['scan']))
        self.assertIn('1 条候选', output.getvalue())
        browser.assert_not_called()

    def test_explicit_urls_and_all_can_reach_previews_without_an_ellipsis(self):
        row = self.seed('stops in the middle')
        with reg.db() as conn:
            self.assertEqual([], backfill.candidates(conn))
            self.assertEqual([row], backfill.candidates(conn, urls=[row['url']]))
            self.assertEqual([row], backfill.candidates(conn, all_posts=True))

    def test_run_is_a_probe_until_yes_is_given(self):
        row = self.seed()
        report = self.root / 'evidence' / 'repair.json'
        for flags, updated in [([], 0), (['--yes'], 1)]:
            with self.subTest(flags=flags), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(Page(snapshot(FULL)))):
                self.assertEqual(0, backfill.main(['run', '--report', str(report), *flags]))
            self.assertEqual(updated, json.loads(report.read_text())['updated'])
            self.assertEqual(FULL if updated else SHORT, self.row(row['key'])['body'])

    def test_a_fully_deferred_run_still_writes_a_report(self):
        self.seed()
        report = self.root / 'new-directory' / 'repair.json'
        stats = backfill.repair([{}], None, should_stop=lambda: True)
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(backfill, 'repair', return_value=stats), \
             patch.object(backfill.x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(Page())):
            self.assertEqual(1, backfill.main(['run', '--report', str(report)]))
        self.assertEqual(1, json.loads(report.read_text())['deferred'])

    def test_non_x_urls_are_not_candidates_even_if_they_contain_x_com(self):
        row = self.seed()
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET url='https://example.org/x.com/Example/status/123'")
            self.assertEqual([], backfill.candidates(conn, all_posts=True))
        self.assertEqual(SHORT, self.row(row['key'])['body'])

    def test_a_natural_ellipsis_and_shorter_read_are_not_overwritten(self):
        row = self.seed()
        for text in [SHORT, 'shorter']:
            with self.subTest(text=text):
                stats = backfill.repair([row], Page(snapshot(text)), apply=True)
                self.assertEqual(1, stats['unchanged'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(0, stats['failed'])
                self.assertEqual(row, self.row(row['key']))

    def test_a_probe_is_read_only_even_when_the_full_body_is_available(self):
        row = self.seed()
        stats = backfill.repair([row], Page(snapshot(FULL)))
        self.assertEqual(1, stats['expanded'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.row(row['key']))

    def test_replacement_requeues_translation_and_rewrites_the_export(self):
        row = self.seed()
        sync.export('social')
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='残文译文',translated_at='old',translate_tries=2")
            row = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        stats = backfill.repair([row], Page(snapshot(FULL)), apply=True)
        updated = self.row(row['key'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual(FULL, updated['body'])
        self.assertEqual('', updated['body_zh'])
        self.assertEqual('', updated['translated_at'])
        self.assertEqual(0, updated['translate_tries'])
        self.assertEqual(1, updated['extract_stale'])
        self.assertEqual(row['content_rev'] + 1, updated['content_rev'])
        self.assertEqual(row['exported_hash'], updated['exported_hash'])
        self.assertFalse(translate._save(row['key'], '旧标题', '旧译文', (row['title'], row['body'])))
        sync.export('social')
        self.assertIn(FULL, (sync.VAULT_ROOT / row['path']).read_text())

    def test_a_concurrent_replacement_is_not_lost_or_counted_as_success(self):
        row = self.seed()
        self.assertTrue(grab_store.replace(row, {'title': 'New title', 'body': BODY}))
        current = self.row(row['key'])
        stats = backfill.repair([row], Page(snapshot(FULL)), apply=True)
        self.assertEqual(1, stats['stale'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(current, self.row(row['key']))

    def test_repost_context_is_retained_and_not_used_to_fake_text_growth(self):
        context = ('Someone reposted ' + 'a long name ' * 20).rstrip()
        row = self.seed(context=context)
        stats = backfill.repair([row], Page(snapshot(FULL)), apply=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual('> ' + context + '\n\n' + FULL, self.row(row['key'])['body'])

    def test_login_failure_preserves_the_row_and_is_reported(self):
        row = self.seed()
        stats = backfill.repair([row], Page(dict(snapshot(), login=True)), apply=True)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(row, self.row(row['key']))
        self.assertIn('登录', stats['results'][0]['error'])

    def test_expired_budget_and_stop_do_not_open_a_page_or_touch_data(self):
        row = self.seed()
        for kwargs in [{'deadline': time.monotonic() - 1}, {'should_stop': lambda: True}]:
            page = Page()
            stats = backfill.repair([row], page, apply=True, **kwargs)
            self.assertEqual(1, stats['deferred'])
            self.assertEqual([], page.urls)
            self.assertEqual(row, self.row(row['key']))


def backfill_entry(item):
    from kb.feeds import x_browser
    return x_browser._entry(item)
