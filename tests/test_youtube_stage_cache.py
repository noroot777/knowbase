import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import media, notes
from tests.youtube_fixture import YouTubeFixture


class YouTubeStageCache(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': self.entry()['video_id'], 'entry': self.entry(), 'channel': '测试频道'}
        self.row['entry']['title'] = '智能体代码生成教程'
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.blocks = [f'中文原文内容{position}。' for position in range(16)]
        self.transcript = self.complete_transcript()

    def build(self, reply):
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes.transcripts, 'blocks', return_value=self.blocks), \
                patch.object(notes, 'call', side_effect=reply) as call:
            product = notes.build(self.row, self.transcript, time.monotonic() + 30, lambda: False)
        return product, call

    def test_long_chinese_source_is_published_without_any_model_call(self):
        """中文原文不进模型，旧流水线留在缓存里的摘要/成稿条目原样不动(87cc380 起没有那两段了)。"""
        directory = media.cache_dir(self.row['video_id'])
        cache = {'old-reduce': 'bad summary', 'old-note': '{bad json'}
        media.save_json(directory / 'notes-cache.json', cache)
        product, call = self.build(AssertionError('no model calls for Chinese'))
        call.assert_not_called()
        for block in self.blocks:
            self.assertIn(block, product['body'])
        self.assertIn('中文字幕原文', product['body'])
        self.assertEqual(self.row['entry']['title'], product['title'])
        self.assertEqual(cache, media.read_json(directory / 'notes-cache.json'))
