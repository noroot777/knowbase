import json
from unittest.mock import patch

from kb import config
from kb.collectors import FetchScope
from kb.feeds import exclusion, registry as reg, sync
from kb.youtube import collector, discovery, media, notes, products, storage
from tests.youtube_fixture import YouTubeFixture


class YouTubeConsumers(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()

    def publish(self, source=None, entry=None):
        source, entry = source or self.source, entry or self.entry_value
        storage.discover(source, [entry])
        row = storage.rows([source['id']])[0]
        note = self.note(row)
        storage.update(entry['video_id'], state='ready', note=json.dumps(note),
                       validation=products.certify(row, {'duration': 600}, self.complete_transcript(), note))
        collector.commit_notes(source, storage.ready(source), {'added': 0, 'message': ''})
        return note

    def collect(self):
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe') as probe, patch.object(media, 'obtain') as obtain, \
                patch.object(notes, 'build') as build:
            result = collector.collect(FetchScope(force=True))
        probe.assert_not_called()
        obtain.assert_not_called()
        build.assert_not_called()
        return result

    def test_current_certified_archived_body_stays_readable(self):
        note = self.publish()
        reg.delete_subscription('youtube', self.source['id'])
        self.assertEqual(note['body'], reg.items('youtube')['items'][0]['body'])
        sync.export('youtube')
        self.assertIn(note['body'], next((self.root / 'vault').rglob('*.md')).read_text())

    def test_repoint_and_back_resets_owners_before_any_discovery(self):
        self.publish()
        endpoint = f"/api/intake/youtube/sources/{self.source['id']}"
        for url in ('https://www.youtube.com/@different', self.source['url']):
            response = self.client.patch(endpoint, json={'url': url})
            self.assertEqual(200, response.status_code)
            with reg.db() as connection:
                self.assertEqual(0, connection.execute('SELECT count(*) FROM youtube_video_sources').fetchone()[0])
        self.assertEqual(0, reg.items('youtube', self.source['id'])['total'])
        result = self.collect()
        self.assertEqual((0, 0), (result['processed'], result['added']))
        self.assertEqual(1, reg.items('youtube', self.source['id'])['total'])

    def test_missing_membership_repairs_even_with_stale_committed_flag(self):
        self.publish()
        with reg.db() as connection:
            connection.execute('DELETE FROM subscription_items WHERE subscription_id=?', (self.source['id'],))
            self.assertEqual(0, connection.execute('SELECT committed FROM youtube_video_sources').fetchone()[0])
            connection.execute('UPDATE youtube_video_sources SET committed=1')
        self.assertEqual(1, len(storage.ready(self.source)))
        self.collect()
        self.assertEqual(1, reg.items('youtube', self.source['id'])['total'])

    def test_permanent_exclusion_does_not_repair_a_missing_membership(self):
        self.publish()
        key = reg.item_key('youtube', self.entry_value['url'])
        with patch.object(config, 'DB_PATH', self.root / 'kb.db'), \
                patch.object(config, 'VAULT_ROOT', self.root / 'vault'):
            exclusion.drop_item('youtube', {'id': key, 'link': self.entry_value['url']})
        self.assertEqual([], storage.ready(self.source))
        self.collect()
        self.assertEqual(0, reg.items('youtube')['total'])
