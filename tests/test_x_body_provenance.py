"""Producer evidence survives SQLite writes; lookalike authored and legacy text never vanishes."""
import contextlib
import io
import json
import time
from unittest.mock import patch

from kb.feeds import grab, grab_store, registry as reg, x_body, x_browser, x_feedgrab, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_empty_post import empty
from tests.test_x_post import BODY, PREVIEW, URL, Page, snapshot


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
                        state = ('unknown_provenance' if legacy else
                                 'blocked_length' if detail is None else 'blocked_content')
                        self.assertEqual(1, stats[state])
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
                    self.assertEqual(x_body.Body('', text, source_known=not legacy), x_body.parts(row))
                    state = 'unknown_provenance' if legacy else 'blocked_length'
                    self.assertEqual(1, self.audit(row)[state])
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
                self.assertEqual(1, stats['unknown_provenance'])
                self.assertEqual('body_provenance_unknown', stats['results'][0]['reason'])
                self.assertEqual(1, self.audit(row, '短')['unknown_provenance'])
                state = 'identical' if row['body'] == row['body'].strip() else 'unknown_provenance'
                self.assertEqual(1, self.audit(row, row['body'])[state])
                self.assertEqual('identical', backfill.proposal(backfill.entry_of(row), row['body'])[0])
                self.assertEqual(row, self.row(row))

    def test_verified_media_and_context_survive_replacement_and_repeated_audits(self):
        entry = self.manual('own', media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]},
                            quote={'author': {'screen_name': 'Other'}, 'text': 'quoted'})
        row = self.seed(entry)
        detail = 'own\n\n' + BODY
        self.assertEqual(entry[x_body.PROVENANCE], row[x_body.PROVENANCE])
        self.assertEqual(1, self.audit(row, detail)['updated'])
        fresh = self.row(row)
        self.assertNotEqual(row[x_body.PROVENANCE], fresh[x_body.PROVENANCE])
        self.assertEqual(detail, x_body.parts(fresh).text)
        self.assertEqual('> 引用 @Other：quoted\n\n' + detail + '\n\n![图片](https://pbs.twimg.com/a.jpg)',
                         fresh['body'])
        self.assertEqual(1, self.audit(fresh, detail)['identical'])
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
        invalid = ['', 'null', '[]', '{', '[' * 2000, json.dumps({**proof, 'version': 2}),
                   json.dumps({**proof, 'fingerprint': 'bad'}),
                   json.dumps({**proof, 'prefix': 42}),
                   json.dumps({**proof, 'prefix': '> other\n\n'}),
                   json.dumps({**proof, 'media': 'invented'}),
                   json.dumps({**proof, 'fallback': 'invented'})]
        for evidence in invalid:
            with self.subTest(evidence=evidence):
                row = self.seed(dict(entry, body_provenance=evidence))
                self.assertEqual(x_body.Body('', row['body']), x_body.parts(row))
                self.assertEqual(1, self.audit(row)['unknown_provenance'])
                self.assertEqual(1, self.audit(row, BODY)['unknown_provenance'])
                with self.assertRaises(x_body.SourceUnknown):
                    x_body.with_text(row, BODY)
                self.assertEqual(row, self.row(row))
        stale = self.seed(dict(entry, body='> ctx\n\nreal authored text'))
        self.assertEqual(stale['body'], x_body.parts(stale).text)
        self.assertEqual(1, self.audit(stale)['unknown_provenance'])
        self.assertEqual(1, self.audit(stale, BODY)['unknown_provenance'])
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
        self.assertEqual(1, self.audit(fresh)['unknown_provenance'])
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
        self.assertEqual(1, self.audit(migrated)['unknown_provenance'])
        self.assertEqual(migrated, self.row(row))

    def test_known_no_fragments_and_unknown_source_are_distinct_even_with_identical_text(self):
        entry = x_browser._entry(tweet('123', text=PREVIEW))
        known = x_body.parts(entry)
        unknown = x_body.parts({**entry, x_body.PROVENANCE: ''})
        self.assertEqual(known.text, unknown.text)
        self.assertTrue(known.source_known)
        self.assertFalse(unknown.source_known)
        self.assertNotEqual(known, unknown)
        with self.assertRaises(x_body.SourceUnknown):
            unknown.require_source()
        complete = x_body.with_text(entry, BODY)
        self.assertEqual(x_body.Body('', BODY, source_known=True), x_body.parts(complete))

    def test_real_old_schema_migration_blocks_longer_overwrite_of_context_media_and_placeholder(self):
        media = {'photos': [{'url': 'https://pbs.twimg.com/media/a.jpg'}]}
        entries = [x_browser._entry(tweet('123', text=PREVIEW, context='向阳乔木 转推了')),
                   self.manual(PREVIEW, media=media),
                   x_browser._entry(tweet('123', text='', context='向阳乔木 转推了')),
                   x_feedgrab._entry(dict(source=URL, author_name='Example Person'), '', 'Example'),
                   self.manual(media=media)]
        for entry in entries:
            with self.subTest(entry=entry):
                original = self.seed(entry)
                with reg.db() as conn:
                    conn.execute('ALTER TABLE feed_items DROP COLUMN body_provenance')
                row = self.row(original)
                self.assertEqual('', row[x_body.PROVENANCE])
                self.assertGreater(len(BODY), len(row['body']))
                with patch.object(grab_store, 'replace', wraps=grab_store.replace) as replace, \
                     patch.object(x_body, 'record', wraps=x_body.record) as record:
                    for detail in [BODY, None, '短']:
                        stats = self.audit(row, detail)
                        self.assertEqual(1, stats['unknown_provenance'])
                        self.assertEqual(0, stats['updated'])
                        self.assertEqual(0, stats['empty'])
                        self.assertEqual(0, stats['blocked_length'])
                        self.assertEqual('body_provenance_unknown', stats['results'][0]['reason'])
                        self.assertEqual(len(row['body']), stats['results'][0]['after'])
                        self.assertIn('来源待核', stats['results'][0]['detail'])
                        self.assertEqual(row, self.row(row))
                    replace.assert_not_called()
                    record.assert_not_called()
                with self.assertRaisesRegex(x_post.PostUnavailable, '来源待核'):
                    x_post.merge(backfill.entry_of(row), BODY)
                with self.assertRaises(x_body.SourceUnknown):
                    x_body.with_text(row, BODY)

    def test_unknown_source_cannot_normalize_links_or_mint_a_proof_but_identical_is_noop(self):
        broken, clean = 'https://exa\nmple.org/long-path\n…', 'https://example.org/long-path'
        entry = x_browser._entry(tweet('123', text=broken))
        for evidence in ['', '{', json.dumps({'version': 99})]:
            with self.subTest(evidence=evidence):
                row = self.seed(dict(entry, body_provenance=evidence))
                self.assertTrue(backfill.aligned_preview(row['body'], clean))
                stats = self.audit(row, clean)
                self.assertEqual(1, stats['unknown_provenance'])
                self.assertEqual(0, stats['normalized'])
                self.assertEqual(0, stats['updated'])
                self.assertTrue(stats['results'][0]['artifacts'])
                self.assertEqual(1, self.audit(row, row['body'])['identical'])
                self.assertEqual(row, self.row(row))

    def test_unknown_source_cli_is_retryable_in_readonly_and_apply_modes(self):
        row = self.seed(x_browser._entry(tweet('123', text=PREVIEW, context='ctx')), legacy=True)
        identity = row['url'].rsplit('/', 1)[1]
        report = self.root / 'unknown-source-report.json'
        for flags in [[], ['--yes']]:
            output = io.StringIO()
            page = Page(dict(snapshot(BODY), id=identity, at=identity))
            with self.subTest(flags=flags), contextlib.redirect_stdout(output), \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(page)):
                self.assertEqual(1, backfill.main(['run', '--report', str(report), *flags]))
                stats = json.loads(report.read_text())
                self.assertEqual(1, stats['unknown_provenance'])
                self.assertEqual(0, stats['expanded'])
                self.assertEqual(0, stats['failed'])
                self.assertIn('来源待核', output.getvalue())
                self.assertEqual(row, self.row(row))

    def test_live_completion_keeps_unknown_source_retryable_without_rebuilding_content(self):
        row = self.seed(x_browser._entry(tweet('123', text=PREVIEW, context='ctx')), legacy=True)
        identity = row['url'].rsplit('/', 1)[1]
        page = Page(dict(snapshot(BODY), id=identity, at=identity))
        entries, note = x_post.complete([backfill.entry_of(row)], session=page)
        self.assertEqual([], entries)
        self.assertIn('下次同步', note)
        self.assertEqual(row, self.row(row))

    def test_zero_byte_legacy_row_is_successfully_audited_without_provenance_or_writes(self):
        original = self.seed(x_browser._entry(tweet('123', text='')))
        with reg.db() as conn:
            conn.execute('ALTER TABLE feed_items DROP COLUMN body_provenance')
        row = self.row(original)
        self.assertEqual({**original, x_body.PROVENANCE: ''}, row)
        self.assertEqual('', row['body'])
        identity = row['url'].rsplit('/', 1)[1]
        with patch.object(grab_store, 'replace', wraps=grab_store.replace) as replace, \
             patch.object(x_body, 'record', wraps=x_body.record) as record:
            for kind in ['photo', 'video', 'quote', 'link', 'unspecified']:
                for apply in [False, True]:
                    with self.subTest(kind=kind, apply=apply):
                        page = Page(dict(empty(kind), at=identity, id=identity))
                        stats = backfill.repair([row], page, apply=apply)
                        self.assertEqual(1, stats['empty'])
                        self.assertEqual(0, stats['unknown_provenance'])
                        self.assertEqual(0, stats['blocked_length'])
                        self.assertEqual(0, stats['updated'])
                        self.assertEqual(0, stats['failed'])
                        self.assertEqual(2, page.reads)
                        result = stats['results'][0]
                        self.assertEqual('confirmed_empty', result['reason'])
                        self.assertEqual(kind, result['empty_kind'])
                        self.assertEqual(0, result['before'])
                        self.assertEqual(0, result['after'])
                        self.assertEqual(row, self.row(row))
                        self.assertFalse(x_body.parts(self.row(row)).source_known)
            replace.assert_not_called()
            record.assert_not_called()

    def test_zero_byte_empty_cli_succeeds_without_repairing_missing_or_invalid_proof(self):
        entry = x_browser._entry(tweet('123', text=''))
        stale = x_browser._entry(tweet('123', text=PREVIEW))[x_body.PROVENANCE]
        evidence_values = ['', '{', json.dumps({'version': 99}), stale, entry[x_body.PROVENANCE]]
        for evidence in evidence_values:
            original = self.seed(dict(entry, body_provenance=evidence))
            if not evidence:
                with reg.db() as conn:
                    conn.execute('ALTER TABLE feed_items DROP COLUMN body_provenance')
            row = self.row(original)
            identity = row['url'].rsplit('/', 1)[1]
            report = self.root / 'zero-byte-empty-report.json'
            for flags in [[], ['--yes']]:
                with self.subTest(evidence=evidence, flags=flags):
                    output = io.StringIO()
                    page = Page(dict(empty(), at=identity, id=identity))
                    with contextlib.redirect_stdout(output), \
                         patch.object(backfill.x_browser, 'browser_session',
                                      return_value=contextlib.nullcontext(page)):
                        code = backfill.main(['run', '--url', row['url'],
                                              '--report', str(report), *flags])
                    self.assertEqual(0, code)
                    stats = json.loads(report.read_text())
                    self.assertEqual(1, stats['empty'])
                    self.assertEqual(0, stats['unknown_provenance'])
                    self.assertEqual(0, stats['updated'])
                    self.assertEqual(0, stats['failed'])
                    self.assertIn('已确认无自写文字 1 条', output.getvalue())
                    self.assertEqual(row, self.row(row))

    def test_nonempty_unknown_rows_including_whitespace_do_not_use_empty_noop_exception(self):
        entry = x_browser._entry(tweet('123', text=''))
        for body in [' ', '\n\t', 'authored', '> ctx\n\n',
                     '![图片](https://pbs.twimg.com/media/a.jpg)']:
            row = self.seed(dict(entry, body=body, body_provenance=''))
            identity = row['url'].rsplit('/', 1)[1]
            for kind in ['photo', 'video', 'quote', 'link', 'unspecified']:
                with self.subTest(body=body, kind=kind):
                    page = Page(dict(empty(kind), at=identity, id=identity))
                    stats = backfill.repair([row], page, apply=True)
                    self.assertEqual(0, stats['empty'])
                    self.assertEqual(1, stats['unknown_provenance'])
                    self.assertEqual(0, stats['updated'])
                    self.assertEqual('body_provenance_unknown', stats['results'][0]['reason'])
                    self.assertEqual(len(body), stats['results'][0]['after'])
                    self.assertEqual(row, self.row(row))

    def test_zero_byte_unknown_row_still_requires_positive_remote_empty_proof(self):
        row = self.seed(x_browser._entry(tweet('123', text='')), legacy=True)
        identity = row['url'].rsplit('/', 1)[1]
        states = [snapshot(''), empty('unknown'), dict(empty(), ready='loading'),
                  dict(empty(), at='999'), dict(empty(), id='999'), dict(empty(), truncated=True)]
        for state in states:
            snap = dict(state, at=identity if state.get('at') == '123' else state['at'],
                        id=identity if state.get('id') == '123' else state['id'])
            with self.subTest(state=state), patch.object(x_post, 'SECONDS', 0.01):
                stats = backfill.repair([row], Page(snap), apply=True)
                self.assertEqual(0, stats['empty'])
                self.assertEqual(0, stats['unknown_provenance'])
                self.assertEqual(1, stats['failed'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(row, self.row(row))
