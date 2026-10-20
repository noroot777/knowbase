"""Combine provenance safety with HTTP policy, layout evidence and resumable audit debt."""
import contextlib
import io
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_body, x_browser, x_channels, x_post
from scripts import x_post_audit as audit, x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_empty_post import empty
from tests.test_x_post import OLD, Page, snapshot
from tests.test_x_post_backfill import backfill_entry, linked_snapshot


class MergeContractTests(ChannelBase):
    def setUp(self):
        super().setUp()
        self.source = self.sub()
        fast = patch.object(x_post, 'STEP', 0)
        fast.start()
        self.addCleanup(fast.stop)

    def seed(self, text, *, proof=True, media=''):
        entry = backfill_entry(tweet('123', text=text, context='Someone reposted'))
        prefix = x_body.parts(entry).prefix
        entry = x_body.record(dict(entry, body=prefix + text + ('\n\n' + media if media else '')),
                              text, prefix=prefix, media=media)
        if not proof:
            entry[x_body.PROVENANCE] = ''
        with reg.db() as conn:
            conn.execute('DELETE FROM subscription_items')
            conn.execute('DELETE FROM feed_items')
        reg.put_items(self.source, [entry])
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='old translation',translated_at='old'")
        return self.saved()

    def saved(self):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def pending(self, **kwargs):
        with reg.db() as conn:
            return backfill.candidates(conn, **kwargs)

    def test_dom_layout_evidence_cannot_grant_unknown_fragment_replacement_permission(self):
        stored = 'Read \n@AsideAI\n today\n\nKeep this paragraph.'
        detail = stored.replace(' \n@AsideAI\n', ' @AsideAI')
        for proof in [True, False]:
            with self.subTest(proof=proof):
                row = self.seed(stored, proof=proof)
                stats = backfill.repair([row], Page(linked_snapshot(detail)),
                                        apply=True, remember=True)
                if proof:
                    self.assertEqual(1, stats['updated'])
                    self.assertEqual(detail, x_body.parts(self.saved()).text)
                    self.assertTrue(x_body.parts(self.saved()).source_known)
                    self.assertEqual([], self.pending())
                else:
                    self.assertEqual(1, stats['unknown_provenance'])
                    self.assertEqual(0, stats['updated'])
                    self.assertEqual(row, self.saved())
                    self.assertEqual([row], self.pending())

    def test_partial_url_repair_refreshes_proof_and_keeps_debt_across_an_identical_reread(self):
        stored = ' \nRead\n\n\nhttps://\nexample.org/path\n\nStill truncated…'
        media = '![图片](https://pbs.twimg.com/a.jpg)'
        row = self.seed(stored, media=media)
        detail = 'Read\n\nhttps://example.org/path\n\nStill truncated with a verified ending.'
        stats = backfill.repair([row], Page(snapshot(detail)), apply=True, remember=True)
        saved = self.saved()
        self.assertEqual(1, stats['updated'])
        self.assertEqual(1, stats['partial'])
        self.assertEqual(row['body'].replace('https://\n', 'https://'), saved['body'])
        self.assertEqual(media, x_body.parts(saved).media)
        self.assertTrue(x_body.parts(saved).source_known)
        self.assertEqual('', saved['body_zh'])
        receipts = audit.load()
        for seen in [row, saved]:
            self.assertEqual('blocked_whitespace', receipts[(seen['key'], audit.version(seen))][1]['remaining'])
        reread = backfill.repair([saved], Page(snapshot(x_body.parts(saved).text)), remember=True)
        self.assertEqual(1, reread['identical'])
        self.assertEqual([saved], self.pending(apply=True))
        complete = x_body.parts(saved).text.replace('truncated…', 'truncated with a verified ending.')
        repaired = backfill.repair([saved], Page(snapshot(complete)), apply=True, remember=True)
        self.assertEqual(1, repaired['updated'])
        self.assertEqual([], self.pending())
        self.assertTrue(x_body.parts(self.saved()).source_known)
        self.assertEqual(media, x_body.parts(self.saved()).media)

    def test_receipts_are_invalidated_by_a_proof_only_change(self):
        row = self.seed('A complete sentence.')
        backfill.repair([row], Page(snapshot('A complete sentence.')), remember=True)
        self.assertEqual([], self.pending())
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_provenance=''")
        unknown = self.saved()
        self.assertEqual(row['content_hash'], unknown['content_hash'])
        self.assertEqual(row['content_rev'], unknown['content_rev'])
        self.assertNotEqual(audit.version(row), audit.version(unknown))
        self.assertEqual([unknown], self.pending())
        stats = backfill.repair([unknown], Page(snapshot('A complete sentence. Extended.')),
                                apply=True, remember=True)
        self.assertEqual(1, stats['unknown_provenance'])
        self.assertEqual([unknown], self.pending())
        self.assertEqual(unknown, self.saved())

    def test_confirmed_zero_byte_legacy_row_is_a_no_write_completed_audit(self):
        self.seed('', proof=False)
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body='',body_provenance='{'")
        row = self.saved()
        for flags in [[], ['--yes', '--recheck']]:
            with self.subTest(flags=flags), contextlib.redirect_stdout(io.StringIO()), \
                 patch.object(x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(Page(empty()))):
                self.assertEqual(0, backfill.main(['run', *flags]))
            self.assertEqual(row, self.saved())
            self.assertFalse(x_body.parts(self.saved()).source_known)
            self.assertEqual([], self.pending())
        receipt = audit.load()[(row['key'], audit.version(row))][1]
        self.assertEqual('empty', receipt['state'])
        self.assertEqual('confirmed_empty', receipt['reason'])

    def test_quote_and_unavailable_notices_coexist_without_hiding_real_incompletion(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            for incomplete in ['', '滚动次数用完']:
                with self.subTest(channel=channel, incomplete=incomplete):
                    self.settings(channel)
                    x_channels.save_watermark(self.source['id'], channel, OLD)
                    entries = x_browser._select([
                        dict(tweet('123', text=''), quoted=True),
                        dict(tweet('124', text='folded…'), truncated=True)], None, 'Example')
                    with patch.object(reg, 'configuration_error', return_value=''), \
                         patch.object(x_browser, 'browser_session',
                                      return_value=contextlib.nullcontext(Page())), \
                         patch.object(x_browser, '_harvest_one', return_value=(entries, incomplete)), \
                         patch.object(x_post, 'fetch', side_effect=x_post.PostGone('页面不存在')), \
                         patch.object(sync, '_stage'):
                        summary = sync.collect('social', FetchScope())
                    saved = reg.subscription('social', self.source['id'])
                    self.assertEqual([], summary['errors'])
                    self.assertEqual(2, summary['notices'][0]['count'])
                    self.assertEqual(1, len(summary['notices'][0]['items']))
                    self.assertIn('本人无正文的引用推文', saved['last_notice'])
                    self.assertIn('页面不存在', saved['last_notice'])
                    self.assertEqual(incomplete, saved['last_note'])
                    self.assertEqual(bool(incomplete), bool(summary['partial']))
                    self.assertEqual(bool(incomplete), x_channels.watermark(saved, channel) == OLD)
                    with reg.db() as conn:
                        self.assertEqual(0, conn.execute('SELECT count(*) FROM feed_items').fetchone()[0])
