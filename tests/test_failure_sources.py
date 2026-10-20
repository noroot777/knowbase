"""跨来源失败明细：链接、可靠身份与永久排除共用一份契约。"""
import shutil
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import collectors, config, extract, pipeline_failures, pipeline_stages, pipeline_state, store
from kb.collectors import FetchScope, document_failure, get_collector, item_dropper
from kb.feeds import exclusion, fulltext, grab, registry as reg, sync, translate
from kb.feeds.fetching import FeedError
from kb.source_types import feed_types
from kb.web import grab_api, pipeline_api
from tests.headless import probe_env, requires_headless, run_probe
from tests.test_extract_no_body import NoBodyBase, REAL, STUB
from tests.test_feed_fulltext import FullTextCase

_connect = store.connect


class FailureSourceTests(FullTextCase):
    def setUp(self):
        super().setUp()
        self.serial = 0
        for replacement in (
                patch.object(config, 'VAULT_ROOT', self.root / 'vault'),
                patch.dict(pipeline_state._state, {'stop_requested': False}),
                patch.object(sync, '_stage'),
                patch.object(pipeline_stages, '_stage'),
                patch.object(pipeline_stages, '_log')):
            replacement.start()
            self.addCleanup(replacement.stop)

    def add(self, kind, *, fulltext=False, url='https://example.org/post'):
        self.serial += 1
        source = reg.save_subscription(kind, {
            'name': f'{kind}-{self.serial}', 'url': f'https://{kind}-{self.serial}.example.org/feed',
            'fulltext': fulltext, 'extract': True})
        entry = {'url': url, 'title': 'An article about coding agents',
                 'body': 'The runtime schedules tools and keeps the transcript. ' * 15,
                 'content_kind': 'content', 'published': '2026-10-18 09:00'}
        reg.put_items(source, [entry])
        return source, exclusion.item_key(kind, url), entry

    def report_ref(self, kind, key, entry):
        return {'channel': kind, 'id': key, 'link': entry['url']}

    def indexed(self, kind):
        source, key, entry = self.add(kind)
        sync.export(kind)
        row = self.row(key)
        conn = _connect(config.DB_PATH)
        try:
            conn.execute('INSERT INTO documents(path,source,title,url) VALUES(?,?,?,?)',
                         (row['path'], kind, entry['title'], entry['url']))
            conn.commit()
            document = dict(conn.execute('SELECT * FROM documents WHERE path=?',
                                         (row['path'],)).fetchone())
        finally:
            conn.close()
        return source, key, entry, document

    def test_all_feed_collectors_have_permanent_removal_and_preserve_other_sources(self):
        entries = {kind: self.add(kind) for kind in feed_types()}
        app = FastAPI()
        app.include_router(pipeline_api.router)
        removed = set()
        with TestClient(app) as client, patch.object(pipeline_api.pl, 'drop_failure') as remember:
            for kind, (source, key, entry) in entries.items():
                with self.subTest(kind=kind):
                    self.assertIsNotNone(item_dropper(get_collector(kind)))
                    report = exclusion.failure(self.row(key), '抓取失败', kind)
                    expected = self.report_ref(kind, key, entry)
                    self.assertEqual(entry['url'], report['url'])
                    self.assertEqual(expected, report['drop'])
                    response = client.post('/api/pipeline/failures/drop', json=expected)
                    self.assertEqual(200, response.status_code, response.text)
                    remember.assert_called_with(kind, key)
                    removed.add(kind)
                    self.assertEqual(0, reg.put_items(source, [entry]))
                    history = {'stages': {'export': {'failures': [report]}}}
                    self.assertEqual(1, pipeline_failures.strip(history, {f'{kind}:{key}'}))
                    with reg.db() as conn:
                        self.assertIsNone(conn.execute('SELECT 1 FROM feed_items WHERE key=?',
                                                       (key,)).fetchone())
                        for other in entries.keys() - removed:
                            self.assertIsNotNone(conn.execute(
                                'SELECT 1 FROM feed_items WHERE key=?',
                                (entries[other][1],)).fetchone())

    def test_translation_and_export_failures_keep_identity_in_every_feed_source(self):
        for kind in feed_types():
            with self.subTest(kind=kind):
                source, key, entry = self.add(kind)
                expected = self.report_ref(kind, key, entry)
                with patch.object(translate, 'translate_item', side_effect=RuntimeError('model down')):
                    result = translate.translate_pending(kind)
                self.assertEqual(expected, result['errors'][0]['drop'])
                with patch.object(sync.os, 'replace', side_effect=PermissionError('read only')):
                    sync.export(kind)
                reports = sync._stage.call_args.kwargs['failures']
                self.assertEqual(expected, reports[0]['drop'])

    def test_fulltext_failures_keep_identity_for_every_source_that_supports_refill(self):
        for kind, spec in feed_types().items():
            if spec.adapter in {'digest', 'manual'}:
                continue
            with self.subTest(kind=kind):
                source, key, entry = self.add(kind, fulltext=True)
                with patch.object(fulltext, 'fetch_one', side_effect=FeedError('HTTP 403')):
                    result = fulltext.fetch_pending(kind)
                self.assertEqual(self.report_ref(kind, key, entry), result['errors'][0]['drop'])

    def test_subscription_errors_link_to_the_source_but_cannot_remove_an_article(self):
        for kind, spec in feed_types().items():
            if not spec.pollable:
                continue
            with self.subTest(kind=kind):
                source, key, entry = self.add(kind)
                with patch.object(sync, 'fetch_source', side_effect=FeedError('source down')), \
                     patch.object(sync, '_prefetch_digest'), patch.object(sync, '_prefetch_x'):
                    result = sync.collect(kind, FetchScope(targets=(str(source['id']),)))
                report = result['errors'][0]
                self.assertEqual(source['url'], report['url'])
                self.assertNotIn('drop', report)
                self.assertEqual(entry['url'], self.row(key)['url'])

    def test_extraction_errors_keep_links_and_removal_identity_for_every_feed_source(self):
        for kind in feed_types():
            with self.subTest(kind=kind):
                source, key, entry, document = self.indexed(kind)
                with reg.db() as conn:
                    conn.execute('UPDATE feed_items SET path=? WHERE key=?', ('', key))
                for outcome in (RuntimeError('LLM unavailable'), (None, 'empty', 'version', '')):
                    options = ({'side_effect': outcome} if isinstance(outcome, Exception)
                               else {'return_value': outcome})
                    stats = pipeline_stages._new_extract_stats()
                    with patch.object(store, 'connect', side_effect=lambda: _connect(config.DB_PATH)), \
                         patch.object(pipeline_stages, '_extract_one', **options):
                        pipeline_stages._extract_pass('', stats)
                    report = stats['failures'][document['path']]
                    self.assertEqual(entry['url'], report['url'])
                    self.assertEqual(self.report_ref(kind, key, entry), report['drop'])
                get_collector(kind).drop_item(report['drop'])
                self.assertFalse((config.VAULT_ROOT / document['path']).exists())
                conn = _connect(config.DB_PATH)
                try:
                    self.assertEqual([], extract.pending_docs(conn))
                finally:
                    conn.close()

    def test_document_identity_requires_matching_source_path_and_url(self):
        for kind in feed_types():
            with self.subTest(kind=kind):
                source, key, entry, document = self.indexed(kind)
                for replacement in ({'path': 'source/unknown/same-title.md'},
                                    {'path': 'source/unknown/same-title.md', 'key': key},
                                    {'url': 'https://example.org/different'},
                                    {'source': 'web' if kind != 'web' else 'rss'},
                                    {'source': 'unmanaged'}):
                    report = document_failure(document | replacement, '失败')
                    self.assertNotIn('drop', report)
                    self.assertEqual((document | replacement)['url'], report['url'])
                with patch.object(reg, 'db', side_effect=sqlite3.OperationalError('busy')) as database:
                    report = document_failure(document, '原始错误')
                    database.assert_not_called()
                self.assertEqual('原始错误', report['reason'])
                self.assertEqual(entry['url'], report['url'])
                self.assertEqual(self.report_ref(kind, key, entry), report['drop'])
                with patch.object(reg, 'db', side_effect=sqlite3.OperationalError('busy')):
                    report = document_failure(document | {'path': f'source/{kind}/legacy.md'}, '原始错误')
                self.assertEqual('原始错误', report['reason'])
                self.assertEqual(entry['url'], report['url'])
                self.assertNotIn('drop', report)

    def test_an_excluded_document_keeps_identity_to_hide_a_late_extraction_failure(self):
        source, key, entry, document = self.indexed('social')

        def late_failure(path):
            get_collector('social').drop_item(self.report_ref('social', key, entry))
            raise RuntimeError('late model failure')

        stats = pipeline_stages._new_extract_stats()
        with patch.object(store, 'connect', side_effect=lambda *args: _connect(config.DB_PATH)), \
             patch.object(pipeline_stages, '_extract_one', side_effect=late_failure):
            pipeline_stages._extract_pass('', stats)
        report = stats['failures'][document['path']]
        self.assertEqual(self.report_ref('social', key, entry), report.get('drop'))
        history = {'stages': {'extract': {'failures': [report]}}}
        self.assertEqual(1, pipeline_failures.strip(history, {f'social:{key}'}))

    def test_an_excluded_inbox_link_is_not_reported_as_already_stored(self):
        source, key, entry = self.add('inbox')
        get_collector('inbox').drop_item(self.report_ref('inbox', key, entry))
        with self.assertRaisesRegex(grab.GrabError, '不再抓取'):
            grab.save(entry)
        app = FastAPI()
        app.include_router(grab_api.router)
        with TestClient(app) as client, \
             patch.object(grab, 'grab', return_value=entry), \
             patch.object(grab_api.pipeline, 'start_pipeline') as start:
            response = client.post('/api/grab', json={'url': entry['url']})
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn('没有重新收录', response.json()['detail'])
        start.assert_not_called()
        with reg.db() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM feed_items WHERE key=?', (key,)).fetchone())

    def test_an_ordinary_inbox_duplicate_remains_a_successful_no_op(self):
        source, key, entry = self.add('inbox')
        self.assertEqual(0, grab.save(entry)['added'])
        self.assertEqual(entry['url'], self.row(key)['url'])


