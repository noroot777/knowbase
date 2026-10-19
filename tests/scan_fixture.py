"""扫描安全回归的独立 vault、索引、收藏 API 与维护锁。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kb import cli, embedder, maintenance, scanner, store
from kb.chunker import split_chunks
from kb.config import EMBEDDING_DIM, Source
from kb.web import common as web_common
from tests.fav_snippet_fixture import REAL_CONNECT, snip_client


TEXT = '---\ntitle: 原文\nurl: https://example.org/original\ntags: [测试]\n---\n原文中的一句。\n'
NOW = '2026-10-19 12:00:00'


class ScanFixture(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='kb-scan-safety-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.vault = self.root / 'vault'
        self.db = self.root / 'kb.db'
        self.sources = [Source(name, self.vault / 'source' / name)
                        for name in ('rss', 'dr-gzh')]
        for source in self.sources:
            source.root.mkdir(parents=True)
        for patcher in (mock.patch.object(scanner, 'VAULT_ROOT', self.vault),
                        mock.patch.object(web_common, 'VAULT_ROOT', self.vault),
                        mock.patch.object(scanner, 'SOURCES', self.sources),
                        mock.patch.object(store, 'SOURCES', self.sources),
                        mock.patch.object(maintenance, 'DATA_DIR', self.root),
                        mock.patch.object(embedder, 'embed_pending_chunks', return_value=0),
                        mock.patch.object(cli, 'scan_all', scanner.scan_all)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.conn = REAL_CONNECT(self.db)
        self.addCleanup(self.conn.close)
        self.client = snip_client(self, self.db)
        self.addCleanup(self.client.close)

    def write(self, name='a.md', text=TEXT, source='rss') -> str:
        relative = f'source/{source}/{name}'
        full = self.vault / relative
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(text, encoding='utf-8')
        return relative

    def scan(self):
        report = scanner.ScanReport()
        documents = list(scanner.scan_all(report=report))
        return report, documents

    def index(self):
        report = scanner.ScanReport()
        for doc in scanner.scan_all(report=report):
            if store.doc_needs_reindex(self.conn, doc.path, doc.content_hash):
                store.upsert_document(self.conn, doc, split_chunks(doc.body), NOW)
        removed = store.remove_missing_documents(self.conn, report)
        self.conn.commit()
        return report, removed

    def favorite(self, path, *, snippet=False):
        if snippet:
            response = self.client.post('/api/favorites', json={
                'path': path, 'origin': 'doc', 'quote': '原文中的一句',
                'prefix': '', 'suffix': '。', 'note': '不能丢的备注',
                'doc_version': scanner.body_version((self.vault / path).read_text()),
                'quote_unique': 1, 'anchor_exact': 1})
        else:
            response = self.client.post('/api/favorites/toggle', json={'path': path})
        self.assertEqual(200, response.status_code, response.text)
        return response.json()['id'] if snippet else self.listed()['favorites'][0]['id']

    def listed(self):
        response = self.client.get('/api/favorites?status=all')
        self.assertEqual(200, response.status_code, response.text)
        return response.json()

    def rows(self, table):
        return [dict(row) for row in self.conn.execute(f'SELECT * FROM {table}')]

    def snapshot(self):
        return {table: self.rows(table) for table in (
            'documents', 'chunks', 'chunks_fts', 'chunk_vec', 'doc_tags',
            'entities', 'favorites', 'favorite_document_recovery', 'index_scan_roots',
            'index_scan_directories')}

    def seed(self, name='a.md', *, snippet=False):
        path = self.write(name)
        self.index()
        self.favorite(path, snippet=snippet)
        chunk_id = self.conn.execute('SELECT id FROM chunks WHERE path=?', (path,)).fetchone()[0]
        store.insert_vectors(self.conn, [chunk_id], [[0.0] * EMBEDDING_DIM])
        self.conn.execute("INSERT INTO entities(path,entity,kind) VALUES(?,'实体','project')",
                          (path,))
        self.conn.commit()
        return path
