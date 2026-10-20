"""Detailed audit output shares one batch limit without starving eligible posts."""
import contextlib
import io
import json
from unittest.mock import patch

from kb.feeds import registry as reg, x_browser, x_post
from scripts import x_post_backfill as backfill
from tests import test_x_audit_summary as summary
from tests.test_x_post_provenance import ProvenanceBase


class AuditLimitTests(ProvenanceBase):
    run_cli = summary.AuditSummaryTests.run_cli

    def legacy_rows(self, count):
        return [self.seed(identity=str(1000 + index), legacy=True) for index in range(count)]

    def test_all_excluded_scan_prints_only_a_bounded_batch_and_separate_aggregate(self):
        self.legacy_rows(60)
        with patch.object(x_browser, 'browser_session') as browser, contextlib.redirect_stdout(
                io.StringIO()) as output:
            code = backfill.main(['scan', '--limit', '5'])
        browser.assert_not_called()
        self.assertEqual(1, code)
        lines = [json.loads(line) for line in output.getvalue().splitlines() if line.startswith('{')]
        self.assertEqual(5, len(lines))
        self.assertTrue(all(row['state'] == 'skipped_ambiguous' for row in lines))
        self.assertIn('实际核对 0 / 本批共 5 条', output.getvalue())
        self.assertIn('另有 55 条资格排除未列出,不计入本批', output.getvalue())

    def test_all_excluded_run_report_obeys_the_same_limit_without_a_browser(self):
        self.legacy_rows(60)
        code, output, stats = self.run_cli('--limit', '5', browser_expected=False)
        self.assertEqual(1, code)
        self.assertEqual((0, 5), (stats['checked'], stats['total']))
        self.assertEqual(5, len(stats['results']))
        self.assertEqual(5, stats['skipped_ambiguous'])
        self.assertEqual(dict(skipped_ambiguous=55), stats['omitted'])
        self.assertIn('另有 55 条资格排除未列出,不计入本批', output)

    def test_eligible_rows_keep_priority_and_exclusions_fill_only_the_remaining_budget(self):
        self.legacy_rows(4)
        for identity in ('2000', '2001'):
            self.seed(identity=identity, article=dict(title='Article', body='Whole Article'))
        eligible = [self.seed(identity=str(3000 + index)) for index in range(3)]
        excluded, omitted = [], {}
        with reg.db() as conn:
            selected = backfill.candidates(conn, limit=5, scan=True, excluded=excluded, omitted=omitted)
        self.assertCountEqual(eligible, selected)
        self.assertEqual(2, len(excluded))
        self.assertEqual(4, sum(omitted.values()))
        for state, total in [('skipped_article', 2), ('skipped_ambiguous', 4)]:
            self.assertEqual(total, sum(row['state'] == state for row in excluded) + omitted.get(state, 0))
        code, _output, stats = self.run_cli('--limit', '5')
        self.assertEqual(1, code)
        self.assertEqual((3, 5, 5), (stats['checked'], stats['total'], len(stats['results'])))

    def test_omitted_exclusions_remain_visible_and_nonzero_when_posts_fill_the_batch(self):
        self.legacy_rows(8)
        self.seed(identity='2000')
        code, output, stats = self.run_cli('--limit', '1')
        self.assertEqual(1, code)
        self.assertEqual((1, 1), (stats['checked'], stats['total']))
        self.assertEqual(1, len(stats['results']))
        self.assertEqual(dict(skipped_ambiguous=8), stats['omitted'])
        self.assertIn('另有 8 条资格排除未列出,不计入本批', output)

    def test_url_filter_limits_both_details_and_omitted_aggregate_to_the_requested_scope(self):
        legacy = self.legacy_rows(8)
        code, output, stats = self.run_cli('--limit', '1', '--url', legacy[0]['url'],
                                           '--url', legacy[1]['url'], browser_expected=False)
        self.assertEqual(1, code)
        self.assertEqual(1, stats['total'])
        self.assertEqual(1, len(stats['results']))
        self.assertEqual(dict(skipped_ambiguous=1), stats['omitted'])
        self.assertIn('另有 1 条资格排除未列出,不计入本批', output)

    def test_zero_limit_still_lists_all_exclusions_and_candidate_posts(self):
        self.legacy_rows(6)
        self.seed(identity='2000')
        code, output, stats = self.run_cli('--limit', '0')
        self.assertEqual(1, code)
        self.assertEqual((1, 7), (stats['checked'], stats['total']))
        self.assertEqual(7, len(stats['results']))
        self.assertEqual({}, stats['omitted'])
        self.assertNotIn('未列出', output)

    def test_progress_reports_also_stay_within_the_batch_limit(self):
        self.legacy_rows(8)
        eligible = [self.seed(identity=str(2000 + index)) for index in range(3)]
        excluded, omitted = [], {}
        with reg.db() as conn:
            rows = backfill.candidates(conn, limit=2, scan=True, excluded=excluded, omitted=omitted)
        reports = []
        with patch.object(x_post, 'fetch', return_value='A short preview with ending'):
            stats = backfill.repair(rows, None, excluded=excluded, omitted=omitted,
                                    progress=lambda _result, snapshot: reports.append(
                                        json.loads(json.dumps(snapshot))))
        self.assertEqual(2, len(reports))
        self.assertTrue(all(report['total'] == 2 and len(report['results']) <= 2 for report in reports))
        self.assertEqual(2, stats['checked'])
        self.assertTrue(all(row in eligible for row in rows))
        self.assertEqual(dict(skipped_ambiguous=8), stats['omitted'])