@requires_headless
class FailureSourcePanelTests(unittest.TestCase):
    def test_history_links_and_removal_work_for_all_other_feed_channels(self):
        probe = Path(__file__).with_name('pipe_drop_probe.mjs')
        for kind in feed_types().keys() - {'rss'}:
            with self.subTest(kind=kind):
                result = run_probe([shutil.which('node'), str(probe)], timeout=90,
                                   env={**probe_env(), 'PIPE_DROP_CHANNEL': kind})
                self.assertEqual(0, result.returncode, result.stdout + result.stderr)


class DocumentFailureTests(NoBodyBase, unittest.TestCase):
    def test_unmanaged_or_legacy_documents_keep_their_original_links(self):
        for body in (STUB, REAL):
            with self.subTest(body=body):
                path = self.write('article', body)
                self.index()
                stats = pipeline_stages._new_extract_stats()
                options = ({'return_value': (None, 'empty', 'version', '')} if body == STUB
                           else {'side_effect': RuntimeError('original failure')})
                with patch.object(pipeline_stages, '_extract_one', **options):
                    pipeline_stages._extract_pass('', stats)
                report = stats['failures'][path]
                self.assertEqual('https://example.org/x', report['url'])
                self.assertNotIn('drop', report)

    def test_future_collectors_can_supply_document_identity_without_pipeline_dispatch(self):
        document = {'title': 'some article', 'source': 'future', 'url': 'https://example.org/post'}
        expected = document | {'reason': 'failed', 'drop': {'channel': 'future', 'id': 'opaque'}}
        collector = SimpleNamespace(document_failure=lambda row, reason: expected)
        with patch.dict(collectors._REGISTRY, {'future': 'unused'}, clear=True), \
             patch.object(collectors, 'get_collector', return_value=collector):
            self.assertEqual(expected, document_failure(document, 'failed'))


if __name__ == '__main__':
    unittest.main()
