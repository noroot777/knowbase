"""Confirmed URL intervals survive unrelated, unproven layout differences."""
import contextlib
import io
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, x_body, x_post, x_post_meta
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_post import Page, snapshot
from tests.test_x_post_backfill import backfill_entry, linked_snapshot

STORED = ('AI 乃至全领域最强主播 Lex Fridman 全部访谈整理好了！\n\n'
          '一共 502 期，全都用本地 ASR 识别配了文稿和中文改写翻译。\n\n\n'
          'https://\nlex.qiaomu.ai\n\n'
          '为提供最好的阅读体验，让 AI 加了中文开源字体、精细排版功能，支持亮色、暗色和护眼模式。\n\n'
          '特意支持了 PWA，可安装成手机应用阅读。')
DETAIL = STORED.replace('。\n\n\nhttps://\n', '。\n\nhttps://') + '\n\n强烈建议收藏，开源仓库见评论区。'


class PartialRepairTests(ChannelBase):
    def setUp(self):
        super().setUp()
        fast = patch.object(x_post, 'STEP', 0)
        fast.start()
        self.addCleanup(fast.stop)
        self.source = self.sub()

    def seed(self, body, context=''):
        reg.put_items(self.source, [backfill_entry(tweet('123', text=body, context=context))])
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='旧译文',translated_at='old',"
                         "path='social/old.md',exported_hash=content_hash")
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def saved(self):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def test_real_report_shape_repairs_url_only_and_keeps_all_unproven_paragraphs(self):
        row = self.seed(STORED, 'Someone reposted')
        evidence = dict(snapshot(DETAIL), inline_breaks=[59, 80],
                        inline_plain=''.join(DETAIL.split()), inline_legacy=DETAIL)
        stats = backfill.repair([row], Page(evidence), apply=True)
        saved = self.saved()
        self.assertEqual(1, stats['updated'])
        self.assertEqual(1, stats['partial'])
        self.assertEqual('blocked_whitespace', stats['results'][0]['remaining'])
        self.assertEqual(row['body'].replace('https://\n', 'https://'), saved['body'])
        self.assertNotIn('开源仓库见评论区', saved['body'])
        self.assertEqual(row['content_rev'] + 1, saved['content_rev'])
        self.assertEqual('', saved['body_zh'])
        self.assertEqual('', saved['translated_at'])
        self.assertEqual(1, saved['extract_stale'])
        self.assertEqual(row['path'], saved['path'])
        self.assertEqual(row['exported_hash'], saved['exported_hash'])
        self.assertNotEqual(row['content_hash'], saved['content_hash'])

    def test_url_prefix_newline_is_not_a_reason_to_discard_two_proven_url_breaks(self):
        stored = ('以假乱真，…正式开源了！\n👇这是它制作的自己的介绍短片，需要的可以自取：\n'
                  'https://\ngithub.com/alchaincyf/hua\nshu-motion\n…')
        expected = stored.replace('https://\n', 'https://').replace('hua\nshu', 'huashu')[:-2]
        detail = expected.replace('自取：\n', '自取：')
        row = self.seed(stored)
        stats = backfill.repair([row], Page(dict(snapshot(detail), inline_breaks=[69, 112],
                               inline_plain=''.join(detail.split()), inline_legacy=detail)), apply=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual(expected, self.saved()['body'])
        self.assertIn('以假乱真，…正式开源了！', self.saved()['body'])

    def test_partial_cleanup_does_not_remove_a_non_url_truncation_marker_or_outer_whitespace(self):
        stored = ' \nRead\n\n\nhttps://\nexample.org/path\n\nStill truncated…\n '
        row = self.seed(stored)
        detail = 'Read\n\nhttps://example.org/path\n\nStill truncated and now complete.'
        stats = backfill.repair([row], Page(snapshot(detail)), apply=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual(row['body'].replace('https://\n', 'https://'), self.saved()['body'])
        self.assertIn('truncated…', self.saved()['body'])

    def test_partial_url_evidence_does_not_need_unrelated_layout_evidence(self):
        row = self.seed('Read 😂\n\n\nhttps://\nexample.org/path')
        detail = 'Read 😂\n\nhttps://example.org/path'
        evidence = linked_snapshot(detail)
        evidence['inline_legacy'] = evidence['inline_legacy'].replace('😂', '')
        stats = backfill.repair([row], Page(evidence), apply=True)
        self.assertEqual(1, stats['updated'])
        self.assertEqual('blocked_evidence', stats['results'][0]['remaining'])
        self.assertEqual(row['body'].replace('https://\n', 'https://'), self.saved()['body'])

    def test_no_proven_url_change_or_nonwhitespace_conflict_preserves_the_entire_row(self):
        for stored, detail in [
                ('First\n\n\nSecond', 'First\n\nSecond'),
                ('Read\n\n\nhttps://\nexample.org/path\nImportant',
                 'Read\n\nhttps://example.org/path\nDifferent'),
                ('Read\n\n\nhttps://\nexample.org/path', 'Read\n\nhttps://example.org/edited')]:
            with self.subTest(stored=stored):
                with reg.db() as conn:
                    conn.execute('DELETE FROM subscription_items')
                    conn.execute('DELETE FROM feed_items')
                row = self.seed(stored)
                stats = backfill.repair([row], Page(snapshot(detail)), apply=True)
                self.assertEqual(0, stats['updated'])
                self.assertEqual(row, self.saved())

    def test_fully_proven_alignment_still_expands_and_partial_update_still_uses_cas(self):
        row = self.seed('Read https://\nexample.org/path')
        detail = 'Read https://example.org/path\n\nA complete new paragraph.'
        stats = backfill.repair([row], Page(snapshot(detail)), apply=True)
        self.assertEqual(1, stats['expanded'])
        self.assertEqual(detail, self.saved()['body'])
        stale = self.saved()
        replacement = dict(backfill.entry_of(stale), title='newer', body=STORED)
        replacement = x_body.record(replacement, STORED)
        replacement.pop(x_post.HINT)
        replacement = x_post_meta.stamp(replacement, 'post', '')
        self.assertTrue(grab_store.replace(stale, replacement))
        current = self.saved()
        replacement.update(body='Concurrently replaced', title='Concurrently replaced')
        replacement = x_post_meta.stamp(replacement, 'post', '')
        self.assertTrue(grab_store.replace(current, replacement))
        newest = self.saved()
        stats = backfill.repair([current], Page(snapshot(DETAIL)), apply=True)
        self.assertEqual(1, stats['stale'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(0, stats['partial'])
        self.assertEqual(newest, self.saved())

    def test_probe_does_not_count_a_partial_proposal_as_a_completed_repair(self):
        row = self.seed(STORED)
        stats = backfill.repair([row], Page(snapshot(DETAIL)))
        self.assertEqual(1, stats['normalized'])
        self.assertEqual(0, stats['partial'])
        self.assertEqual(0, stats['updated'])
        self.assertEqual(row, self.saved())

    def test_cli_reports_partial_not_full_expansion(self):
        self.seed(STORED)
        with contextlib.redirect_stdout(io.StringIO()) as output, patch.object(
                backfill.x_browser, 'browser_session',
                return_value=contextlib.nullcontext(Page(snapshot(DETAIL)))):
            self.assertEqual(0, backfill.main(['run', '--yes']))
        self.assertIn('仅作局部修复 1 条', output.getvalue())
        self.assertEqual(STORED.replace('https://\n', 'https://'), self.saved()['body'])
