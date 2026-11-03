import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import fulltext, media, model_response, notes, transcripts, translation
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import encoded


class YouTubeNotes(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': self.entry()['video_id'], 'entry': self.entry(), 'channel': '测试频道'}
        self.row['entry']['title'] = '智能体代码核验'
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.transcript = self.complete_transcript()
        self.deadline = time.monotonic() + 30
        self.chinese = '作者要求使用前独立核验每次生成的修改，并保留事实、方法和局限。'

    def build(self, reply=None):
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=reply or (lambda *arguments: self.chinese)):
            return notes.build(self.row, self.transcript, self.deadline, lambda: False)

    def test_whole_transcript_uses_one_plain_text_request_and_no_atomic_fallback(self):
        self.transcript['segments'][0]['text'] = 'Verify every generated change independently. ' * 100
        expected = ' '.join(segment['text'].strip() for segment in self.transcript['segments'])
        requested = []

        def reply(prompt, *arguments):
            requested.append(prompt)
            self.assertIn(fulltext.SOURCE_START + expected + '\n</source>', prompt)
            return self.chinese * 30

        with patch.object(translation, 'prompt', side_effect=AssertionError('no atomic requests')):
            entry = self.build(reply)
        self.assertEqual(1, len(requested))
        self.assertIn(self.chinese * 30, entry['body'])
        self.assertIn(expected, entry['body'])
        self.assertIn('未进行逐句语义审校', entry['body'])
        self.assertNotIn('已进行分段覆盖与语言校验', entry['body'])

    def test_long_transcripts_use_only_capacity_chunks_in_source_order(self):
        source = ' '.join(f'Section {position} explains the method and its limitations.' for position in range(600))
        self.transcript['segments'] = [{'start': 0, 'end': 600, 'text': source}]
        blocks = transcripts.blocks(self.transcript['segments'], limit=fulltext.MAX_SOURCE_CHARS)
        self.assertGreater(len(blocks), 1)
        self.assertTrue(all(len(block) <= fulltext.MAX_SOURCE_CHARS for block in blocks))
        requested = []

        def reply(prompt, *arguments):
            block = prompt.split(fulltext.SOURCE_START, 1)[1].rsplit('\n</source>', 1)[0]
            position = blocks.index(block)
            requested.append(block)
            return f'第{position}部分译文。' + self.chinese * 60

        entry = self.build(reply)
        self.assertCountEqual(blocks, requested)
        positions = [entry['body'].index(f'第{position}部分译文。') for position in range(len(blocks))]
        self.assertEqual(sorted(positions), positions)
        original = entry['body'].split('## 原文对照（未改写）\n\n', 1)[1]
        self.assertEqual(source.split(), original.split())

    def test_new_cache_is_reused_and_model_changes_invalidate_only_new_identity(self):
        first = self.build()
        directory = media.cache_dir(self.row['video_id'])
        before = media.read_json(directory / 'notes-cache.json')
        self.assertEqual(1, len(before))
        self.assertEqual(first, self.build(AssertionError('cached')))
        self.selected.model_id = 'another-model'
        self.build()
        self.assertTrue(before.items() <= media.read_json(directory / 'notes-cache.json').items())

    def test_legacy_complete_cache_is_reused_without_mutation(self):
        source = 'Review every generated change before deployment. ' * 100
        self.transcript['segments'] = [{'start': 0, 'end': 600, 'text': source}]
        cache = {notes.signature(translation.STAGE, self.selected, block): encoded(block, self.chinese)
                 for block in transcripts.blocks(self.transcript['segments'])}
        cache['old-summary'] = 'not a translation'
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-cache.json', cache)
        entry = self.build(AssertionError('reuse valid old blocks'))
        self.assertIn(self.chinese, entry['body'])
        self.assertEqual(cache, media.read_json(directory / 'notes-cache.json'))

    def test_invalid_old_cache_is_retained_but_does_not_block_plain_translation(self):
        source = self.transcript['segments'][0]['text']
        identity = notes.signature(translation.STAGE, self.selected, source)
        cache = {identity: encoded(source, source), 'old-atomic-unit': '保留旧缓存'}
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-cache.json', cache)
        self.assertIn(self.chinese, self.build()['body'])
        self.assertTrue(cache.items() <= media.read_json(directory / 'notes-cache.json').items())

    def test_bad_new_cache_is_revalidated_before_replacement(self):
        source = self.transcript['segments'][0]['text']
        identity = notes.signature(fulltext.STAGE, self.selected, source)
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-cache.json', {identity: 'English only'})
        self.assertIn(self.chinese, self.build()['body'])
        self.assertEqual(self.chinese, media.read_json(directory / 'notes-cache.json')[identity])

    def test_technical_terms_and_long_english_quotations_are_not_language_failures(self):
        quote = 'the server must respond with a valid JSON object containing all of the requested fields'
        output = f'作者引用规范：“{quote}”。请使用 FastAPI、Supabase 和 PostgreSQL，并核验返回结果。'
        entry = self.build(lambda *arguments: output)
        self.assertIn(output, entry['body'])

    def test_nonempty_and_chinese_presence_are_basic_checks_not_semantic_certification(self):
        for output in ('', '   ', None, {'text': self.chinese}, 'English words without translation'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                fulltext.validate('Check the changes.', output)
        self.assertEqual(self.chinese, fulltext.validate('Check the changes.', self.chinese))

    def test_abnormally_short_answers_are_rejected_without_fine_grained_alignment(self):
        source = 'The author explains the method and its limitations. ' * 100
        with self.assertRaisesRegex(RuntimeError, '异常短'):
            fulltext.validate(source, '作者介绍了方法。')
        output = self.chinese * 30
        self.assertEqual(output, fulltext.validate(source, output))

    def test_unchanged_foreign_source_is_not_a_translation(self):
        source = 'Verify all generated changes. 作者提醒保持谨慎。'
        with self.assertRaisesRegex(RuntimeError, '未返回中文'):
            fulltext.validate(source, source)

    def test_chinese_source_preserves_original_without_model_or_cache(self):
        self.transcript['segments'][0]['text'] = self.chinese
        with patch.object(notes, 'model', side_effect=AssertionError('no model')):
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        self.assertIn('中文字幕原文', entry['body'])
        self.assertNotIn('原文对照', entry['body'])
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-cache.json').exists())

    def test_title_failure_does_not_repeat_body_or_downgrade_it(self):
        self.row['entry']['title'] = 'Review all changes'

        def reply(prompt, *arguments):
            if fulltext.SOURCE_START + self.row['entry']['title'] + '\n</source>' in prompt:
                raise model_response.OutputError('model refused title')
            return self.chinese

        entry = self.build(reply)
        self.assertEqual(self.row['entry']['title'], entry['title'])
        self.assertNotIn('fallback', entry['body_provenance'])
        self.assertIn('## 视频原题', entry['body'])

    def test_failure_counters_from_old_algorithm_do_not_block_or_change_new_translation(self):
        directory = media.cache_dir(self.row['video_id'])
        failures = {'old-language-stage': 3}
        media.save_json(directory / 'notes-language-failures.json', failures)
        self.assertIn(self.chinese, self.build()['body'])
        self.assertEqual(failures, media.read_json(directory / 'notes-language-failures.json'))

    def test_explicit_truncation_does_not_retry_as_small_segments(self):
        calls = []

        def reply(prompt, *arguments):
            calls.append(prompt)
            raise model_response.OutputError('模型回复未正常完成：chat/length')

        with self.assertRaises(model_response.OutputError):
            self.build(reply)
        self.assertEqual(1, len(calls))
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-cache.json').exists())

    def test_stopped_or_expired_work_never_calls_the_model(self):
        for deadline, stopped in ((time.monotonic() - 1, lambda: False), (self.deadline, lambda: True)):
            with self.subTest(deadline=deadline), patch.object(notes, 'call') as call, \
                    self.assertRaises(media.Interrupted):
                notes.build(self.row, self.transcript, deadline, stopped)
            call.assert_not_called()

    def test_cached_json_is_not_the_new_plain_text_cache_format(self):
        entry = self.build()
        cache = media.read_json(media.cache_dir(self.row['video_id']) / 'notes-cache.json')
        self.assertEqual([self.chinese], list(cache.values()))
        self.assertNotIn(json.dumps({'translations': []}), entry['body'])
