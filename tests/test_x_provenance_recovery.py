"""Legacy provenance needs remote body evidence; production snapshots retain that evidence."""
import contextlib
import io
import json
from unittest.mock import patch

from kb.feeds import grab, grab_store, registry as reg, x_article, x_browser, x_feedgrab, x_post
from kb.feeds import x_post_meta
from kb.web import grab_api
from scripts import x_post_backfill as backfill
from tests.grab_fixtures import GrabCase, Response, TWEET, tweet_payload
from tests.test_x_collectors import tweet
from tests.test_x_post_provenance import ProvenanceBase
from tests.test_x_post_backfill import CLEAN, LEGACY


class LegacyRecoveryTests(ProvenanceBase):
    def run_recovery(self, row, *, article=None, detail='A short preview and a complete ending',
                     apply=True):
        with patch.object(x_article, 'API', 'https://example.invalid'), \
                patch.object(x_article, 'fetch', return_value=article) as api, \
                patch.object(x_post, 'fetch', return_value=detail) as fetch:
            stats = backfill.repair([row], None, apply=apply, resolve_legacy=True)
        return stats, api, fetch

    def test_unmarked_article_title_collision_is_not_a_post_and_never_gets_overwritten(self):
        body = '谈谈知识库的索引\n\n这一篇很短,只有两段。'
        row = self.seed('', article=dict(title='谈谈知识库的索引', body=body), legacy=True)
        detail = '谈谈知识库的索引\n\n我写了一篇新文章,点开看看吧,顺手说说这几天的想法。'
        self.assertEqual(('unclassified_legacy', ''), x_post_meta.eligibility(row))
        with patch.object(x_post, 'fetch') as fetch:
            self.assertEqual(1, backfill.repair([row], None, apply=True)['skipped_ambiguous'])
        fetch.assert_not_called()
        stats, _api, fetch = self.run_recovery(row, article=dict(title=row['title'], body=body),
                                               detail=detail)
        fetch.assert_not_called()
        self.assertEqual(1, stats['skipped_article'])
        self.assertEqual(1, stats['provenance_saved'])
        self.assertEqual(0, stats['updated'])
        saved = self.current(row)
        self.assertEqual(body, saved['body'])
        self.assertEqual(row['content_hash'], saved['content_hash'])
        self.assertEqual(row['exported_hash'], saved['exported_hash'])
        self.assertEqual(row['content_rev'] + 1, saved['content_rev'])
        self.assertEqual('article_merged', x_post_meta.eligibility(saved)[0])
        with patch.object(x_post, 'fetch') as fetch, patch.object(x_article, 'fetch') as api:
            backfill.repair([saved], None, apply=True, resolve_legacy=True)
        fetch.assert_not_called()
        api.assert_not_called()

    def test_no_article_response_alone_does_not_prove_stored_text_is_a_post(self):
        row = self.seed('', article=dict(title='Article', body='Article\n\nThe complete old article'),
                        legacy=True)
        stats, _api, _fetch = self.run_recovery(row, detail='Article\n\nSome unrelated newer words' * 3)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(0, stats['provenance_saved'])
        self.assertEqual(row, self.current(row))

    def test_partial_or_changed_article_remains_unclassified_without_a_detail_probe(self):
        row = self.seed('A short preview', legacy=True)
        stats, _api, fetch = self.run_recovery(row, article=dict(title='Article', body='Full Article'))
        fetch.assert_not_called()
        self.assertEqual(1, stats['failed'])
        self.assertEqual(row, self.current(row))

    def test_source_checks_are_read_only_without_yes_and_the_post_is_still_audited(self):
        row = self.seed(legacy=True)
        stats, _api, fetch = self.run_recovery(row, apply=False)
        self.assertEqual(1, stats['provenance_resolved'])
        self.assertEqual(1, stats['expanded'])
        self.assertEqual(0, stats['provenance_saved'])
        self.assertEqual(1, fetch.call_count)
        self.assertEqual(row, self.current(row))

    def test_saved_post_source_and_replacement_share_the_new_cas_snapshot(self):
        row = self.seed(context='Someone reposted', legacy=True)
        full = 'A short preview and a complete ending'
        stats, _api, fetch = self.run_recovery(row, detail=full)
        self.assertEqual(1, stats['provenance_saved'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual(1, fetch.call_count)
        saved = self.current(row)
        self.assertEqual('> Someone reposted\n\n' + full, saved['body'])
        self.assertEqual(('', '> Someone reposted\n\n'), x_post_meta.eligibility(saved))

    def test_resolving_legacy_source_repairs_confirmed_urls_but_preserves_unproven_whitespace(self):
        row = self.seed(LEGACY, legacy=True)
        stats, _api, _fetch = self.run_recovery(row, detail=CLEAN)
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual(CLEAN.replace(':@', ':\n@'), self.current(row)['body'])
        self.assertEqual(1, stats['partial'])

    def test_a_concurrent_replacement_rejects_provenance_without_rewriting_content(self):
        row = self.seed(legacy=True)

        def fetch(*_args, **_kwargs):
            replacement = x_post_meta.stamp(dict(row, title='New', body='New body'), 'post', '')
            self.assertTrue(grab_store.replace(row, replacement))
            return 'A short preview and a complete ending'

        with patch.object(x_article, 'API', 'https://example.invalid'), \
                patch.object(x_article, 'fetch', return_value=None), \
                patch.object(x_post, 'fetch', side_effect=fetch):
            stats = backfill.repair([row], None, apply=True, resolve_legacy=True)
        self.assertEqual(1, stats['stale'])
        self.assertEqual(0, stats['provenance_saved'])
        self.assertEqual('New body', self.current(row)['body'])
        self.assertEqual(('', ''), x_post_meta.eligibility(self.current(row)))

    def test_disabled_or_failed_article_api_never_invents_no_article_evidence(self):
        row = self.seed(legacy=True)
        for api, failure in [('', None), ('https://example.invalid', x_article.ArticleUnavailable('offline'))]:
            with self.subTest(api=api), patch.object(x_article, 'API', api), \
                    patch.object(x_article, 'fetch', side_effect=failure), \
                    patch.object(x_post, 'fetch') as fetch:
                stats = backfill.repair([row], None, apply=True, resolve_legacy=True)
            self.assertEqual(1, stats['failed'])
            fetch.assert_not_called()
            self.assertEqual(row, self.current(row))

    def test_legacy_context_only_body_is_reported_until_remote_text_proves_its_boundary(self):
        row = self.seed('', context='Someone reposted', legacy=True)
        stats = backfill.repair([row], None, apply=True)
        self.assertEqual(1, stats['skipped_ambiguous'])
        self.assertEqual(row['url'], stats['results'][0]['url'])
        self.assertEqual('unclassified_legacy', stats['results'][0]['reason'])
        stats, _api, _fetch = self.run_recovery(row, detail='Recovered text')
        self.assertEqual(1, stats['updated'])
        self.assertEqual('> Someone reposted\n\nRecovered text', self.current(row)['body'])

    def test_legacy_resolution_can_be_selected_but_all_and_url_cannot_force_admission(self):
        row = self.seed(legacy=True)
        with reg.db() as conn:
            self.assertEqual([], backfill.candidates(conn, urls=[row['url']], all_posts=True))
            self.assertEqual([row], backfill.candidates(conn, resolve_legacy=True))

    def test_missing_snapshot_provenance_is_rejected_instead_of_silently_erased(self):
        row = self.seed()
        seen = grab_store.known(row['url'])
        seen.pop(x_post_meta.FIELD)
        with self.assertRaises(KeyError):
            grab_store.replace(seen, dict(title='New', body='New body'))
        self.assertEqual(row, self.current(row))

    def test_feedgrab_frontmatter_title_is_not_required_to_match_its_body(self):
        entry = x_feedgrab._entry(dict(source='https://x.com/Example/status/123',
                                      title='A frontmatter title', author='Example'),
                                 'The actual post body', 'Example')
        reg.put_items(self.source, [entry])
        with reg.db() as conn:
            rows = backfill.candidates(conn)
        self.assertEqual(1, len(rows))
        self.assertEqual(('', ''), x_post_meta.eligibility(rows[0]))

    def test_cli_explicit_resolution_preserves_articles_and_writes_a_durable_report(self):
        row = self.seed('', article=dict(title='Article', body='Article\n\nWhole body'), legacy=True)
        report = self.root / 'resolved.json'
        page = type('Page', (), {'open_page': lambda *_args: None})()
        with contextlib.redirect_stdout(io.StringIO()), \
                patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                patch.object(x_article, 'API', 'https://example.invalid'), \
                patch.object(x_article, 'fetch', return_value=dict(title='Article', body=row['body'])), \
                patch.object(x_post, 'fetch') as fetch:
            self.assertEqual(1, backfill.main(['run', '--resolve-legacy', '--yes', '--report', str(report)]))
        fetch.assert_not_called()
        self.assertEqual(1, json.loads(report.read_text())['provenance_saved'])


class ProductionRefetchProvenanceTests(GrabCase):
    def test_real_recover_and_backfill_preserve_a_quoted_tweet_context(self):
        source = reg.save_subscription('social', dict(name='Example', url='https://x.com/example'))
        reg.put_items(source, [x_browser._entry(tweet('12345', handle='example', text='My opinion…'))])
        payload = tweet_payload(text='My opinion', quote=dict(text='The original quoted words',
                                                             author=dict(screen_name='someone')))
        with patch.object(grab, 'request', return_value=Response(
                200, TWEET, payload, {'Content-Type': 'application/json'})), \
                patch.object(grab_api, '_write_through', side_effect=lambda result, *_args: result):
            result = grab_api._recover(dict(ok=True, content_ok=True), TWEET,
                                       grab_store.known(TWEET), '大V观点', asked=True)
        self.assertTrue(result['replaced'])
        with reg.db() as conn:
            row = backfill.candidates(conn, urls=[TWEET])[0]
        prefix = '> 引用 @someone：The original quoted words\n\n'
        self.assertEqual(('', prefix), x_post_meta.eligibility(row))
        detail = 'My opinion followed by the full author text' * 3
        with patch.object(x_post, 'fetch', return_value=detail):
            self.assertEqual(1, backfill.repair([row], None, apply=True)['updated'])
        with reg.db() as conn:
            saved = dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (row['key'],)).fetchone())
        self.assertEqual(prefix + detail, saved['body'])
        self.assertEqual(('', prefix), x_post_meta.eligibility(saved))

    def test_real_recover_snapshot_and_x_fetch_keep_the_replacement_in_the_audit(self):
        source = reg.save_subscription('social', dict(name='Example', url='https://x.com/example'))
        entry = x_browser._entry(tweet('12345', handle='example', text='An old preview'))
        reg.put_items(source, [entry])
        seen = grab_store.known(TWEET)
        self.assertIn(x_post_meta.FIELD, seen)
        with patch.object(grab, 'request', return_value=Response(
                200, TWEET, tweet_payload(text='An old preview followed by the whole post'),
                {'Content-Type': 'application/json'})), \
                patch.object(grab_api, '_write_through', side_effect=lambda result, *_args: result):
            result = grab_api._recover(dict(ok=True, content_ok=True), TWEET, seen, '大V观点', asked=True)
        self.assertTrue(result['replaced'])
        with reg.db() as conn:
            rows = backfill.candidates(conn, urls=[TWEET])
        self.assertEqual(1, len(rows))
        self.assertEqual(('', ''), x_post_meta.eligibility(rows[0]))
