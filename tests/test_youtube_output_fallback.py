import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.youtube import collector, discovery, media, model_response, notes, products, storage, throttle
from tests.youtube_fixture import YouTubeFixture


class TranslationFailure(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()
        self.entry_value['title'] = '智能体代码生成教程'
        self.row = {'video_id': self.entry_value['video_id'], 'entry': self.entry_value, 'channel': '测试频道'}
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.transcript = self.complete_transcript()
        self.deadline = time.monotonic() + 30
        self.chinese = '作者要求使用前独立核验每次生成的修改，并记录方法和局限。'

    def test_repeated_output_failures_never_produce_original_or_mixed_fallback(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-language-failures.json', {'old-algorithm': 3})
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', return_value='English only') as call:
            for attempt in range(4):
                with self.subTest(attempt=attempt), self.assertRaisesRegex(RuntimeError, '等待重试'):
                    notes.build(self.row, self.transcript, self.deadline, lambda: False)
        self.assertEqual(4, call.call_count)
        self.assertFalse((directory / 'notes-cache.json').exists())
        self.assertEqual({'old-algorithm': 3}, media.read_json(directory / 'notes-language-failures.json'))

    def test_service_local_and_unattempted_errors_propagate_without_fallback(self):
        errors = (OSError('disk full'), media.Interrupted('stopped'),
                  media.ToolFailure('cannot start worker', origin='local'),
                  media.ToolFailure('model unavailable', origin='model'),
                  throttle.NotAttempted('request not attempted', origin='model'),
                  model_response.OutputError('response truncated'))
        for error in errors:
            with self.subTest(error=error), patch.object(notes, 'model', return_value=self.selected), \
                    patch.object(notes, 'call', side_effect=error), self.assertRaises(type(error)):
                notes.build(self.row, self.transcript, self.deadline, lambda: False)
        directory = media.cache_dir(self.row['video_id'])
        self.assertFalse((directory / 'notes-cache.json').exists())
        self.assertFalse((directory / 'notes-language-failures.json').exists())

    def collector_failure(self, error, expected, recover=False):
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=error) as call, \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            for attempt in range(4):
                result = collector.collect(FetchScope(force=True))
                self.assertEqual((0, 0), (result['processed'], result['added']), result)
                row, = storage.rows()
                self.assertEqual(expected, row['state'])
                self.assertFalse(row['note'])
                self.assertEqual(attempt + 1 if expected == 'failed' else 0, row['attempts'])
                self.assertFalse(collector.YouTubeCollector('youtube').materialize())
                with storage.db() as connection:
                    self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM feed_items WHERE kind='youtube'").fetchone()[0])
            if recover:
                call.side_effect = lambda *arguments: self.chinese
                result = collector.collect(FetchScope(force=True))
                self.assertEqual((1, 1), (result['processed'], result['added']))
                row, = storage.rows()
                self.assertEqual('ready', row['state'])
                self.assertTrue(products.valid(row))
                self.assertNotIn('fallback', json.loads(row['note'])['body_provenance'])
                self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
                filename, = (self.root / 'vault' / 'source' / 'youtube').rglob('*.md')
                self.assertIn(self.chinese, filename.read_text())
                self.assertIn('## 原文对照（未改写）', filename.read_text())

    def test_collector_keeps_invalid_translations_retryable_and_publishes_only_real_recovery(self):
        self.collector_failure(RuntimeError('模型未返回中文直译正文，保留原文等待重试'), 'failed', recover=True)

    def test_collector_keeps_model_outages_retryable(self):
        self.collector_failure(media.ToolFailure('model unavailable', origin='model'), 'failed')

    def test_collector_defers_unattempted_work_without_spending_failure_attempts(self):
        self.collector_failure(throttle.NotAttempted('request not attempted', origin='model'), 'deferred')

    def test_collector_never_publishes_a_truncated_response(self):
        self.collector_failure(model_response.OutputError('模型回复未正常完成：chat/length'), 'failed')
