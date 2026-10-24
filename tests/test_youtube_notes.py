import json
import re
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
            return '\n\n'.join(marker + ' 本段完整中文译文，保留数字、事实和限制。'
                                for marker in re.findall(r'\[\d{2}:\d{2}:\d{2}\]', prompt))
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
        self.assertIn('&t=60)', result['body'])
        self.assertNotIn('Final conclusion', result['body'])
        self.assertIn('Final conclusion', '\n'.join(self.prompts))
        count = len(self.prompts)
        self.assertEqual(result, self.build())
        self.assertEqual(count, len(self.prompts))
        self.selected.model_id = 'new-fixture-model'
        self.build()
        self.assertGreater(len(self.prompts), count)

    def test_lost_timestamp_or_english_translation_not_published(self):
        for output in ('中文但是没有时间戳。', '[00:00:00] Still entirely English words.'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                self.build(lambda *args: output)
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-cache.json').exists())

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
