"""真正缺失可撤销,永久排除不可撤销;不靠 URL 或路径猜引文身份。"""

from __future__ import annotations

import json
from types import SimpleNamespace

from kb import scanner, store
from kb.chunker import split_chunks
from tests.fav_snippet_fixture import REAL_CONNECT
from tests.scan_fixture import NOW, ScanFixture, TEXT


class ScanFavoriteRecoveryTests(ScanFixture):
    def test_the_same_original_restores_article_and_snippet_without_new_ids_or_cards(self):
        path = self.seed()
        self.favorite(path, snippet=True)
        self.conn.execute("UPDATE favorites SET status='done', note='保留', action_json=?",
                          (json.dumps({'task': '保留行动卡'}),))
        self.conn.commit()
        before = self.rows('favorites')
        (self.vault / path).unlink()
        self.index()
        detached = self.rows('favorites')
        self.assertEqual([None, None], [row['path'] for row in self.rows('favorites')])
        self.assertEqual(2, len(self.rows('favorite_document_recovery')))
        self.assertEqual([], self.listed()['doc_paths'])
        self.write()
        self.index()
        expected = [{**row, 'origin_title': row['origin_title'] or '原文',
                     'origin_url': row['origin_url'] or 'https://example.org/original',
                     'origin_source': row['origin_source'] or 'rss'}
                    for row in before]
        self.assertEqual(expected, self.rows('favorites'))
        self.assertEqual([{**row, 'path': path} for row in detached], self.rows('favorites'))
        self.assertEqual([], self.rows('favorite_document_recovery'))
        self.assertEqual([path], self.listed()['doc_paths'])
        self.assertEqual(2, len(self.listed()['favorites']))

    def test_path_or_url_alone_cannot_reattach_a_different_original(self):
        path = self.seed(snippet=True)
        (self.vault / path).unlink()
        self.index()
        for text in (TEXT.replace('原文中的一句。', '完全不同的正文。'),
                     TEXT.replace('https://example.org/original', 'https://example.org/other'),
                     TEXT.replace('title: 原文', 'title: 同正文的不同文档'),
                     TEXT.replace('---\n原文中的一句', 'source: dr-gzh\n---\n原文中的一句')):
            with self.subTest(text=text):
                self.write(text=text)
                self.index()
                self.assertIsNone(self.rows('favorites')[0]['path'])
                self.assertEqual(1, len(self.rows('favorite_document_recovery')))
        self.write(text=TEXT)
        self.index()
        self.assertEqual(path, self.rows('favorites')[0]['path'])

    def test_identical_text_at_a_different_path_is_not_the_same_original(self):
        path = self.seed()
        (self.vault / path).unlink()
        self.index()
        self.write('other.md')
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])
        self.assertEqual(1, len(self.rows('favorite_document_recovery')))

    def test_snippet_version_must_still_match_even_when_the_last_indexed_file_returns(self):
        path = self.seed(snippet=True)
        changed = TEXT + '\n收藏之后改过的正文\n'
        self.write(text=changed)
        self.index()
        (self.vault / path).unlink()
        self.index()
        self.write(text=changed)
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])
        self.assertEqual([], self.listed()['doc_paths'])

    def test_legacy_snippets_without_body_version_are_not_guessed(self):
        path = self.seed(snippet=True)
        self.conn.execute('UPDATE favorites SET doc_version=NULL')
        self.conn.commit()
        (self.vault / path).unlink()
        self.index()
        self.write()
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])

    def test_explicit_deletion_never_restores_even_the_exact_same_original(self):
        path = self.seed()
        store.remove_documents(self.conn, [path])
        self.conn.commit()
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])
        self.assertEqual([], self.rows('favorite_document_recovery'))

    def test_input_without_a_body_version_does_not_crash_or_downgrade_snippet_proof(self):
        path = self.seed(snippet=True)
        (self.vault / path).unlink()
        self.index()
        self.write()
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        unversioned = SimpleNamespace(**{key: value for key, value in vars(doc).items()
                                        if key != 'body_version'})
        store.upsert_document(self.conn, unversioned, split_chunks(doc.body), NOW)
        self.conn.commit()
        self.assertIsNone(self.rows('favorites')[0]['path'])

    def test_permanent_exclusion_revokes_an_earlier_missing_file_receipt(self):
        path = self.seed()
        (self.vault / path).unlink()
        self.index()
        self.assertEqual(1, len(self.rows('favorite_document_recovery')))
        self.assertEqual(0, store.remove_documents(self.conn, [path]))
        self.conn.commit()
        self.write()
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])
        self.assertEqual([], self.rows('favorite_document_recovery'))

    def test_a_legacy_detached_favorite_without_a_receipt_is_never_guessed(self):
        path = self.seed(snippet=True)
        self.conn.execute('DELETE FROM documents WHERE path=?', (path,))
        self.conn.commit()
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])

    def test_detachment_and_receipt_rollback_together(self):
        path = self.seed()
        (self.vault / path).unlink()
        report, _ = self.scan()
        store.remove_missing_documents(self.conn, report)
        self.conn.rollback()
        self.assertEqual([path], self.listed()['doc_paths'])
        self.assertEqual([], self.rows('favorite_document_recovery'))

    def test_recovery_survives_restart_and_does_not_overwrite_detached_user_edits(self):
        path = self.seed(snippet=True)
        favorite_id = self.rows('favorites')[0]['id']
        (self.vault / path).unlink()
        self.index()
        response = self.client.patch(f'/api/favorites/{favorite_id}',
                                     json={'status': 'done', 'note': '脱钩后改的备注'})
        self.assertEqual(200, response.status_code, response.text)
        before = self.rows('favorites')[0]
        self.write()
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        restarted = REAL_CONNECT(self.db)
        try:
            store.upsert_document(restarted, doc, split_chunks(doc.body), NOW)
            restarted.rollback()
            self.assertIsNone(self.rows('favorites')[0]['path'])
            store.upsert_document(restarted, doc, split_chunks(doc.body), NOW)
            restarted.commit()
        finally:
            restarted.close()
        self.assertEqual({**before, 'path': path}, self.rows('favorites')[0])
        self.assertEqual([], self.rows('favorite_document_recovery'))

    def test_deleting_the_favorite_removes_its_recovery_receipt(self):
        path = self.seed()
        favorite_id = self.rows('favorites')[0]['id']
        (self.vault / path).unlink()
        self.index()
        self.assertEqual(204, self.client.delete(f'/api/favorites/{favorite_id}').status_code)
        self.assertEqual([], self.rows('favorite_document_recovery'))
        self.write()
        self.index()
        self.assertEqual([], self.rows('favorites'))

    def test_recovery_never_merges_or_overwrites_an_existing_whole_article_favorite(self):
        path = self.seed()
        (self.vault / path).unlink()
        self.index()
        self.conn.execute("INSERT INTO documents(path,source) VALUES(?,'rss')", (path,))
        self.conn.execute("INSERT INTO favorites(path,created_at,updated_at,note)"
                          " VALUES(?,'x','x','另一条用户记录')", (path,))
        self.conn.commit()
        self.write()
        self.index()
        self.assertEqual([None, path], [row['path'] for row in self.rows('favorites')])
        self.assertEqual('另一条用户记录', self.rows('favorites')[1]['note'])
