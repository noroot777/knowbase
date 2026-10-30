import json
import time
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import candidate_metadata as candidates
from kb.youtube import collector, discovery, login, media, notes, ranking, storage, throttle
from kb.youtube.settings import of
from tests.youtube_fixture import YouTubeFixture


class IncrementalFixture(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.scope = FetchScope()
        self.settings_value = {'youtube': {}}

    def pending(self, number=1, source=None, position=0):
        entry = {**self.entry(number), 'approximate': True, 'views': 0,
                 'description': '', 'listing_position': position}
        storage.discover(source or self.source, [entry])
        return entry

    def metadata(self, entry, views=3210, hours=1):
        return {'id': entry['video_id'], 'title': entry['title'], 'view_count': views,
                'timestamp': (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp(),
                'description': 'AI agent coding tutorial', 'duration': 600}

    def refresh(self, probe=None, deadline=None, stopped=lambda: False):
        with throttle.opened(), patch.object(media, 'probe', side_effect=probe) as asked:
            result = candidates.refresh(storage.rows(), self.settings_value, self.scope,
                                        deadline if deadline is not None else time.monotonic() + 30,
                                        stopped)
        return result, asked

    def expire(self, row):
        entry = {**row['entry'], 'metadata_checked_at': time.time() - candidates.TTL_SECONDS - 1}
        with reg.db() as connection:
            connection.execute('UPDATE youtube_videos SET entry=? WHERE video_id=?',
                               (json.dumps(entry, ensure_ascii=False), row['video_id']))

    def exclude(self, entry):
        with reg.db() as connection:
            connection.execute('INSERT INTO feed_item_exclusions(key,kind,url,created_at) VALUES(?,?,?,?)',
                               (reg.item_key('youtube', entry['url']), 'youtube', entry['url'], reg.now()))


class IncrementalMetadata(IncrementalFixture):
    def test_precise_cache_survives_repeated_flat_discovery_without_network_work(self):
        entry = self.entry(1, views=9876)
        storage.discover(self.source, [entry])
        before = storage.rows()[0]['entry']
        self.pending()
        self.assertEqual(before, storage.rows()[0]['entry'])
        result, asked = self.refresh()
        asked.assert_not_called()
        self.assertEqual(1, result['stats']['reused'])
        self.assertEqual(9876, result['rows'][0]['views'])
        self.assertEqual(before['metadata_checked_at'], result['rows'][0]['entry']['metadata_checked_at'])

    def test_only_new_video_is_probed_and_unknown_values_are_calibrated_before_scoring(self):
        storage.discover(self.source, [self.entry(1, views=900)])
        new = self.pending(2)
        result, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        self.assertEqual([new['video_id']], [call.args[0]['video_id'] for call in asked.call_args_list])
        self.assertEqual((1, 1, 0), tuple(result['stats'][key] for key in ('reused', 'refreshed', 'pending')))
        hydrated = next(row for row in result['rows'] if row['video_id'] == new['video_id'])
        self.assertNotIn('approximate', hydrated['entry'])
        self.assertEqual(3210, hydrated['views'])
        self.assertTrue(candidates.fresh(hydrated))
        ranked = ranking.score(result['rows'], of(self.settings_value), self.scope)
        self.assertEqual(2, len(ranked))
        self.assertTrue(all(not row['entry'].get('approximate') for row in ranked))

    def test_expired_cache_refreshes_real_view_count_including_zero(self):
        storage.discover(self.source, [self.entry(1, views=900)])
        self.expire(storage.rows()[0])
        result, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry'], views=0))
        self.assertEqual(1, asked.call_count)
        self.assertEqual(0, result['rows'][0]['views'])
        self.assertEqual(1, result['stats']['refreshed'])

    def test_budget_exhaustion_does_not_rank_guessed_or_expired_values(self):
        self.pending(1)
        storage.discover(self.source, [self.entry(2)])
        self.expire(next(row for row in storage.rows() if row['video_id'] == '00000000002'))
        result, asked = self.refresh(deadline=time.monotonic() - 1)
        asked.assert_not_called()
        self.assertEqual([], result['rows'])
        self.assertEqual(2, result['stats']['pending'])
        self.assertEqual({self.source['id']}, result['pending_sources'])
        self.assertTrue(all(row['score_rank'] == 0 for row in storage.rows()))
        self.assertTrue(all(row['state'] == 'deferred' for row in storage.rows()))

    def test_large_pending_batch_does_not_open_a_database_for_every_video(self):
        storage.discover(self.source, [{**self.entry(number), 'approximate': True}
                                      for number in range(1, 31)])
        with patch.object(storage, 'db', wraps=storage.db) as opened:
            result, asked = self.refresh(deadline=time.monotonic() - 1)
        asked.assert_not_called()
        self.assertEqual(30, result['stats']['pending'])
        self.assertLessEqual(opened.call_count, 3)

    def test_missing_date_or_view_count_remains_retryable_and_unranked(self):
        for invalid in ({'timestamp': None}, {'view_count': None}, {'view_count': True},
                        {'view_count': float('nan')}, {'view_count': -1}, {'view_count': 2 ** 63}):
            with self.subTest(invalid=invalid):
                number = len(storage.rows()) + 1
                entry = self.pending(number)
                result, asked = self.refresh(
                    lambda row, *arguments, **kwargs: {**self.metadata(row['entry']), **invalid})
                self.assertEqual(1, asked.call_count)
                self.assertEqual([], result['rows'])
                stored = next(row for row in storage.rows() if row['video_id'] == entry['video_id'])
                self.assertTrue(stored['entry']['approximate'])
                self.assertGreater(stored['retry_at'], time.time())
                self.assertEqual(0, stored['score_rank'])

    def test_facts_outside_window_are_rejected_after_calibration(self):
        self.pending()
        result, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry'], hours=24 * 20))
        self.assertEqual(1, asked.call_count)
        self.assertEqual([], ranking.score(result['rows'], of(self.settings_value), self.scope))
        self.assertEqual('filtered', storage.rows()[0]['state'])
        self.assertNotIn('approximate', storage.rows()[0]['entry'])

    def test_missing_description_is_filled_before_ai_eligibility_check(self):
        entry = {**self.pending(), 'title': 'A new workflow'}
        storage.discover(self.source, [entry])
        result, _ = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        self.assertEqual(1, len(ranking.score(result['rows'], of(self.settings_value), self.scope)))

    def test_metadata_requests_rotate_across_channels_before_older_list_positions(self):
        second = self.channel(2)
        self.pending(1, position=0)
        self.pending(2, position=1)
        self.pending(3, source=second, position=0)
        self.pending(4, source=second, position=1)
        _, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        self.assertEqual(['00000000001', '00000000003', '00000000002', '00000000004'],
                         [call.args[0]['video_id'] for call in asked.call_args_list])

    def test_per_video_deadline_is_capped_by_remaining_global_budget(self):
        self.pending()
        global_deadline = time.monotonic() + 5
        _, asked = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']),
                                deadline=global_deadline)
        self.assertEqual(global_deadline, asked.call_args.args[2])
        self.assertTrue(asked.call_args.kwargs['force'])

    def test_stop_does_not_start_metadata_requests(self):
        self.pending()
        result, asked = self.refresh(stopped=lambda: True)
        asked.assert_not_called()
        self.assertEqual(1, result['stats']['pending'])
        self.assertEqual([], result['errors'])

    def test_rate_limit_stops_further_probes_without_failing_unattempted_rows(self):
        self.pending(1)
        self.pending(2)
        failure = media.ToolFailure('too many requests', 'HTTP Error 429: Too Many Requests', 'rate_limited')
        result, asked = self.refresh(lambda *arguments, **kwargs: (_ for _ in ()).throw(failure))
        self.assertEqual(1, asked.call_count)
        self.assertEqual(1, result['stats']['failed'])
        first, second = storage.rows()
        self.assertEqual('failed', first['state'])
        self.assertEqual('deferred', second['state'])
        self.assertEqual(0, second['attempts'])
        self.assertEqual(0, second['retry_at'])

    def test_local_timeout_does_not_mark_a_video_as_failed(self):
        self.pending()
        result, asked = self.refresh(lambda *arguments, **kwargs: (_ for _ in ()).throw(media.Interrupted('预算用尽')))
        self.assertEqual(1, asked.call_count)
        self.assertEqual([], result['errors'])
        self.assertEqual('deferred', storage.rows()[0]['state'])
        self.assertEqual(0, storage.rows()[0]['attempts'])
        self.assertGreater(storage.rows()[0]['retry_at'], time.time())

    def test_permanent_exclusion_before_probe_prevents_any_request(self):
        entry = self.pending()
        self.exclude(entry)
        result, asked = self.refresh()
        asked.assert_not_called()
        self.assertEqual([], result['rows'])
        self.assertEqual(0, result['stats']['pending'])

    def test_removal_during_probe_cannot_refresh_or_rank_the_video(self):
        entry = self.pending()
        def removed(row, *arguments, **kwargs):
            self.exclude(entry)
            return self.metadata(row['entry'])
        result, asked = self.refresh(removed)
        self.assertEqual(1, asked.call_count)
        self.assertEqual([], result['rows'])
        self.assertEqual(0, result['stats']['pending'])
        self.assertTrue(storage.rows()[0]['entry']['approximate'])

    def test_refresh_cannot_overwrite_a_newer_concurrent_snapshot(self):
        self.pending()
        row = storage.rows()[0]
        newer = {**self.entry(1), 'views': 9999}
        storage.discover(self.source, [newer])
        self.assertFalse(storage.refresh_metadata(row, {**self.entry(1), 'views': 2}))
        self.assertEqual(9999, storage.rows()[0]['views'])

    def test_repointed_source_cannot_receive_old_metadata(self):
        self.pending()
        row = storage.rows()[0]
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET url=? WHERE id=?',
                               ('https://www.youtube.com/@changed', self.source['id']))
        self.assertFalse(storage.refresh_metadata(row, {**self.entry(1), 'views': 2}))


