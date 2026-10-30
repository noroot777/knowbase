import errno
import sqlite3
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

from kb import maintenance
from kb.feeds import registry as reg
from kb.youtube import candidate_metadata as candidates
from kb.youtube import collector, discovery, login, media, notes, products, ranking, storage
from kb.youtube.faults import local_fault, record_local
from kb.youtube.settings import of
from tests.test_youtube_incremental import IncrementalFixture


class LocalFaultClassification(unittest.TestCase):
    def test_local_storage_resources_are_distinct_from_remote_or_programming_errors(self):
        for error in (maintenance.LockBusy('索引正在跑'), sqlite3.OperationalError('database is locked'),
                      sqlite3.OperationalError('database table is locked'),
                      sqlite3.OperationalError('database is busy'),
                      sqlite3.OperationalError('database or disk is full'),
                      sqlite3.OperationalError('disk I/O error'),
                      sqlite3.OperationalError('attempt to write a readonly database'),
                      OSError(errno.ENOSPC, '磁盘已满'), OSError(errno.EDQUOT, '配额已满'),
                      PermissionError(errno.EACCES, '无写入权限')):
            with self.subTest(error=error):
                self.assertTrue(local_fault(error))
        for error in (RuntimeError('上游失败'), sqlite3.OperationalError('near invalid: syntax error'),
                      TimeoutError(errno.ETIMEDOUT, '网络超时'),
                      ConnectionResetError(errno.ECONNRESET, '远端关闭'),
                      media.ToolFailure('会员专享', 'This video is members-only', 'unknown')):
            with self.subTest(error=error):
                self.assertFalse(local_fault(error))

    def test_structured_extended_sqlite_error_codes_are_used(self):
        error = sqlite3.OperationalError('本地化错误文本')
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY | (2 << 8)
        self.assertTrue(local_fault(error))

    def test_local_notices_deduplicate_without_creating_channel_failure_fields(self):
        summary = {'message': ''}
        for _ in range(2):
            record_local(summary, maintenance.LockBusy('索引忙'), [1, 1, 2])
        self.assertEqual(['1', '2'], summary['local_ids'])
        self.assertEqual(1, summary['message'].count('索引忙'))
        self.assertNotIn('failed_ids', summary)
        self.assertNotIn('errors', summary)


class MetadataFactPreservation(IncrementalFixture):
    def precise(self):
        entry = {**self.entry(), 'title': '每周工具速览 第 12 期',
                 'description': 'This episode covers LLM agents and RAG pipelines.'}
        storage.discover(self.source, [entry])
        return entry

    def test_missing_or_empty_description_keeps_ai_candidate_across_refreshes(self):
        entry = self.precise()
        for description in ('missing', None, '', '   '):
            with self.subTest(description=description):
                self.expire(storage.rows()[0])

                def probe(row, *arguments, **kwargs):
                    metadata = self.metadata(row['entry'], views=8800)
                    metadata.pop('description')
                    if description != 'missing':
                        metadata['description'] = description
                    return metadata

                result, asked = self.refresh(probe)
                self.assertEqual(1, asked.call_count)
                ranked = ranking.score(result['rows'], of(self.settings_value), self.scope)
                self.assertEqual([entry['video_id']], [row['video_id'] for row in ranked])
                stored = storage.rows()[0]
                self.assertEqual(entry['description'], stored['entry']['description'])
                self.assertEqual('candidate', stored['state'])
                self.assertEqual(8800, stored['views'])

    def test_date_only_metadata_cannot_downgrade_a_precise_published_time(self):
        entry = self.precise()
        row = storage.rows()[0]
        metadata = self.metadata(entry, views=8800)
        metadata.pop('timestamp')
        metadata['upload_date'] = entry['published'][:10].replace('-', '')
        refreshed = candidates.calibrated(row, metadata)
        self.assertEqual(entry['published'], refreshed['published'])
        self.assertEqual(entry['description'], refreshed['description'])
        self.assertTrue(storage.refresh_metadata(row, refreshed))
        self.assertEqual(entry['published'], storage.rows()[0]['entry']['published'])

    def test_approximate_entries_still_receive_real_date_and_description(self):
        self.pending()
        row = storage.rows()[0]
        metadata = self.metadata(row['entry'], views=8800, hours=24)
        refreshed = candidates.calibrated(row, metadata)
        self.assertNotIn('approximate', refreshed)
        self.assertNotEqual(row['entry']['published'], refreshed['published'])
        self.assertEqual(metadata['description'], refreshed['description'])
        self.assertEqual(8800, refreshed['views'])

    def test_metadata_write_lock_is_not_a_video_or_channel_failure(self):
        self.pending(1)
        self.pending(2)
        with patch.object(storage, 'refresh_metadata', side_effect=maintenance.LockBusy('本机索引忙')):
            result, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        self.assertEqual(2, asked.call_count)
        self.assertEqual(2, len(result['local_errors']))
        self.assertEqual({}, result['source_errors'])
        self.assertEqual([], result['errors'])
        self.assertEqual(0, result['stats']['failed'])
        self.assertEqual({'00000000001', '00000000002'}, result['unfinished_ids'])
        for row in storage.rows():
            self.assertEqual(('candidate', 0, '', 0),
                             (row['state'], row['attempts'], row['last_error'], row['retry_at']))


