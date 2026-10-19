"""扫描缺口不等于删除证明;断言原文、派生索引和收藏的最终持久化状态。"""

from __future__ import annotations

import os
import contextlib
import shutil
from dataclasses import replace
from pathlib import Path
from unittest import mock

from kb import scanner, store
from tests.scan_fixture import ScanFixture, TEXT


class ScanIntegrityTests(ScanFixture):
    def test_read_and_decode_failures_keep_every_index_table_and_favorite(self):
        path = self.seed(snippet=True)
        before = self.snapshot()
        full = self.vault / path
        read = Path.read_text
        for failure in ('decode', 'permission'):
            with self.subTest(failure=failure):
                if failure == 'decode':
                    full.write_bytes(b'\xff\xfe')
                    report, removed = self.index()
                else:
                    def unreadable(candidate, *args, **kwargs):
                        if candidate == full:
                            raise PermissionError('temporary read failure')
                        return read(candidate, *args, **kwargs)
                    with mock.patch.object(Path, 'read_text', unreadable):
                        report, removed = self.index()
                self.assertEqual(before, self.snapshot())
                self.assertEqual(0, removed)
                self.assertTrue(report.errors)
                full.write_text(TEXT)
        report, removed = self.index()
        self.assertEqual(([], 0), (report.errors, removed))
        self.assertEqual(path, self.listed()['favorites'][0]['path'])

    def test_unavailable_enabled_root_keeps_existing_data_then_recovers_without_duplicates(self):
        path = self.seed()
        before = self.snapshot()
        root = self.sources[0].root
        moved = root.with_name('temporarily-offline')
        root.rename(moved)
        report, removed = self.index()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0, removed)
        self.assertIn(str(root), report.warning)
        moved.rename(root)
        self.index()
        self.assertEqual([path], self.listed()['doc_paths'])
        self.assertEqual(1, len(self.listed()['favorites']))

    def test_directory_enumeration_failure_protects_the_entire_source_only(self):
        kept = self.seed('nested/a.md')
        absent = self.write('gone.md')
        healthy = self.write('gone.md', source='dr-gzh')
        self.index()
        for path in (absent, healthy):
            (self.vault / path).unlink()
        scandir = os.scandir

        def unavailable(directory):
            if Path(directory) == (self.vault / kept).parent:
                raise PermissionError('unreadable subtree')
            return scandir(directory)

        with mock.patch('kb.scan_status.os.scandir', unavailable):
            report, removed = self.index()
        self.assertEqual({kept, absent}, {row['path'] for row in self.rows('documents')})
        self.assertEqual([kept], self.listed()['doc_paths'])
        self.assertEqual(1, removed)
        self.assertTrue(report.errors)

    def test_an_empty_replacement_root_cannot_delete_documents_from_the_offline_root(self):
        path = self.seed()
        before = self.snapshot()
        root = self.sources[0].root
        moved = root.with_name('offline-original')
        root.rename(moved)
        root.mkdir()
        for _attempt in range(2):
            report, removed = self.index()
            self.assertEqual(before, self.snapshot())
            self.assertEqual(0, removed)
            self.assertIn('来源根身份变化', report.warning)
        root.rmdir()
        moved.rename(root)
        report, _ = self.index()
        self.assertFalse(report.errors)
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_a_fully_observed_replacement_root_can_resume_normal_cleanup(self):
        path = self.seed()
        root = self.sources[0].root
        moved = root.with_name('original-root')
        root.rename(moved)
        shutil.copytree(moved, root)
        report, _ = self.index()
        self.assertFalse(report.errors)
        self.assertEqual([path], self.listed()['doc_paths'])
        (self.vault / path).unlink()
        self.assertEqual(1, self.index()[1])

    def test_skip_scope_root_change_removed_source_and_disabled_source_are_not_deletions(self):
        path = self.seed('excluded/a.md')
        before = self.snapshot()
        before.pop('index_scan_roots')
        moved_root = self.vault / 'renamed-root'
        moved_root.mkdir()
        variants = (
            [replace(self.sources[0], skip_dirs=frozenset({'excluded'})), self.sources[1]],
            [replace(self.sources[0], root=moved_root), self.sources[1]],
            [self.sources[1]],
            [replace(self.sources[0], enabled=False), self.sources[1]],
        )
        for sources in variants:
            with self.subTest(sources=sources), mock.patch.object(scanner, 'SOURCES', sources):
                report, removed = self.index()
                after = self.snapshot()
                after.pop('index_scan_roots')
                self.assertEqual(before, after)
                self.assertEqual(0, removed)
                self.assertFalse(report.errors)
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_an_accessible_legitimately_empty_source_does_authorize_real_deletion(self):
        path = self.seed(snippet=True)
        (self.vault / path).unlink()
        report, removed = self.index()
        self.assertEqual([], self.rows('documents'))
        for table in ('chunks', 'chunks_fts', 'chunk_vec', 'doc_tags', 'entities'):
            self.assertEqual([], self.rows(table), table)
        self.assertEqual(1, removed)
        self.assertFalse(report.errors)
        self.assertIsNone(self.rows('favorites')[0]['path'])
        self.assertEqual(1, len(self.rows('favorite_document_recovery')))

    def test_raw_paths_and_a_partially_consumed_scan_never_authorize_cleanup(self):
        self.seed()
        before = self.snapshot()
        with self.assertRaises(TypeError):
            store.remove_missing_documents(self.conn, set())
        report = scanner.ScanReport()
        iterator = scanner.scan_all(report=report)
        next(iterator)
        with self.assertRaises(ValueError):
            store.remove_missing_documents(self.conn, report)
        iterator.close()
        self.assertEqual(before, self.snapshot())

    def test_reappearing_file_is_rechecked_even_with_a_completed_scan(self):
        path = self.seed()
        (self.vault / path).unlink()
        report, docs = self.scan()
        self.assertEqual([], docs)
        self.write()
        self.assertEqual(0, store.remove_missing_documents(self.conn, report))
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_root_disappearing_after_enumeration_invalidates_deletion_authority(self):
        path = self.seed()
        (self.vault / path).unlink()
        report, _ = self.scan()
        self.sources[0].root.rmdir()
        self.assertEqual(0, store.remove_missing_documents(self.conn, report))
        self.assertEqual([path], self.listed()['doc_paths'])
        self.assertTrue(report.errors)

    def test_export_intermediate_states_do_not_replace_the_indexed_version(self):
        path = self.seed(snippet=True)
        before = self.snapshot()
        for text in ('', '  \n', '---\ntitle: 尚未写完',
                     '---\ntitle: [broken\n---\n正文', '---\n- invalid\n---\n正文'):
            with self.subTest(text=text):
                (self.vault / path).write_text(text)
                report, removed = self.index()
                self.assertEqual(before, self.snapshot())
                self.assertEqual(0, removed)
                self.assertTrue(report.errors)

    def test_a_file_replaced_while_reading_is_not_indexed(self):
        path = self.seed(snippet=True)
        full = self.vault / path
        before = self.snapshot()
        read = Path.read_text

        def changing(candidate, *args, **kwargs):
            text = read(candidate, *args, **kwargs)
            if candidate == full:
                candidate.write_text(TEXT + '并发写入')
            return text

        with mock.patch.object(Path, 'read_text', changing):
            report, removed = self.index()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(0, removed)
        self.assertTrue(report.errors)

    def test_symlink_subtrees_are_not_authorized_for_deletion(self):
        path = self.seed('linked/a.md')
        subtree = self.sources[0].root / 'linked'
        outside = self.root / 'external'
        subtree.rename(outside)
        subtree.symlink_to(outside, target_is_directory=True)
        (outside / 'a.md').unlink()
        self.assertEqual(0, self.index()[1])
        self.assertEqual([path], self.listed()['doc_paths'])

    def test_relative_vault_configuration_keeps_the_same_index_path(self):
        path = self.write()
        sources = [replace(source, root=Path('vault') / 'source' / source.name)
                   for source in self.sources]
        with contextlib.chdir(self.root), \
             mock.patch.object(scanner, 'VAULT_ROOT', Path('vault')), \
             mock.patch.object(scanner, 'SOURCES', sources):
            report, documents = self.scan()
        self.assertFalse(report.errors)
        self.assertEqual([path], [doc.path for doc in documents])
