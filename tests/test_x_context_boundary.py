"""Every post producer supplies a boundary without misclassifying authored quotes."""
import json
import unittest

from kb.feeds import x_browser, x_feedgrab, x_post, x_post_meta
from tests.test_x_collectors import tweet


class ContextBoundaryTests(unittest.TestCase):
    def entry(self, body):
        return dict(title='A post', body=body, content_kind='post')

    def test_boundary_cannot_be_omitted_even_for_an_article(self):
        for kind in ('post', 'article'):
            with self.subTest(kind=kind), self.assertRaises(TypeError):
                x_post_meta.stamp(self.entry('A post'), kind)

    def test_unexplained_empty_or_incorrect_boundaries_are_rejected(self):
        entry = self.entry('> Quoted context\n\nAuthor text')
        for prefix, evidence in [('', None), ('', 'Author text'), ('> Different\n\n', None)]:
            with self.subTest(prefix=prefix, evidence=evidence), self.assertRaises(x_post.PostUnavailable):
                x_post_meta.stamp(entry, 'post', prefix, author_text=evidence)

    def test_existing_unsafe_empty_boundary_is_not_accepted_as_valid_provenance(self):
        entry = self.entry('> Quoted context\n\nAuthor text')
        stamped = x_post_meta.stamp(entry, 'post', '> Quoted context\n\n')
        meta = json.loads(stamped[x_post_meta.FIELD])
        meta['prefix'] = ''
        stamped[x_post_meta.FIELD] = json.dumps(meta)
        self.assertEqual(('invalid_provenance', ''), x_post_meta.eligibility(stamped))

    def test_context_boundary_uses_only_the_source_declared_block(self):
        context = '> Context first\n> Context second'
        for suffix in ['', '\n\n', '\n\n> Authored quote\n\nAuthor text']:
            body = context + suffix
            expected = context + ('\n\n' if suffix else '')
            with self.subTest(suffix=suffix):
                self.assertEqual(expected, x_post_meta.context_prefix(body, context))
                self.assertEqual('', x_post_meta.context_prefix(body, ''))
        for context in ['not a quote', '> Wrong context']:
            with self.subTest(context=context), self.assertRaises(x_post.PostUnavailable):
                x_post_meta.context_prefix('> Real context\n\nText', context)

    def test_browser_keeps_independently_read_author_quotes_inside_the_post(self):
        text = '> An authored quote\n\nMy own paragraph'
        browser = x_browser._entry(tweet('123', text=text))
        for entry in (browser,):
            with self.subTest(entry=entry):
                self.assertEqual(('', ''), x_post_meta.eligibility(entry))
                self.assertTrue(json.loads(entry[x_post_meta.FIELD])['authored_quote'])
                full = text + '\n\nA complete ending'
                complete = x_post.merge(dict(entry, **{x_post.HINT: ''}), full)
                self.assertEqual(full, complete['body'])
                self.assertEqual(('', ''), x_post_meta.eligibility(complete))

    def test_feedgrab_markdown_quote_is_context_not_self_certified_author_text(self):
        text = '> A quoted tweet\n\nMy own paragraph'
        entry = x_feedgrab._entry(dict(source='https://x.com/Example/status/123',
                                      title='Frontmatter title'), text, 'Example')
        self.assertEqual(('', '> A quoted tweet\n\n'), x_post_meta.eligibility(entry))
        self.assertNotIn('authored_quote', json.loads(entry[x_post_meta.FIELD]))

    def test_plain_body_copies_and_untrusted_evidence_sources_cannot_certify_authored_quotes(self):
        body = '> A quoted tweet\n\nMy own paragraph'
        for evidence in [body, x_post_meta.AuthorText(body, 'feedgrab'),
                         x_post_meta.AuthorText(None, 'detail')]:
            with self.subTest(evidence=evidence), self.assertRaises(x_post.PostUnavailable):
                x_post_meta.stamp(self.entry(body), 'post', '', author_text=evidence)

    def test_an_authored_quote_marker_without_a_trusted_origin_is_not_proof(self):
        entry = x_browser._entry(tweet('123', text='> My own quote\n\nMy paragraph'))
        for source in (None, 'feedgrab', {}):
            meta = json.loads(entry[x_post_meta.FIELD])
            meta['author_source'] = source
            changed = dict(entry, **{x_post_meta.FIELD: json.dumps(meta)})
            with self.subTest(source=source):
                self.assertEqual(('invalid_provenance', ''), x_post_meta.eligibility(changed))

    def test_browser_distinguishes_real_context_from_the_authors_own_quote(self):
        text = '> An authored quote\n\nMy own paragraph'
        entry = x_browser._entry(dict(tweet('123', text=text, context='Someone reposted'), truncated=True))
        prefix = '> Someone reposted\n\n'
        self.assertEqual(('', prefix), x_post_meta.eligibility(entry))
        self.assertEqual(prefix, entry[x_post.HINT])
        full = text + '\n\nA complete ending'
        complete = x_post.merge(entry, full)
        self.assertEqual(prefix + full, complete['body'])
