"""Context-only posts retain a separated, fingerprinted prefix through both merges."""
import json
from unittest.mock import patch

from kb.feeds import grab, registry as reg, x_article, x_post, x_post_meta
from scripts import x_post_backfill as backfill
from tests.test_x_post_provenance import ProvenanceBase
from tests.test_x_post_backfill import CLEAN, LEGACY, linked_snapshot


class PrefixSeparationTests(ProvenanceBase):
    context = '> 引用 @Origin：原话\n> 第二行'
    detail = '作者自己的正文\n\n正文的下一段'

    def test_context_only_is_normalized_before_fingerprinting_without_mutating_input(self):
        entry = dict(title='A post', body=self.context, content_kind='post')
        prefix = x_post_meta.context_prefix(entry['body'], self.context)
        self.assertEqual(self.context + '\n\n', prefix)
        stamped = x_post_meta.stamp(entry, 'post', prefix)
        self.assertEqual(self.context, entry['body'])
        self.assertEqual(prefix, stamped['body'])
        self.assertEqual(('', prefix), x_post_meta.eligibility(stamped))
        self.assertEqual(stamped, x_post_meta.stamp(stamped, 'post', prefix))

    def test_unterminated_prefix_is_rejected_for_empty_and_nonempty_author_text(self):
        for tail in ('', '\n\n' + self.detail):
            entry = dict(title='A post', body=self.context + tail)
            with self.subTest(tail=tail), self.assertRaises(x_post.PostUnavailable):
                x_post_meta.stamp(entry, 'post', self.context)

    def test_preexisting_matching_fingerprint_cannot_authorize_an_unterminated_prefix(self):
        for tail in ('', '\n\n' + self.detail):
            entry = x_post_meta.stamp(dict(title='A post', body=self.context + tail), 'article', '')
            meta = json.loads(entry[x_post_meta.FIELD])
            meta.update(kind='post', prefix=self.context)
            entry[x_post_meta.FIELD] = json.dumps(meta)
            with self.subTest(tail=tail):
                self.assertEqual(('invalid_provenance', ''), x_post_meta.eligibility(entry))

    def test_both_merge_paths_preserve_the_separation_and_the_replacement_is_repeatable(self):
        prefix = self.context + '\n\n'
        entry = x_post_meta.stamp(dict(title='A post', body=self.context, content_kind='post'),
                                  'post', prefix)
        entry[x_post.HINT] = prefix
        complete = x_post.merge(entry, self.detail)
        self.assertEqual(prefix + self.detail, complete['body'])
        self.assertEqual(('', prefix), x_post_meta.eligibility(complete))
        state, normalized, _info = backfill.proposal(complete | {x_post.HINT: prefix}, self.detail)
        self.assertEqual('identical', state)
        self.assertIsNone(normalized)
        source = x_post_meta.stamp(dict(title='A post', body=prefix + LEGACY, content_kind='post'),
                                   'post', prefix)
        evidence = linked_snapshot(CLEAN)
        evidence['text'] = CLEAN
        state, normalized, _info = backfill.proposal(source | {x_post.HINT: prefix}, CLEAN, evidence)
        self.assertEqual('normalized', state)
        self.assertEqual(prefix + CLEAN, normalized['body'])
        self.assertEqual(('', prefix), x_post_meta.eligibility(normalized))

    def test_manual_quote_only_post_roundtrips_and_audits_without_gluing_authors(self):
        url = 'https://x.com/Someone/status/4242'
        payload = dict(text=None, author=dict(name='Someone', screen_name='Someone'),
                       quote=dict(author=dict(screen_name='Origin'), text='被引用的原话'), media=None)
        with patch.object(grab, '_tweet_payload', return_value=payload), \
                patch.object(x_article, 'fetch', return_value=None):
            entry = grab._grab_x(url, 1e9)
        prefix = '> 引用 @Origin：被引用的原话\n\n'
        self.assertEqual(prefix, entry['body'])
        reg.put_items(self.source, [entry])
        with reg.db() as conn:
            row = dict(conn.execute('SELECT * FROM feed_items WHERE url=?', (url,)).fetchone())
        with patch.object(x_post, 'fetch', return_value=self.detail):
            stats = backfill.repair([row], None, apply=True)
            self.assertEqual(1, stats['updated'])
            saved = self.current(row)
            self.assertEqual(prefix + self.detail, saved['body'])
            self.assertEqual(('', prefix), x_post_meta.eligibility(saved))
            again = backfill.repair([saved], None, apply=True)
        self.assertEqual(1, again['identical'])
        self.assertEqual(0, again['updated'])
        self.assertEqual(saved, self.current(row))
