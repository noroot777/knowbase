import json
import shutil
import sqlite3
import threading
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import config, pipeline_failures, pipeline_state, store
from kb.collectors import CollectorError, item_dropper
from kb.collectors.feeds import FeedCollector
from kb.feeds import exclusion, fulltext, registry as reg, sync, translate
from kb.feeds.fetching import FeedError
from kb.feeds.grab_store import work_version
from kb.web import pipeline_api
from tests.headless import probe_env, requires_headless, run_probe
from tests.test_feed_fulltext import FullTextCase


class FeedDropTests(FullTextCase):
    def setUp(self):
        super().setUp()
        self.saved_state = deepcopy(pipeline_state._state)
        self.addCleanup(lambda: pipeline_state._state.update(self.saved_state))
        pipeline_state._state.update(running=False, stop_requested=False)
        for replacement in (
                patch.object(config, 'VAULT_ROOT', self.root / 'vault'),
                patch.object(pipeline_state, '_STATE_FILE', self.root / 'pipeline.json')):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.collector = FeedCollector('rss')

    def ref(self, key):
        item = self.row(key)
        return {'channel': 'rss', 'id': key, 'link': item['url']}

    def exists(self, key):
        with reg.db() as conn:
            return conn.execute('SELECT 1 FROM feed_items WHERE key=?', (key,)).fetchone() is not None

    def assert_excluded(self, key):
        self.assertFalse(self.exists(key))
        with reg.db() as conn:
            self.assertIsNotNone(conn.execute(
                'SELECT 1 FROM feed_item_exclusions WHERE key=?', (key,)).fetchone())
            self.assertIsNone(conn.execute(
                'SELECT 1 FROM subscription_items WHERE item_key=?', (key,)).fetchone())

    def test_fulltext_error_and_empty_body_keep_the_original_link_and_identity(self):
        for index, result in enumerate((FeedError('订阅连接超时'), ('', '页面上没有识别出正文'))):
            with self.subTest(result=result):
                key = self.put(url=f'https://e.org/post-{index}')
                expected = self.ref(key)
                options = {'side_effect': result} if isinstance(result, Exception) else {'return_value': result}
                with patch.object(fulltext, 'fetch_one', **options):
                    summary = fulltext.fetch_pending('rss', limit=1)
                report = summary['errors'][0]
                self.assertEqual(expected['link'], report['url'])
                self.assertEqual(expected, report['drop'])
                with patch.object(sync, '_stage') as stage:
                    sync.export('rss', fetched=summary)
                self.assertIn(report, stage.call_args.kwargs['failures'])
                self.collector.drop_item(expected)

    def test_translation_failures_also_keep_the_item_identity(self):
        key = self.put(source=self.plain, body='An article in English about coding agents.')
        with patch.object(translate, 'translate_item', side_effect=RuntimeError('model unavailable')):
            summary = translate.translate_pending('rss')
        self.assertEqual(self.ref(key), summary['errors'][0]['drop'])

    def test_export_failures_also_keep_the_item_identity(self):
        key = self.put(source=self.plain)
        with patch.object(sync.os, 'replace', side_effect=PermissionError('read only')), \
             patch.object(sync, '_stage') as stage:
            sync.export('rss')
        self.assertEqual(self.ref(key), stage.call_args.kwargs['failures'][0]['drop'])

    def test_degraded_reports_remain_visible_without_inventing_an_identity(self):
        report = exclusion.failure({'title': '重复标题'}, '抓取失败', 'rss')
        self.assertEqual({'title': '重复标题', 'reason': '抓取失败'}, report)
        report = exclusion.failure({'title': '重复标题', 'url': 'https://e.org/post',
                                    'key': 'not-the-real-key'}, '失败', 'rss')
        self.assertNotIn('drop', report)
        self.assertEqual('https://e.org/post', report['url'])

    def test_drop_is_permanent_across_subscriptions_and_does_not_match_titles(self):
        key = self.put(title='重复标题')
        other = self.put(source=self.plain, url='https://e.org/other', title='重复标题')
        self.put(source=self.plain)
        ref = self.ref(key)
        self.assertIsNotNone(item_dropper(self.collector))
        self.collector.drop_item(ref)
        self.assert_excluded(key)
        self.assertTrue(self.exists(other))
        for source in (self.source, self.plain):
            added = reg.put_items(source, [{'url': ref['link'], 'title': '已换标题', 'body': '新正文'}])
            self.assertEqual(0, added)
            self.assert_excluded(key)
        self.collector.drop_item(ref)
        self.collector.drop_item({'id': key})

    def test_the_database_itself_rejects_a_stale_insert(self):
        key = self.put()
        old = self.row(key)
        self.collector.drop_item(self.ref(key))
        with reg.db() as conn:
            columns = ','.join(old)
            placeholders = ','.join('?' for column in old)
            cursor = conn.execute(f'INSERT OR REPLACE INTO feed_items ({columns}) '
                                  f'VALUES ({placeholders})', list(old.values()))
            self.assertEqual(0, cursor.rowcount)
        self.assert_excluded(key)

    def test_wrong_channel_or_url_does_not_remove_or_blacklist_anything(self):
        key = self.put()
        ref = self.ref(key)
        for collector, request in ((FeedCollector('social'), ref),
                                   (self.collector, {**ref, 'link': 'https://e.org/other'}),
                                   (self.collector, {'id': ''}),
                                   (self.collector, {'id': 'unknown'})):
            with self.subTest(request=request), self.assertRaises(CollectorError):
                collector.drop_item(request)
        self.assertTrue(self.exists(key))
        with reg.db() as conn:
            self.assertEqual(0, conn.execute('SELECT count(*) FROM feed_item_exclusions').fetchone()[0])

    def test_a_missing_row_requires_a_verifiable_link_before_excluding_it(self):
        key = exclusion.item_key('rss', 'https://e.org/gone')
        with self.assertRaises(CollectorError):
            self.collector.drop_item({'id': key})
        self.collector.drop_item({'id': key, 'link': 'https://e.org/gone'})
        self.assert_excluded(key)

    def test_other_feed_kinds_are_not_excluded_by_an_rss_identity(self):
        key = self.put()
        ref = self.ref(key)
        source = reg.save_subscription('web', {'name': '网页', 'url': 'https://e.org/news'})
        reg.put_items(source, [{'url': ref['link'], 'title': '网页条目', 'body': '正文'}])
        self.collector.drop_item(ref)
        self.assertTrue(self.exists(exclusion.item_key('web', ref['link'])))

    def test_late_fulltext_results_cannot_restore_a_removed_item(self):
        for index, result in enumerate((('迟到正文', ''), FeedError('迟到错误'))):
            key = self.put(url=f'https://e.org/late-{index}')
            ref = self.ref(key)

            def fetch(url):
                self.collector.drop_item(ref)
                if isinstance(result, Exception):
                    raise result
                return result

            with patch.object(fulltext, 'fetch_one', side_effect=fetch):
                summary = fulltext.fetch_pending('rss')
            self.assertEqual(1, summary['stale'])
            self.assertEqual([], summary['errors'])
            self.assert_excluded(key)

    def test_late_translation_success_or_failure_cannot_restore_a_removed_item(self):
        key = self.put(source=self.plain)
        old = self.row(key)
        self.collector.drop_item(self.ref(key))
        self.assertFalse(translate._save(key, '标题', '正文', (old['title'], old['body'])))
        self.assertFalse(translate._fail(key, RuntimeError('late'), (old['title'], old['body'])))
        self.assertFalse(fulltext._save(key, '正文', work_version(old)))
        self.assert_excluded(key)

    def export_item(self):
        key = self.put(source=self.plain)
        sync.export('rss')
        return key, self.row(key)['path']

    def test_exported_markdown_and_search_data_are_removed_together(self):
        key, relative = self.export_item()
        conn = store.connect(config.DB_PATH)
        try:
            conn.execute('INSERT INTO documents(path,source,title) VALUES(?,?,?)',
                         (relative, 'rss', '原文'))
            conn.execute('INSERT INTO doc_tags(path,tag) VALUES(?,?)', (relative, 'topic/test'))
            chunk_id = conn.execute('INSERT INTO chunks(path,seq,text) VALUES(?,0,?)',
                                    (relative, '正文')).lastrowid
            conn.execute('INSERT INTO chunks_fts(chunk_id,title_tok,text_tok) VALUES(?,?,?)',
                         (chunk_id, '标题', '正文'))
            store.insert_vectors(conn, [chunk_id], [[0.0] * config.EMBEDDING_DIM])
            favorite_id = conn.execute(
                'INSERT INTO favorites(path,quote,note,action_json,origin_url,created_at,updated_at)'
                ' VALUES(?,?,?,?,?,?,?)',
                (relative, '用户引文', '用户备注', '{"name":"行动卡"}',
                 self.ref(key)['link'], 'x', 'x')).lastrowid
            conn.commit()
        finally:
            conn.close()
        self.collector.drop_item(self.ref(key))
        self.assertFalse((config.VAULT_ROOT / relative).exists())
        conn = store.connect(config.DB_PATH)
        try:
            for table in ('documents', 'doc_tags', 'chunks', 'chunks_fts', 'chunk_vec'):
                self.assertEqual(0, conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0], table)
            self.assertEqual((None, '用户引文', '用户备注', '{"name":"行动卡"}', '原文'),
                             tuple(conn.execute(
                                 'SELECT path,quote,note,action_json,origin_title FROM favorites'
                                 ' WHERE id=?', (favorite_id,)).fetchone()))
        finally:
            conn.close()
        self.assert_excluded(key)

    def test_cleanup_failure_can_be_retried_without_allowing_rediscovery(self):
        key, relative = self.export_item()
        ref = self.ref(key)
        with patch.object(exclusion, '_cleanup', side_effect=PermissionError('locked')):
            with self.assertRaisesRegex(CollectorError, '已永久排除.*请重试'):
                self.collector.drop_item(ref)
        self.assert_excluded(key)
        self.assertTrue((config.VAULT_ROOT / relative).exists())
        self.collector.drop_item(ref)
        self.assertFalse((config.VAULT_ROOT / relative).exists())

    def test_a_path_outside_the_source_is_refused_before_any_deletion(self):
        key = self.put()
        ref = self.ref(key)
        outside = self.root / 'keep.md'
        outside.write_text('keep', encoding='utf-8')
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET path=? WHERE key=?', ('../keep.md', key))
        with self.assertRaises(CollectorError):
            self.collector.drop_item(ref)
        self.assertTrue(self.exists(key))
        self.assertEqual('keep', outside.read_text(encoding='utf-8'))

    def test_a_source_directory_symlink_cannot_redirect_deletion_outside_the_vault(self):
        key = self.put()
        ref = self.ref(key)
        parent = config.VAULT_ROOT / 'source'
        parent.mkdir(parents=True)
        outside = self.root / 'outside'
        outside.mkdir()
        (outside / 'keep.md').write_text('keep', encoding='utf-8')
        (parent / 'rss').symlink_to(outside, target_is_directory=True)
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET path=? WHERE key=?', ('source/rss/keep.md', key))
        with self.assertRaises(CollectorError):
            self.collector.drop_item(ref)
        self.assertTrue(self.exists(key))
        self.assertTrue((outside / 'keep.md').exists())

    def test_existing_databases_get_an_additive_exclusion_migration(self):
        key = self.put()
        with sqlite3.connect(reg.DB_PATH) as conn:
            conn.execute('DROP TRIGGER feed_items_excluded_insert')
            conn.execute('DROP TABLE feed_item_exclusions')
        self.assertTrue(self.exists(key))
        self.collector.drop_item(self.ref(key))
        self.assert_excluded(key)

    def test_drop_waits_for_an_inflight_export_then_removes_its_output(self):
        key = self.put(source=self.plain)
        ref = self.ref(key)
        entered, release = threading.Event(), threading.Event()
        removed = threading.Event()
        failures = []
        replace = sync.os.replace

        def held_replace(source, target):
            entered.set()
            if not release.wait(3):
                raise TimeoutError('test export did not resume')
            return replace(source, target)

        def exporting():
            try:
                sync.export('rss')
            except Exception as error:
                failures.append(error)

        def dropping():
            try:
                self.collector.drop_item(ref)
                removed.set()
            except Exception as error:
                failures.append(error)

        writer = threading.Thread(target=exporting)
        dropper = threading.Thread(target=dropping)
        with patch.object(sync.os, 'replace', side_effect=held_replace):
            writer.start()
            try:
                self.assertTrue(entered.wait(3))
                dropper.start()
                self.assertFalse(removed.wait(.15))
            finally:
                release.set()
                writer.join(5)
                if dropper.ident:
                    dropper.join(5)
        self.assertEqual([], failures)
        self.assertTrue(removed.is_set())
        self.assert_excluded(key)
        self.assertEqual([], list((config.VAULT_ROOT / 'source/rss').glob('*.md')))

    def test_the_real_endpoint_records_exclusion_only_after_removal(self):
        key = self.put()
        report = exclusion.failure(self.row(key), '抓取失败')
        app = FastAPI()
        app.include_router(pipeline_api.router)
        with TestClient(app) as client:
            response = client.post('/api/pipeline/failures/drop', json=report['drop'])
        self.assertEqual(200, response.status_code, response.text)
        self.assert_excluded(key)
        history = {'stages': {'export': {'failures': [report]}}}
        pipeline_failures.strip(history)
        self.assertEqual([], history['stages']['export']['failures'])
        saved = json.loads((self.root / 'pipeline_dropped.json').read_text(encoding='utf-8'))
        self.assertIn('rss:' + key, saved['dropped'])

    def test_cleanup_refusal_keeps_the_history_action_available_for_retry(self):
        key, relative = self.export_item()
        ref = self.ref(key)
        app = FastAPI()
        app.include_router(pipeline_api.router)
        with TestClient(app) as client:
            with patch.object(exclusion, '_cleanup', side_effect=PermissionError('locked')):
                response = client.post('/api/pipeline/failures/drop', json=ref)
            self.assertEqual(400, response.status_code)
            self.assertIn('已永久排除', response.json()['detail'])
            self.assertEqual(set(), pipeline_failures.dropped_keys())
            response = client.post('/api/pipeline/failures/drop', json=ref)
        self.assertEqual(200, response.status_code, response.text)
        self.assertFalse((config.VAULT_ROOT / relative).exists())


@requires_headless
class FeedDropPanelTests(FullTextCase):
    def test_rss_failures_can_be_opened_and_dropped_including_from_history(self):
        env = {**probe_env(), 'PIPE_DROP_CHANNEL': 'rss'}
        probe = Path(__file__).with_name('pipe_drop_probe.mjs')
        result = run_probe([shutil.which('node'), str(probe)], timeout=90, env=env)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
