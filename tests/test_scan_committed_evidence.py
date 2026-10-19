"""已提交文档必须携带同版本目录凭据;缺证据不能靠眼前空目录补造。"""

from __future__ import annotations

import contextlib
import io
import sqlite3
from dataclasses import replace
from types import SimpleNamespace
from unittest import mock

from kb import cli, pipeline_stages, scanner, store
from kb.chunker import split_chunks
from kb.config import Source
from tests.scan_fixture import NOW, REAL_CONNECT, ScanFixture, TEXT


class CommittedScanEvidenceTests(ScanFixture):
    def cli_index(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.cmd_index(SimpleNamespace(reembed=False, no_vector=True))
        return result, output.getvalue()

    def reopen(self):
        self.conn.close()
        self.conn = REAL_CONNECT(self.db)
        self.addCleanup(self.conn.close)

    def replace_subtree(self, source='rss'):
        nested = self.vault / 'source' / source / 'nested'
        outside = self.root / f'offline-{source}'
        nested.rename(outside)
        nested.mkdir()
        return outside

    def assert_offline_kept(self, path):
        self.favorite(path, snippet=True)
        outside = self.replace_subtree(path.split('/')[1])
        before = self.snapshot()
        for _attempt in range(2):
            self.reopen()
            result, output = self.cli_index()
            self.assertEqual(1, result, output)
            self.assertIn('不授权清理', output)
            after = self.snapshot()
            for table, key in (('index_scan_roots', 'root'), ('index_scan_directories', 'path')):
                current = {row[key]: row for row in after.pop(table)}
                self.assertTrue(all(current.get(row[key]) == row for row in before[table]))
            self.assertEqual({table: rows for table, rows in before.items()
                              if table not in ('index_scan_roots', 'index_scan_directories')}, after)
            self.assertTrue(any(item['path'] == path for item in self.listed()['favorites']))
            self.assertTrue((outside / path.rsplit('/', 1)[-1]).is_file())

    def test_successful_incremental_ingestion_commits_directory_evidence(self):
        self.seed()
        source = Source('dr-gzh-projects', self.vault / 'source/dr-gzh-projects',
                        pre_tagged=True, derived=True)
        self.sources.append(source)
        path = self.write('nested/new.md', source=source.name)
        self.assertEqual((1, 0), pipeline_stages._ingest_pretagged_sources())
        receipt = next(row for row in self.rows('document_scan_evidence') if row['path'] == path)
        self.assertEqual(source.name, receipt['source'])
        self.assertTrue(any(row['path'] == 'source/dr-gzh-projects/nested'
                            for row in self.rows('index_scan_directories')))
        self.assert_offline_kept(path)

    def test_new_directory_from_an_erroneous_scan_is_protected_after_restart(self):
        self.seed()
        path = self.write('nested/new.md')
        broken = self.write('broken.md')
        (self.vault / broken).write_bytes(b'\xff')
        self.assertEqual(1, self.cli_index()[0])
        self.assertIn(path, [row['path'] for row in self.rows('document_scan_evidence')])
        (self.vault / broken).unlink()
        self.assert_offline_kept(path)

    def test_cli_early_commit_and_interruption_keep_evidence_for_every_committed_document(self):
        for number in range(51):
            self.write(f'nested/{number:02d}.md')
        read = scanner._doc_from_file

        def interrupted(full, source):
            if full.name == '50.md':
                raise RuntimeError('interrupted after a committed batch')
            return read(full, source)

        with mock.patch.object(scanner, '_doc_from_file', interrupted), \
             self.assertRaisesRegex(RuntimeError, 'committed batch'):
            self.cli_index()
        self.reopen()
        self.assertEqual(50, len(self.rows('documents')))
        self.assertEqual(50, len(self.rows('document_scan_evidence')))
        self.assert_offline_kept('source/rss/nested/00.md')

    def test_periodic_early_commit_and_failure_have_the_same_protection(self):
        for number in range(26):
            self.write(f'nested/{number:02d}.md')
        read = scanner._doc_from_file

        def interrupted(full, source):
            if full.name == '25.md':
                raise RuntimeError('periodic interruption')
            return read(full, source)

        with mock.patch.object(scanner, '_doc_from_file', interrupted), \
             mock.patch.object(pipeline_stages, '_stage') as stage:
            pipeline_stages._run_index()
        self.assertEqual(('index', 'error'), stage.call_args_list[-1].args[:2])
        self.reopen()
        self.assertEqual(25, len(self.rows('documents')))
        self.assertEqual(25, len(self.rows('document_scan_evidence')))
        self.assert_offline_kept('source/rss/nested/00.md')

    def test_store_upsert_itself_commits_evidence_without_a_completed_report(self):
        path = self.write('nested/new.md')
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        store.upsert_document(self.conn, doc, split_chunks(doc.body), NOW)
        self.conn.commit()
        self.reopen()
        self.assertEqual([path], [row['path'] for row in self.rows('document_scan_evidence')])
        (self.vault / path).unlink()
        report, removed = self.index()
        self.assertFalse(report.errors)
        self.assertEqual(1, removed)
        self.assertEqual([], self.rows('document_scan_evidence'))

    def test_unknown_legacy_document_is_not_blessed_by_a_new_empty_shell(self):
        path = self.seed('nested/new.md')
        self.conn.execute('DELETE FROM document_scan_evidence')
        self.conn.execute('DELETE FROM index_scan_directories')
        self.conn.execute('DELETE FROM index_scan_roots')
        self.conn.commit()
        before = self.snapshot()
        self.replace_subtree()
        for _attempt in range(2):
            self.reopen()
            result, output = self.cli_index()
            self.assertEqual(1, result)
            self.assertIn('缺少与已提交版本对应', output)
            after = self.snapshot()
            before.pop('index_scan_roots', None)
            before.pop('index_scan_directories', None)
            after.pop('index_scan_roots')
            after.pop('index_scan_directories')
            self.assertEqual(before, after)
            self.assertEqual([path], self.listed()['doc_paths'])

    def test_missing_receipt_is_protected_even_when_directory_history_exists(self):
        path = self.seed()
        self.conn.execute('DELETE FROM document_scan_evidence')
        self.conn.commit()
        before = self.snapshot()
        (self.vault / path).unlink()
        report, removed = self.index()
        self.assertEqual(0, removed)
        self.assertEqual(before, self.snapshot())
        self.assertIn('缺少与已提交版本对应', report.warning)
        self.assertEqual(1, store.remove_documents(self.conn, [path]))

    def test_document_lineage_protects_a_replaced_subtree_without_shared_history(self):
        path = self.seed('nested/new.md')
        self.conn.execute('DELETE FROM index_scan_roots')
        self.conn.execute('DELETE FROM index_scan_directories')
        self.conn.commit()
        self.assert_offline_kept(path)

    def test_old_schema_upgrades_to_unknown_until_original_file_is_verified(self):
        path = self.seed()
        self.conn.execute('DROP TABLE document_scan_evidence')
        self.conn.commit()
        (self.vault / path).unlink()
        self.reopen()
        report, removed = self.index()
        self.assertEqual(0, removed)
        self.assertIn('缺少与已提交版本对应', report.warning)
        self.assertEqual([], self.rows('document_scan_evidence'))
        self.write()
        self.assertEqual(0, self.cli_index()[0])
        self.assertEqual([path], [row['path'] for row in self.rows('document_scan_evidence')])
        (self.vault / path).unlink()
        self.assertEqual(1, self.index()[1])

    def test_unchanged_real_document_can_establish_a_safe_baseline_without_reindexing(self):
        path = self.seed()
        self.conn.execute('DELETE FROM document_scan_evidence')
        self.conn.execute("UPDATE documents SET summary='模型摘要' WHERE path=?", (path,))
        self.conn.commit()
        before = self.snapshot()
        result, output = self.cli_index()
        self.assertEqual(0, result)
        self.assertIn('未变 1', output)
        self.assertEqual(1, len(self.rows('document_scan_evidence')))
        after = self.snapshot()
        before.pop('document_scan_evidence')
        after.pop('document_scan_evidence')
        self.assertEqual(before, after)
        (self.vault / path).unlink()
        self.assertEqual(1, self.index()[1])

    def test_a_changed_file_is_not_a_baseline_for_the_committed_old_version(self):
        path = self.seed()
        self.conn.execute('DELETE FROM document_scan_evidence')
        self.conn.commit()
        self.write(text=TEXT + '另一版正文')
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        self.assertFalse(store.observed_document_is_current(self.conn, doc))
        self.conn.commit()
        self.assertEqual([], self.rows('document_scan_evidence'))
        (self.vault / path).unlink()
        self.assertEqual(0, self.index()[1])

    def test_all_three_entries_baseline_unchanged_documents_without_rebuilding(self):
        source = Source('dr-gzh-projects', self.vault / 'source/dr-gzh-projects',
                        pre_tagged=True, derived=True)
        self.sources.append(source)
        path = self.write('nested/new.md', source=source.name)
        self.index()
        self.conn.execute("UPDATE documents SET summary='模型摘要' WHERE path=?", (path,))
        self.conn.commit()
        for entry in (self.cli_index, pipeline_stages._run_index,
                      pipeline_stages._ingest_pretagged_sources):
            with self.subTest(entry=entry):
                self.conn.execute('DELETE FROM document_scan_evidence')
                self.conn.commit()
                before = self.snapshot()
                with mock.patch.object(store, 'upsert_document',
                                       side_effect=AssertionError('must not reindex')), \
                     mock.patch.object(pipeline_stages, '_stage'):
                    entry()
                after = self.snapshot()
                self.assertEqual([path], [row['path'] for row in after['document_scan_evidence']])
                before.pop('document_scan_evidence')
                after.pop('document_scan_evidence')
                self.assertEqual(before, after)

    def test_a_proof_from_a_different_version_cannot_be_attached_to_a_new_write(self):
        path = self.seed()
        old = scanner._doc_from_file(self.vault / path, self.sources[0])
        self.write(text=TEXT + '不同版本')
        new = scanner._doc_from_file(self.vault / path, self.sources[0])
        new.scan_evidence = old.scan_evidence
        store.upsert_document(self.conn, new, split_chunks(new.body), NOW)
        self.conn.commit()
        self.assertEqual([], self.rows('document_scan_evidence'))
        (self.vault / path).unlink()
        self.assertEqual(0, self.index()[1])

    def test_baseline_sql_rejects_a_different_source_even_with_the_same_file_hash(self):
        path = self.seed()
        self.conn.execute('DELETE FROM document_scan_evidence')
        self.conn.commit()
        doc = scanner._doc_from_file(self.vault / path,
                                     replace(self.sources[0], name='renamed-source'))
        store.observed_document_is_current(self.conn, doc)
        self.conn.commit()
        self.assertEqual([], self.rows('document_scan_evidence'))
        (self.vault / path).unlink()
        self.assertEqual(0, self.index()[1])

    def test_baseline_sql_rechecks_version_after_the_incremental_read_gate(self):
        path = self.seed()
        self.conn.execute('DELETE FROM document_scan_evidence')
        self.conn.commit()
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        needs_index = store.doc_needs_reindex

        def changed_after_read(conn, candidate, content_hash):
            result = needs_index(conn, candidate, content_hash)
            conn.execute("UPDATE documents SET content_hash='another-committed-version' "
                         'WHERE path=?', (candidate,))
            return result

        with mock.patch.object(store, 'doc_needs_reindex', changed_after_read):
            self.assertTrue(store.observed_document_is_current(self.conn, doc))
        self.conn.commit()
        self.assertEqual([], self.rows('document_scan_evidence'))
        (self.vault / path).unlink()
        self.assertEqual(0, self.index()[1])

    def test_unproven_replacement_version_revokes_previous_receipt(self):
        path = self.seed()
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        store.upsert_document(self.conn, replace(doc, scan_evidence=None),
                              split_chunks(doc.body), NOW)
        self.conn.commit()
        self.assertEqual([], self.rows('document_scan_evidence'))
        (self.vault / path).unlink()
        self.assertEqual(0, self.index()[1])

    def test_corrupt_or_version_mismatched_receipts_never_authorize_deletion(self):
        path = self.seed()
        original = self.rows('document_scan_evidence')[0]
        (self.vault / path).unlink()
        for column, value in (('directories', 'not-json'), ('directories', '[]'),
                              ('source', 'different'), ('content_hash', 'different')):
            with self.subTest(column=column, value=value):
                self.conn.execute('UPDATE document_scan_evidence SET '
                                  'directories=?, source=?, content_hash=? WHERE path=?',
                                  (original['directories'], original['source'],
                                   original['content_hash'], path))
                self.conn.execute(f'UPDATE document_scan_evidence SET {column}=? WHERE path=?',
                                  (value, path))
                self.conn.commit()
                before = self.snapshot()
                report, removed = self.index()
                self.assertEqual(0, removed)
                self.assertTrue(report.errors)
                self.assertEqual(before, self.snapshot())

    def test_receipt_write_failure_rolls_back_this_document_not_the_previous_batch(self):
        first = self.write('first.md')
        doc = scanner._doc_from_file(self.vault / first, self.sources[0])
        store.upsert_document(self.conn, doc, split_chunks(doc.body), NOW)
        self.conn.execute("CREATE TEMP TRIGGER reject_receipt BEFORE INSERT ON "
                          "document_scan_evidence BEGIN SELECT RAISE(ABORT,'receipt failed'); END")
        second = self.write('nested/new.md')
        doc = scanner._doc_from_file(self.vault / second, self.sources[0])
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'receipt failed'):
            store.upsert_document(self.conn, doc, split_chunks(doc.body), NOW)
        self.conn.commit()
        self.reopen()
        self.assertEqual([first], [row['path'] for row in self.rows('documents')])
        self.assertEqual([first], [row['path'] for row in self.rows('document_scan_evidence')])
        self.assertEqual([first], [row['path'] for row in self.rows('chunks')])

    def test_updating_receipt_failure_restores_old_version_and_all_user_data(self):
        path = self.seed(snippet=True)
        before = self.snapshot()
        self.write(text=TEXT + '更新正文')
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        self.conn.execute("CREATE TEMP TRIGGER reject_receipt BEFORE INSERT ON "
                          "document_scan_evidence BEGIN SELECT RAISE(ABORT,'receipt failed'); END")
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'receipt failed'):
            store.upsert_document(self.conn, doc, split_chunks(doc.body), NOW)
        self.conn.commit()
        self.assertEqual(before, self.snapshot())

    def test_receipt_failure_also_rolls_back_favorite_recovery(self):
        path = self.seed(snippet=True)
        (self.vault / path).unlink()
        self.assertEqual(1, self.index()[1])
        before = self.snapshot()
        self.write()
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        self.conn.execute("CREATE TEMP TRIGGER reject_receipt BEFORE INSERT ON "
                          "document_scan_evidence BEGIN SELECT RAISE(ABORT,'receipt failed'); END")
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'receipt failed'):
            store.upsert_document(self.conn, doc, split_chunks(doc.body), NOW)
        self.conn.commit()
        self.assertEqual(before, self.snapshot())

    def test_caller_rollback_also_removes_evidence_without_an_implicit_commit(self):
        path = self.write('nested/new.md')
        doc = scanner._doc_from_file(self.vault / path, self.sources[0])
        store.upsert_document(self.conn, doc, split_chunks(doc.body), NOW)
        self.conn.rollback()
        self.reopen()
        for table in ('documents', 'chunks', 'document_scan_evidence', 'index_scan_directories'):
            self.assertEqual([], self.rows(table), table)
