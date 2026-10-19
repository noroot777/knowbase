import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from kb import config, pipeline_state, store
from kb.collectors import document_failure
from kb.collectors.feeds import FeedCollector
from kb.feeds import exclusion, registry as reg, sync
from kb.feeds.paths import document_key, export_path, item_key
from tests.test_feed_fulltext import FullTextCase


class FeedPathIdentityTests(unittest.TestCase):
    def test_only_the_canonical_path_and_matching_link_prove_identity(self):
        url = 'https://e.org/post'
        key = item_key('rss', url)
        relative = str(export_path('rss', key))
        self.assertEqual(key, document_key('rss', relative, url))
        for kind, candidate, link in (
                ('web', relative, url), ('rss', relative, 'https://e.org/other'),
                ('rss', relative, ''), ('rss', '', url),
                ('rss', '/' + relative, url), ('rss', f'source/rss/../rss/{key}.md', url),
                ('rss', f'source/rss/sub/{key}.md', url),
                ('rss', relative + '.bak', url), ('rss', f'source/rss/{key}.txt', url)):
            with self.subTest(kind=kind, candidate=candidate, link=link):
                self.assertEqual('', document_key(kind, candidate, link))


class FeedDropPathTests(FullTextCase):
    def setUp(self):
        super().setUp()
        for replacement in (patch.object(config, 'VAULT_ROOT', self.root / 'vault'),
                            patch.dict(pipeline_state._state, {'stop_requested': False})):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.collector = FeedCollector('rss')

    def index(self, relative, url='https://e.org/post'):
        conn = store.connect(config.DB_PATH)
        try:
            conn.execute('INSERT INTO documents(path,source,title,url) VALUES(?,?,?,?)',
                         (relative, 'rss', '原文', url))
            conn.commit()
            return dict(conn.execute('SELECT * FROM documents WHERE path=?', (relative,)).fetchone())
        finally:
            conn.close()

    def assert_cleaned(self, ref, relatives):
        self.collector.drop_item(ref)
        for relative in relatives:
            self.assertFalse((config.VAULT_ROOT / relative).exists())
        conn = store.connect(config.DB_PATH)
        try:
            self.assertEqual(0, conn.execute('SELECT count(*) FROM documents').fetchone()[0])
        finally:
            conn.close()

    def test_published_but_unacknowledged_exports_are_removed(self):
        for mode in ('database_error', 'changed_body'):
            with self.subTest(mode=mode):
                key = self.put(source=self.plain, url=f'https://e.org/{mode}')
                ref = exclusion.failure(self.row(key), '失败', 'rss')['drop']
                relative = str(export_path('rss', key))
                original_db, replace = reg.db, sync.os.replace
                published = False
                failed = False

                def publish(source, target):
                    nonlocal published
                    result = replace(source, target)
                    if mode == 'changed_body':
                        with original_db() as conn:
                            conn.execute('UPDATE feed_items SET content_hash=? WHERE key=?',
                                         ('new-version', key))
                    published = True
                    return result

                @contextmanager
                def acknowledge():
                    nonlocal failed
                    if published and mode == 'database_error' and not failed:
                        failed = True
                        raise sqlite3.OperationalError('database is locked')
                    with original_db() as conn:
                        yield conn

                with patch.object(sync.os, 'replace', side_effect=publish), \
                     patch.object(reg, 'db', side_effect=acknowledge), \
                     patch.object(sync, '_stage') as stage:
                    sync.export('rss')
                self.assertTrue((config.VAULT_ROOT / relative).is_file())
                self.assertEqual('', self.row(key)['path'])
                if mode == 'database_error':
                    self.assertEqual(ref, stage.call_args.kwargs['failures'][0]['drop'])
                document = self.index(relative, ref['link'])
                report = document_failure(document, '正文为空，需回填')
                self.assertEqual(ref, report.get('drop'))
                self.assert_cleaned(report['drop'], [relative])

    def test_canonical_and_legacy_recorded_paths_are_both_removed(self):
        key = self.put(source=self.plain)
        sync.export('rss')
        canonical = str(export_path('rss', key))
        legacy = 'source/rss/legacy.md'
        (config.VAULT_ROOT / legacy).write_text('旧位置', encoding='utf-8')
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET path=? WHERE key=?', (legacy, key))
        ref = exclusion.failure(self.row(key), '失败', 'rss')['drop']
        for relative in (canonical, legacy):
            self.index(relative)
        self.assert_cleaned(ref, [canonical, legacy])

    def test_canonical_path_cleanup_is_idempotent_without_a_live_row(self):
        link = 'https://e.org/missing'
        key = exclusion.item_key('rss', link)
        relative = str(export_path('rss', key))
        path = config.VAULT_ROOT / relative
        path.parent.mkdir(parents=True)
        path.write_text('已落盘但暂存行丢失', encoding='utf-8')
        document = self.index(relative, link)
        report = document_failure(document, '正文为空，需回填')
        ref = {'channel': 'rss', 'id': key, 'link': link}
        self.assertEqual(ref, report.get('drop'))
        self.assert_cleaned(report['drop'], [relative])
        self.assert_cleaned(ref, [relative])

    def test_legacy_paths_use_live_rows_or_exclusions_as_supplemental_identity(self):
        key = self.put(source=self.plain)
        legacy = 'source/rss/legacy.md'
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET path=? WHERE key=?', (legacy, key))
        document = self.index(legacy)
        for excluded in (False, True):
            with self.subTest(excluded=excluded):
                report = document_failure(document, '正文为空，需回填')
                self.assertEqual(key, report['drop']['id'])
                self.assertEqual('https://e.org/post', report['url'])
                self.assert_cleaned(report['drop'], [legacy, export_path('rss', key)])
        for changed in ({'path': 'source/rss/unknown.md'},
                        {'url': 'https://e.org/other'}, {'source': 'web'}):
            self.assertNotIn('drop', document_failure(document | changed, '失败'))