class IncrementalCollector(IncrementalFixture):
    def collect(self, listing, probe=None):
        with ExitStack() as stack:
            stack.enter_context(patch.object(login, 'readiness', return_value={'ready': True, 'message': ''}))
            stack.enter_context(patch.object(collector, 'DISCOVERY_SPACING', 0))
            stack.enter_context(patch.object(discovery, 'fetch', side_effect=listing))
            asked = stack.enter_context(patch.object(media, 'probe', side_effect=probe))
            stack.enter_context(patch.object(notes, 'available', return_value=(False, '未配置内容模型')))
            result = collector.collect(self.scope)
        return result, asked

    def test_all_channel_lists_finish_before_first_metadata_request(self):
        second = self.channel(2)
        events = []
        def listing(source, *arguments):
            events.append(('list', source['id']))
            return [{**self.entry(source['id']), 'approximate': True, 'views': 0}]
        def probe(row, *arguments, **kwargs):
            events.append(('metadata', row['video_id']))
            return self.metadata(row['entry'])
        result, asked = self.collect(listing, probe)
        self.assertEqual([('list', self.source['id']), ('list', second['id'])], events[:2])
        self.assertEqual(2, asked.call_count)
        self.assertEqual(2, result['checked'])
        self.assertEqual(2, result['ranked'])
        self.assertEqual(2, result['metadata']['refreshed'])
        self.assertEqual({'discovery_seconds', 'metadata_seconds', 'processing_seconds'}, set(result['timings']))

    def test_warm_round_performs_no_repeated_metadata_queries(self):
        entry = self.pending()
        listed = lambda *arguments: [{**entry}]
        first, first_asked = self.collect(listed, lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        second, second_asked = self.collect(listed)
        self.assertEqual(1, first_asked.call_count)
        second_asked.assert_not_called()
        self.assertEqual((1, 0), (first['metadata']['refreshed'], second['metadata']['refreshed']))
        self.assertEqual(1, second['metadata']['reused'])

    def test_expired_metadata_budget_leaves_source_incomplete_without_ranking_zero_views(self):
        entry = self.pending()
        with patch.object(candidates, 'RUN_SECONDS', 0):
            result, asked = self.collect(lambda *arguments: [entry])
        asked.assert_not_called()
        self.assertEqual(0, result['ranked'])
        self.assertEqual(1, result['metadata']['pending'])
        self.assertEqual([], result['succeeded_ids'])
        self.assertGreater(result['deferred'], 0)
        self.assertIn('暂不排名', result['message'])

    def test_processing_reuses_metadata_already_fetched_for_this_round(self):
        self.pending()
        prepared, _ = self.refresh(lambda row, *arguments, **kwargs: self.metadata(row['entry']))
        ranked = ranking.score(prepared['rows'], of(self.settings_value), self.scope)
        summary = {'stopped': False, 'message': '', 'processed': 0}
        with throttle.opened(), patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe') as duplicate, \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=self.note), \
                patch.object(collector.products, 'certify', return_value='fixture'):
            collector.process(ranked, self.settings_value, self.scope, summary, time.monotonic() + 10)
        duplicate.assert_not_called()
        self.assertEqual(1, summary['processed'])

    def test_ranking_api_keeps_pending_dates_and_views_marked_unknown(self):
        entry = self.pending()
        with patch.object(candidates, 'RUN_SECONDS', 0):
            self.collect(lambda *arguments: [entry])
        response = self.client.get('/api/intake/youtube/ranking')
        self.assertEqual(200, response.status_code)
        item = response.json()['items'][0]
        self.assertTrue(item['approximate'])
        self.assertEqual(0, item['score_rank'])
        self.assertEqual('deferred', item['state'])
