"""CLI、周期索引和复用清理入口采用同一份删除授权,异常不能报绿色成功。"""

from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace
from unittest import mock

from kb import cli, config, pipeline_stages
from kb.feeds import exclusion
from tests.feed_trim_fixture import trim
from tests.scan_fixture import ScanFixture, TEXT


class ScanIndexEntryTests(ScanFixture):
    def cli_index(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.cmd_index(SimpleNamespace(reembed=False, no_vector=True))
        return result, output.getvalue()

    def pipeline_index(self):
        with mock.patch.object(pipeline_stages, '_stage') as stage:
            pipeline_stages._run_index()
        return stage.call_args_list

    def test_cli_reports_a_decode_failure_but_keeps_all_user_and_index_data(self):
        path = self.seed()
        before = self.snapshot()
        (self.vault / path).write_bytes(b'\xff')
        result, output = self.cli_index()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(1, result)
        self.assertIn('扫描不完整', output)
        self.assertIn('移除 0', output)
        self.write()
        self.assertEqual(0, self.cli_index()[0])
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_scheduled_pipeline_reports_an_unavailable_root_without_detaching_favorites(self):
        path = self.seed()
        before = self.snapshot()
        root = self.sources[0].root
        root.rename(root.with_name('offline'))
        stages = self.pipeline_index()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(('index', 'error'), stages[-1].args[:2])
        self.assertIn('扫描不完整', stages[-1].args[2])
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_cli_and_scheduled_pipeline_both_restore_the_original_favorite_before_recollecting(self):
        for entry in (self.cli_index, self.pipeline_index):
            with self.subTest(entry=entry):
                path = self.seed()
                favorite_id = self.rows('favorites')[0]['id']
                (self.vault / path).unlink()
                entry()
                self.assertIsNone(self.rows('favorites')[0]['path'])
                self.write(text=TEXT)
                entry()
                self.assertEqual([(favorite_id, path)],
                                 [(row['id'], row['path']) for row in self.rows('favorites')])
                self.assertEqual([path], self.listed()['doc_paths'])
                self.client.delete(f'/api/favorites/{favorite_id}')

    def test_history_cleanup_is_explicit_and_only_removes_its_authorized_paths(self):
        path = self.seed()
        unrelated = self.write('other.md')
        self.index()
        (self.vault / path).unlink()
        self.assertEqual(1, trim._drop_index(self.conn, [path]))
        self.conn.commit()
        self.assertEqual([unrelated], [row['path'] for row in self.rows('documents')])
        self.assertEqual([], self.rows('favorite_document_recovery'))
        self.write()
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])

    def test_permanent_feed_exclusion_revokes_recovery_even_after_a_missing_scan(self):
        path = self.seed()
        (self.vault / path).unlink()
        self.index()
        with mock.patch.object(config, 'VAULT_ROOT', self.vault), \
             mock.patch.object(config, 'DB_PATH', self.db):
            exclusion._cleanup('rss', path)
        self.assertEqual([], self.rows('favorite_document_recovery'))
        self.write()
        self.index()
        self.assertIsNone(self.rows('favorites')[0]['path'])
