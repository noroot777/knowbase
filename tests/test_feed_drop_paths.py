import sqlite3
from contextlib import contextmanager
from unittest.mock import patch

from kb import config, pipeline_state, store
from kb.collectors.feeds import FeedCollector
from kb.feeds import exclusion, registry as reg, sync
from kb.feeds.paths import export_path
from tests.test_feed_fulltext import FullTextCase


class FeedDropPathTests(FullTextCase):
    def setUp(self):
        super().setUp()
        for replacement in (patch.object(config, 'VAULT_ROOT', self.root / 'vault'),
                            patch.dict(pipeline_state._state, {'stop_requested': False})):
            replacement.start()
            self.addCleanup(replacement.stop)
        self.collector = FeedCollector('rss')

    def index(self, relative):
        conn = store.connect(config.DB_PATH)
        try:
            conn.execute('INSERT INTO documents(path,source,title) VALUES(?,?,?)',
                         (relative, 'rss', '原文'))
            conn.commit()
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
                self.index(relative)
                self.assert_cleaned(ref, [relative])

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
        self.index(relative)
        ref = {'id': key, 'link': link}
        self.assert_cleaned(ref, [relative])
        self.assert_cleaned(ref, [relative])
