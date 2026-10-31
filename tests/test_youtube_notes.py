import json
import math
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import media, notes
from tests.youtube_fixture import YouTubeFixture


class YouTubeNotes(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': '12345678901', 'entry': self.entry(), 'channel': '测试频道'}
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture-model')
        self.transcript = {'origin': 'automatic_captions', 'language': 'en', 'segments': [
            {'start': 0, 'end': 59, 'text': 'The AI agent builds code with limitations. ' * 120},
            {'start': 60, 'end': 119, 'text': 'Final conclusion: verify every generated change.'}]}
        self.deadline = time.monotonic() + 30
        self.prompts = []

    def reply(self, prompt, *rest):
        self.prompts.append(prompt)
        if prompt.startswith('将以下字幕'):
            source = prompt.split('\n\n', 1)[1]
            return '本段完整中文译文，保留数字、事实和限制。' * max(1, len(source) // 50)
        if '只输出 JSON' in prompt:
            return json.dumps({'title': '智能体代码生成的实践', 'summary': '作者展示代码生成方法，并提示核验限制。',
                               'body': '## 具体方法\n作者认为每次修改都应该验证。'}, ensure_ascii=False)
        return '作者介绍智能体代码生成的方法和局限，需要核验结果。'

    def build(self, reply=None):
        with patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply or self.reply):
            return notes.build(self.row, self.transcript, self.deadline, lambda: False)

    def test_full_translation_and_note_cached_without_frames(self):
        result = self.build()
        self.assertIn('完整中文转录', result['body'])
        self.assertIn('未分析视频画面', result['body'])
        self.assertNotIn('&t=', result['body'])
        self.assertNotRegex('\n'.join(self.prompts), r'\[\d{2}:\d{2}:\d{2}\]')
        self.assertNotIn('Final conclusion', result['body'])
        self.assertIn('Final conclusion', '\n'.join(self.prompts))
        count = len(self.prompts)
        self.assertEqual(result, self.build())
        self.assertEqual(count, len(self.prompts))
        self.selected.model_id = 'new-fixture-model'
        self.build()
        self.assertGreater(len(self.prompts), count)

    def test_empty_english_or_summary_translation_not_published(self):
        for output in ('', '中文摘要。', 'Still entirely English words.'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                self.build(lambda *args: output)
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-cache.json').exists())

    def test_translation_accepts_natural_paragraphs_without_timestamp_alignment(self):
        source = ('[00:00:00] The AI agent builds code.\n'
                  '[00:00:02] It still needs review.\n'
                  '[00:00:04] Verify every generated change before using it.')
        output = '这个 AI 智能体能够编写代码，但仍需要审查；使用前必须核验每次生成的改动。'
        with patch.object(notes, 'call', return_value=output):
            self.assertEqual(output, notes.translate(source, self.selected, self.deadline, lambda: False))

    def test_long_text_rejects_partial_translations_and_accepts_normal_compression(self):
        source = 'Every generated change needs independent verification. ' * 50
        source_size = sum(char.isalnum() for char in source)
        for ratio in (.15, .20, .29):
            output = '译' * int(source_size * ratio)
            with self.subTest(ratio=ratio), self.assertRaisesRegex(RuntimeError, '过短'):
                notes.translated_output(source, output)
        for ratio in (.30, .41, .60):
            output = '译' * math.ceil(source_size * ratio)
            with self.subTest(ratio=ratio):
                self.assertEqual(output, notes.translated_output(source, output))

    def test_short_closing_translation_is_not_judged_as_a_long_text_block(self):
        source = 'Thank you very much for watching this video.'
        output = '感谢观看这个视频。'
        self.assertLess(len(output), sum(char.isalnum() for char in source) * .30)
        self.assertEqual(output, notes.translated_output(source, output))

    def test_partial_translation_cache_is_revalidated_before_reuse(self):
        source = 'Every generated change needs independent verification. ' * 50
        source_size = sum(char.isalnum() for char in source)
        cache = {'translation': '译' * int(source_size * .20)}
        output = '译' * math.ceil(source_size * .41)
        with patch.object(notes, 'call', return_value=output) as call:
            actual = notes.cached(media.cache_dir(self.row['video_id']), cache, 'translation',
                                  lambda: notes.translate(source, self.selected, self.deadline, lambda: False),
                                  lambda value: notes.translated_output(source, value))
        call.assert_called_once()
        self.assertEqual(output, actual)
        self.assertEqual(output, cache['translation'])

    def test_chinese_transcript_needs_no_translation_and_keeps_source_timing(self):
        self.transcript['language'] = 'zh'
        self.transcript['segments'] = [
            {'start': 0, 'end': 60, 'text': '作者展示智能体如何编写代码。'},
            {'start': 60, 'end': 120, 'text': '最后强调人工核验结果与限制。'}]
        original = json.loads(json.dumps(self.transcript))
        result = self.build()
        self.assertIn('作者展示智能体如何编写代码。 最后强调人工核验结果与限制。', result['body'])
        self.assertFalse(any(prompt.startswith('将以下字幕') for prompt in self.prompts))
        self.assertEqual(original, self.transcript)

    def test_new_translation_policy_invalidates_notes_but_preserves_transcript_cache(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'transcript.json', self.transcript)
        with patch.object(notes, 'VERSION', 'youtube-notes-v2'):
            self.build()
        self.prompts.clear()
        self.build()
        self.assertTrue(any(prompt.startswith('将以下字幕') for prompt in self.prompts))
        self.assertEqual(self.transcript, media.read_json(directory / 'transcript.json'))

    def test_failed_translation_retries_only_unfinished_text_blocks(self):
        attempts = 0

        def failed(prompt, *rest):
            nonlocal attempts
            if prompt.startswith('将以下字幕'):
                attempts += 1
                if attempts == 2:
                    return '中文摘要。'
            return self.reply(prompt, *rest)

        with self.assertRaisesRegex(RuntimeError, '过短'):
            self.build(failed)
        self.assertEqual(2, attempts)
        self.build()
        self.assertEqual(2, sum(prompt.startswith('将以下字幕') for prompt in self.prompts))

    def test_failed_final_note_retries_using_completed_translations(self):
        def invalid(prompt, *rest):
            return 'bad JSON' if '只输出 JSON' in prompt else self.reply(prompt)

        with self.assertRaisesRegex(RuntimeError, '格式无效'):
            self.build(invalid)
        count = sum(prompt.startswith('将以下字幕') for prompt in self.prompts)
        self.build()
        self.assertEqual(count, sum(prompt.startswith('将以下字幕') for prompt in self.prompts))

    def test_no_credentials_and_interruption_are_explicit(self):
        with patch.object(notes, 'model', side_effect=RuntimeError('请配置模型')):
            self.assertEqual((False, '请配置模型'), notes.available())
        with self.assertRaises(media.Interrupted):
            notes.call('资料', self.selected, time.monotonic() - 1, lambda: False)
