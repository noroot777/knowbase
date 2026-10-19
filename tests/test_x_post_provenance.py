"""Article bodies and uncertain legacy shapes never enter the short-post audit."""
import contextlib
import io
import json
import sqlite3
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, x_article, x_browser, x_post, x_post_meta
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet


class PostProvenanceTests(ChannelBase):
    def setUp(self):
        super().setUp()
        self.source = self.sub()

    def seed(self, body='A short preview', identity='123', context='', article=None, legacy=False):
        entry = x_browser._entry(tweet(identity, text=body, context=context))
        if article is not None:
            entry = x_article.merge(entry, article)
        if legacy:
            entry.pop(x_post_meta.FIELD)
        reg.put_items(self.source, [entry])
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE url=?', (entry['url'],)).fetchone())

    def current(self, row):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (row['key'],)).fetchone())

    def test_new_article_is_excluded_even_when_its_title_matches_the_lead(self):
        row = self.seed(article=dict(title='A short preview', body='Full Article Markdown'))
        self.assertEqual('article_merged', x_post_meta.eligibility(row)[0])
        with patch.object(x_post, 'fetch', return_value='A much longer detail' * 10) as fetch:
            for _attempt in range(2):
                stats = backfill.repair([row], None, apply=True)
                self.assertEqual(1, stats['skipped_article'])
                self.assertEqual(0, stats['blocked_length'])
                self.assertEqual(0, stats['updated'])
        fetch.assert_not_called()
        self.assertEqual(row, self.current(row))

    def test_article_exclusion_precedes_limits_and_cannot_be_overridden_by_all_or_url(self):
        article = self.seed(article=dict(title='An Article', body='The whole body'))
        post = self.seed('A preview', '124')
        for kwargs in ({'limit': 1}, {'all_posts': True}, {'urls': [article['url'], post['url']]}):
            excluded = []
            with self.subTest(kwargs=kwargs), reg.db() as conn:
                self.assertEqual([post], backfill.candidates(conn, excluded=excluded, **kwargs))
            self.assertEqual('skipped_article', excluded[0]['state'])
            self.assertEqual(article['url'], excluded[0]['url'])

    def test_old_article_shape_is_ambiguous_not_declared_complete_or_overwritten(self):
        row = self.seed('短短的开头', context='转推的原话',
                        article=dict(title='一篇长文的标题', body=''), legacy=True)
        self.assertEqual('> 转推的原话\n\n短短的开头', row['body'])
        with patch.object(x_post, 'fetch', return_value='短短的开头,后面还有很多很多很多内容') as fetch:
            stats = backfill.repair([row], None, apply=True)
        fetch.assert_not_called()
        self.assertEqual(1, stats['skipped_ambiguous'])
        self.assertEqual('unclassified_legacy', stats['results'][0]['reason'])
        self.assertEqual(0, stats['skipped_article'])
        self.assertEqual(row, self.current(row))
        with self.assertRaises(ValueError):
            backfill.entry_of(row)

    def test_direct_proposal_cannot_bypass_article_or_legacy_eligibility(self):
        for legacy in (False, True):
            row = self.seed('Intro', str(130 + legacy), article=dict(title='Title', body='Article'),
                            legacy=legacy)
            with self.subTest(legacy=legacy):
                state, complete, info = backfill.proposal(dict(row, **{x_post.HINT: ''}),
                                                         'A much longer unrelated detail' * 3)
                self.assertEqual('skipped_ambiguous' if legacy else 'skipped_article', state)
                self.assertIsNone(complete)
                self.assertTrue(info['reason'])

    def test_a_different_title_alone_is_not_proof_of_an_article(self):
        row = self.seed(legacy=True)
        row['title'] = 'A custom post title'
        self.assertEqual(('unclassified_legacy', ''), x_post_meta.eligibility(row))

    def test_legacy_exact_contracts_keep_context_and_authored_blockquotes_separate(self):
        for legacy in (False, True):
            for context in ('', 'Someone reposted'):
                text = '> The author quotes this\n\nThe author adds this'
                row = self.seed(text, str(140 + legacy * 2 + bool(context)), context=context,
                                legacy=legacy)
                entry = backfill.entry_of(row)
                prefix = '> Someone reposted\n\n' if context else ''
                with self.subTest(legacy=legacy, context=context):
                    self.assertEqual(prefix, entry[x_post.HINT])
                    full = text + '\n\nThe complete ending'
                    with patch.object(x_post, 'fetch', return_value=full):
                        stats = backfill.repair([row], None, apply=True)
                    self.assertEqual(1, stats['updated'])
                    saved = self.current(row)
                    self.assertEqual(prefix + full, saved['body'])
                    self.assertEqual(('', prefix), x_post_meta.eligibility(saved))

    def test_legacy_title_prefix_coincidence_does_not_establish_repost_context(self):
        row = self.seed('> An authored quote\n\nA post title has more words', legacy=True)
        row['title'] = 'A post title'
        self.assertEqual(('unclassified_legacy', ''), x_post_meta.eligibility(row))

    def test_legacy_expanded_first_line_title_and_empty_body_remain_eligible(self):
        row = self.seed('First line\n\nSecond paragraph', legacy=True)
        row['title'] = 'First line'
        self.assertEqual(('', ''), x_post_meta.eligibility(row))
        empty = self.seed('', '125', legacy=True)
        self.assertEqual(('', ''), x_post_meta.eligibility(empty))

    def test_source_metadata_survives_versioned_replacement_and_prevents_article_downgrade(self):
        row = self.seed(context='Someone reposted')
        complete = x_article.merge(backfill.entry_of(row), dict(title='Article', body='Full text'))
        self.assertTrue(grab_store.replace(row, complete))
        saved = self.current(row)
        self.assertEqual(row['content_rev'] + 1, saved['content_rev'])
        self.assertEqual('article_merged', x_post_meta.eligibility(saved)[0])
        self.assertIn('> Someone reposted', saved['body'])
        self.assertFalse(grab_store.replace(row, complete))

    def test_stale_or_invalid_metadata_is_not_used_to_strip_body_prefixes(self):
        row = self.seed(context='Someone reposted')
        changed = dict(row, body='A completely different body')
        self.assertEqual(('invalid_provenance', ''), x_post_meta.eligibility(changed))
        for changed in (dict(row, body=row['body'] + ' but a later writer changed the ending'),
                        dict(row, title='A later writer changed the title')):
            self.assertEqual(('invalid_provenance', ''), x_post_meta.eligibility(changed))
        for raw in ('{', '[]', 'null', '{"version": 2}'):
            with self.subTest(raw=raw):
                self.assertEqual('invalid_provenance', x_post_meta.eligibility(
                    dict(row, **{x_post_meta.FIELD: raw}))[0])
        self.assertEqual('invalid_provenance', x_post_meta.eligibility(
            x_post_meta.stamp(row, 'post', '> A different context\n\n'))[0])
        replacement = dict(title='New post', body='New post body')
        self.assertTrue(grab_store.replace(row, replacement))
        self.assertEqual('invalid_provenance', x_post_meta.eligibility(self.current(row))[0])

    def test_metadata_free_author_only_update_does_not_lose_article_provenance(self):
        row = self.seed(article=dict(title='An Article', body='Full Markdown'))
        self.assertTrue(grab_store.replace(row, dict(title=row['title'], body=row['body'],
                                                     author='Corrected author')))
        self.assertEqual('article_merged', x_post_meta.eligibility(self.current(row))[0])

    def test_post_expansion_refreshes_provenance_and_keeps_a_precise_context_boundary(self):
        row = self.seed(context='Someone reposted')
        full = row['body'].split('\n\n', 1)[1] + ' and the complete ending'
        complete = x_post.merge(backfill.entry_of(row), full)
        self.assertEqual(('', '> Someone reposted\n\n'), x_post_meta.eligibility(complete))
        self.assertTrue(grab_store.replace(row, complete))
        self.assertEqual(('', '> Someone reposted\n\n'), x_post_meta.eligibility(self.current(row)))

    def test_legacy_article_only_cli_runs_write_skip_reports_without_opening_a_browser(self):
        row = self.seed(article=dict(title='Article', body='Full Markdown'), legacy=True)
        for args in ([], ['--all'], ['--url', row['url']]):
            report = self.root / 'article-skips.json'
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()), \
                    patch.object(x_browser, 'browser_session') as browser:
                self.assertEqual(0, backfill.main(['run', '--yes', '--report', str(report), *args]))
            browser.assert_not_called()
            stats = json.loads(report.read_text())
            self.assertEqual(1, stats['skipped_ambiguous'])
            self.assertEqual(0, stats['candidates'])
            self.assertEqual(row['url'], stats['results'][0]['url'])
            self.assertEqual(row, self.current(row))

    def test_migration_adds_unknown_provenance_without_reclassifying_existing_bodies(self):
        with sqlite3.connect(':memory:') as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(reg.SCHEMA)
            conn.execute("INSERT INTO feed_items(key,kind,url,title,author,body,fetched_at,content_hash)"
                         " VALUES('old','social','url','title','author','legacy body','now','hash')")
            reg._migrate(conn)
            self.assertEqual(('', 'legacy body'), tuple(conn.execute(
                'SELECT x_post_meta,body FROM feed_items').fetchone()))
            reg._migrate(conn)
            self.assertEqual(1, conn.execute('SELECT count(*) FROM feed_items').fetchone()[0])
