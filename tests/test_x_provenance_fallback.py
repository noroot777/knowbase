"""Legacy source recovery shares Article fallback without inventing negative evidence."""
import time
from unittest.mock import patch

from kb.feeds import x_article, x_post, x_post_meta, x_post_provenance
from scripts import x_post_backfill as backfill
from tests.test_x_post_provenance import ProvenanceBase


class ProvenanceFallbackTests(ProvenanceBase):
    def recover(self, row, *, api='https://example.invalid', article=None,
                api_error=True, browser_error=False, fallback=True, session=None):
        session = object() if session is None else session
        with patch.object(x_article, 'API', api), \
                patch.object(x_article, 'BROWSER_FALLBACK', fallback), \
                patch.object(x_article, 'fetch', return_value=None,
                             side_effect=x_article.ArticleUnavailable('API offline') if api_error else None) as api_fetch, \
                patch.object(x_article, 'browser_fetch', return_value=article,
                             side_effect=x_article.ArticleUnavailable('browser offline') if browser_error else None) as browser, \
                patch.object(x_post, 'fetch', return_value='A short preview and the complete ending') as detail:
            stats = backfill.repair([row], session, apply=True, resolve_legacy=True)
        return stats, api_fetch, browser, detail, session

    def test_unavailable_or_disabled_api_falls_back_before_recovering_a_post(self):
        for identity, api in [('801', 'https://example.invalid'), ('802', '')]:
            row = self.seed(identity=identity, legacy=True)
            with self.subTest(api=api):
                stats, api_fetch, browser, detail, session = self.recover(row, api=api)
                self.assertEqual(bool(api), bool(api_fetch.call_count))
                browser.assert_called_once_with(session, row['url'], deadline=None)
                detail.assert_called_once()
                self.assertEqual(1, stats['provenance_saved'])
                self.assertEqual(1, stats['updated'])
                self.assertEqual(0, stats['failed'])
                self.assertEqual(('', ''), x_post_meta.eligibility(self.current(row)))

    def test_browser_article_evidence_excludes_the_row_without_reading_post_text(self):
        article = dict(title='An Article', body='The complete Article')
        row = self.seed(article=article, legacy=True)
        stats, _api, browser, detail, _session = self.recover(row, article=article)
        browser.assert_called_once()
        detail.assert_not_called()
        self.assertEqual(1, stats['skipped_article'])
        self.assertEqual(1, stats['provenance_saved'])
        self.assertEqual(row['body'], self.current(row)['body'])

    def test_api_negative_is_definite_and_does_not_open_the_article_browser(self):
        row = self.seed(legacy=True)
        stats, api, browser, detail, _session = self.recover(row, api_error=False)
        api.assert_called_once()
        browser.assert_not_called()
        detail.assert_called_once()
        self.assertEqual(1, stats['updated'])

    def test_both_unavailable_preserve_the_unknown_row(self):
        row = self.seed(legacy=True)
        stats, _api, browser, detail, _session = self.recover(row, browser_error=True)
        browser.assert_called_once()
        detail.assert_not_called()
        self.assertEqual(1, stats['failed'])
        self.assertEqual(0, stats['provenance_saved'])
        self.assertEqual(row, self.current(row))

    def test_disabled_fallback_is_not_no_article_evidence(self):
        for api in ('', 'https://example.invalid'):
            row = self.seed(legacy=True)
            with self.subTest(api=api):
                stats, _api, browser, detail, _session = self.recover(row, api=api, fallback=False)
                browser.assert_not_called()
                detail.assert_not_called()
                self.assertEqual(1, stats['failed'])
                self.assertEqual(row, self.current(row))

    def test_stop_or_exhausted_budget_prevents_starting_the_fallback(self):
        row = self.seed(legacy=True)
        for stopped in (False, True):
            exhausted = False

            def fail_api(*_args, **_kwargs):
                nonlocal exhausted
                exhausted = True
                raise x_article.ArticleUnavailable('offline')

            def clock():
                return 20 if exhausted and not stopped else 10

            with self.subTest(stopped=stopped), patch.object(x_article, 'API', 'enabled'), \
                    patch.object(x_article, 'BROWSER_FALLBACK', True), \
                    patch.object(x_article, 'fetch', side_effect=fail_api), \
                    patch.object(time, 'monotonic', side_effect=clock), \
                    patch.object(x_article, 'browser_fetch') as browser, \
                    patch.object(x_post, 'fetch') as detail, self.assertRaises(x_post.PostUnavailable):
                x_post_provenance.resolve(row, object(), deadline=15,
                                          should_stop=lambda: exhausted and stopped)
            browser.assert_not_called()
            detail.assert_not_called()

    def test_budget_is_rechecked_after_browser_negative_evidence(self):
        row = self.seed(legacy=True)
        exhausted = False

        def browser_negative(*_args, **_kwargs):
            nonlocal exhausted
            exhausted = True
            return None

        with patch.object(x_article, 'API', ''), patch.object(x_article, 'BROWSER_FALLBACK', True), \
                patch.object(x_article, 'browser_fetch', side_effect=browser_negative), \
                patch.object(time, 'monotonic', side_effect=lambda: 20 if exhausted else 10), \
                patch.object(x_post, 'fetch') as detail, self.assertRaises(x_post.PostUnavailable):
            x_post_provenance.resolve(row, object(), deadline=15)
        detail.assert_not_called()

    def test_browser_article_still_requires_exact_stored_body_alignment(self):
        row = self.seed(legacy=True)
        stats, _api, _browser, detail, _session = self.recover(
            row, article=dict(title='A short preview', body='An unrelated Article'))
        detail.assert_not_called()
        self.assertEqual(1, stats['failed'])
        self.assertEqual(row, self.current(row))
