import json
import time
from contextlib import ExitStack
from unittest.mock import patch

from kb.feeds import registry as reg
from kb.youtube import candidate_metadata as candidates
from kb.youtube import collector, media, products, ranking, storage
from kb.youtube.settings import of
from tests.test_youtube_incremental import IncrementalFixture


class MetadataSnapshots(IncrementalFixture):
    def set_state(self, number, state, **values):
        row = next(row for row in storage.rows() if row['video_id'] == f'{number:011d}')
        proof = {'gate_code': 'duration'} if state == 'excluded' else {}
        if state == 'ready':
            note = self.note(row)
            proof = {'note': json.dumps(note), 'validation': products.certify(
                row, {'duration': 600}, self.complete_transcript(), note)}
        storage.update(row['video_id'], state=state, **proof, **values)

    def test_finished_and_excluded_videos_never_consume_incremental_requests(self):
        for number, state in ((1, 'ready'), (2, 'excluded')):
            storage.discover(self.source, [self.entry(number)])
            self.set_state(number, state, score_rank=number)
            self.expire(next(row for row in storage.rows() if row['video_id'] == f'{number:011d}'))
        result, asked = self.refresh()
        asked.assert_not_called()
        self.assertEqual(0, result['stats']['pending'])
        self.assertEqual(set(), result['pending_sources'])
        self.assertEqual([1, 2], [row['score_rank'] for row in storage.rows()])

    def test_bulk_deferral_preserves_protected_ranks_and_existing_cause(self):
        states = ('ready', 'excluded', 'failed', 'deferred')
        for number, state in enumerate(states, 1):
            self.pending(number)
            self.set_state(number, state, score_rank=number,
                           reason='本轮数量预算已用完，留待下轮', retry_at=123)
        storage.defer_metadata([(row, candidates.PENDING, 456) for row in storage.rows()])
        rows = storage.rows()
        self.assertEqual([1, 2, 3, 0], [row['score_rank'] for row in rows])
        self.assertTrue(all(row['reason'] == '本轮数量预算已用完，留待下轮' for row in rows))
        self.assertEqual([123, 123, 123, 456], [row['retry_at'] for row in rows])

    def test_provisional_ranking_does_not_erase_a_real_deferral_cause(self):
        self.pending()
        storage.update('00000000001', state='deferred', reason='撞上限流，没有再发起新的请求')
        result, asked = self.refresh(deadline=time.monotonic() - 1)
        asked.assert_not_called()
        ranking.score(result['rows'], of(self.settings_value), self.scope)
        self.assertEqual('撞上限流，没有再发起新的请求', storage.rows()[0]['reason'])

    def test_all_fact_writers_stamp_precise_snapshots(self):
        for writer in ('discover', 'recalibrate', 'refresh_metadata'):
            with self.subTest(writer=writer):
                number = len(storage.rows()) + 1
                entry = self.entry(number)
                if writer == 'discover':
                    storage.discover(self.source, [entry])
                else:
                    self.pending(number)
                    if writer == 'recalibrate':
                        storage.recalibrate(entry['video_id'], entry)
                    else:
                        row = next(row for row in storage.rows() if row['video_id'] == entry['video_id'])
                        self.assertTrue(storage.refresh_metadata(row, entry))
                stored = next(row for row in storage.rows() if row['video_id'] == entry['video_id'])
                self.assertTrue(candidates.fresh(stored))
        result, asked = self.refresh()
        asked.assert_not_called()
        self.assertEqual(3, result['stats']['reused'])

    def test_force_remains_an_explicit_disk_cache_bypass(self):
        self.scope = type(self.scope)(force=True)
        self.pending()
        _, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        self.assertTrue(asked.call_args.kwargs['force'])

    def test_shared_failure_is_only_promoted_for_the_owner_without_any_healthy_candidate(self):
        second = self.channel(2)
        failed = self.pending(1)
        storage.discover(second, [failed])
        storage.discover(self.source, [self.entry(2)])
        result, _ = self.refresh(lambda *arguments, **kwargs: (_ for _ in ()).throw(
            media.ToolFailure('会员专享', 'This video is members-only', 'unknown')))
        self.assertEqual({second['id']}, result['pending_sources'])
        self.assertEqual({second['id']}, set(result['source_errors']))


