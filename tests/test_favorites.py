"""收藏工作台 API 契约：全状态浏览、片段与整篇独立、无损状态流转。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import store
from kb.web import favorites_api


class FavoritesApiTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        directory = Path(temporary)
        self.enterContext(mock.patch.object(store, 'DATA_DIR', directory))
        real_connect = store.connect
        self.enterContext(mock.patch.object(store, 'connect',
                                           side_effect=lambda: real_connect(directory / 'kb.db')))
        conn = store.connect()
        for path in ('article.md', 'done.md', 'archive.md'):
            conn.execute('INSERT INTO documents(path,source,title,summary) VALUES(?,?,?,?)',
                         (path, 'rss', path, '完整摘要'))
        for path, status, quote, action in (
            ('article.md', 'inbox', '', json.dumps({'name': '工具', 'eta': '~5 分钟'})),
            ('article.md', 'inbox', '只收藏这一段', '{broken'),
            ('done.md', 'done', '', ''),
            ('archive.md', 'archived', '', ''),
        ):
            conn.execute('INSERT INTO favorites(path,status,quote,action_json,created_at,updated_at)'
                         ' VALUES(?,?,?,?,?,?)',
                         (path, status, quote, action, '2026-10-10T12:00:00', '2026-10-10T12:00:00'))
        conn.commit()
        conn.close()
        app = FastAPI()
        app.include_router(favorites_api.router)
        self.api = self.enterContext(TestClient(app))

    def test_default_still_returns_only_inbox(self):
        payload = self.api.get('/api/favorites').json()
        self.assertEqual([2, 1], [item['id'] for item in payload['favorites']])
        self.assertEqual({'inbox': 2, 'done': 1, 'archived': 1}, payload['counts'])

    def test_stale_threshold_is_shared_with_the_response_and_count(self):
        conn = store.connect()
        for favorite_id, days in ((1, 45), (2, 15), (3, 90)):
            updated = (datetime.now() - timedelta(days=days)).isoformat(timespec='seconds')
            conn.execute('UPDATE favorites SET updated_at=? WHERE id=?', (updated, favorite_id))
        conn.commit()
        conn.close()
        for days, count in ((30, 1), (60, 0)):
            with self.subTest(days=days), mock.patch.object(favorites_api, 'STALE_DAYS', days):
                result = self.api.get('/api/favorites?status=all').json()
                self.assertEqual(days, result['stale_days'])
                self.assertEqual(count, result['stale'])

    def test_all_is_a_union_without_losing_snippets(self):
        response = self.api.get('/api/favorites?status=all')
        self.assertEqual(200, response.status_code)
        items = response.json()['favorites']
        self.assertEqual([4, 3, 2, 1], [item['id'] for item in items])
        self.assertEqual({'inbox', 'done', 'archived'}, {item['status'] for item in items})
        self.assertEqual('只收藏这一段', items[2]['quote'])

    def test_each_status_remains_addressable(self):
        for status, count in (('inbox', 2), ('done', 1), ('archived', 1)):
            with self.subTest(status=status):
                items = self.api.get('/api/favorites', params={'status': status}).json()['favorites']
                self.assertEqual(count, len(items))
                self.assertTrue(all(item['status'] == status for item in items))

    def test_invalid_status_is_rejected(self):
        self.assertEqual(400, self.api.get('/api/favorites?status=missing').status_code)

    def test_broken_action_does_not_hide_the_favorite(self):
        items = self.api.get('/api/favorites').json()['favorites']
        self.assertIsNone(items[0]['action'])
        self.assertEqual('工具', items[1]['action']['name'])

    def test_whole_document_stars_include_all_states_but_not_snippet_duplicates(self):
        paths = self.api.get('/api/favorites?status=all').json()['doc_paths']
        self.assertEqual({'article.md', 'done.md', 'archive.md'}, set(paths))
        self.assertEqual(3, len(paths))

    def test_note_can_be_saved_and_found_without_changing_state(self):
        response = self.api.patch('/api/favorites/2', json={'note': '  用来写每周总结  '})
        self.assertEqual(200, response.status_code)
        item = self.api.get('/api/favorites').json()['favorites'][0]
        self.assertEqual('用来写每周总结', item['note'])
        self.assertEqual('inbox', item['status'])
        self.assertEqual('只收藏这一段', item['quote'])

    def test_completion_and_undo_do_not_change_content(self):
        before = self.api.get('/api/favorites').json()['favorites'][1]
        for status in ('done', 'inbox'):
            self.assertEqual(200, self.api.patch('/api/favorites/1', json={'status': status}).status_code)
        after = self.api.get('/api/favorites?status=all').json()['favorites'][-1]
        for key in ('path', 'quote', 'note', 'action', 'created_at'):
            self.assertEqual(before[key], after[key])

    def test_archiving_keeps_notes_and_document(self):
        self.api.patch('/api/favorites/2', json={'note': '还会用到', 'status': 'archived'})
        items = self.api.get('/api/favorites?status=archived').json()['favorites']
        snippet = next(item for item in items if item['id'] == 2)
        self.assertEqual(('只收藏这一段', '还会用到'), (snippet['quote'], snippet['note']))
        self.assertEqual(4, len(self.api.get('/api/favorites?status=all').json()['favorites']))

    def test_deleting_snippet_keeps_whole_document_favorite(self):
        self.assertEqual(204, self.api.delete('/api/favorites/2').status_code)
        items = self.api.get('/api/favorites?status=all').json()['favorites']
        self.assertTrue(any(item['id'] == 1 for item in items))
        conn = store.connect()
        self.assertIsNotNone(conn.execute("SELECT path FROM documents WHERE path='article.md'").fetchone())
        conn.close()

    def test_all_is_not_a_writable_status(self):
        self.assertEqual(400, self.api.patch('/api/favorites/1', json={'status': 'all'}).status_code)


@unittest.skipUnless(shutil.which('node'), '需要 node 执行收藏前端状态回归')
class FavoritesStateTests(unittest.TestCase):
    def test_frontend_state_contract(self):
        probe = Path(__file__).with_name('favorites_state_test.mjs')
        result = subprocess.run([shutil.which('node'), '--test', str(probe)],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
