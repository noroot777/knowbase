"""Provenance admission composes with strict detail evidence and durable audit receipts."""
import contextlib
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, x_article, x_browser, x_login_words
from kb.feeds import x_post, x_post_meta, x_post_provenance
from scripts import x_post_audit as audit, x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_access_gate import gate
from tests.test_x_collectors import tweet
from tests.test_x_empty_post import empty
from tests.test_x_post import Page, snapshot
from tests.test_x_post_backfill import CLEAN, LEGACY, backfill_entry, linked_snapshot


class MergeContractTests(ChannelBase):
    def setUp(self):
        super().setUp()
        self.source = self.sub()
        fast = patch.object(x_post, 'STEP', 0)
        fast.start()
        self.addCleanup(fast.stop)

    def seed(self, body=LEGACY, *, legacy=True):
        item = backfill_entry(tweet('123', text=body))
        if legacy:
            item.pop(x_post_meta.FIELD)
        reg.put_items(self.source, [item])
        return self.current()

    def current(self):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def pending(self, **kwargs):
        with reg.db() as conn:
            return backfill.candidates(conn, **kwargs)

    def test_resolved_legacy_uses_dom_evidence_and_records_the_final_repaired_version(self):
        row = self.seed()
        with patch.object(x_article, '_one', return_value=None):
            stats = backfill.repair([row], Page(linked_snapshot(CLEAN)), apply=True,
                                    remember=True, resolve_legacy=True)
        self.assertEqual(1, stats['provenance_saved'])
        self.assertEqual(1, stats['updated'])
        current = self.current()
        self.assertEqual(CLEAN, current['body'])
        self.assertEqual(row['content_rev'] + 2, current['content_rev'])
        self.assertEqual(('', ''), x_post_meta.eligibility(current))
        self.assertEqual('updated', audit.load()[(row['key'], audit.version(current))][1]['state'])
        self.assertEqual([], self.pending(apply=True))
        self.assertEqual([current], self.pending(recheck=True))

    def test_legacy_probe_keeps_the_provenance_write_pending_without_repeating_the_probe(self):
        row = self.seed()
        with patch.object(x_article, '_one', return_value=None):
            stats = backfill.repair([row], Page(linked_snapshot(CLEAN)), remember=True,
                                    resolve_legacy=True)
        self.assertEqual(1, stats['provenance_resolved'])
        self.assertEqual(0, stats['provenance_saved'])
        self.assertEqual(row, self.current())
        self.assertEqual([], self.pending(resolve_legacy=True))
        self.assertEqual([row], self.pending(resolve_legacy=True, scan=True))
        self.assertEqual([row], self.pending(resolve_legacy=True, apply=True))

    def test_identical_legacy_probe_still_allows_a_later_provenance_only_write(self):
        row = self.seed(body='A complete post')
        with patch.object(x_article, '_one', return_value=None):
            stats = backfill.repair([row], Page(snapshot(row['body'])), remember=True,
                                    resolve_legacy=True)
            self.assertEqual(1, stats['identical'])
            self.assertEqual(row, self.current())
            pending = self.pending(apply=True, resolve_legacy=True)
            self.assertEqual([row], pending)
            saved = backfill.repair(pending, Page(snapshot(row['body'])), apply=True,
                                    remember=True, resolve_legacy=True)
        self.assertEqual(1, saved['provenance_saved'])
        self.assertEqual(0, saved['updated'])
        self.assertEqual(row['body'], self.current()['body'])
        self.assertEqual(('', ''), x_post_meta.eligibility(self.current()))
        self.assertEqual([], self.pending(apply=True))

    def test_failed_provenance_cas_cannot_mark_a_concurrently_replaced_version_audited(self):
        row = self.seed(body='Original')

        def replace_before_save(seen, annotated):
            replacement = x_post_meta.stamp(dict(annotated, title='Newer', body='Newer'), 'post', '')
            self.assertTrue(grab_store.replace(seen, replacement))
            return None

        with patch.object(x_article, '_one', return_value=None), \
                patch.object(x_post_provenance, 'save', side_effect=replace_before_save):
            stats = backfill.repair([row], Page(snapshot('Original with ending')), apply=True,
                                    remember=True, resolve_legacy=True)
        self.assertEqual(1, stats['stale'])
        current = self.current()
        self.assertEqual('Newer', current['body'])
        self.assertEqual([current], self.pending(apply=True))
        self.assertNotIn((row['key'], audit.version(current)), audit.load())

    def test_rechecking_the_original_version_retires_a_late_provenance_resolution_receipt(self):
        row = self.seed(body='A complete post')
        save = x_post_provenance.save

        def recheck_after_save(seen, annotated):
            resolved = save(seen, annotated)
            self.assertEqual(1, audit.begin(audit.Candidate(seen, True)))
            return resolved

        with patch.object(x_article, '_one', return_value=None), \
                patch.object(x_post_provenance, 'save', side_effect=recheck_after_save):
            stats = backfill.repair([row], Page(snapshot(row['body'])), apply=True,
                                    remember=True, resolve_legacy=True)
        self.assertEqual(1, stats['provenance_saved'])
        current = self.current()
        self.assertEqual(('', ''), x_post_meta.eligibility(current))
        receipts = audit.load()
        self.assertEqual('deferred', receipts[(row['key'], audit.version(row))][1]['state'])
        self.assertNotIn((row['key'], audit.version(current)), receipts)
        self.assertEqual([current], self.pending(apply=True))

    def test_explicit_empty_legacy_resolution_preserves_fragment_unknown_and_original_content(self):
        row = self.seed(body='')
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_provenance='' WHERE key=?", (row['key'],))
        row = self.current()
        for apply in (False, True):
            with self.subTest(apply=apply), patch.object(x_article, '_one', return_value=None):
                stats = backfill.repair([row], Page(empty()), apply=apply,
                                        remember=True, resolve_legacy=True)
            self.assertEqual(1, stats['empty'])
            self.assertEqual(1, stats['provenance_resolved'])
            self.assertEqual(int(apply), stats['provenance_saved'])
            self.assertEqual(0, stats['updated'])
            current = self.current()
            self.assertEqual(row['body'], current['body'])
            self.assertEqual(row['content_hash'], current['content_hash'])
            self.assertEqual(row['exported_hash'], current['exported_hash'])
            self.assertEqual(row['body_provenance'], current['body_provenance'])
            self.assertEqual('', current['body_provenance'])
            if apply:
                self.assertEqual(('', ''), x_post_meta.eligibility(current))
                self.assertEqual('empty', audit.load()[(row['key'], audit.version(current))][1]['state'])
            else:
                self.assertEqual(row, current)

    def test_remote_empty_cannot_resolve_a_nonempty_legacy_row(self):
        row = self.seed(body='Old unknown text')
        with patch.object(x_article, '_one', return_value=None):
            stats = backfill.repair([row], Page(empty()), apply=True, resolve_legacy=True)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(0, stats['provenance_saved'])
        self.assertEqual(row, self.current())

    def test_partial_normalization_cannot_self_certify_reconstructed_author_quotes(self):
        row = self.seed(body='> Read\nthis https://\nexample.org/path', legacy=False)
        stats = backfill.repair([row], Page(snapshot('> Read this https://example.org/path with ending')),
                                apply=True, remember=True)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.current())
        self.assertEqual([row], self.pending(apply=True))

    def test_shared_gate_verdict_reaches_connection_and_both_import_messages(self):
        page = Page(gate())
        with patch.object(x_browser, 'profile_busy', return_value=False), \
                patch.object(x_browser, 'has_login_footprint', return_value=True), \
                patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)), \
                patch.object(x_browser.time, 'sleep'):
            ready, detail = x_browser.probe('unused')
        self.assertFalse(ready)
        self.assertEqual(2, page.reads)
        self.assertIn('完成验证后重试', detail)
        for verdict, state in [('challenge', {}), ('unconfirmed', dict(again='challenge'))]:
            with self.subTest(verdict=verdict):
                detail = x_login_words._detail(verdict, 2, state, '')
                self.assertIn('安全验证', detail)
                self.assertIn('完成验证后重试', detail)
                self.assertNotIn('当场也能用', detail)
