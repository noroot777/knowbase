"""Stored-preview recovery is opt-in, versioned, and does not trust ellipsis as proof."""
import contextlib
import io
import json
import re
import time
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, sync, translate, x_post
from kb.feeds.x_cdp import CdpTransportError
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_post import BODY, Page, snapshot

SHORT = 'Review every app…'
FULL = SHORT[:-1] + ' and ask before disconnecting anything.\n\nThe full ending.'
LEGACY = 'Read this:\n@Example\n\nhttps://\nexample.org/long/\npath\n…'
CLEAN = 'Read this:@Example\n\nhttps://example.org/long/path'


def linked_snapshot(text):
    """Model anchor-boundary evidence produced by the real-DOM reader tests."""
    offsets = set()
    for anchor in re.finditer(r'https?://\S+|[@#]\w+', text):
        offsets.update(len(re.sub(r'\s+', '', text[:position]))
                       for position in anchor.span())
    legacy = re.sub(r'(https?://\S+|[@#]\w+)', r'\n\1\n', text)
    return dict(snapshot(text), inline_breaks=sorted(offsets),
                inline_plain=re.sub(r'\s+', '', text), inline_legacy=legacy)


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
        self.assertIn('2 条候选', output.getvalue())
        browser.assert_not_called()

    def test_default_scan_includes_unmarked_previews_and_all_is_a_compatibility_alias(self):
        row = self.seed('stops in the middle')
        with reg.db() as conn:
            self.assertEqual([row], backfill.candidates(conn))
            self.assertEqual([row], backfill.candidates(conn, urls=[row['url']]))
            self.assertEqual([row], backfill.candidates(conn, all_posts=True))

    def test_default_scan_selects_protocol_splits_without_an_ellipsis(self):
        rows = [self.seed(text, str(130 + index)) for index, text in enumerate([
            'https://\nexample.org/guide',
            'Read this: http://\r\nexample.org/guide\nThe end.',
            'Text...\nhttps://\nexample.org/guide\nThe end.',
            'Text…\nhttps://\nexample.org/guide\nThe end.',
            LEGACY.removesuffix('\n…'),
        ])]
        plain = [self.seed('Read this: https://example.org/guide. The end.', '140'),
                 self.seed('https://example.org/guide Another paragraph', '141')]
        with reg.db() as conn:
            self.assertEqual({row['key'] for row in rows + plain},
                             {row['key'] for row in backfill.candidates(conn)})
            self.assertTrue({row['key'] for row in backfill.candidates(conn, limit=2)}
                            <= {row['key'] for row in rows})
            self.assertEqual([rows[0]], backfill.candidates(conn, urls=[rows[0]['url']]))

    def test_default_cli_can_repair_a_preview_without_local_truncation_markers(self):
        row = self.seed('stops in the middle')
        full = row['body'] + ' of a sentence, followed by its complete ending.'
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(backfill.x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(Page(snapshot(full)))):
            self.assertEqual(0, backfill.main(['run', '--yes']))
        self.assertEqual(full, self.row(row['key'])['body'])

    def test_default_scan_includes_empty_x_posts_but_not_other_content(self):
        empty = self.seed('', '124')
        foreign = self.seed('not an X post', '125')
        article = self.seed('an article', '126')
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET url=? WHERE key=?',
                         ('https://example.org/status/125', foreign['key']))
            conn.execute("UPDATE feed_items SET content_kind='article' WHERE key=?",
                         (article['key'],))
            self.assertEqual([empty], backfill.candidates(conn))

    def test_missing_detail_is_terminal_for_the_audit_and_never_replaces_stored_text(self):
        row = self.seed()
        gone = dict(snapshot(''), id='', ready='complete', empty=True)
        with patch.object(x_post, 'SECONDS', 0.02):
            stats = backfill.repair([row], Page(gone), apply=True)
        self.assertEqual(1, stats.get('unavailable', 0))
        self.assertEqual(0, stats['failed'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.row(row['key']))

    def test_rate_limit_stops_the_whole_audit_without_probing_or_writing_later_rows(self):
        rows = [self.seed(), self.seed(identity='124')]
        page = Page(dict(snapshot(''), id='', ready='complete', limited=True, dom_problem='limited'))
        with patch.object(x_post, 'SECONDS', 0.02):
            stats = backfill.repair(rows, page, apply=True)
        self.assertEqual([rows[0]['url']], page.urls)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(1, stats['deferred'])
        self.assertIn('频率', stats.get('stop_reason', ''))
        self.assertEqual(rows, [self.row(row['key']) for row in rows])

    def test_cli_reports_absence_and_batch_rate_limits_without_overwriting_stored_rows(self):
        row = self.seed()
        report = self.root / 'terminal-report.json'
        for flag, state, code in [('empty', 'unavailable', 0), ('limited', 'failed', 1)]:
            page = Page(dict(snapshot(''), id='', ready='complete',
                             dom_problem='limited' if flag == 'limited' else '', **{flag: True}))
            with self.subTest(flag=flag), contextlib.redirect_stdout(io.StringIO()) as output, \
                 patch.object(x_post, 'SECONDS', 0.02), \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(page)):
                self.assertEqual(code, backfill.main(['run', '--yes', '--recheck', '--report', str(report)]))
            stats = json.loads(report.read_text())
            self.assertEqual(state, stats['results'][0]['state'])
            self.assertEqual(row, self.row(row['key']))
            self.assertIn('远端明确不可用', output.getvalue())
            if flag == 'limited':
                self.assertIn('整轮已停止', output.getvalue())

    def test_default_cli_repairs_a_protocol_split_without_an_ellipsis(self):
        row = self.seed('Read this: https://\nexample.org/guide')
        clean = 'Read this: https://example.org/guide'
        report = self.root / 'report.json'
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(backfill.x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(Page(snapshot(clean)))):
            self.assertEqual(0, backfill.main(['run', '--yes', '--report', str(report)]))
        self.assertTrue(report.is_file())
        stats = json.loads(report.read_text())
        self.assertEqual(1, stats['candidates'])
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual(clean, self.row(row['key'])['body'])

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
                state = 'identical' if text == SHORT else 'blocked_content'
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
        stats = backfill.repair([row], Page(linked_snapshot(CLEAN)), apply=True)
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
        stats = backfill.repair([row], Page(linked_snapshot(CLEAN)))
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.row(row['key']))
        stats = backfill.repair([row], Page(linked_snapshot(CLEAN)), apply=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual('> Someone reposted\n\n' + CLEAN, self.row(row['key'])['body'])

    def test_known_artifacts_do_not_allow_arbitrary_shorter_text(self):
        row = self.seed(LEGACY)
        stats = backfill.repair([row], Page(snapshot('Read this: https://example.org')), apply=True)
        self.assertEqual(1, stats['blocked_content'])
        self.assertEqual('blocked_content', stats['results'][0]['state'])
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
        stats = backfill.repair([row], Page(linked_snapshot(text)), apply=True)
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(text, self.row(row['key'])['body'])

    def test_protocol_split_normalization_preserves_all_nonwhitespace_text(self):
        for index, (stored, clean) in enumerate([
            ('https://\nexample.org/guide', 'https://example.org/guide'),
            ('http://\r\nexample.org/guide', 'http://example.org/guide'),
            ('Text...\nhttps://\nexample.org\nEnding.', 'Text...\nhttps://example.org\nEnding.'),
            ('Text…\nhttps://\nexample.org\nEnding.', 'Text…\nhttps://example.org\nEnding.'),
            (LEGACY.removesuffix('\n…'), CLEAN),
            ('https://\nexample.org/one\nNext: http://\nexample.org/two\n…',
             'https://example.org/one\nNext: http://example.org/two'),
        ]):
            with self.subTest(stored=stored):
                identity = str(150 + index)
                row = self.seed(stored, identity, context='Someone reposted')
                detail = dict(linked_snapshot(clean), at=identity, id=identity)
                self.assertLess(len(clean), len(stored))
                probe = backfill.repair([row], Page(detail))
                self.assertEqual(1, probe['normalized'])
                self.assertEqual(row, self.row(row['key']))
                stats = backfill.repair([row], Page(detail), apply=True)
                self.assertEqual(1, stats['updated'])
                self.assertTrue(stats['results'][0]['artifacts'])
                self.assertEqual('> Someone reposted\n\n' + clean, self.row(row['key'])['body'])
                self.assertEqual(row['content_rev'] + 1, self.row(row['key'])['content_rev'])

    def test_protocol_split_does_not_allow_text_loss_or_url_edits(self):
        row = self.seed('Read this: https://\nexample.org/Case\nKeep this ending.')
        for clean in ['Read this: https://example.org/Case',
                      'Read this: https://example.org/case\nKeep this ending.']:
            with self.subTest(clean=clean):
                stats = backfill.repair([row], Page(snapshot(clean)), apply=True)
                self.assertEqual(1, stats['blocked_content'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(row, self.row(row['key']))

    def test_confirmed_detail_cannot_erase_unproven_non_url_line_breaks(self):
        row = self.seed('Read this: https://example.org/guide\nKeep this ending.')
        clean = row['body'].replace('\n', ' ')
        stats = backfill.repair([row], Page(snapshot(clean)), apply=True)
        self.assertEqual(1, stats['blocked_whitespace'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual([], backfill.link_artifacts(row['body'], clean))
        self.assertEqual(row, self.row(row['key']))

    def test_inline_mentions_and_hashtags_are_repaired_without_a_url(self):
        for index, (stored, clean) in enumerate([
            ('Prediction: you’re all going to be using \n@AsideAI\n with your agents in a year or so',
             'Prediction: you’re all going to be using @AsideAI with your agents in a year or so'),
            ('Read\n@AsideAI\ntoday', 'Read @AsideAI today'),
            ('Try \n#AI\n and \r\n@AsideAI\r\n today…', 'Try #AI and @AsideAI today…'),
            ('Read \n@AsideAI\n today', 'Read @AsideAI today!'),
            ('Read \n@AsideAI\n today\n\nNext paragraph.',
             'Read @AsideAI today\n\nNext paragraph.'),
        ]):
            with self.subTest(stored=stored):
                identity = str(400 + index)
                row = self.seed(stored, identity, context='Someone reposted')
                detail = dict(linked_snapshot(clean), at=identity, id=identity)
                self.assertLessEqual(len(clean), len(stored))
                self.assertEqual([], backfill.link_artifacts(stored, clean))
                probe = backfill.repair([row], Page(detail))
                self.assertEqual(1, probe['normalized'])
                self.assertEqual(0, probe['updated'])
                self.assertEqual(row, self.row(row['key']))
                stats = backfill.repair([row], Page(detail), apply=True)
                updated = self.row(row['key'])
                self.assertEqual(1, stats['updated'])
                self.assertFalse(stats['results'][0]['artifacts'])
                self.assertEqual('legacy_whitespace', stats['results'][0]['reason'])
                self.assertEqual('> Someone reposted\n\n' + clean, updated['body'])
                self.assertEqual(clean.splitlines()[0], updated['title'])
                self.assertEqual(row['content_rev'] + 1, updated['content_rev'])

    def test_inline_cleanup_cli_uses_versioned_replacement_and_reexports(self):
        stored, clean = 'Read \n@AsideAI\n today', 'Read @AsideAI today'
        row = self.seed(stored, context='Someone reposted')
        sync.export('social')
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='旧译文',translated_at='old'")
            row = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        report = self.root / 'report.json'
        with contextlib.redirect_stdout(io.StringIO()), \
             patch.object(backfill.x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(Page(linked_snapshot(clean)))):
            self.assertEqual(0, backfill.main(['run', '--yes', '--report', str(report)]))
        self.assertEqual(1, json.loads(report.read_text())['normalized'])
        updated = self.row(row['key'])
        self.assertEqual('> Someone reposted\n\n' + clean, updated['body'])
        self.assertEqual(row['content_rev'] + 1, updated['content_rev'])
        self.assertEqual('', updated['body_zh'])
        self.assertEqual('', updated['translated_at'])
        self.assertEqual(1, updated['extract_stale'])
        self.assertEqual(row['exported_hash'], updated['exported_hash'])
        sync.export('social')
        exported = (sync.VAULT_ROOT / row['path']).read_text()
        self.assertIn(updated['body'], exported)
        self.assertNotIn(stored, exported)

    def test_whitespace_alignment_does_not_allow_nonwhitespace_loss_or_edits(self):
        row = self.seed('Keep \n@AsideAI\n and \n#AI\n today…')
        for text in ['Keep @AsideAI and #AI', 'Keep @OtherAI and #AI today…',
                     'Keep @AsideAI and #AI today', 'Keep #AI and @AsideAI today…']:
            with self.subTest(text=text):
                stats = backfill.repair([row], Page(snapshot(text)), apply=True)
                self.assertEqual(1, stats['blocked_content'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(row, self.row(row['key']))

    def test_inline_cleanup_still_requires_the_matching_unfolded_detail(self):
        stored, clean = 'Read \n@AsideAI\n today', 'Read @AsideAI today'
        row = self.seed(stored)
        for detail in [dict(snapshot(clean), at='999'), dict(snapshot(clean), id='999'),
                       dict(snapshot(clean), truncated=True)]:
            with self.subTest(detail=detail), patch.object(x_post, 'SECONDS', 0.002):
                stats = backfill.repair([row], Page(detail), apply=True)
                self.assertEqual(1, stats['failed'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(row, self.row(row['key']))

    def test_inline_cleanup_preserves_concurrent_replacements(self):
        row = self.seed('Read \n@AsideAI\n today')
        self.assertTrue(grab_store.replace(row, {'title': 'New title', 'body': BODY}))
        current = self.row(row['key'])
        stats = backfill.repair([row], Page(linked_snapshot('Read @AsideAI today')), apply=True)
        self.assertEqual(1, stats['stale'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(current, self.row(row['key']))

    def test_any_url_character_can_be_split_without_an_ellipsis(self):
        url = 'https://example.org/a-long-path?one=two%20three#section'
        for offset in range(1, len(url)):
            with self.subTest(offset=offset):
                identity = str(200 + offset)
                stored = 'Read ' + url[:offset] + '\n' + url[offset:]
                row = self.seed(stored, identity)
                with reg.db() as conn:
                    self.assertEqual([row], backfill.candidates(conn, urls=[row['url']]))
                    self.assertIn(row['key'], {item['key'] for item in backfill.candidates(conn)})
                detail = dict(snapshot('Read ' + url), at=identity, id=identity)
                stats = backfill.repair([row], Page(detail), apply=True)
                self.assertEqual(1, stats['normalized'])
                self.assertEqual(1, stats['updated'])
                self.assertEqual('Read ' + url, self.row(row['key'])['body'])

    def test_multiple_url_breaks_and_adjacent_ellipsis_forms_are_repaired(self):
        clean = 'Read https://example.org/a-long-path which matters.'
        for index, stored in enumerate([
            'Read https://exam\nple.org/a\n-long-path\n… which matters.',
            'Read https://exam\nple.org/a\n-long-path… which matters.',
            'Read https://exam\nple.org/a\n-long-path... which matters.',
            'Read\n…\nhttps://exam\nple.org/a\n-long-path which matters.',
            'Read\n...\nhttps://exam\nple.org/a\n-long-path which matters.',
            'Read htt\r\nps://exam\r\nple.org/a\r\n-long-path\r\n… which matters.',
        ]):
            with self.subTest(stored=stored):
                identity = str(300 + index)
                row = self.seed(stored, identity, context='Someone reposted')
                detail = dict(linked_snapshot(clean), at=identity, id=identity)
                stats = backfill.repair([row], Page(detail), apply=True)
                self.assertEqual(1, stats['normalized'])
                self.assertEqual(1, stats['updated'])
                self.assertEqual('> Someone reposted\n\n' + clean, self.row(row['key'])['body'])

    def test_natural_ellipsis_is_preserved_when_a_broken_url_is_repaired(self):
        for index, (stored, clean) in enumerate([
            ('Wait…\nhttps://exam\nple.org/path', 'Wait…\nhttps://example.org/path'),
            ('Wait\n…\nhttps://exam\nple.org/path', 'Wait\n…\nhttps://example.org/path'),
            ('Read https://exam\nple.org/path… and wait…',
             'Read https://example.org/path… and wait…'),
            ('Read https://exam\nple.org/one...two', 'Read https://example.org/one...two'),
        ]):
            with self.subTest(stored=stored):
                identity = str(320 + index)
                row = self.seed(stored, identity)
                stats = backfill.repair([row], Page(dict(snapshot(clean), at=identity, id=identity)),
                                        apply=True)
                self.assertEqual(1, stats['normalized'])
                self.assertEqual(clean, self.row(row['key'])['body'])

    def test_url_cleanup_does_not_discard_narrative_or_edit_the_url(self):
        for index, (stored, detail) in enumerate([
            ('Wait…\n\nhttps://exam\nple.org/path', 'Wait\n\nhttps://example.org/path'),
            ('Wait\n…\n\nhttps://exam\nple.org/path', 'Wait\n\nhttps://example.org/path'),
            ('Read https://exam\nple.org/one...two', 'Read https://example.org/onetwo'),
            ('Read https://exam\nple.org/one.../tail and https://example.org/one',
             'Read https://example.org/one/tail and https://example.org/one'),
            ('Read https://exam\nple.org/Case\n… ending',
             'Read https://example.org/case ending'),
            ('Read https://exam\nple.org/path\n… Important ending',
             'Read https://example.org/path'),
        ]):
            with self.subTest(stored=stored):
                identity = str(340 + index)
                row = self.seed(stored, identity)
                stats = backfill.repair([row], Page(dict(snapshot(detail), at=identity, id=identity)),
                                        apply=True)
                self.assertEqual(0, stats['updated'])
                self.assertEqual(row, self.row(row['key']))

    def test_multiple_urls_are_each_aligned_with_their_own_detail_url(self):
        stored = 'https://exam\nple.org/one\n…\nhttps://oth\ner.org/two\n… End'
        clean = 'https://example.org/one\nhttps://other.org/two End'
        row = self.seed(stored)
        stats = backfill.repair([row], Page(snapshot(clean)), apply=True)
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(clean, self.row(row['key'])['body'])

    def test_word_split_cleanup_preserves_concurrent_replacements(self):
        row = self.seed('Read https://exam\nple.org/path\n… ending')
        self.assertTrue(grab_store.replace(row, {'title': 'New title', 'body': BODY}))
        current = self.row(row['key'])
        stats = backfill.repair([row], Page(snapshot('Read https://example.org/path ending')),
                                apply=True)
        self.assertEqual(1, stats['stale'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(current, self.row(row['key']))

    def test_legacy_cleanup_does_not_weaken_live_collection(self):
        for index, stored in enumerate([LEGACY, LEGACY.removesuffix('\n…'),
                                        'Read \n@AsideAI\n today']):
            with self.subTest(stored=stored):
                identity = str(160 + index)
                row = self.seed(stored, identity)
                clean = 'Read @AsideAI today' if '@AsideAI' in stored else CLEAN
                detail = dict(snapshot(clean), at=identity, id=identity)
                entries, note = x_post.complete([backfill.entry_of(row)], session=Page(detail))
                self.assertEqual([], entries)
                self.assertIn('下次同步', note)

    def test_cli_reports_blocked_identical_and_cleanable_artifacts_separately(self):
        row = self.seed(LEGACY)
        report = self.root / 'report.json'
        for text, state, code in [('shorter', 'blocked_content', 0),
                                  (LEGACY, 'identical', 0), (CLEAN, 'normalized', 0)]:
            with self.subTest(state=state), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(Page(linked_snapshot(text)))):
                self.assertEqual(code, backfill.main(['run', '--recheck', '--report', str(report)]))
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

    def test_session_failure_stops_the_audit_without_reopening_later_posts(self):
        for snap, reason in [(dict(snapshot(), login=True), '重新登录'),
                             (CdpTransportError('disconnected'), '会话中断')]:
            with self.subTest(reason=reason):
                rows = [self.seed(), self.seed(identity='124')]
                page = Page(snap)
                stats = backfill.repair(rows, page, apply=True)
                self.assertEqual([rows[0]['url']], page.urls)
                self.assertEqual(1, stats['failed'])
                self.assertEqual(1, stats['deferred'])
                self.assertEqual('batch', stats['results'][0]['scope'])
                self.assertIn(reason, stats['stop_reason'])
                self.assertEqual(rows, [self.row(row['key']) for row in rows])

    def test_request_error_is_reported_without_replacing_the_row_or_stopping_other_repairs(self):
        row = self.seed()
        other = self.seed(identity='124')
        failed = dict(snapshot(''), id='', ready='complete', failed=True, dom_problem='failed')
        complete = dict(snapshot(FULL), at='124', id='124')
        page = Page(failed, failed, complete, complete)
        stats = backfill.repair([row, other], page, apply=True)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(0, stats['unavailable'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual(0, stats['deferred'])
        self.assertNotIn('stop_reason', stats)
        self.assertEqual(row, self.row(row['key']))
        self.assertEqual(FULL, self.row(other['key'])['body'])
        self.assertIn('X 页面请求出错', stats['results'][0]['error'])
        self.assertEqual(4, page.reads)

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
