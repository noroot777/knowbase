"""跨轮目录连续性和最终授权必须同时成立,数据库行顺序不能决定删除结果。"""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from kb import scanner, store
from kb.config import Source
from tests.scan_fixture import REAL_CONNECT, ScanFixture


class ScanAuthorizationTests(ScanFixture):
    def test_replaced_nested_directory_preserves_all_data_across_restarts(self):
        path = self.seed('nested/deeper/a.md', snippet=True)
        before = self.snapshot()
        nested = self.sources[0].root / 'nested'
        moved = self.root / 'offline-subtree'
        root_identity = self.sources[0].root.stat().st_ino
        nested.rename(moved)
        (nested / 'deeper').mkdir(parents=True)
        self.assertEqual(root_identity, self.sources[0].root.stat().st_ino)
        for _attempt in range(2):
            self.conn.close()
            self.conn = REAL_CONNECT(self.db)
            self.addCleanup(self.conn.close)
            report, removed = self.index()
            self.assertEqual(before, self.snapshot())
            self.assertEqual(0, removed)
            self.assertIn('目录身份变化', report.warning)
            self.assertTrue((moved / 'deeper/a.md').is_file())
        shutil.rmtree(nested)
        moved.rename(nested)
        report, _ = self.index()
        self.assertFalse(report.errors)
        self.assertEqual(path, self.listed()['favorites'][0]['path'])
        self.assertEqual(1, len(self.listed()['favorites']))

    def test_missing_known_subdirectory_is_not_proof_of_file_deletion(self):
        self.seed('nested/a.md')
        before = self.snapshot()
        nested = self.sources[0].root / 'nested'
        nested.rename(self.root / 'offline')
        for _attempt in range(2):
            report, removed = self.index()
            self.assertEqual(before, self.snapshot())
            self.assertEqual(0, removed)
            self.assertIn('已知目录不可用', report.warning)

    def test_a_changed_device_at_a_known_subdirectory_protects_its_contents(self):
        path = self.seed('nested/a.md')
        before = self.snapshot()
        (self.vault / path).unlink()
        stat = Path.stat

        def changed_device(candidate, *args, **kwargs):
            info = stat(candidate, *args, **kwargs)
            if candidate == (self.vault / path).parent:
                values = {name: getattr(info, name) for name in (
                    'st_dev', 'st_ino', 'st_mode', 'st_size', 'st_mtime_ns', 'st_ctime_ns')}
                values['st_dev'] += 1
                return SimpleNamespace(**values)
            return info

        with mock.patch.object(Path, 'stat', changed_device):
            report, removed = self.index()
        self.assertEqual(0, removed)
        self.assertEqual(before, self.snapshot())
        self.assertIn('目录身份变化', report.warning)

    def test_upgrading_directory_history_still_honors_the_existing_root_identity(self):
        self.seed()
        self.conn.execute('DELETE FROM index_scan_directories')
        self.conn.commit()
        before = self.snapshot()
        root = self.sources[0].root
        root.rename(self.root / 'offline')
        root.mkdir()
        report, removed = self.index()
        self.assertEqual(0, removed)
        self.assertIn('来源根身份变化', report.warning)
        after = self.snapshot()
        before.pop('index_scan_directories')
        after.pop('index_scan_directories')
        self.assertEqual(before, after)

    def test_directory_history_is_part_of_the_cleanup_transaction(self):
        path = self.seed('nested/a.md')
        before = self.snapshot()
        root = self.sources[0].root
        root.rename(self.root / 'old')
        shutil.copytree(self.root / 'old', root)
        report, _ = self.scan()
        self.assertEqual(0, store.remove_missing_documents(self.conn, report))
        self.assertNotEqual(before['index_scan_directories'],
                            self.rows('index_scan_directories'))
        self.conn.rollback()
        self.assertEqual(before, self.snapshot())
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_fully_observed_replacement_subtree_can_resume_real_file_cleanup(self):
        path = self.seed('nested/a.md')
        nested = self.sources[0].root / 'nested'
        moved = self.root / 'old-subtree'
        nested.rename(moved)
        shutil.copytree(moved, nested)
        report, _ = self.index()
        self.assertFalse(report.errors)
        (self.vault / path).unlink()
        report, removed = self.index()
        self.assertFalse(report.errors)
        self.assertEqual(1, removed)
        self.assertEqual([], self.rows('documents'))

    def test_explicit_removal_can_release_an_unverified_replacement_subtree(self):
        path = self.seed('nested/a.md')
        nested = self.sources[0].root / 'nested'
        nested.rename(self.root / 'offline')
        nested.mkdir()
        self.assertEqual(0, self.index()[1])
        self.assertEqual(1, store.remove_documents(self.conn, [path]))
        self.conn.commit()
        report, removed = self.index()
        self.assertFalse(report.errors)
        self.assertEqual(0, removed)
        self.assertEqual([], self.rows('favorite_document_recovery'))

    def assert_final_permission_failure_preserves_source(self, first, second):
        self.seed(first, snippet=True)
        self.write(second)
        self.index()
        before = self.snapshot()
        for row in self.rows('documents'):
            (self.vault / row['path']).unlink()
        report, documents = self.scan()
        self.assertEqual([], documents)
        original_lstat = Path.lstat

        def unavailable(candidate, *args, **kwargs):
            if candidate == self.sources[0].root / 'b.md':
                raise PermissionError('cleanup-time permission loss')
            return original_lstat(candidate, *args, **kwargs)

        with mock.patch.object(Path, 'lstat', unavailable):
            removed = store.remove_missing_documents(self.conn, report)
        self.conn.commit()
        self.assertEqual(0, removed)
        self.assertEqual(before, self.snapshot())
        self.assertIn('cleanup-time permission loss', report.warning)

    def test_late_candidate_failure_revokes_earlier_candidates(self):
        self.assert_final_permission_failure_preserves_source('a.md', 'b.md')

    def test_early_candidate_failure_has_the_same_preservation_result(self):
        self.assert_final_permission_failure_preserves_source('b.md', 'a.md')

    def test_candidate_failure_in_overlapping_scope_protects_outer_scope_too(self):
        self.seed('a.md')
        nested = self.write('nested/b.md')
        sources = [*self.sources, Source('overlap', self.sources[0].root / 'nested')]
        with mock.patch.object(scanner, 'SOURCES', sources):
            self.index()
            before = self.snapshot()
            for row in self.rows('documents'):
                (self.vault / row['path']).unlink()
            report, _ = self.scan()
        original_lstat = Path.lstat

        def unavailable(candidate, *args, **kwargs):
            if candidate == self.vault / nested:
                raise PermissionError('overlapping verification failure')
            return original_lstat(candidate, *args, **kwargs)

        with mock.patch.object(Path, 'lstat', unavailable):
            removed = store.remove_missing_documents(self.conn, report)
        self.conn.commit()
        self.assertEqual(0, removed)
        self.assertEqual(before, self.snapshot())
        self.assertTrue(all(scope.errors for scope in (report.scopes[0], report.scopes[2])))

    def test_directory_changed_by_last_probe_revokes_all_earlier_candidates(self):
        self.seed()
        second = self.write('b.md')
        self.index()
        before = self.snapshot()
        for row in self.rows('documents'):
            (self.vault / row['path']).unlink()
        report, _ = self.scan()
        original_lstat = Path.lstat

        def change_directory(candidate, *args, **kwargs):
            if candidate == self.vault / second:
                (candidate.parent / 'export-in-progress.tmp').write_text('writing')
            return original_lstat(candidate, *args, **kwargs)

        with mock.patch.object(Path, 'lstat', change_directory):
            removed = store.remove_missing_documents(self.conn, report)
        self.conn.commit()
        self.assertEqual(0, removed)
        self.assertEqual(before, self.snapshot())
        self.assertIn('扫描期间目录发生变化', report.warning)

    def test_a_late_failure_does_not_block_a_different_healthy_source(self):
        kept = self.seed()
        absent = self.write('b.md')
        deleted = self.write(source='dr-gzh')
        self.index()
        for path in (kept, absent, deleted):
            (self.vault / path).unlink()
        report, _ = self.scan()
        original_lstat = Path.lstat

        def unavailable(candidate, *args, **kwargs):
            if candidate == self.vault / absent:
                raise PermissionError('rss unavailable')
            return original_lstat(candidate, *args, **kwargs)

        with mock.patch.object(Path, 'lstat', unavailable):
            removed = store.remove_missing_documents(self.conn, report)
        self.conn.commit()
        self.assertEqual(1, removed)
        self.assertEqual({kept, absent}, {row['path'] for row in self.rows('documents')})
        self.assertEqual([kept], self.listed()['doc_paths'])
        self.assertFalse(report.scopes[1].errors)

    def test_unused_missing_root_has_neither_failure_nor_deletion_authority(self):
        self.sources[1].root.rmdir()
        report, removed = self.index()
        self.assertFalse(report.errors)
        self.assertEqual(0, removed)
        self.assertFalse(report.allows_missing('source/dr-gzh/absent.md', 'dr-gzh'))
        self.assertEqual(['source/rss'], [row['root'] for row in self.rows('index_scan_roots')])

    def test_final_authorization_is_pure_and_does_not_probe_files_again(self):
        path = self.seed()
        (self.vault / path).unlink()
        report, _ = self.scan()
        with mock.patch.object(Path, 'lstat', side_effect=AssertionError('not a pure check')):
            self.assertTrue(report.allows_missing(path, 'rss'))
            self.assertTrue(report.allows_missing(path, 'rss'))

    def test_one_overlapping_scope_excluding_a_path_vetoes_its_deletion(self):
        path = self.seed('nested/excluded/a.md')
        nested = self.sources[0].root / 'nested'
        sources = [*self.sources, Source('overlap', nested, skip_dirs=frozenset({'excluded'}))]
        (self.vault / path).unlink()
        with mock.patch.object(scanner, 'SOURCES', sources):
            report, removed = self.index()
        self.assertFalse(report.errors)
        self.assertEqual(0, removed)
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_explicit_deletion_does_not_require_scan_authorization(self):
        path = self.seed('nested/a.md')
        before_root = self.sources[0].root
        before_root.rename(self.root / 'offline')
        self.assertEqual(1, store.remove_documents(self.conn, [path]))
        self.conn.commit()
        self.assertEqual([], self.rows('documents'))
        self.assertEqual([], self.rows('favorite_document_recovery'))