class LocalFaultRound(IncrementalFixture):
    def setUp(self):
        super().setUp()
        with reg.db() as connection:
            connection.execute('''UPDATE subscriptions SET last_success=?,last_error='旧频道错误',
                failures=2,retry_at=?,last_notice='原有提醒' WHERE id=?''',
                               ('2026-10-29T01:00:00+00:00', time.time() - 60, self.source['id']))
        source = reg.subscription('youtube', self.source['id'])
        self.before = {key: source[key] for key in ('last_success', 'last_error', 'failures', 'retry_at')}

    def collect(self, listing=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(login, 'readiness', return_value={
                'ready': True, 'message': '', 'browser': 'chrome'}))
            stack.enter_context(patch.object(collector, 'DISCOVERY_SPACING', 0))
            stack.enter_context(patch.object(discovery, 'fetch', side_effect=listing or (lambda *arguments: [])))
            stack.enter_context(patch.object(media, 'probe', side_effect=lambda row, *arguments, **kwargs:
                                            self.metadata(row['entry'])))
            stack.enter_context(patch.object(notes, 'available', return_value=(True, '')))
            stack.enter_context(patch.object(notes, 'build', side_effect=self.note))
            stack.enter_context(patch.object(media, 'obtain', return_value=self.complete_transcript()))
            self.stage = stack.enter_context(patch.object(collector, '_stage'))
            self.retry = stack.enter_context(patch.object(collector, 'note_retry_targets'))
            return collector.collect(self.scope)

    def assert_local_only(self, summary, preserved_notice=False):
        source = reg.subscription('youtube', self.source['id'])
        self.assertEqual(self.before, {key: source[key] for key in self.before})
        if preserved_notice:
            self.assertEqual('原有提醒', source['last_notice'])
        self.assertEqual([], summary['failed_ids'])
        self.assertEqual([], summary['succeeded_ids'])
        self.assertEqual([], summary['errors'])
        self.assertEqual([], self.retry.call_args.args[1])
        self.assertEqual(1, summary['deferred'])
        self.assertIn('本机', summary['message'])
        self.assertIn('下一轮重算', self.stage.call_args.args[2])
        self.assertEqual('error', self.stage.call_args.args[1])
        self.assertEqual(summary, reg.latest_run('youtube')['summary'])
        for row in storage.rows():
            self.assertNotEqual('failed', row['state'])
            self.assertEqual((0, '', 0), (row['attempts'], row['last_error'], row['retry_at']))

    def test_discovery_storage_lock_does_not_penalize_subscription(self):
        with patch.object(storage, 'discover', side_effect=maintenance.LockBusy('发现写入忙')):
            result = self.collect(lambda *arguments: [self.entry()])
        self.assert_local_only(result, preserved_notice=True)
        self.assertEqual(1, result['checked'])

    def test_metadata_storage_busy_is_not_promoted_to_subscription(self):
        self.pending(1)
        self.pending(2)
        with patch.object(storage, 'refresh_metadata', side_effect=sqlite3.OperationalError('database is locked')):
            result = self.collect()
        self.assert_local_only(result)
        self.assertEqual(0, result['metadata']['failed'])
        self.assertEqual(0, result['processed'])

    def test_metadata_local_fault_does_not_block_a_different_healthy_channel(self):
        self.pending()
        second = self.channel(2)
        storage.discover(second, [self.entry(2)])
        with patch.object(storage, 'refresh_metadata', side_effect=maintenance.LockBusy('元数据写入忙')):
            result = self.collect()
        self.assertEqual([str(second['id'])], result['succeeded_ids'])
        self.assertEqual([], result['failed_ids'])
        self.assertEqual([], result['errors'])
        self.assertEqual(1, result['deferred'])
        source = reg.subscription('youtube', self.source['id'])
        self.assertEqual(self.before, {key: source[key] for key in self.before})
        self.assertEqual('ready', next(row for row in storage.rows() if row['video_id'] == '00000000002')['state'])

    def test_processing_storage_errors_do_not_increment_video_attempts(self):
        storage.discover(self.source, [self.entry()])
        original = storage.update

        def update(video_id, **values):
            if 'duration' in values:
                raise sqlite3.OperationalError('database is busy')
            return original(video_id, **values)

        with patch.object(storage, 'update', side_effect=update):
            result = self.collect()
        self.assert_local_only(result)

    def test_product_certification_disk_full_is_not_a_video_failure(self):
        storage.discover(self.source, [self.entry()])
        with patch.object(products, 'certify', side_effect=OSError(errno.ENOSPC, '磁盘已满')):
            result = self.collect()
        self.assert_local_only(result)
        self.assertEqual('processing', storage.rows()[0]['state'])

    def test_note_publication_lock_is_not_a_subscription_failure(self):
        storage.discover(self.source, [self.entry()])
        with patch.object(collector, 'commit_notes', side_effect=maintenance.LockBusy('发布写入忙')):
            result = self.collect()
        self.assert_local_only(result, preserved_notice=True)
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertFalse(storage.rows()[0]['committed'])

    def test_final_success_commit_uses_the_same_local_notice_path(self):
        storage.discover(self.source, [self.entry()])
        with patch.object(collector, 'commit_successes', side_effect=maintenance.LockBusy('最终写入忙')):
            result = self.collect()
        self.assert_local_only(result, preserved_notice=True)
        self.assertEqual('最终写入忙', result['local_error'])

    def test_batch_metadata_annotation_busy_still_leaves_round_history(self):
        self.pending()
        with patch.object(storage, 'defer_metadata', side_effect=sqlite3.OperationalError('database is locked')):
            result = self.collect()
        self.assert_local_only(result)

    def test_budget_state_write_busy_still_uses_local_fault_boundary(self):
        self.settings(rank_limit=2, max_videos=1)
        storage.discover(self.source, [self.entry(1), self.entry(2)])
        original = storage.update

        def update(video_id, **values):
            if values.get('state') == 'deferred':
                raise sqlite3.OperationalError('database is locked')
            return original(video_id, **values)

        with patch.object(storage, 'update', side_effect=update):
            result = self.collect()
        self.assert_local_only(result)
        self.assertEqual(1, result['processed'])

    def test_metadata_row_read_busy_still_records_the_round(self):
        storage.discover(self.source, [self.entry()])
        original = storage.rows
        calls = 0

        def rows(*arguments, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError('database is locked')
            return original(*arguments, **kwargs)

        with patch.object(storage, 'rows', side_effect=rows):
            result = self.collect()
        self.assert_local_only(result)

    def test_real_upstream_failure_keeps_existing_subscription_failure_path(self):
        def listing(*arguments):
            raise RuntimeError('远端频道读取失败')

        result = self.collect(listing)
        source = reg.subscription('youtube', self.source['id'])
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        self.assertEqual(self.before['failures'] + 1, source['failures'])
        self.assertGreater(source['retry_at'], time.time())
        self.assertNotIn('local_error', result)

    def test_real_metadata_failures_still_use_video_and_channel_error_ledgers(self):
        self.pending()
        with patch.object(storage, 'refresh_metadata', side_effect=RuntimeError('远端元数据不可用')):
            result = self.collect()
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        row = storage.rows()[0]
        self.assertEqual(('failed', 1), (row['state'], row['attempts']))
        self.assertGreater(row['retry_at'], time.time())
        self.assertNotIn('local_error', result)
