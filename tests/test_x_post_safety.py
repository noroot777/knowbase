"""Stored-body replacement requires evidence, including when the detail grows."""
import contextlib
import io
import json
import time
from unittest.mock import patch

from kb.feeds import registry as reg, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_post import Page, snapshot
from tests.test_x_post_backfill import backfill_entry, linked_snapshot


class SafeReplacementTests(ChannelBase):
    def setUp(self):
        super().setUp()
        fast = patch.object(x_post, 'STEP', 0.0)
        fast.start()
        self.addCleanup(fast.stop)
        self.source = self.sub()

    def seed(self, body):
        reg.put_items(self.source, [backfill_entry(tweet('123', text=body))])
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='保留译文',translated_at='old'")
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def saved(self, row):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (row['key'],)).fetchone())

    def test_lost_paragraphs_never_overwrite_the_row_even_with_a_longer_ending(self):
        row = self.seed('第一段讲了一件事。\n\n第二段讲了另一件事。\n\n第三段收尾。')
        for separator in ['', '\n', ' ']:
            for ending in ['', '这是后面新出现的很长很长的完整结尾，不能拿它绕过段落保护。']:
                with self.subTest(separator=separator, ending=ending):
                    detail = row['body'].replace('\n\n', separator) + ending
                    stats = backfill.repair([row], Page(snapshot(detail)), apply=True)
                    self.assertEqual(1, stats['blocked_whitespace'])
                    self.assertEqual(0, stats['updated'])
                    self.assertEqual(row, self.saved(row))

    def test_a_real_url_repair_does_not_authorize_paragraph_loss_elsewhere(self):
        row = self.seed('Read https://\nexample.org/a\n\nKeep this paragraph.\n\nAnd this one.')
        detail = row['body'].replace('https://\n', 'https://').replace('\n\n', ' ')
        stats = backfill.repair([row], Page(linked_snapshot(detail)), apply=True)
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(1, stats['updated'])
        self.assertEqual('blocked_whitespace', stats['results'][0]['remaining'])
        self.assertEqual(row['body'].replace('https://\n', 'https://'), self.saved(row)['body'])

    def test_source_anchors_do_not_authorize_double_break_loss(self):
        row = self.seed('Read\n\n@AsideAI\n\ntoday')
        stats = backfill.repair([row], Page(linked_snapshot('Read @AsideAI today')), apply=True)
        self.assertEqual(1, stats['blocked_whitespace'])
        self.assertEqual(row, self.saved(row))

    def test_inline_cleanup_without_source_evidence_is_blocked(self):
        row = self.seed('Read \n@AsideAI\n today')
        stats = backfill.repair([row], Page(snapshot('Read @AsideAI today')), apply=True)
        self.assertEqual(1, stats['blocked_whitespace'])
        self.assertEqual('unproven_whitespace_change', stats['results'][0]['reason'])
        self.assertEqual(row, self.saved(row))

    def test_a_longer_but_different_body_does_not_overwrite_old_content(self):
        row = self.seed('Keep my original words.')
        stats = backfill.repair([row], Page(snapshot('An entirely different and much longer body.')),
                                apply=True)
        self.assertEqual(1, stats['blocked_content'])
        self.assertEqual(row, self.saved(row))

    def test_added_whitespace_preserves_the_existing_paragraphs(self):
        row = self.seed('One paragraph.\nAnother paragraph.')
        detail = row['body'].replace('\n', '\n\n')
        stats = backfill.repair([row], Page(snapshot(detail)), apply=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual(detail, self.saved(row)['body'])

    def test_source_evidence_is_bound_to_the_exact_detail_and_valid_offsets(self):
        row = self.seed('Read \n@AsideAI\n today')
        entry = backfill.entry_of(row)
        text = 'Read @AsideAI today'
        for evidence in [None, dict(text='another post', inline_breaks=[4, 12]),
                         dict(text=text, inline_breaks=None),
                         dict(text=text, inline_breaks=[True, -1, 999])]:
            with self.subTest(evidence=evidence):
                state, complete, _ = backfill.proposal(entry, text, evidence)
                self.assertEqual('blocked_whitespace', state)
                self.assertIsNone(complete)
        state, complete, _ = backfill.proposal(entry, text, linked_snapshot(text))
        self.assertEqual('normalized', state)
        self.assertEqual(text, complete['body'])
        evidence = linked_snapshot(text)
        evidence['inline_plain'] = 'a differently indexed string'
        self.assertEqual('blocked_evidence', backfill.proposal(entry, text, evidence)[0])

    def test_layout_evidence_must_stabilize_together_with_the_text(self):
        first = snapshot('Read @AsideAI today')
        linked = linked_snapshot(first['text'])
        page = Page(first, linked, linked)
        evidence = {}
        self.assertEqual(first['text'], x_post.fetch('https://x.com/Example/status/123', page,
                                                    evidence=evidence))
        self.assertEqual(3, page.reads)
        self.assertEqual(linked['inline_breaks'], evidence['inline_breaks'])

    def test_a_failed_fetch_clears_old_layout_evidence(self):
        evidence = dict(text='old post', inline_breaks=[4, 12])
        with self.assertRaises(x_post.PostHalted):
            x_post.fetch('https://x.com/Example/status/123', Page(), evidence=evidence,
                         deadline=time.monotonic() - 1)
        self.assertEqual({}, evidence)

    def test_cli_reports_unsafe_changes_as_blocked_without_touching_the_row(self):
        row = self.seed('One paragraph.\n\nAnother paragraph.')
        text = row['body'].replace('\n', '')
        report = self.root / 'safety-report.json'
        with contextlib.redirect_stdout(io.StringIO()) as output, \
             patch.object(backfill.x_browser, 'browser_session',
                          return_value=contextlib.nullcontext(Page(snapshot(text)))):
            self.assertEqual(0, backfill.main(['run', '--yes', '--report', str(report)]))
        stats = json.loads(report.read_text())
        self.assertEqual(1, stats['blocked_whitespace'])
        self.assertIn('空白改动缺少证据 1 条', output.getvalue())
        self.assertEqual(row, self.saved(row))
