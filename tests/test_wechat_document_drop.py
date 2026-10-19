import subprocess
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import config, pipeline_failures, pipeline_stages, pipeline_state, projects, scanner, store
from kb.chunker import split_chunks
from kb.collectors import CollectorError, document_failure, item_aliases
from kb.collectors import wechat_documents, wechat_local
from kb.web import pipeline_api
from tests.test_extract_no_body import NoBodyBase, REAL, STUB

LINK = 'https://mp.weixin.qq.com/s?__biz=biz-a&mid=101&idx=1&scene=1'


class WeChatDocumentDropTests(NoBodyBase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.excluded = False
        self.real_cli = wechat_local.WeChatLocalCollector._accounts_raw
        for replacement in (
                patch.object(config, 'VAULT_ROOT', self.vault),
                patch.object(config, 'DB_PATH', self.root / 'kb.db'),
                patch.object(pipeline_state, '_STATE_FILE', self.root / 'pipeline.json'),
                patch.object(wechat_local.WeChatLocalCollector, '_accounts_raw',
                             side_effect=self.cli)):
            replacement.start()
            self.addCleanup(replacement.stop)

    def cli(self, *argv, timeout):
        self.assertEqual('articles', argv[0])
        self.assertEqual(LINK, argv[argv.index('--link') + 1])
        if argv[1] == 'resolve':
            return {'ok': True, 'id': 'archive-id', 'account': 'A', 'link': LINK}
        self.assertEqual('drop', argv[1])
        self.assertEqual('archive-id', argv[argv.index('--id') + 1])
        self.excluded = True
        return {'ok': True, 'detail': '已永久排除'}

    def article(self, name='article', body=STUB):
        relative = self.write(name, body)
        path = self.vault / relative
        path.write_text(path.read_text().replace('https://example.org/x', LINK), encoding='utf-8')
        self.index()
        return relative, dict(self.conn.execute('SELECT * FROM documents WHERE path=?',
                                                (relative,)).fetchone())

    def client(self):
        app = FastAPI()
        app.include_router(pipeline_api.router)
        return TestClient(app)

    def exported_document(self, source, name, link=LINK, indexed=True):
        relative = f'source/{source}/2026-10-19/{name}.md'
        path = self.vault / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (f'---\nsource: {source}\ntitle: same title\nurl: "{link}"\n'
                f'article_title: 母文章\nproject: {name}\n---\n\n{REAL}')
        path.write_text(text, encoding='utf-8')
        if indexed:
            document = scanner.doc_from_text(text, path, config.Source(source, path.parent), relative)
            store.upsert_document(self.conn, document, split_chunks(document.body), '2026-10-19')
            self.conn.commit()
        return relative

    def test_drop_removes_same_article_derivatives_but_not_independent_sources(self):
        relative, document = self.article(body=REAL)
        derivatives = [self.exported_document('dr-gzh-projects', 'tool-a'),
                       self.exported_document('dr-gzh-projects', 'tool-b',
                                              LINK.replace('scene=1', 'scene=99'))]
        kept = [self.exported_document('dr-gzh-projects', 'other-article',
                                       'https://mp.weixin.qq.com/s/other'),
                self.exported_document('rss', 'independent'),
                self.exported_document('dr-video-projects', 'video')]
        chunk_ids = [row[0] for item in [relative, *derivatives]
                     for row in self.conn.execute('SELECT id FROM chunks WHERE path=?', (item,))]
        self.assertTrue(chunk_ids)
        store.insert_vectors(self.conn, chunk_ids, [[0.0] * config.EMBEDDING_DIM for item in chunk_ids])
        self.conn.commit()
        with self.client() as client, patch.object(projects, 'VAULT_ROOT', self.vault), \
             patch.dict(projects._cache, {'sig': None, 'entries': None, 'groups': None}, clear=True):
            self.assertEqual(4, projects.list_tools()['total_entries'])
            response = client.post('/api/pipeline/failures/drop',
                                   json=document_failure(document, '失败')['drop'])
            self.assertEqual(2, projects.list_tools()['total_entries'])
            remaining = {entry['path'] for group in projects.all_groups() for entry in group['entries']}
            self.assertEqual({kept[0], kept[2]}, remaining)
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(self.excluded)
        for removed in [relative, *derivatives]:
            self.assertFalse((self.vault / removed).exists())
        self.assertEqual(set(kept), {row[0] for row in self.conn.execute('SELECT path FROM documents')})
        for chunk_id in chunk_ids:
            self.assertIsNone(self.conn.execute('SELECT id FROM chunks WHERE id=?', (chunk_id,)).fetchone())
            self.assertIsNone(self.conn.execute('SELECT chunk_id FROM chunks_fts WHERE chunk_id=?',
                                               (chunk_id,)).fetchone())
            self.assertIsNone(self.conn.execute('SELECT chunk_id FROM chunk_vec WHERE chunk_id=?',
                                               (chunk_id,)).fetchone())

    def test_unindexed_and_index_only_derivatives_are_cleaned_idempotently(self):
        relative, document = self.article(body=REAL)
        unindexed = self.exported_document('dr-gzh-projects', 'unindexed', indexed=False)
        index_only = self.exported_document('dr-gzh-projects', 'index-only')
        (self.vault / index_only).unlink()
        ref = document_failure(document, '失败')['drop']
        for attempt in range(2):
            with self.subTest(attempt=attempt):
                wechat_local.collector.drop_item(ref)
                for removed in (relative, unindexed, index_only):
                    self.assertFalse((self.vault / removed).exists())
                self.assertEqual(0, self.conn.execute('SELECT count(*) FROM documents').fetchone()[0])

    def test_derived_cleanup_rejects_symlinks_outside_the_vault(self):
        relative, document = self.article(body=REAL)
        outside = self.root / 'outside'
        outside.mkdir()
        external = outside / 'keep.md'
        external.write_text(f'---\nurl: "{LINK}"\n---\n{REAL}', encoding='utf-8')
        (self.vault / 'source/dr-gzh-projects').symlink_to(outside, target_is_directory=True)
        with self.client() as client:
            response = client.post('/api/pipeline/failures/drop',
                                   json=document_failure(document, '失败')['drop'])
        self.assertEqual(409, response.status_code, response.text)
        self.assertTrue(external.exists())
        self.assertTrue(self.excluded)
        self.assertEqual(set(), pipeline_failures.dropped_keys())

    def test_empty_body_and_model_failure_have_an_action_and_leave_the_extraction_queue(self):
        for body in (STUB, REAL):
            with self.subTest(body=body):
                relative, document = self.article(body=body)
                options = ({'return_value': (None, 'empty', 'version', '')} if body == STUB
                           else {'side_effect': RuntimeError('model failed')})
                stats = pipeline_stages._new_extract_stats()
                with patch.object(pipeline_stages, '_extract_one', **options):
                    pipeline_stages._extract_pass('', stats)
                report = stats['failures'][relative]
                self.assertEqual({'channel': 'wechat_local', 'id': 'archive-id',
                                  'link': LINK, 'account': 'A'}, report['drop'])
                self.assertEqual(LINK, report['url'])
                with self.client() as client:
                    response = client.post('/api/pipeline/failures/drop', json=report['drop'])
                self.assertEqual(200, response.status_code, response.text)
                self.assertTrue(self.excluded)
                self.assertFalse((self.vault / relative).exists())
                self.assertEqual([], self.pending())
                history = {'stages': {'extract': {'failures': [report]}}}
                self.assertEqual(1, pipeline_failures.strip(history))

    def test_cleanup_matches_link_identity_across_legacy_sources_not_titles(self):
        relative, document = self.article(body=REAL)
        keep = self.write('same-title', REAL)
        self.index()
        legacy = self.vault / 'source/gzh/older.md'
        legacy.parent.mkdir(parents=True)
        legacy.write_text((self.vault / relative).read_text().replace('&scene=1', '&scene=99'),
                          encoding='utf-8')
        ref = document_failure(document, 'failed')['drop']
        wechat_local.collector.drop_item(ref)
        self.assertFalse(legacy.exists())
        self.assertFalse((self.vault / relative).exists())
        self.assertTrue((self.vault / keep).exists())
        self.assertEqual([keep], self.pending())
        wechat_local.collector.drop_item(ref)

    def test_cleanup_error_keeps_the_action_and_can_be_retried_after_exclusion(self):
        relative, document = self.article(body=REAL)
        derivative = self.exported_document('dr-gzh-projects', 'retry')
        ref = document_failure(document, 'failed')['drop']
        with self.client() as client:
            with patch.object(wechat_documents.store, 'remove_documents',
                              side_effect=PermissionError('read only')):
                response = client.post('/api/pipeline/failures/drop', json=ref)
            self.assertEqual(409, response.status_code)
            self.assertIn('已永久排除', response.json()['detail'])
            self.assertTrue(self.excluded)
            self.assertEqual(set(), pipeline_failures.dropped_keys())
            self.assertIn(relative, self.pending())
            response = client.post('/api/pipeline/failures/drop', json=ref)
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual([], self.pending())
        self.assertFalse((self.vault / derivative).exists())
        self.assertEqual(0, self.conn.execute('SELECT count(*) FROM documents').fetchone()[0])

    def test_identity_lookup_failure_preserves_the_original_failure_without_a_button(self):
        relative, document = self.article()
        for result in ({'id': '', 'link': LINK},
                       {'id': 'other', 'link': 'https://mp.weixin.qq.com/s/other'}):
            with patch.object(wechat_local.collector, '_accounts_raw', return_value=result):
                report = document_failure(document, 'original failure')
            self.assertEqual('original failure', report['reason'])
            self.assertEqual(LINK, report['url'])
            self.assertNotIn('drop', report)
        with patch.object(wechat_local.collector, '_accounts_raw',
                          side_effect=CollectorError('backend unavailable')):
            self.assertNotIn('drop', document_failure(document, 'original failure'))

    def test_wrong_source_path_or_link_cannot_invent_a_drop_identity(self):
        relative, document = self.article()
        for changed in ({'source': 'rss'}, {'path': 'source/rss/a.md'},
                        {'path': 'source/dr-gzh/../../../keep.md'},
                        {'url': 'https://example.org/article'}, {'url': ''}):
            with self.subTest(changed=changed), \
                 patch.object(wechat_local.collector, '_accounts_raw') as cli:
                self.assertNotIn('drop', document_failure(document | changed, 'failed'))
                cli.assert_not_called()

    def test_cli_timeout_is_unknown_not_a_definite_refusal(self):
        relative, document = self.article()
        ref = document_failure(document, 'failed')['drop']
        with patch.object(wechat_local.collector, '_accounts_raw',
                          self.real_cli), \
             patch.object(wechat_local, '_backend_problem', return_value=''), \
             patch.object(wechat_local, '_run_cli',
                          side_effect=subprocess.TimeoutExpired('collector', 20)), \
             self.client() as client:
            response = client.post('/api/pipeline/failures/drop', json=ref)
        self.assertEqual(504, response.status_code, response.text)
        self.assertEqual(set(), pipeline_failures.dropped_keys())
        self.assertTrue((self.vault / relative).exists())

    def test_late_failure_and_historical_archive_identity_are_both_filtered(self):
        relative, document = self.article()
        report = document_failure(document, 'failed')
        stable_id = item_aliases(wechat_local.collector, report['drop'])[-1]
        with self.client() as client:
            response = client.post('/api/pipeline/failures/drop', json=report['drop'])
        self.assertEqual(200, response.status_code, response.text)
        with patch.object(wechat_local.collector, '_accounts_raw', return_value={
                'id': stable_id, 'link': LINK, 'account': ''}):
            late = document_failure(document, 'late failure')
        for failure, keys in ((late, None), (report, {f'wechat_local:{stable_id}'}),
                              (report, {'wechat_local:archive-id'})):
            history = {'stages': {'extract': {'failures': [failure]}}}
            self.assertEqual(1, pipeline_failures.strip(history, keys))
