import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import media, notes
from tests.youtube_fixture import YouTubeFixture


class YouTubeStageCache(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': self.entry()['video_id'], 'entry': self.entry(), 'channel': '测试频道'}
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.payload = {'title': '具体中文标题', 'summary': '保留事实与限制的中文概述。', 'body': '具体中文内容。'}
        self.blocks = [f'[{position:02d}:00:00] 中文原文内容{position}。' for position in range(16)]
        self.transcript = self.complete_transcript()

    def build(self, reply):
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes.transcripts, 'blocks', return_value=self.blocks), \
                patch.object(notes, 'call', side_effect=reply) as call:
            product = notes.build(self.row, self.transcript, time.monotonic() + 30, lambda: False)
        return product, call

    def test_failed_reduction_retries_with_validated_maps_and_translations(self):
        calls = []

        def failed(prompt, *arguments):
            calls.append(prompt)
            block = prompt.split('\n\n', 1)[1]
            return block if len(block) > 10000 else '中文要点。' * 200

        with self.assertRaisesRegex(RuntimeError, '未能压缩'):
            self.build(failed)
        self.assertEqual(17, len(calls))
        path = media.cache_dir(self.row['video_id']) / 'notes-cache.json'
        cache = media.read_json(path)
        self.assertEqual(32, len(cache))
        failed_block = calls[-1].split('\n\n', 1)[1]
        key = notes.signature('reduce', self.selected, failed_block)
        self.assertNotIn(key, cache)
        cache[key] = failed_block
        media.save_json(path, cache)

        def recovered(prompt, *arguments):
            return json.dumps(self.payload) if '只输出 JSON' in prompt else '已压缩的中文事实与限制。'

        product, call = self.build(recovered)
        self.assertEqual(3, call.call_count)
        self.assertIn('完整中文转录', product['body'])
        self.assertNotEqual(failed_block, media.read_json(path)[key])

    def test_invalid_stage_outputs_are_not_cached_and_good_stages_survive(self):
        directory = media.cache_dir(self.row['video_id'])
        cache = {'valid-transcript': '已校验原文'}
        for output in ('', 'English summary without Chinese', '中文' * 600):
            with self.subTest(output=output[:20]), self.assertRaises(RuntimeError):
                notes.cached(directory, cache, 'bad-summary', lambda: output, notes.summary_output)
            self.assertNotIn('bad-summary', cache)
            self.assertEqual('已校验原文', cache['valid-transcript'])
        cache['bad-summary'] = ''
        output = notes.cached(directory, cache, 'bad-summary', lambda: '可用的中文要点。', notes.summary_output)
        self.assertEqual('可用的中文要点。', output)

    def test_bad_cached_translation_and_final_json_are_revalidated(self):
        directory = media.cache_dir(self.row['video_id'])
        cache = {'translation': '[00:00:00] English only', 'note': '{bad json'}
        source = '[00:00:00] The original AI tutorial has important facts.'
        translation = notes.cached(directory, cache, 'translation', lambda: '[00:00:00] 中文事实和限制。',
                                   lambda output: notes.translated_output(source, output))
        payload = notes.cached(directory, cache, 'note', lambda: json.dumps(self.payload), notes.note_payload)
        self.assertIn('中文', translation)
        self.assertEqual(self.payload, payload)
