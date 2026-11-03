import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import media, notes, products, storage, transcripts, translation
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import response


class TranslationOnly(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': self.entry()['video_id'], 'entry': self.entry(), 'channel': '测试频道'}
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.transcript = self.complete_transcript()
        self.deadline = time.monotonic() + 30

    def test_available_requires_only_the_translation_model(self):
        with patch.object(notes, 'model', return_value=self.selected) as model:
            self.assertEqual((True, ''), notes.available())
        model.assert_called_once_with('translate')

    def test_each_foreign_block_has_only_one_translation_request(self):
        self.transcript['segments'][0]['text'] = ' '.join(
            f'Checkpoint {position}: check every generated change before deployment.' for position in range(200))
        blocks = transcripts.blocks(self.transcript['segments'])
        self.assertGreater(len(blocks), 1)
        self.assertEqual(len(blocks), len(set(blocks)))

        def reply(prompt, *arguments):
            self.assertTrue(prompt.startswith('将以下字幕'), prompt)
            return response(prompt, '部署前逐项检查生成的修改。')

        with patch.object(notes, 'model', return_value=self.selected) as model, \
                patch.object(notes, 'call', side_effect=reply) as call:
            result = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        model.assert_called_once_with('translate')
        self.assertEqual(len(blocks), call.call_count)
        self.assertEqual(self.row['entry']['title'], result['title'])
        self.assertIn('## 中文译文（自动生成）', result['body'])
        self.assertIn('## 原文对照（未改写）', result['body'])
        self.assertNotIn('已进行中文整理', result['body'])

    def test_existing_coverage_cache_skips_every_model_request(self):
        source = self.transcript['segments'][0]['text']
        cache = {notes.signature(translation.STAGE, self.selected, source):
                 response(translation.prompt(source), '智能体生成代码，运行测试并核验限制。'),
                 'old-summary': '', 'old-note': '{bad JSON'}
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-cache.json', cache)
        with patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call') as call:
            result = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        call.assert_not_called()
        self.assertIn('智能体生成代码，运行测试并核验限制。', result['body'])
        self.assertEqual(cache, media.read_json(directory / 'notes-cache.json'))

    def test_native_chinese_needs_neither_model_credentials_nor_translation_cache(self):
        self.transcript['language'] = 'zh'
        self.transcript['segments'][0]['text'] = '逐项检查生成的修改，核实事实和限制。'
        with patch.object(notes, 'model', side_effect=RuntimeError('missing key')), \
                patch.object(notes, 'call') as call:
            result = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        call.assert_not_called()
        self.assertIn('中文字幕原文', result['body'])
        self.assertNotIn('自动译文', result['body'])
        self.assertNotIn('原文对照', result['body'])

    def test_full_translation_is_certified_and_published_without_a_note_model(self):
        self.isolate()
        source = self.channel()
        storage.discover(source, [self.row['entry']])
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=lambda prompt, *arguments:
                             response(prompt, '智能体生成代码，运行测试并核验限制。')):
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        proof = products.certify(self.row, {'duration': 600}, self.transcript, entry)
        storage.update(self.row['video_id'], state='ready', note=json.dumps(entry), validation=proof)
        row, = storage.rows([source['id']])
        self.assertTrue(products.valid(row))
        with storage.db() as connection:
            self.assertEqual(1, products.publish(connection, source, row))

    def test_empty_transcript_is_not_a_translation_product(self):
        self.transcript['segments'] = []
        with self.assertRaisesRegex(RuntimeError, '没有可翻译'):
            notes.build(self.row, self.transcript, self.deadline, lambda: False)

    def test_cancellation_after_cached_translation_cannot_return_a_product(self):
        source = self.transcript['segments'][0]['text']
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-cache.json', {
            notes.signature(translation.STAGE, self.selected, source):
                response(translation.prompt(source), '核验智能体生成的代码。')})
        states = iter((False, False, True))
        with patch.object(notes, 'model', return_value=self.selected), self.assertRaises(media.Interrupted):
            notes.build(self.row, self.transcript, self.deadline, lambda: next(states))
