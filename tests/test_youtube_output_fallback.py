import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.youtube import collector, discovery, media, notes, products, storage, transcripts, translation
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import response


class OutputFallback(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()
        self.row = {'video_id': self.entry_value['video_id'], 'entry': self.entry_value, 'channel': '测试频道'}
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.transcript = {'origin': 'uploaded_captions', 'language': 'en', 'segments': [
            {'start': 0, 'end': 600, 'text': 'Verify every generated change independently before using it. '
             'The author explains the method and records its limitations in detail.'}]}
        self.deadline = time.monotonic() + 30

    def reply(self, prompt, *arguments):
        if prompt.startswith('将以下字幕'):
            return response(prompt, '作者要求独立核验修改，并记录方法和局限。')
        if '只输出 JSON' in prompt:
            return json.dumps({'title': '独立核验修改', 'summary': '记录方法和局限。', 'body': '使用前核验结果。'})
        return '作者要求独立核验修改，并记录方法和局限。'

    def untranslated(self, prompt, *arguments):
        segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
        return json.dumps({'translations': segments})

    def build(self, reply=None):
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=reply or self.reply):
            return notes.build(self.row, self.transcript, self.deadline, lambda: False)

    def test_each_language_stage_has_a_bounded_labelled_original_exit(self):
        for number, stage in enumerate(('translation', 'summary', 'note'), 10):
            self.row['video_id'] = f'{number:011d}'
            requested = []

            def reply(prompt, *arguments):
                requested.append(prompt)
                if stage == 'translation' and prompt.startswith('将以下字幕'):
                    return self.untranslated(prompt)
                if stage == 'summary' and not prompt.startswith('将以下字幕') and '只输出 JSON' not in prompt:
                    return 'This summary is still entirely English.'
                if stage == 'note' and '只输出 JSON' in prompt:
                    return json.dumps({'title': 'English title', 'summary': 'English summary', 'body': 'English note'})
                return self.reply(prompt)

            with self.subTest(stage=stage):
                for attempt in range(notes.MAX_LANGUAGE_TRIES - 1):
                    with self.assertRaises(translation.LanguageError):
                        self.build(reply)
                result = self.build(reply)
                self.assertTrue(result['title'].startswith('原文待校验：'))
                self.assertEqual('youtube:original-fallback', result['body_provenance'])
                self.assertIn('不是中文译文或已核验笔记', result['body'])
                self.assertIn('\n\n'.join(transcripts.blocks(self.transcript['segments'])), result['body'])
                self.assertNotIn('## 中文译文', result['body'])
                self.assertNotIn('English note', result['body'])
                count = len(requested)
                self.assertEqual(result, self.build(reply))
                self.assertEqual(count, len(requested))

    def test_format_transport_local_and_interruption_failures_never_spend_the_language_budget(self):
        for error in (media.ToolFailure('model transport unavailable', 'HTTP 429', origin='model'),
                      OSError('disk full'), media.Interrupted('stopped'), RuntimeError('refused')):
            with self.subTest(error=error), patch.object(notes, 'model', return_value=self.selected), \
                    patch.object(notes, 'call', side_effect=error) as call:
                for attempt in range(notes.MAX_LANGUAGE_TRIES + 1):
                    with self.assertRaises(type(error)):
                        notes.build(self.row, self.transcript, self.deadline, lambda: False)
                self.assertEqual(notes.MAX_LANGUAGE_TRIES + 1, call.call_count)
        for attempt in range(notes.MAX_LANGUAGE_TRIES + 1):
            with self.assertRaises(RuntimeError):
                self.build(lambda *arguments: json.dumps({'translations': []}))
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-language-failures.json').exists())

    def test_language_budget_is_scoped_to_models_and_transcript_and_resets_on_success(self):
        for attempt in range(2):
            with self.assertRaises(translation.LanguageError):
                self.build(self.untranslated)
        self.selected.model_id = 'another-model'
        with self.assertRaises(translation.LanguageError):
            self.build(self.untranslated)
        self.selected.model_id = 'fixture'
        self.transcript['segments'][0]['end'] = 599
        with self.assertRaises(translation.LanguageError):
            self.build(self.untranslated)
        self.transcript['segments'][0]['end'] = 600
        self.assertIn('## 中文译文', self.build()['body'])
        directory = media.cache_dir(self.row['video_id'])
        cache = media.read_json(directory / 'notes-cache.json')
        block, = transcripts.blocks(self.transcript['segments'])
        cache.pop(notes.signature(translation.STAGE, self.selected, block))
        media.save_json(directory / 'notes-cache.json', cache)
        for attempt in range(2):
            with self.assertRaises(translation.LanguageError):
                self.build(self.untranslated)

    def test_cancel_and_expired_deadline_cannot_publish_a_budget_exhausted_fallback(self):
        for attempt in range(2):
            with self.assertRaises(translation.LanguageError):
                self.build(self.untranslated)
        stopped = False

        def interrupted(*arguments):
            nonlocal stopped
            stopped = True
            raise translation.LanguageError('not Chinese')

        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=interrupted), self.assertRaises(media.Interrupted):
            notes.build(self.row, self.transcript, self.deadline, lambda: stopped)
        path = media.cache_dir(self.row['video_id']) / 'notes-language-failures.json'
        self.assertEqual([2], list(media.read_json(path).values()))
        fallback = self.build(self.untranslated)
        self.assertEqual('youtube:original-fallback', fallback['body_provenance'])
        with patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call') as call:
            for deadline, stopped in ((self.deadline, lambda: True), (time.monotonic() - 1, lambda: False)):
                with self.subTest(deadline=deadline), self.assertRaises(media.Interrupted):
                    notes.build(self.row, self.transcript, deadline, stopped)
            call.assert_not_called()

    def test_collector_exports_labelled_full_original_only_after_the_language_budget(self):
        prompts = []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            return self.untranslated(prompt)

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            for attempt in range(2):
                result = collector.collect(FetchScope(force=True))
                self.assertEqual((0, 0), (result['processed'], result['added']))
                self.assertEqual('failed', storage.rows()[0]['state'])
                self.assertFalse(collector.YouTubeCollector('youtube').materialize())
            result = collector.collect(FetchScope(force=True))
            self.assertEqual((1, 1), (result['processed'], result['added']), result)
            self.assertEqual('ready', storage.rows()[0]['state'])
            self.assertTrue(products.valid(storage.rows()[0]))
            self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
            files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
            self.assertEqual(1, len(files))
            body = files[0].read_text()
            self.assertIn('不是中文译文或已核验笔记', body)
            self.assertIn(self.transcript['segments'][0]['text'], body)
            self.assertNotIn('## 中文译文', body)
            count = len(prompts)
            collector.collect(FetchScope(force=True))
            self.assertEqual(count, len(prompts))

    def test_collector_accepts_names_and_quotes_in_one_pass_and_exports_them(self):
        quote = 'the server must respond with a valid JSON object containing the field name and the field value'
        source = ('Compare GPT 4o, Claude 3.5 Sonnet, Gemini 1.5 Pro, Llama 3.1, Mistral Large, '
                  'Command R, DeepSeek V2, Qwen 2.5, Phi 3 and Yi 34B. '
                  'Reviewers verify implementation constraints and record deployment failures independently. '
                  'The spec says: "' + quote + '".')
        value = ('对比 GPT 4o、Claude 3.5 Sonnet、Gemini 1.5 Pro、Llama 3.1、Mistral Large、'
                 'Command R、DeepSeek V2、Qwen 2.5、Phi 3 和 Yi 34B。审查者独立核验实现约束并记录部署失败。'
                 '规范原文是：“' + quote + '”。')
        self.transcript['segments'][0]['text'] = source
        payload = {'title': 'ソフトバンク 的模型投资', 'summary': '作者讨论 삼성전자 的投入。',
                   'body': '作者引用了 ソフトバンク 的公告。' + value}
        prompts = []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if prompt.startswith('将以下字幕'):
                return response(prompt, value)
            return json.dumps(payload) if '只输出 JSON' in prompt else payload['summary']

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertEqual(1, sum(prompt.startswith('将以下字幕') for prompt in prompts))
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        body = files[0].read_text()
        for literal in ('ソフトバンク', '삼성전자', 'Yi 34B', quote, '中文译文（自动生成）'):
            self.assertIn(literal, body)
        self.assertNotIn('原文待校验：', body)

    def test_collector_rejects_half_translated_caches_then_exports_a_real_chinese_recovery(self):
        tail = ('Then we move on to the second part, which covers the deployment story, '
                'and after that we look at the rollout, with the numbers from last quarter, '
                'and finally we close with the roadmap, which the team has already published, '
                'so you can read it yourself, if you want the full detail, and that is all for today.')
        source = 'Welcome back to the channel. ' + tail
        bad = '欢迎回到本频道。' + tail
        self.transcript['segments'][0]['text'] = source
        directory = media.cache_dir(self.row['video_id'])
        cached_bad = json.dumps({'translations': [bad]})
        media.save_json(directory / 'notes-cache.json', {
            notes.signature(translation.STAGE, self.selected, source): cached_bad,
            notes.signature('translate-unit-v1', self.selected, source): cached_bad})
        broken, prompts = True, []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if broken and prompt.startswith('将以下字幕'):
                return cached_bad
            return self.reply(prompt)

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            for attempt in range(2):
                result = collector.collect(FetchScope(force=True))
                self.assertEqual((0, 0), (result['processed'], result['added']), result)
                self.assertEqual('failed', storage.rows()[0]['state'])
                self.assertFalse(collector.YouTubeCollector('youtube').materialize())
                self.assertNotIn(cached_bad, media.read_json(directory / 'notes-cache.json').values())
            self.assertTrue(all(prompt.startswith('将以下字幕') for prompt in prompts))
            broken = False
            result = collector.collect(FetchScope(force=True))
            self.assertEqual((1, 1), (result['processed'], result['added']), result)
            self.assertEqual('ready', storage.rows()[0]['state'])
            self.assertTrue(products.valid(storage.rows()[0]))
            self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
            files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
            self.assertEqual(1, len(files))
            body = files[0].read_text()
            self.assertIn('中文译文（自动生成）', body)
            self.assertIn('作者要求独立核验修改，并记录方法和局限。', body)
            self.assertNotIn('Then we move on', body)
            self.assertNotIn('原文待校验', body)
            self.assertEqual({}, media.read_json(directory / 'notes-language-failures.json'))
