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
LEGACY = 'Read this:\n@Example\n\nhttps://\nexample.org/long/\npath\n…'
CLEAN = 'Read this:@Example\n\nhttps://example.org/long/path'


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
                state = 'identical' if text == SHORT else 'blocked_length'
                self.assertEqual(1, stats[state])
                self.assertEqual(state, stats['results'][0]['state'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(0, stats['failed'])
                self.assertEqual(row, self.row(row['key']))

    def test_shorter_clean_text_repairs_link_artifacts_and_reexports(self):
        row = self.seed(LEGACY)
        sync.export('social')
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='旧译文',translated_at='old'")
            row = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        self.assertLess(len(CLEAN), len(LEGACY))
        stats = backfill.repair([row], Page(snapshot(CLEAN)), apply=True)
        updated = self.row(row['key'])
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual('legacy_link_artifacts', stats['results'][0]['reason'])
        self.assertEqual(CLEAN, updated['body'])
        self.assertEqual(CLEAN.splitlines()[0], updated['title'])
        self.assertEqual(row['content_rev'] + 1, updated['content_rev'])
        self.assertEqual('', updated['body_zh'])
        self.assertEqual('', updated['translated_at'])
        self.assertEqual(1, updated['extract_stale'])
        self.assertEqual(row['exported_hash'], updated['exported_hash'])
        sync.export('social')
        text = (sync.VAULT_ROOT / row['path']).read_text()
        self.assertIn(CLEAN, text)
        self.assertNotIn('https://\n', text)

    def test_link_cleanup_probe_is_read_only_and_keeps_repost_context(self):
        row = self.seed(LEGACY, context='Someone reposted')
        stats = backfill.repair([row], Page(snapshot(CLEAN)))
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.row(row['key']))
        stats = backfill.repair([row], Page(snapshot(CLEAN)), apply=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual('> Someone reposted\n\n' + CLEAN, self.row(row['key'])['body'])

    def test_known_artifacts_do_not_allow_arbitrary_shorter_text(self):
        row = self.seed(LEGACY)
        stats = backfill.repair([row], Page(snapshot('Read this: https://example.org')), apply=True)
        self.assertEqual(1, stats['blocked_artifacts'])
        self.assertEqual('blocked_artifacts', stats['results'][0]['state'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.row(row['key']))

    def test_identical_artifacts_are_not_called_repaired_or_complete(self):
        row = self.seed(LEGACY)
        stats = backfill.repair([row], Page(snapshot(LEGACY)), apply=True)
        self.assertEqual(1, stats['identical'])
        self.assertTrue(stats['results'][0]['artifacts'])
        self.assertEqual(0, stats['normalized'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.row(row['key']))

    def test_ascii_tail_and_appended_text_can_be_repaired_without_growth(self):
        row = self.seed(LEGACY.replace('…', '...'))
        text = CLEAN + '\nNew'
        self.assertLess(len(text), len(row['body']))
        stats = backfill.repair([row], Page(snapshot(text)), apply=True)
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(text, self.row(row['key'])['body'])

    def test_legacy_cleanup_does_not_weaken_live_collection(self):
        row = self.seed(LEGACY)
        entries, note = x_post.complete([backfill.entry_of(row)], session=Page(snapshot(CLEAN)))
        self.assertEqual([], entries)
        self.assertIn('下次同步', note)

    def test_cli_reports_blocked_identical_and_cleanable_artifacts_separately(self):
        row = self.seed(LEGACY)
        report = self.root / 'report.json'
        for text, state, code in [('shorter', 'blocked_artifacts', 1),
                                  (LEGACY, 'identical', 0), (CLEAN, 'normalized', 0)]:
            with self.subTest(state=state), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(Page(snapshot(text)))):
                self.assertEqual(code, backfill.main(['run', '--report', str(report)]))
            recorded = json.loads(report.read_text())
            self.assertEqual(state, recorded['results'][0]['state'])
            self.assertTrue(recorded['results'][0]['artifacts'])
            self.assertEqual(0, recorded['updated'])
            self.assertEqual(row, self.row(row['key']))

    def test_artifact_repair_still_requires_matching_unfolded_detail(self):
        row = self.seed(LEGACY)
        for snap in [dict(snapshot(CLEAN), id='999'), dict(snapshot(CLEAN), truncated=True)]:
            with self.subTest(snap=snap), patch.object(x_post, 'SECONDS', 0.002):
                stats = backfill.repair([row], Page(snap), apply=True)
            self.assertEqual(1, stats['failed'])
            self.assertEqual(0, stats['updated'])
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
