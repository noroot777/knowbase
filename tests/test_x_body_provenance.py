"""Producer evidence survives SQLite writes; lookalike authored and legacy text never vanishes."""
import json
import time
from unittest.mock import patch

from kb.feeds import grab, grab_store, registry as reg, x_body, x_browser, x_feedgrab, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_empty_post import empty
from tests.test_x_post import BODY, URL, Page, snapshot


class ProvenanceTests(ChannelBase):
    def setUp(self):
        super().setUp()
        self.source = self.sub()
        self.sequence = 123
        fast = patch.object(x_post, 'STEP', 0.0)
        fast.start()
        self.addCleanup(fast.stop)

    def seed(self, entry, *, legacy=False):
        self.sequence += 1
        entry = dict(entry, url=URL.rsplit('/', 1)[0] + '/' + str(self.sequence))
        if legacy:
            entry.pop(x_body.PROVENANCE, None)
        reg.put_items(self.source, [entry])
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE url=?', (entry['url'],)).fetchone())

    def row(self, original):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE key=?', (original['key'],)).fetchone())

    def audit(self, row, text=None):
        state = empty() if text is None else snapshot(text)
        identity = row['url'].rsplit('/', 1)[1]
        return backfill.repair([row], Page(dict(state, id=identity, at=identity)), apply=True)

    def manual(self, text='', *, media=None, quote=None):
        payload = dict(author={'name': 'Example Person'}, text=text, media=media, quote=quote)
        with patch.object(grab, '_tweet_payload', return_value=payload), \
             patch.object(grab.x_article, 'needs_article', return_value=False):
            return grab._grab_x(URL, time.monotonic() + 10)

    def channels(self, text):
        return [x_browser._entry(tweet('123', text=text)),
                x_feedgrab._entry(dict(source=URL, author='Example Person', title=text.splitlines()[0]),
                                  text, 'Example'), self.manual(text)]

    def test_identical_authored_markdown_survives_all_channels_and_legacy_rows_byte_for_byte(self):
        authored = '教程：如何嵌入图片\n\n![图片](https://example.org/tutorial.png)'
        item = dict(tweet('123', text='教程…'), truncated=True)
        entries = [x_post.merge(x_browser._entry(item), authored), *self.channels(authored)]
        for entry in entries:
            for legacy in [False, True]:
                with self.subTest(legacy=legacy, entry=entry):
                    row = self.seed(entry, legacy=legacy)
                    stats = self.audit(row, authored)
                    self.assertEqual(1, stats['identical'])
                    self.assertEqual(0, stats['updated'])
                    self.assertEqual(row, self.row(row))
                    self.assertEqual(authored, x_body.parts(row).text)

    def test_authored_placeholder_never_bypasses_empty_or_shortening_gates(self):
        for entry in self.channels('Example Person 的动态'):
            for legacy in [False, True]:
                with self.subTest(legacy=legacy, entry=entry):
                    row = self.seed(entry, legacy=legacy)
                    self.assertEqual(row['body'], x_body.parts(row).text)
                    for detail in [None, '短']:
                        stats = self.audit(row, detail)
                        self.assertEqual(1, stats['blocked_length'])
                        self.assertEqual(0, stats['empty'])
                        self.assertEqual(0, stats['updated'])
                        self.assertEqual(row, self.row(row))
                    with self.assertRaises(x_post.PostUnavailable):
                        x_post.merge(backfill.entry_of(row), '短')

    def test_authored_prefix_and_video_syntax_remain_authored_even_when_title_aligns(self):
        for text in ['> 自写引用\n\n正文', '正文\n\n[视频](https://example.org/v.mp4)',
                     '正文\n\n![视频封面](https://example.org/v.jpg)']:
            entry = x_feedgrab._entry(dict(source=URL, title='正文'), text, 'Example')
            for legacy in [False, True]:
                with self.subTest(text=text, legacy=legacy):
                    row = self.seed(entry, legacy=legacy)
                    self.assertEqual(x_body.Body('', text), x_body.parts(row))
                    self.assertEqual(1, self.audit(row)['blocked_length'])
                    self.assertEqual(1, self.audit(row, text)['identical'])
                    self.assertEqual(row, self.row(row))

    def test_unmarked_generated_shapes_stay_conservatively_nonempty(self):
        entries = [x_feedgrab._entry(dict(source=URL, author='Example Person'), '', 'Example'),
                   x_browser._entry(tweet('123', text='', context='ctx')),
                   self.manual(media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]})]
        for entry in entries:
            with self.subTest(entry=entry):
                row = self.seed(entry, legacy=True)
                self.assertEqual(row['body'], x_body.parts(row).text)
                stats = self.audit(row)
                self.assertEqual(1, stats['blocked_length'])
                self.assertEqual('remote_empty_stored_text', stats['results'][0]['reason'])
                self.assertEqual(1, self.audit(row, '短')['blocked_length'])
                state = 'identical' if row['body'] == row['body'].strip() else 'blocked_length'
                self.assertEqual(1, self.audit(row, row['body'])[state])
                self.assertEqual('identical', backfill.proposal(backfill.entry_of(row), row['body'])[0])
                self.assertEqual(row, self.row(row))

    def test_verified_media_and_context_survive_replacement_and_repeated_audits(self):
        entry = self.manual('own', media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]},
                            quote={'author': {'screen_name': 'Other'}, 'text': 'quoted'})
        row = self.seed(entry)
        self.assertEqual(entry[x_body.PROVENANCE], row[x_body.PROVENANCE])
        self.assertEqual(1, self.audit(row, BODY)['updated'])
        fresh = self.row(row)
        self.assertNotEqual(row[x_body.PROVENANCE], fresh[x_body.PROVENANCE])
        self.assertEqual(BODY, x_body.parts(fresh).text)
        self.assertEqual('> 引用 @Other：quoted\n\n' + BODY + '\n\n![图片](https://pbs.twimg.com/a.jpg)',
                         fresh['body'])
        self.assertEqual(1, self.audit(fresh, BODY)['identical'])
        same_raw = self.audit(fresh, fresh['body'])
        self.assertEqual(1, same_raw['identical'])
        self.assertEqual(len(fresh['body']), same_raw['results'][0]['after'])
        self.assertEqual(1, self.audit(fresh)['blocked_length'])
        self.assertEqual(fresh, self.row(row))
        self.assertFalse(grab_store.replace(row, entry))
        self.assertEqual(fresh, self.row(row))

    def test_link_normalization_refreshes_proof_in_the_same_guarded_write(self):
        clean = 'https://example.org/long-path'
        entry = self.manual('https://exa\nmple.org/long-path',
                            media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]})
        row = self.seed(entry)
        self.assertEqual(1, self.audit(row, clean)['normalized'])
        fresh = self.row(row)
        self.assertEqual(clean, x_body.parts(fresh).text)
        self.assertEqual(1, self.audit(fresh, clean)['identical'])
        self.assertEqual(fresh, self.row(row))

    def test_bad_missing_or_stale_evidence_and_transient_hint_cannot_strip_characters(self):
        entry = x_browser._entry(tweet('123', text='', context='ctx'))
        proof = json.loads(entry[x_body.PROVENANCE])
        invalid = ['', 'null', '[]', '{', json.dumps({**proof, 'version': 2}),
                   json.dumps({**proof, 'fingerprint': 'bad'}),
                   json.dumps({**proof, 'prefix': 42}),
                   json.dumps({**proof, 'prefix': '> other\n\n'}),
                   json.dumps({**proof, 'media': 'invented'}),
                   json.dumps({**proof, 'fallback': 'invented'})]
        for evidence in invalid:
            with self.subTest(evidence=evidence):
                row = self.seed(dict(entry, body_provenance=evidence))
                self.assertEqual(x_body.Body('', row['body']), x_body.parts(row))
                self.assertEqual(1, self.audit(row)['blocked_length'])
                self.assertEqual(row, self.row(row))
        stale = self.seed(dict(entry, body='> ctx\n\nreal authored text'))
        self.assertEqual(stale['body'], x_body.parts(stale).text)
        self.assertEqual(1, self.audit(stale)['blocked_length'])
        plain = dict(entry, body_provenance='', **{x_post.HINT: entry['body']})
        with self.assertRaises(x_post.PostUnavailable):
            x_post.merge(plain, '短')

    def test_generic_replacement_clears_inapplicable_proof_without_changing_hash_contract(self):
        row = self.seed(x_feedgrab._entry(dict(source=URL), '', 'Example'))
        replacement = {key: row[key] for key in ('title', 'body', 'author', 'published')}
        replacement['body'] = 'Example Person 的动态'
        self.assertTrue(grab_store.replace(row, replacement))
        fresh = self.row(row)
        self.assertEqual('', fresh[x_body.PROVENANCE])
        self.assertEqual(grab_store.item_version(replacement), fresh['content_hash'])
        self.assertEqual(row['content_rev'] + 1, fresh['content_rev'])
        self.assertEqual(1, self.audit(fresh)['blocked_length'])
        self.assertFalse(grab_store.replace(fresh, {**replacement, **{
            x_body.PROVENANCE: row[x_body.PROVENANCE]}}))
        self.assertEqual(fresh, self.row(row))

    def test_old_schema_migration_does_not_invent_provenance(self):
        row = self.seed(x_feedgrab._entry(dict(source=URL), '', 'Example'))
        with reg.db() as conn:
            conn.execute('ALTER TABLE feed_items DROP COLUMN body_provenance')
        migrated = self.row(row)
        self.assertEqual('', migrated[x_body.PROVENANCE])
        self.assertEqual({**row, x_body.PROVENANCE: ''}, migrated)
        self.assertEqual(1, self.audit(migrated)['blocked_length'])
        self.assertEqual(migrated, self.row(row))
