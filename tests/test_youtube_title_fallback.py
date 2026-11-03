import errno
import json
import time
from threading import Barrier, Event
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import fulltext, llm_request, media, model_response, notes, throttle, transcripts, translation
from kb.youtube.diagnosis import LOCAL, MODEL
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import response, source_text


class TitleFallback(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.row = {'video_id': self.entry()['video_id'], 'entry': self.entry(), 'channel': '测试频道'}
        self.transcript = self.complete_transcript()
        self.deadline = time.monotonic() + 10
        override = patch.object(notes, 'model', return_value=self.selected)
        override.start()
        self.addCleanup(override.stop)

    def build(self, reply, stopped=lambda: False):
        with patch.object(notes, 'call', side_effect=reply):
            return notes.build(self.row, self.transcript, self.deadline, stopped)

    def failures(self):
        return media.read_json(media.cache_dir(self.row['video_id']) / 'notes-language-failures.json')

    def cache(self):
        return media.read_json(media.cache_dir(self.row['video_id']) / 'notes-cache.json')

    def is_title(self, prompt):
        return source_text(prompt) == self.row['entry']['title']

    def test_product_names_with_foreign_or_chinese_subtitles_succeed_on_first_attempt(self):
        number = 0
        for title in ('Claude Opus 5.5', 'GPT-5.5 Codex', 'Gemini 3 Deep Think', 'OpenAI DevDay 2026'):
            for chinese in (False, True):
                with self.subTest(title=title, chinese=chinese):
                    number += 1
                    self.row['video_id'] = self.entry(number)['video_id']
                    self.row['entry']['title'] = title
                    self.transcript = self.complete_transcript()
                    if chinese:
                        self.transcript['segments'][0]['text'] = '独立核验代码生成结果并记录限制。'
                    calls = []

                    def reply(prompt, *arguments):
                        calls.append(prompt)
                        return response(prompt, title if self.is_title(prompt) else '独立核验代码生成结果并记录限制。')

                    entry = self.build(reply)
                    self.assertEqual(title, entry['title'])
                    self.assertEqual('youtube:uploaded_captions', entry['body_provenance'])
                    self.assertIn('独立核验代码生成结果并记录限制。', entry['body'])
                    self.assertIn('## 视频原题\n\n' + title, entry['body'])
                    self.assertNotIn('连续三轮', entry['body'])
                    self.assertFalse(self.failures())
                    self.assertEqual(1 if chinese else 2, len(calls))
                    if chinese:
                        self.assertIsNone(self.cache())
                    else:
                        self.assertNotIn(notes.signature(fulltext.TITLE_STAGE, self.selected, title), self.cache())

    def test_malformed_or_non_chinese_title_output_is_not_a_video_failure(self):
        self.transcript['segments'][0]['text'] = '独立核验每项生成的修改。'
        for output in (None, '', 'not JSON', '{"translations": []}',
                       '{"translations": ["Still English"]}', '{"translations": ["テストタイトル"]}'):
            with self.subTest(output=output):
                entry = self.build(lambda *arguments: output)
                self.assertEqual(self.row['entry']['title'], entry['title'])
                self.assertEqual('youtube:uploaded_captions', entry['body_provenance'])
                self.assertFalse(self.failures())
                self.assertFalse(self.cache())

    def test_title_output_error_keeps_the_original_title(self):
        self.transcript['segments'][0]['text'] = '独立核验每项生成的修改。'
        with patch.object(notes, 'call', side_effect=model_response.OutputError('incomplete title')):
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        self.assertEqual(self.row['entry']['title'], entry['title'])
        self.assertEqual('youtube:uploaded_captions', entry['body_provenance'])
        self.assertFalse(self.failures())

    def test_bad_title_does_not_cancel_other_translation_jobs(self):
        blocks = ['Verify generated changes.', 'Record all observed limitations.']
        self.transcript['segments'][0]['text'] = ' '.join(blocks)
        split = transcripts.blocks
        barrier = Barrier(3, timeout=3)

        def sections(segments, limit=3000):
            return blocks if segments is self.transcript['segments'] else split(segments, limit)

        def reply(prompt, *arguments):
            barrier.wait()
            return 'invalid title' if self.is_title(prompt) else response(prompt, '逐项核验生成的修改。')

        with patch.object(transcripts, 'blocks', side_effect=sections):
            entry = self.build(reply)
        self.assertEqual(2, entry['body'].count('逐项核验生成的修改。'))
        self.assertEqual('youtube:uploaded_captions', entry['body_provenance'])
        self.assertEqual(2, len(self.cache()))
        self.assertFalse(self.failures())

    def test_title_and_identical_subtitle_do_not_share_the_optional_job(self):
        title = self.row['entry']['title'] = 'Claude Opus 5.5'
        self.transcript['segments'][0]['text'] = title
        calls = []
        barrier = Barrier(2, timeout=3)

        def reply(prompt, *arguments):
            calls.append(prompt)
            if len(calls) <= 2:
                barrier.wait()
            return response(prompt, title)

        with self.assertRaisesRegex(RuntimeError, '未返回中文'):
            self.build(reply)
        self.assertEqual(2, len(calls))
        self.assertFalse(self.cache())
        self.assertFalse(self.failures())

    def test_real_subtitle_failures_remain_retryable_without_publishing_fallback(self):
        def reply(prompt, *arguments):
            if self.is_title(prompt):
                return 'invalid title'
            return source_text(prompt)

        for attempt in range(4):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(RuntimeError, '等待重试'):
                self.build(reply)
            self.assertFalse(self.failures())
            self.assertFalse(self.cache())

    def test_success_with_original_title_leaves_legacy_failure_counter_untouched(self):
        fingerprint = (json.dumps(self.transcript, ensure_ascii=False, sort_keys=True) + '\n' +
                       self.selected.provider + '\n' + self.selected.model_id)
        key = notes.signature('language-failure-v2-translation', self.selected, fingerprint)
        media.save_json(media.cache_dir(self.row['video_id']) / 'notes-language-failures.json', {key: 2})

        def reply(prompt, *arguments):
            return 'invalid title' if self.is_title(prompt) else response(prompt, '独立核验代码生成结果。')

        entry = self.build(reply)
        self.assertEqual('youtube:uploaded_captions', entry['body_provenance'])
        self.assertEqual({key: 2}, self.failures())

    def test_legacy_valid_title_and_subtitle_caches_still_skip_requests(self):
        source = self.transcript['segments'][0]['text']
        title = self.row['entry']['title']
        cache = {notes.signature(translation.STAGE, self.selected, title):
                 response(translation.prompt(title), '智能体代码生成教程'),
                 notes.signature(translation.STAGE, self.selected, source):
                 response(translation.prompt(source), '智能体生成代码并运行测试。')}
        media.save_json(media.cache_dir(self.row['video_id']) / 'notes-cache.json', cache)
        with patch.object(notes, 'call') as call:
            entry = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        call.assert_not_called()
        self.assertEqual('智能体代码生成教程', entry['title'])
        self.assertEqual(cache, self.cache())

    def test_plain_subtitles_reuse_new_or_legacy_valid_title_cache(self):
        title = self.row['entry']['title']
        directory = media.cache_dir(self.row['video_id'])
        for stage in (notes.TITLE_STAGE, translation.STAGE, fulltext.TITLE_STAGE):
            with self.subTest(stage=stage):
                media.save_json(directory / 'notes-cache.json', {
                    notes.signature(stage, self.selected, title): ('智能体代码生成教程' if stage == fulltext.TITLE_STAGE else
                        response(translation.prompt(title), '智能体代码生成教程'))})
                entry = self.build(lambda prompt, *arguments: response(prompt, '作者说明应当独立核验生成结果。'))
                self.assertEqual('智能体代码生成教程', entry['title'])
                self.assertNotIn('fallback', entry['body_provenance'])
        media.save_json(directory / 'notes-cache.json', {
            notes.signature(notes.TITLE_STAGE, self.selected, title): 'invalid title'})
        entry = self.build(lambda prompt, *arguments: 'invalid title' if self.is_title(prompt) else '作者要求核验生成结果。')
        self.assertEqual(title, entry['title'])
        self.assertNotIn('fallback', entry['body_provenance'])

    def test_title_transport_local_and_interruption_errors_are_not_swallowed(self):
        self.transcript['segments'][0]['text'] = '独立核验每项生成的修改。'
        for error in (media.ToolFailure('service unavailable', origin=MODEL),
                      media.ToolFailure('cannot start process', origin=LOCAL),
                      OSError(errno.ENOSPC, 'disk full'), media.Interrupted('cancelled')):
            with self.subTest(error=error), patch.object(notes, 'call', side_effect=error), \
                    self.assertRaises(type(error)) as raised:
                notes.build(self.row, self.transcript, self.deadline, lambda: False)
            self.assertIs(error, raised.exception)
            self.assertFalse(self.failures())
            self.assertFalse(self.cache())

    def test_title_429_keeps_model_origin_and_trips_the_round_breaker(self):
        self.transcript['segments'][0]['text'] = '独立核验每项生成的修改。'

        def limited(*arguments):
            raise llm_request.failed('Error code: 429 too many requests')

        with throttle.opened(), self.assertRaises(media.ToolFailure) as raised:
            try:
                self.build(limited)
            finally:
                with self.assertRaises(throttle.NotAttempted):
                    throttle.guard(MODEL)
        self.assertEqual(MODEL, raised.exception.origin)
        self.assertFalse(self.failures())

    def test_cancellation_during_bad_title_output_cannot_publish_or_cache(self):
        cancelled = Event()
        self.transcript['segments'][0]['text'] = '独立核验每项生成的修改。'

        def reply(*arguments):
            cancelled.set()
            return 'invalid title'

        with self.assertRaises(media.Interrupted):
            self.build(reply, cancelled.is_set)
        self.assertFalse(self.failures())
        self.assertFalse(self.cache())

    def test_late_title_output_cannot_publish_or_cache(self):
        self.transcript['segments'][0]['text'] = '独立核验每项生成的修改。'
        self.deadline = time.monotonic() + .05

        def reply(*arguments):
            time.sleep(.08)
            return 'invalid title'

        with self.assertRaises(media.Interrupted):
            self.build(reply)
        self.assertFalse(self.failures())
        self.assertFalse(self.cache())
