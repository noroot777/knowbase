"""All X body writers keep provenance; ambiguous imports need independent recovery."""
import json
from unittest.mock import patch

from kb.feeds import registry as reg, x_article, x_feedgrab, x_post, x_post_meta
from scripts import x_article_backfill, x_post_backfill
from tests.test_x_post_provenance import ProvenanceBase


class AuditInvariantTests(ProvenanceBase):
    def test_feedgrab_preserves_both_long_and_short_quote_contexts_when_expanding(self):
        for index, context in enumerate(['> 引用 @a:短', '> 引用 @a:' + '很长的被引用原话' * 30]):
            body = context + '\n\nAuthor preview'
            entry = x_feedgrab._entry(dict(source=f'https://x.com/Example/status/{500 + index}',
                                          title='An imported title'), body, 'Example')
            reg.put_items(self.source, [entry])
            with reg.db() as conn:
                row = dict(conn.execute('SELECT * FROM feed_items WHERE url=?', (entry['url'],)).fetchone())
            detail = 'Author preview with the complete ending'
            with self.subTest(context=context), patch.object(x_post, 'fetch', return_value=detail):
                result = x_post_backfill.repair([row], None, apply=True)
                self.assertEqual(1, result['updated'])
                saved = self.current(row)
                self.assertEqual(context + '\n\n' + detail, saved['body'])
                self.assertEqual(('', context + '\n\n'), x_post_meta.eligibility(saved))

    def test_feedgrab_unseparated_or_context_only_quotes_remain_unclassified(self):
        for body in ['> A context only', '> A context\nUnseparated nonquote text',
                     'Author text\n\n> A trailing quote', '> Context\n\nText\n\n> Another quote']:
            with self.subTest(body=body):
                entry = x_feedgrab._entry(dict(source='https://x.com/Example/status/123'), body, 'Example')
                self.assertNotIn(x_post_meta.FIELD, entry)
                self.assertEqual(('unclassified_legacy', ''), x_post_meta.eligibility(entry))

    def test_article_backfill_atomically_replaces_the_empty_post_source(self):
        row = self.seed('')
        with reg.db() as conn:
            self.assertTrue(x_article_backfill.apply_one(conn, row, 'An Article', 'The whole Article'))
        saved = self.current(row)
        self.assertNotEqual(row[x_post_meta.FIELD], saved[x_post_meta.FIELD])
        self.assertEqual(('article_merged', ''), x_post_meta.eligibility(saved))
        with reg.db() as conn:
            self.assertEqual([], x_post_backfill.candidates(conn, resolve_legacy=True))
        with patch.object(x_post, 'fetch') as detail, patch.object(x_article, 'fetch') as article:
            self.assertEqual(1, x_post_backfill.repair([saved], None, resolve_legacy=True)['skipped_article'])
        detail.assert_not_called()
        article.assert_not_called()

    def test_a_historical_self_certified_quote_requires_remote_recovery_before_writing(self):
        row = self.seed(context='The quoted words')
        meta = json.loads(row[x_post_meta.FIELD])
        meta.update(prefix='', authored_quote=True)
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET x_post_meta=? WHERE key=?', (json.dumps(meta), row['key']))
        row = self.current(row)
        with patch.object(x_post, 'fetch') as fetch:
            self.assertEqual(1, x_post_backfill.repair([row], None, apply=True)['skipped_ambiguous'])
        fetch.assert_not_called()
        detail = 'A short preview followed by the complete text'
        with patch.object(x_article, 'API', 'https://example.invalid'), \
                patch.object(x_article, 'fetch', return_value=None), \
                patch.object(x_post, 'fetch', return_value=detail):
            stats = x_post_backfill.repair([row], None, apply=True, resolve_legacy=True)
        self.assertEqual(1, stats['provenance_saved'])
        self.assertEqual(1, stats['updated'])
        saved = self.current(row)
        self.assertEqual('> The quoted words\n\n' + detail, saved['body'])
        self.assertEqual(('', '> The quoted words\n\n'), x_post_meta.eligibility(saved))

    def test_invalid_source_requires_the_same_remote_evidence_as_unknown_legacy_rows(self):
        for index, article, detail, expected in [
                (0, None, 'A short preview followed by the complete text', 'post'),
                (1, dict(title='Article', body='A short preview'), '', 'article'),
                (2, None, 'Unrelated detail cannot prove this stored text', 'unknown')]:
            row = self.seed(identity=str(600 + index))
            with reg.db() as conn:
                conn.execute('UPDATE feed_items SET x_post_meta=? WHERE key=?', ('{broken', row['key']))
            row = self.current(row)
            with reg.db() as conn:
                self.assertEqual([], x_post_backfill.candidates(conn, urls=[row['url']]))
                self.assertEqual([row], x_post_backfill.candidates(conn, urls=[row['url']], resolve_legacy=True))
            with self.subTest(expected=expected), patch.object(x_article, 'API', 'https://example.invalid'), \
                    patch.object(x_article, 'fetch', return_value=article), \
                    patch.object(x_post, 'fetch', return_value=detail) as fetch:
                stats = x_post_backfill.repair([row], None, apply=True, resolve_legacy=True)
            if expected == 'unknown':
                self.assertEqual(0, stats['provenance_saved'])
                self.assertEqual(row, self.current(row))
            else:
                self.assertEqual(1, stats['provenance_saved'])
                self.assertEqual('article_merged' if expected == 'article' else '',
                                 x_post_meta.eligibility(self.current(row))[0])
            if expected == 'article':
                fetch.assert_not_called()
