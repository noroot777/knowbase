"""Insufficient X source evidence fails one row, never the remaining batch."""
import contextlib
import io
import json
from unittest.mock import patch

from kb.feeds import x_article, x_browser, x_post, x_post_meta, x_post_provenance
from scripts import x_post_backfill as backfill
from tests.test_x_post_provenance import ProvenanceBase


class EvidenceFailureTests(ProvenanceBase):
    def test_unproven_authored_quote_remains_a_retryable_resolution_failure(self):
        row = self.seed('> 引用 @somebody：我引用一句…', legacy=True)
        detail = '> 引用 @somebody：我引用一句完整的话，后面还有一长段。'
        self.assertTrue(x_post_provenance._covers(row['body'], detail))
        with patch.object(x_article, 'API', 'https://example.invalid'), \
                patch.object(x_article, 'fetch', return_value=None), \
                patch.object(x_post, 'fetch', return_value=detail), \
                self.assertRaises(x_post.PostUnavailable):
            x_post_provenance.resolve(row, None)
        self.assertEqual(row, self.current(row))

    def test_recovery_records_failed_evidence_and_continues_in_apply_and_dry_run(self):
        for index, (body, detail) in enumerate([
                ('> 引用一句…', '> 引用一句完整的话'),
                ('> 引用一句...', '> 引用一句完整的话'),
                ('> A quoted\tline', '> A quoted line with the whole ending')]):
            for apply in (False, True):
                bad = self.seed(body, str(710 + index * 2 + apply), legacy=True)
                good = self.seed('A normal preview', str(810 + index * 2 + apply), legacy=True)
                progress = []
                details = {bad['url']: detail, good['url']: 'A normal preview with the ending'}
                with self.subTest(body=body, apply=apply), \
                        patch.object(x_article, 'API', 'https://example.invalid'), \
                        patch.object(x_article, 'fetch', return_value=None), \
                        patch.object(x_post, 'fetch', side_effect=lambda url, *_args, **_kwargs: details[url]):
                    stats = backfill.repair([bad, good], None, apply=apply, resolve_legacy=True,
                                            progress=lambda result, _stats: progress.append(dict(result)))
                    self.assertEqual(1, stats['failed'])
                    self.assertEqual(0, stats['deferred'])
                    self.assertEqual(1, stats['provenance_resolved'])
                    self.assertEqual(int(apply), stats['provenance_saved'])
                    self.assertEqual(['failed', 'updated' if apply else 'expanded'],
                                     [result['state'] for result in stats['results']])
                    self.assertEqual(stats['results'], progress)
                    self.assertEqual(bad, self.current(bad))
                    self.assertEqual(details[good['url']] if apply else good['body'],
                                     self.current(good)['body'])

    def test_cli_writes_the_failure_and_tail_results_to_its_report(self):
        bad = self.seed('> 引用一句…', '901', legacy=True)
        good = self.seed('A normal preview', '902', legacy=True)
        details = {bad['url']: '> 引用一句完整的话', good['url']: 'A normal preview with the ending'}
        report = self.root / 'evidence-failures.json'
        output = io.StringIO()
        page = type('Page', (), {'open_page': lambda *_args: None})()
        with contextlib.redirect_stdout(output), \
                patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                patch.object(x_article, 'API', 'https://example.invalid'), \
                patch.object(x_article, 'fetch', return_value=None), \
                patch.object(x_post, 'fetch', side_effect=lambda url, *_args, **_kwargs: details[url]):
            self.assertEqual(1, backfill.main(['run', '--resolve-legacy', '--yes', '--report', str(report)]))
        stats = json.loads(report.read_text())
        self.assertEqual(1, stats['failed'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual(['failed', 'updated'], [result['state'] for result in stats['results']])
        self.assertIn('失败 1 条', output.getvalue())
        self.assertEqual(bad, self.current(bad))
        self.assertEqual(details[good['url']], self.current(good)['body'])

    def test_proposal_stamping_has_the_same_per_row_failure_boundary(self):
        bad = self.seed(identity='903')
        good = self.seed(identity='904')
        stamp = x_post_meta.stamp

        def checked_stamp(entry, kind, prefix, **evidence):
            return stamp(entry, kind, '> An unproven boundary\n\n' if entry['url'] == bad['url']
                         else prefix, **evidence)

        with patch.object(x_post, 'fetch', return_value='A short preview with the ending'), \
                patch.object(x_post_meta, 'stamp', side_effect=checked_stamp):
            stats = backfill.repair([bad, good], None, apply=True)
        self.assertEqual(['failed', 'updated'], [result['state'] for result in stats['results']])
        self.assertEqual(1, stats['failed'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual(bad, self.current(bad))
