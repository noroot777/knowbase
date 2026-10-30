import sqlite3
import time
from contextlib import ExitStack
from unittest.mock import patch

from kb import maintenance
from kb.feeds import registry as reg
from kb.youtube import candidate_metadata as candidates
from kb.youtube import collector, discovery, login, media, notes, storage
from tests.test_youtube_incremental import IncrementalFixture


class MetadataOutcomes(IncrementalFixture):
    def assert_partition(self, result, not_reached, unfinished):
        self.assertEqual(set(not_reached), result['not_reached_ids'])
        self.assertEqual(set(unfinished), result['unfinished_ids'])
        self.assertEqual(set(not_reached) | set(unfinished), result['pending_ids'])
        self.assertFalse(result['not_reached_ids'] & result['unfinished_ids'])

    def test_only_clean_unattempted_candidates_receive_the_budget_exemption(self):
        for number in range(1, 5):
            self.pending(number)
        storage.update('00000000002', state='deferred', reason='待下轮', retry_at=time.time() + 300)
        storage.failed(storage.rows()[2], RuntimeError('旧字幕错误'))
        storage.update('00000000004', state='processing', reason='仍在取字幕')
        result, asked = self.refresh(deadline=time.monotonic() - 1)
        asked.assert_not_called()
        self.assert_partition(result, ['00000000001', '00000000002'], ['00000000003', '00000000004'])
        self.assertFalse(result['stopped'])

    def test_attempted_but_incomplete_metadata_is_not_unreached(self):
        entry = self.pending()
        result, asked = self.refresh(lambda row, *arguments, **kwargs: {
            **self.metadata(row['entry']), 'view_count': None})
        self.assertEqual(1, asked.call_count)
        self.assert_partition(result, [], [entry['video_id']])
        self.assertEqual([], result['errors'])
        self.assertEqual(1, storage.rows()[0]['metadata_pending'])
        repeated, asked = self.refresh()
        asked.assert_not_called()
        self.assert_partition(repeated, [], [entry['video_id']])

    def test_local_interruption_is_unfinished_without_channel_failure(self):
        entry = self.pending()
        result, _ = self.refresh(lambda *arguments, **kwargs: (_ for _ in ()).throw(
            media.Interrupted('单条元数据预算用尽')))
        self.assert_partition(result, [], [entry['video_id']])
        self.assertFalse(result['stopped'])
        self.assertEqual({}, result['source_errors'])
        repeated, asked = self.refresh()
        asked.assert_not_called()
        self.assert_partition(repeated, [], [entry['video_id']])
        self.assertEqual(1, storage.rows()[0]['metadata_pending'])

    def test_successful_refresh_clears_persistent_metadata_cooldown(self):
        self.pending()
        self.refresh(lambda row, *arguments, **kwargs: {**self.metadata(row['entry']), 'view_count': None})
        self.assertEqual(1, storage.rows()[0]['metadata_pending'])
        self.scope = type(self.scope)(force=True)
        result, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        self.assertEqual(1, asked.call_count)
        self.assert_partition(result, [], [])
        stored = storage.rows()[0]
        self.assertEqual(0, stored['metadata_pending'])
        self.assertEqual(0, stored['retry_at'])
        self.assertEqual('candidate', stored['state'])
        self.assertTrue(candidates.fresh(stored))

    def test_legacy_upgrade_preserves_uncertain_cooldowns_and_restores_clean_candidates(self):
        for number in range(1, 4):
            self.pending(number)
        storage.update('00000000001', state='deferred', reason=candidates.PENDING, retry_at=time.time() + 300)
        storage.update('00000000002', state='deferred', reason=candidates.PENDING)
        storage.failed(storage.rows()[2], RuntimeError('旧字幕错误'))
        with reg.db() as connection:
            connection.execute('ALTER TABLE youtube_videos DROP COLUMN metadata_pending')
        rows = storage.rows()
        self.assertEqual([1, 0, 0], [row['metadata_pending'] for row in rows])
        self.assertEqual(['deferred', 'candidate', 'failed'], [row['state'] for row in rows])
        self.assertEqual('旧字幕错误', rows[2]['last_error'])
        entry = candidates.calibrated(rows[0], self.metadata(rows[0]['entry']))
        self.assertTrue(storage.refresh_metadata(rows[0], entry))
        self.assertEqual(0, storage.rows()[0]['metadata_pending'])
        self.assertEqual(0, storage.rows()[0]['metadata_pending'])

    def test_budget_only_metadata_annotations_do_not_defer_healthy_candidates(self):
        self.pending(1)
        self.pending(2)
        result, _ = self.refresh(deadline=time.monotonic() - 1)
        self.assert_partition(result, ['00000000001', '00000000002'], [])
        self.assertTrue(all(row['state'] == 'candidate' for row in storage.rows()))
        self.assertTrue(all(not row['metadata_pending'] for row in storage.rows()))

    def test_stopped_queue_is_not_a_budget_only_exemption(self):
        entries = [self.pending(1), self.pending(2)]
        result, asked = self.refresh(stopped=lambda: True)
        asked.assert_not_called()
        self.assert_partition(result, [], [entry['video_id'] for entry in entries])
        self.assertTrue(result['stopped'])
        self.assertEqual({}, result['source_errors'])

    def test_request_refusal_keeps_the_remaining_queue_unfinished(self):
        from kb.youtube import throttle

        entries = [self.pending(1), self.pending(2)]
        result, asked = self.refresh(lambda *arguments, **kwargs: (_ for _ in ()).throw(
            throttle.NotAttempted('本轮不再发起请求')))
        self.assertEqual(1, asked.call_count)
        self.assert_partition(result, [], [entry['video_id'] for entry in entries])
        self.assertEqual({}, result['source_errors'])


