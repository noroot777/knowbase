import json
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.youtube import collector, discovery, media, notes, products, storage
from tests.youtube_fixture import YouTubeFixture


class YouTubeProducts(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()

    def run_collect(self, available=True, transcript=None):
        with patch.object(discovery, 'fetch', return_value=[]), \
                patch.object(notes, 'available', return_value=(available, '' if available else '缺模型')), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=transcript or self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=self.note) as build:
            result = collector.collect(FetchScope(force=True))
        return result, build.call_count

    def test_proof_binds_input_policy_and_final_body_at_publication(self):
        storage.discover(self.source, [self.entry_value])
        self.run_collect()
        row = storage.rows()[0]
        self.assertTrue(products.valid(row))
        damaged = json.loads(row['note']) | {'body': '被替换成不完整正文。'}
        storage.update(row['video_id'], note=json.dumps(damaged))
        self.assertEqual([], storage.ready(self.source))
        with self.assertRaises(ValueError):
            collector.commit_notes(self.source, [row], {'added': 0, 'message': ''})
        altered = json.loads(row['validation'])
        altered['input']['duration'] = 3000
        self.assertFalse(products.valid({**row, 'validation': json.dumps(altered)}))
        altered = json.loads(row['validation'])
        altered['version'] = 'obsolete'
        self.assertFalse(products.valid({**row, 'validation': json.dumps(altered)}))
