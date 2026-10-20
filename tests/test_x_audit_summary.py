"""CLI coverage includes exclusions, remote verification, and unfinished audit work."""
import contextlib
import io
import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.feeds import x_article, x_browser, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_post_provenance import ProvenanceBase


class AuditSummaryTests(ProvenanceBase):
    def run_cli(self, *args, detail='A short preview with the full ending', browser_expected=True):
        report = self.root / 'audit.json'
        page = SimpleNamespace(open_page=lambda _url: None)
        with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(
                x_browser, 'browser_session', return_value=contextlib.nullcontext(page)) as browser, patch.object(
                x_post, 'fetch', return_value=detail):
            code = backfill.main(['run', '--report', str(report), *args])
        if not browser_expected:
            browser.assert_not_called()
        return code, output.getvalue(), json.loads(report.read_text())

    def test_mixed_rows_report_actual_detail_coverage_and_both_exclusions(self):
        self.seed()
        self.seed(identity='124', legacy=True)
        self.seed(identity='125', article=dict(title='Article', body='The whole Article'))
        code, output, stats = self.run_cli()
        self.assertEqual(1, code)
        self.assertEqual((1, 3), (stats['checked'], stats['total']))
        self.assertIn('实际核对 1 / 本批共 3 条', output)
        self.assertIn('长文跳过 1 条,来源不明跳过 1 条', output)
        self.assertEqual(1, stats['expanded'])
        self.assertEqual(0, stats['failed'])

    def test_an_all_excluded_run_reports_coverage_without_opening_a_browser(self):
        self.seed(legacy=True)
        self.seed(identity='124', article=dict(title='Article', body='The whole Article'))
        code, output, stats = self.run_cli(browser_expected=False)
        self.assertEqual(1, code)
        self.assertEqual((0, 2), (stats['checked'], stats['total']))
        self.assertIn('实际核对 0 / 本批共 2 条', output)
        self.assertIn('长文跳过 1 条,来源不明跳过 1 条', output)

    def test_empty_storage_is_distinct_from_all_excluded(self):
        code, output, stats = self.run_cli()
        self.assertEqual(0, code)
        self.assertEqual((0, 0), (stats['checked'], stats['total']))
        self.assertIn('实际核对 0 / 本批共 0 条', output)

    def test_a_checked_but_concurrently_replaced_row_does_not_exit_successfully(self):
        self.seed()
        with patch.object(backfill.grab_store, 'replace', return_value=False):
            code, output, stats = self.run_cli('--yes')
        self.assertEqual(1, code)
        self.assertEqual((1, 1), (stats['checked'], stats['total']))
        self.assertEqual((0, 1), (stats['updated'], stats['stale']))
        self.assertIn('并发换版跳过 1 条', output)

    def test_scan_reports_exclusions_but_does_not_claim_detail_checks(self):
        self.seed()
        self.seed(identity='124', legacy=True)
        with patch.object(x_browser, 'browser_session') as browser, contextlib.redirect_stdout(
                io.StringIO()) as output:
            code = backfill.main(['scan'])
        browser.assert_not_called()
        self.assertEqual(1, code)
        self.assertIn('实际核对 0 / 本批共 2 条', output.getvalue())
        self.assertIn('仅列候选,未访问详情', output.getvalue())

    def test_limit_denominator_is_explicitly_the_batch_not_all_storage(self):
        for identity in ('123', '124', '125'):
            self.seed(identity=identity)
        self.seed(identity='126', legacy=True)
        code, output, stats = self.run_cli('--limit', '1')
        self.assertEqual(1, code)
        self.assertEqual((1, 2), (stats['checked'], stats['total']))
        self.assertIn('实际核对 1 / 本批共 2 条', output)
        self.assertIn('不代表其余存量已核对', output)

    def test_source_verification_and_persistence_have_separate_summary_counts(self):
        for apply in (False, True):
            with self.subTest(apply=apply):
                self.seed(identity=str(123 + apply), legacy=True)
                with patch.object(x_article, 'API', 'https://example.invalid'), patch.object(
                        x_article, 'fetch', return_value=None):
                    code, output, stats = self.run_cli(
                        '--resolve-legacy', *(['--yes'] if apply else []))
                self.assertEqual(0, code)
                self.assertEqual(1 + apply, stats['checked'])
                self.assertIn(f'来源核实 {1 + apply} 条,来源保存 {int(apply) * 2} 条', output)

    def test_unfinished_requests_are_not_counted_as_checked(self):
        row = self.seed()
        for options in [dict(deadline=time.monotonic() - 1), dict(should_stop=lambda: True)]:
            with self.subTest(options=options):
                stats = backfill.repair([row], None, **options)
                self.assertEqual((0, 1), (stats['checked'], stats['total']))
                self.assertEqual(1, stats['deferred'])
        with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('Temporary error')):
            stats = backfill.repair([row], None)
        self.assertEqual((0, 1), (stats['checked'], stats['total']))
        self.assertEqual(1, stats['failed'])

    def test_confirmed_absence_is_checked_but_resolved_articles_are_only_excluded(self):
        row = self.seed()
        with patch.object(x_post, 'fetch', side_effect=x_post.PostGone('Gone')):
            stats = backfill.repair([row], None)
        self.assertEqual((1, 1), (stats['checked'], stats['total']))
        self.assertEqual(1, stats['unavailable'])
        legacy = self.seed('The whole Article', identity='124', legacy=True)
        with patch.object(x_article, 'API', 'https://example.invalid'), patch.object(
                x_article, 'fetch', return_value=dict(title='Article', body=legacy['body'])):
            code, output, stats = self.run_cli('--resolve-legacy', '--url', legacy['url'])
        self.assertEqual(1, code)
        self.assertEqual((0, 1), (stats['checked'], stats['total']))
        self.assertEqual(1, stats['skipped_article'])
        self.assertIn('来源核实 1 条,来源保存 0 条', output)