class CompletionIntegrity(IncrementalFixture):
    def setUp(self):
        super().setUp()
        self.cancelled = False

    def standing(self, source=None):
        return reg.subscription('youtube', (source or self.source)['id'])

    def previous_error(self, source=None):
        source = source or self.source
        with reg.db() as connection:
            connection.execute('''UPDATE subscriptions SET last_notice='上轮提醒',last_success=?,last_error=?,failures=3,
                retry_at=? WHERE id=?''', ('2026-10-29T01:00:00+00:00', '旧字幕错误',
                                          time.time() - 60, source['id']))
        return {key: self.standing(source)[key]
                for key in ('last_success', 'last_error', 'failures', 'retry_at')}

    def assert_preserved(self, before, source=None):
        after = self.standing(source)
        self.assertEqual(before, {key: after[key] for key in before})

    def collect(self, listing=None, probe=None, metadata_seconds=120):
        def metadata(row, *arguments, **kwargs):
            return self.metadata(row['entry'])

        with ExitStack() as stack:
            stack.enter_context(patch.object(login, 'readiness', return_value={
                'ready': True, 'message': '', 'browser': 'chrome'}))
            stack.enter_context(patch.object(collector, 'DISCOVERY_SPACING', 0))
            stack.enter_context(patch.object(collector, 'stopped', side_effect=lambda: self.cancelled))
            stack.enter_context(patch.object(discovery, 'fetch', side_effect=listing or (lambda *arguments: [])))
            stack.enter_context(patch.object(candidates, 'RUN_SECONDS', metadata_seconds))
            stack.enter_context(patch.object(notes, 'available', return_value=(True, '')))
            stack.enter_context(patch.object(notes, 'build', side_effect=self.note))
            stack.enter_context(patch.object(media, 'obtain', side_effect=lambda *arguments: self.complete_transcript()))
            asked = stack.enter_context(patch.object(media, 'probe', side_effect=probe or metadata))
            self.stages = stack.enter_context(patch.object(collector, '_stage'))
            result = collector.collect(self.scope)
        return result, asked

    def test_failed_video_does_not_become_successful_when_its_cache_expires(self):
        entries = [self.entry(1, views=9000), self.entry(2)]
        storage.discover(self.source, entries)
        storage.failed(storage.rows()[0], RuntimeError('旧字幕错误'))
        before = self.previous_error()
        video_before = storage.rows()[0]
        flat = [{**entry, 'approximate': True, 'views': 0} for entry in entries]
        for expired in (False, True):
            with self.subTest(expired=expired):
                if expired:
                    self.expire(storage.rows()[0])
                result, asked = self.collect(lambda *arguments: flat)
                self.assertNotIn(entries[0]['video_id'], [call.args[0]['video_id']
                                                        for call in asked.call_args_list])
                self.assertEqual([], result['succeeded_ids'])
                self.assertEqual([], result['failed_ids'])
                self.assertEqual(1, result['deferred'])
                self.assert_preserved(before)
                failed = storage.rows()[0]
                self.assertEqual('failed', failed['state'])
                self.assertEqual(video_before['last_error'], failed['last_error'])
                self.assertEqual(video_before['retry_at'], failed['retry_at'])
        self.assertEqual('ready', storage.rows()[1]['state'])
        self.assertTrue(storage.rows()[1]['committed'])

    def test_current_video_failure_blocks_success_without_channel_backoff(self):
        bad = self.pending(1)
        storage.discover(self.source, [self.entry(2)])
        before = self.previous_error()

        def probe(row, *arguments, **kwargs):
            if row['video_id'] == bad['video_id']:
                raise media.ToolFailure('会员专享', 'This video is members-only', 'unknown')
            return self.metadata(row['entry'])

        result, _ = self.collect(probe=probe)
        self.assertEqual([], result['succeeded_ids'])
        self.assertEqual([], result['failed_ids'])
        self.assertEqual(1, result['metadata']['failed'])
        self.assert_preserved(before)
        self.assertEqual(['failed', 'ready'], [row['state'] for row in storage.rows()])

    def test_attempted_incomplete_metadata_does_not_reset_subscription_errors(self):
        self.pending(1)
        storage.discover(self.source, [self.entry(2)])
        before = self.previous_error()

        def probe(row, *arguments, **kwargs):
            return {**self.metadata(row['entry']), **({'view_count': None}
                    if row['video_id'] == '00000000001' else {})}

        for round_number in range(3):
            with self.subTest(round_number=round_number):
                if round_number == 2:
                    storage.update('00000000001', retry_at=0)
                result, asked = self.collect(probe=probe)
                self.assertEqual([], result['succeeded_ids'])
                self.assertEqual([], result['failed_ids'])
                self.assert_preserved(before)
                self.assertEqual('ready', storage.rows()[1]['state'])
                self.assertEqual(1, storage.rows()[0]['metadata_pending'])
                if round_number == 1:
                    asked.assert_not_called()

    def test_clean_unreached_tail_does_not_block_real_content_completion(self):
        self.settings(rank_limit=1, max_videos=1)
        storage.discover(self.source, [self.entry(1, views=9000)])
        self.pending(2)
        self.pending(3)
        result, asked = self.collect(metadata_seconds=0)
        self.assertEqual(['00000000001'], [call.args[0]['video_id'] for call in asked.call_args_list])
        self.assertEqual([str(self.source['id'])], result['succeeded_ids'])
        self.assertEqual(0, result['deferred'])
        self.assertEqual(2, result['metadata']['pending'])
        self.assertEqual(1, result['processed'])
        self.assertEqual(1, result['added'])
        self.assertTrue(storage.rows()[0]['committed'])

    def test_stopping_metadata_preserves_all_sources_even_without_selected_videos(self):
        second = self.channel(2)
        self.pending(1)
        self.pending(2, source=second)
        previous = [(source, self.previous_error(source)) for source in (self.source, second)]

        def stop(row, *arguments, **kwargs):
            self.cancelled = True
            raise media.Interrupted('用户停止')

        result, asked = self.collect(probe=stop)
        self.assertEqual(1, asked.call_count)
        self.assertTrue(result['stopped'])
        self.assertEqual([], result['succeeded_ids'])
        self.assertEqual([], result['failed_ids'])
        for source, before in previous:
            self.assert_preserved(before, source)

    def test_stopping_after_an_empty_listing_does_not_clear_source_history(self):
        before = self.previous_error()

        def listing(*arguments):
            self.cancelled = True
            return []

        result, asked = self.collect(listing)
        asked.assert_not_called()
        self.assertTrue(result['stopped'])
        self.assertEqual([], result['succeeded_ids'])
        self.assert_preserved(before)

    def prepared_sources(self):
        second = self.channel(2)
        storage.discover(self.source, [self.entry(1)])
        storage.discover(second, [self.entry(2)])
        return [(source, self.previous_error(source)) for source in (self.source, second)]

    def test_stopping_between_note_commits_does_not_leave_an_earlier_success(self):
        previous = self.prepared_sources()
        original = collector.commit_notes

        def commit(*arguments, **kwargs):
            result = original(*arguments, **kwargs)
            self.cancelled = True
            return result

        with patch.object(collector, 'commit_notes', side_effect=commit):
            result, _ = self.collect()
        self.assertTrue(result['stopped'])
        self.assertEqual([], result['succeeded_ids'])
        self.assertEqual(2, result['deferred'])
        for source, before in previous:
            self.assert_preserved(before, source)

    def test_stopping_inside_success_transaction_rolls_back_every_reset(self):
        previous = self.prepared_sources()
        original = collector._repointed

        def repointed(connection, source):
            if source['id'] == previous[-1][0]['id']:
                self.cancelled = True
            return original(connection, source)

        with patch.object(collector, '_repointed', side_effect=repointed):
            result, _ = self.collect()
        self.assertTrue(result['stopped'])
        self.assertEqual([], result['succeeded_ids'])
        self.assertEqual(2, result['deferred'])
        for source, before in previous:
            self.assert_preserved(before, source)

    def test_empty_success_list_never_takes_the_maintenance_lock(self):
        summary = {'stopped': False, 'succeeded_ids': []}
        with patch.object(collector, 'maintenance_lock', side_effect=AssertionError('空列表不应取锁')):
            collector.commit_successes([], summary)
        self.assertEqual([], summary['succeeded_ids'])

    def assert_finalization_failure(self, result, previous):
        identifiers = {str(source['id']) for source, before in previous}
        self.assertEqual([], result['succeeded_ids'])
        self.assertEqual(identifiers, set(result['failed_ids']))
        self.assertEqual(len(previous), len(result['errors']))
        self.assertEqual(result, reg.latest_run('youtube')['summary'])
        self.assertEqual('error', self.stages.call_args.args[1])
        for source, before in previous:
            after = self.standing(source)
            self.assertEqual(before['last_success'], after['last_success'])
            self.assertEqual(before['failures'] + 1, after['failures'])
            self.assertEqual('上轮提醒', after['last_notice'])
            self.assertGreater(after['retry_at'], time.time())

    def test_success_lock_failure_is_recorded_per_source_and_round(self):
        previous = self.prepared_sources()
        original = collector.maintenance_lock
        acquisitions = 0

        def lock():
            nonlocal acquisitions
            acquisitions += 1
            if acquisitions == len(previous) + 1:
                raise maintenance.LockBusy('索引正在跑')
            return original()

        with patch.object(collector, 'maintenance_lock', side_effect=lock), \
                patch.object(collector, 'note_retry_targets') as retry:
            result, _ = self.collect()
        self.assert_finalization_failure(result, previous)
        self.assertEqual(result['failed_ids'], retry.call_args.args[1])
        self.assertTrue(all('索引正在跑' in row['reason'] for row in result['errors']))

    def test_success_database_failure_keeps_history_and_retry_targets(self):
        previous = self.prepared_sources()
        with patch.object(collector, 'commit_successes', side_effect=sqlite3.OperationalError('database is locked')), \
                patch.object(collector, 'note_retry_targets') as retry:
            result, _ = self.collect()
        self.assert_finalization_failure(result, previous)
        self.assertEqual(result['failed_ids'], retry.call_args.args[1])

    def test_clean_cold_start_displays_unselected_videos_as_candidates(self):
        for number in range(2, 7):
            self.channel(number)
        self.settings(rank_limit=3, max_videos=3)

        def listing(source, *arguments):
            return [{**self.entry(source['id'] * 100 + position), 'approximate': True,
                     'description': '', 'views': 0, 'listing_position': position}
                    for position in range(8)]

        result, _ = self.collect(listing, metadata_seconds=0)
        self.assertEqual(3, result['processed'])
        self.assertEqual(48, result['ranked'])
        response = self.client.get('/api/intake/youtube/ranking')
        self.assertEqual(200, response.status_code)
        self.assertEqual({'ready': 3, 'candidate': 45}, response.json()['counts'])
        self.assertTrue(all(row['score_rank'] > 0 for row in response.json()['items']))
