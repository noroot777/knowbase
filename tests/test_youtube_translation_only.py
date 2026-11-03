import json
import time
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import fulltext, media, notes, products, storage, transcripts, translation
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import response


class TranslationOnly(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': self.entry()['video_id'], 'entry': self.entry(), 'channel': '测试频道'}
        self.row['entry']['title'] = '智能体代码生成教程'
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
        blocks = transcripts.blocks(self.transcript['segments'], limit=fulltext.MAX_SOURCE_CHARS)
        self.assertGreater(len(blocks), 1)
        self.assertEqual(len(blocks), len(set(blocks)))

        def reply(prompt, *arguments):
            self.assertTrue(prompt.startswith('将以下字幕'), prompt)
            return response(prompt, '部署前逐项检查生成的修改。' * 150)

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
        cancelled = Event()
        render = fulltext.validate
        media.save_json(directory / 'notes-cache.json', {
            notes.signature(fulltext.STAGE, self.selected, source): '核验智能体生成的代码。'})

        def validate(text, output):
            value = render(text, output)
            cancelled.set()
            return value

        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(fulltext, 'validate', side_effect=validate), self.assertRaises(media.Interrupted):
            notes.build(self.row, self.transcript, self.deadline, cancelled.is_set)

    def test_foreign_title_is_translated_and_original_retained(self):
        self.row['entry']['title'] = 'AI agents verify every generated change'
        calls = []

        def reply(prompt, *arguments):
            calls.append(prompt)
            return response(prompt, '智能体核验每项生成的修改。')

        with patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply):
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        self.assertEqual('智能体核验每项生成的修改。', entry['title'])
        self.assertIn('## 视频原题\n\n' + self.row['entry']['title'], entry['body'])
        self.assertEqual(2, len(calls))
        self.assertTrue(all(prompt.startswith('将以下字幕') for prompt in calls))
        with patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call') as call:
            self.assertEqual(entry, notes.build(self.row, self.transcript, self.deadline, lambda: False))
        call.assert_not_called()

    def test_foreign_title_with_chinese_subtitles_still_needs_translation(self):
        self.row['entry']['title'] = 'Verify all changes before deployment'
        self.transcript['segments'][0]['text'] = '部署前核验所有修改并记录限制。'
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=lambda prompt, *arguments:
                             response(prompt, '部署前核验所有修改')) as call:
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        self.assertEqual('部署前核验所有修改', entry['title'])
        self.assertEqual(1, call.call_count)
        self.assertIn('中文字幕原文', entry['body'])
        self.assertIn(self.row['entry']['title'], entry['body'])

    def test_cached_body_only_requests_the_missing_title(self):
        self.row['entry']['title'] = 'AI agents verify generated changes'
        source = self.transcript['segments'][0]['text']
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-cache.json', {
            notes.signature(translation.STAGE, self.selected, source):
                response(translation.prompt(source), '智能体生成代码，运行测试并核验限制。')})

        def reply(prompt, *arguments):
            self.assertIn(fulltext.SOURCE_START + self.row['entry']['title'] + '\n</source>', prompt)
            return response(prompt, '智能体核验生成的修改')

        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=reply) as call:
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        self.assertEqual(1, call.call_count)
        self.assertEqual('智能体核验生成的修改', entry['title'])
        self.assertIn('智能体生成代码，运行测试并核验限制。', entry['body'])

    def test_failed_title_does_not_downgrade_a_successful_chinese_body(self):
        self.row['entry']['title'] = 'Verify changes before deployment'
        self.transcript['segments'][0]['text'] = '部署前核验修改并记录限制。'
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', return_value='English title without translation') as call:
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        self.assertEqual(self.row['entry']['title'], entry['title'])
        self.assertEqual('youtube:uploaded_captions', entry['body_provenance'])
        self.assertIn(self.transcript['segments'][0]['text'], entry['body'])
        self.assertEqual(1, call.call_count)
        self.assertFalse(media.read_json(media.cache_dir(self.row['video_id']) / 'notes-language-failures.json'))
