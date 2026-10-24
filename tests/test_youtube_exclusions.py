import time
from unittest.mock import patch

from kb import config
from kb.collectors import FetchScope
from kb.feeds import exclusion, registry as reg
from kb.youtube import collector, discovery, media, notes, products, ranking, storage
from tests.youtube_fixture import YouTubeFixture


class YouTubeExclusions(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()
        self.metadata = {'duration': 600}

    def remove(self, entry=None):
        entry = entry or self.entry_value
        with patch.object(config, 'DB_PATH', self.root / 'kb.db'), \
                patch.object(config, 'VAULT_ROOT', self.root / 'vault'):
            exclusion.drop_item('youtube', {'id': reg.item_key('youtube', entry['url']), 'link': entry['url']})

    def collect(self, force=False, probe_action=None, obtain_action=None):
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=self.metadata, side_effect=probe_action) as probe, \
                patch.object(media, 'obtain', return_value=self.complete_transcript(), side_effect=obtain_action) as obtain, \
                patch.object(notes, 'build', side_effect=self.note) as build:
            result = collector.collect(FetchScope(force=force))
        return result, probe, obtain, build

    def publish(self):
        result, _, _, _ = self.collect()
        self.assertEqual(1, result['added'])

    def assert_no_work(self, result, probe, obtain, build):
        self.assertEqual((0, 0, 0), (result['ranked'], result['admitted'], result['processed']))
        probe.assert_not_called()
        obtain.assert_not_called()
        build.assert_not_called()
        self.assertEqual(0, reg.items('youtube')['total'])

    def test_removed_legacy_product_never_reprocesses_even_with_force(self):
        self.publish()
        self.remove()
        storage.update(self.entry_value['video_id'], validation='')
        for force in (False, True):
            with self.subTest(force=force):
                self.assert_no_work(*self.collect(force=force))
                row = storage.rows()[0]
                self.assertTrue(row['excluded'])
                self.assertTrue(row['repair_pending'])
                self.assertEqual((0, 0), (row['heat_rank'], row['value_rank']))
                self.assertEqual([], storage.ready(self.source))

    def test_permanent_removal_is_independent_of_processing_state_and_retry(self):
        self.publish()
        self.remove()
        for state in ('candidate', 'filtered', 'deferred', 'processing', 'failed', 'ready', 'excluded'):
            with self.subTest(state=state):
                storage.update(self.entry_value['video_id'], state=state, gate_code='replay',
                               last_error='旧处理错误', retry_at=time.time() + 3600)
                result, probe, obtain, build = self.collect(force=True)
                self.assert_no_work(result, probe, obtain, build)
                self.assertIn(str(self.source['id']), result['succeeded_ids'])
                self.assertEqual([], result['errors'])

    def test_new_discovery_cannot_recreate_a_removed_identity_for_any_owner(self):
        self.remove()
        alias = self.channel(2)
        for source in (self.source, alias):
            storage.discover(source, [self.entry_value])
        self.assertEqual([], storage.rows())
        self.assert_no_work(*self.collect(force=True))
        with reg.db() as connection:
            self.assertEqual(1, connection.execute('SELECT COUNT(*) FROM feed_item_exclusions').fetchone()[0])

    def test_removed_video_has_no_rank_and_does_not_change_channel_baseline(self):
        removed = self.entry(views=1000000000)
        ordinary = self.entry(2, views=10000)
        storage.discover(self.source, [removed, ordinary])
        self.remove(removed)
        rows = storage.rows()
        policy = self.settings()['youtube']
        now = ranking.datetime.now(ranking.timezone.utc)
        expected = ranking.score([row for row in rows if not row['excluded']], policy, FetchScope(), now=now)
        actual = ranking.score(rows, policy, FetchScope(force=True), now=now)
        self.assertEqual([ordinary['video_id']], [row['video_id'] for row in actual])
        self.assertEqual((expected[0]['heat'], expected[0]['value']), (actual[0]['heat'], actual[0]['value']))

    def test_selection_itself_rejects_removed_rows_even_on_force(self):
        row = {'excluded': True, 'state': 'candidate', 'retry_at': 0, 'heat_rank': 1, 'value_rank': 1}
        for force in (False, True):
            self.assertEqual([], ranking.select([row], self.settings()['youtube'], force=force))

    def test_removal_after_ranking_is_rechecked_before_metadata(self):
        storage.discover(self.source, [self.entry_value])
        settings = self.settings()
        ranked = ranking.score(storage.rows(), settings['youtube'], FetchScope())
        self.assertFalse(ranked[0]['excluded'])
        self.remove()
        summary = {'stopped': False, 'message': '', 'processed': 0}
        with patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe') as probe, patch.object(media, 'obtain') as obtain, \
                patch.object(notes, 'build') as build:
            failures = collector.process(ranked, settings, FetchScope(force=True), summary, time.monotonic() + 30)
        self.assertEqual({}, failures)
        self.assertEqual((0, 0), (summary['processed'], summary['admitted']))
        probe.assert_not_called()
        obtain.assert_not_called()
        build.assert_not_called()

    def test_removal_during_metadata_prevents_media_and_model(self):
        def probe_action(*arguments):
            self.remove()
            return self.metadata

        result, probe, obtain, build = self.collect(probe_action=probe_action)
        probe.assert_called_once()
        obtain.assert_not_called()
        build.assert_not_called()
        self.assertEqual((0, 0), (result['processed'], result['admitted']))

    def test_removal_during_media_prevents_subsequent_model_request(self):
        def obtain_action(*arguments):
            self.remove()
            return self.complete_transcript()

        result, probe, obtain, build = self.collect(obtain_action=obtain_action)
        probe.assert_called_once()
        obtain.assert_called_once()
        build.assert_not_called()
        self.assertEqual((0, 1), (result['processed'], result['admitted']))
        self.assertEqual(0, reg.items('youtube')['total'])

    def test_publish_rechecks_removal_against_a_stale_ready_snapshot(self):
        self.publish()
        with storage.db() as connection:
            connection.execute('DELETE FROM subscription_items WHERE subscription_id=?', (self.source['id'],))
        row = storage.ready(self.source)[0]
        self.remove()
        with reg.db() as connection, patch.object(reg, 'put_items') as put_items:
            self.assertEqual(0, products.publish(connection, self.source, row))
        put_items.assert_not_called()
        self.assertEqual(0, reg.items('youtube')['total'])