class ChannelCompletion(IncrementalFixture):
    def collect(self, listing, probe=None, metadata_seconds=120):
        from kb.youtube import discovery, login

        with ExitStack() as stack:
            stack.enter_context(patch.object(login, 'readiness', return_value={
                'ready': True, 'message': '', 'browser': 'chrome'}))
            stack.enter_context(patch.object(collector, 'DISCOVERY_SPACING', 0))
            stack.enter_context(patch.object(discovery, 'fetch', side_effect=listing))
            stack.enter_context(patch.object(candidates, 'RUN_SECONDS', metadata_seconds))
            stack.enter_context(patch.object(collector, 'process', return_value={}))
            asked = stack.enter_context(patch.object(media, 'probe', side_effect=probe))
            stages = stack.enter_context(patch.object(collector, '_stage'))
            result = collector.collect(self.scope)
        return result, asked, stages

    def standing(self, source=None):
        return next(row for row in reg.subscriptions('youtube')
                    if row['id'] == (source or self.source)['id'])

    def test_cold_start_with_48_channels_ranks_all_candidates_and_advances_each_source(self):
        for number in range(2, 49):
            self.channel(number)
        def listing(source, *arguments):
            return [{**self.entry(source['id'] * 10 + position), 'approximate': True,
                     'description': '', 'views': 0, 'listing_position': position}
                    for position in range(8)]
        result, asked, _ = self.collect(listing, metadata_seconds=0)
        asked.assert_not_called()
        self.assertEqual(384, result['ranked'])
        self.assertEqual(384, result['metadata']['pending'])
        self.assertEqual(48, len(result['succeeded_ids']))
        self.assertEqual(0, result['deferred'])
        active = [row for row in reg.subscriptions('youtube') if row['enabled']]
        self.assertTrue(all(row['last_success'] and not row['last_error'] for row in active))
        response = self.client.get('/api/intake/youtube/ranking').json()
        self.assertEqual(384, response['total'])
        self.assertTrue(all(row['score_rank'] > 0 and row['approximate'] for row in response['items']))

    def test_one_unavailable_video_does_not_poison_a_healthy_channel(self):
        entries = [self.pending(1), self.pending(2)]
        def probe(row, *arguments, **kwargs):
            if row['video_id'] == entries[0]['video_id']:
                raise media.ToolFailure('会员专享', 'This video is members-only', 'unknown')
            return self.metadata(row['entry'])
        result, asked, _ = self.collect(lambda *arguments: entries, probe)
        self.assertEqual(2, asked.call_count)
        self.assertEqual(1, result['metadata']['failed'])
        self.assertEqual([], result['failed_ids'])
        self.assertEqual([str(self.source['id'])], result['succeeded_ids'])
        standing = self.standing()
        self.assertTrue(standing['last_success'])
        self.assertEqual(0, standing['failures'])
        self.assertEqual(0, standing['retry_at'])
        self.assertEqual('failed', storage.rows()[0]['state'])
        self.assertGreater(storage.rows()[0]['retry_at'], time.time())

    def test_an_entire_failed_candidate_batch_still_reports_channel_failure(self):
        entries = [self.pending(1), self.pending(2)]
        result, _, _ = self.collect(lambda *arguments: entries,
            lambda *arguments, **kwargs: (_ for _ in ()).throw(RuntimeError('视频详情不可用')))
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        self.assertEqual([], result['succeeded_ids'])
        self.assertGreater(self.standing()['retry_at'], time.time())

    def test_auth_failure_is_promoted_even_when_other_metadata_is_healthy(self):
        self.pending(1)
        storage.discover(self.source, [self.entry(2)])
        result, _, _ = self.collect(lambda *arguments: [],
            lambda *arguments, **kwargs: (_ for _ in ()).throw(
                media.ToolFailure('登录态失效', 'Sign in to confirm your age', 'not_logged_in')))
        self.assertEqual([str(self.source['id'])], result['failed_ids'])
        self.assertIn('not_logged_in', result['walls'])
        self.assertGreater(self.standing()['failures'], 0)

    def test_clean_sync_keeps_warning_fields_empty_and_shows_statistics_separately(self):
        result, _, stages = self.collect(lambda *arguments: [self.entry()])
        self.assertEqual('', result['message'])
        self.assertEqual('', self.standing()['last_notice'])
        self.assertIn('timings', result)
        self.assertIn('列表发现', stages.call_args.args[2])
        self.assertIn('失败 0 条', stages.call_args.args[2])

    def test_explicit_date_budget_keeps_the_original_detailed_discovery_ceiling(self):
        self.assertEqual(900, collector.discovery_budget(48))
        self.assertEqual(1800, collector.discovery_budget(48, 90, collector.RANGE_DISCOVERY_CEILING))
