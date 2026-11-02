import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.youtube import collector, discovery, media, model_response, notes, products, storage, throttle, transcripts, translation
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

    def test_each_language_stage_preserves_valid_chinese_in_its_bounded_exit(self):
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
                if stage == 'translation':
                    self.assertTrue(result['title'].startswith('原文待校验：'))
                    self.assertEqual('youtube:original-fallback', result['body_provenance'])
                    self.assertIn('不是中文译文或已核验笔记', result['body'])
                    self.assertIn('\n\n'.join(transcripts.blocks(self.transcript['segments'])), result['body'])
                else:
                    self.assertTrue(result['title'].startswith('整理待校验：'))
                    self.assertEqual('youtube:translation-fallback', result['body_provenance'])
                    self.assertIn('不是已核验内容笔记', result['body'])
                    self.assertIn('作者要求独立核验修改，并记录方法和局限。', result['body'])
                    chinese, original = result['body'].split('\n\n## 原文对照（未改写）\n\n')
                    self.assertNotIn('Verify every generated change', chinese)
                    self.assertIn('Verify every generated change', original)
                self.assertNotIn('## 中文译文', result['body'])
                self.assertNotIn('English note', result['body'])
                count = len(requested)
                self.assertEqual(result, self.build(reply))
                self.assertEqual(count, len(requested))

    def test_each_stage_gets_its_own_budget_and_success_clears_only_that_stage(self):
        stage = 'translation'

        def reply(prompt, *arguments):
            if stage == 'translation' and prompt.startswith('将以下字幕'):
                return self.untranslated(prompt)
            if stage == 'summary' and not prompt.startswith('将以下字幕') and '只输出 JSON' not in prompt:
                return 'This summary remains English.'
            if stage == 'note' and '只输出 JSON' in prompt:
                return json.dumps({'title': 'English title', 'summary': 'English summary', 'body': 'English note'})
            return self.reply(prompt)

        for stage in ('translation', 'summary', 'note'):
            for attempt in range(2):
                with self.subTest(stage=stage, attempt=attempt), self.assertRaises(translation.LanguageError) as error:
                    self.build(reply)
                self.assertEqual(stage, error.exception.stage)
                counters = media.read_json(media.cache_dir(self.row['video_id']) / 'notes-language-failures.json')
                self.assertEqual([attempt + 1], list(counters.values()))
        result = self.build(reply)
        self.assertEqual('youtube:translation-fallback', result['body_provenance'])
        self.assertIn('作者要求独立核验修改', result['body'])
        self.assertNotIn('English note', result['body'])

    def test_legacy_shared_budget_cannot_exhaust_a_new_stage(self):
        fingerprint = (json.dumps(self.transcript, ensure_ascii=False, sort_keys=True) + '\n' +
                       self.selected.provider + '\n' + self.selected.model_id)
        legacy = notes.signature('language-failure-v1', self.selected, fingerprint)
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'notes-language-failures.json', {legacy: notes.MAX_LANGUAGE_TRIES})
        result = self.build()
        self.assertNotIn('待校验：', result['title'])
        self.assertIn('## 中文译文', result['body'])

    def test_fallback_keeps_valid_atomic_chinese_and_labels_only_the_missing_original(self):
        source = ('Verify every generated change before use. ' * 8 + '\n\n' +
                  'Review the deployment results independently. ' * 8).strip()
        self.transcript['segments'][0]['text'] = source
        units = translation.units(source)
        self.assertEqual(2, len(units))
        directory = media.cache_dir(self.row['video_id'])
        good = '作者要求使用前核验所有生成的修改。'
        media.save_json(directory / 'notes-cache.json', {
            notes.signature('translate-unit-v1', self.selected, units[0]['text']):
                json.dumps({'translations': [good]})})
        for attempt in range(2):
            with self.assertRaises(translation.LanguageError):
                self.build(self.untranslated)
        result = self.build(self.untranslated)
        self.assertEqual('youtube:mixed-fallback', result['body_provenance'])
        self.assertNotIn('## 原文对照', result['body'])
        self.assertIn(good, result['body'])
        self.assertEqual(1, result['body'].count(units[1]['text']))
        self.assertNotIn(units[0]['text'], result['body'])
        self.assertIn('第 1 段：中文资料（待核验）', result['body'])
        self.assertIn('第 2 段：原文（未翻译）', result['body'])

    def test_original_comparison_is_only_appended_when_fallback_sections_are_all_chinese(self):
        source = ' '.join(f'Checkpoint {position} requires independent validation before deployment.'
                          for position in range(70))
        self.transcript['segments'][0]['text'] = source
        blocks = transcripts.blocks(self.transcript['segments'])
        self.assertEqual(2, len(blocks))
        good = '作者要求对各个阶段独立核验，然后再部署。'
        directory = media.cache_dir(self.row['video_id'])
        for translated_count, provenance in ((0, 'original-fallback'), (1, 'mixed-fallback'),
                                             (2, 'translation-fallback')):
            cache = {notes.signature(translation.STAGE, self.selected, block):
                     json.dumps({'translations': [good] * len(translation.units(block))})
                     for block in blocks[:translated_count]}
            media.save_json(directory / 'notes-cache.json', cache)
            with self.subTest(translated_count=translated_count), patch.object(notes, 'call') as call:
                result = notes.fallback(self.row, self.transcript, directory, self.selected,
                                        self.deadline, lambda: False)
                call.assert_not_called()
                self.assertEqual('youtube:' + provenance, result['body_provenance'])
                self.assertEqual(translated_count == len(blocks), '## 原文对照' in result['body'])
                if translated_count == len(blocks):
                    chinese, original = result['body'].split('\n\n## 原文对照（未改写）\n\n')
                    self.assertIn(good, chinese)
                    self.assertEqual('\n\n'.join(blocks), original.split('\n\n> 原文依据：', 1)[0])
                else:
                    for block in blocks[translated_count:]:
                        for unit in translation.units(block):
                            self.assertEqual(1, result['body'].count(unit['text']))
                self.assertEqual(source, self.transcript['segments'][0]['text'])

    def test_exhausted_fallback_revalidates_chinese_cache_without_model_calls(self):
        def reply(prompt, *arguments):
            return 'This summary remains English.' if not prompt.startswith('将以下字幕') else self.reply(prompt)

        for attempt in range(2):
            with self.assertRaises(translation.LanguageError):
                self.build(reply)
        result = self.build(reply)
        self.assertEqual('youtube:translation-fallback', result['body_provenance'])
        directory = media.cache_dir(self.row['video_id'])
        block, = transcripts.blocks(self.transcript['segments'])
        media.save_json(directory / 'notes-cache.json', {
            notes.signature(translation.STAGE, self.selected, block): json.dumps({'translations': [block]})})
        with patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call') as call:
            result = notes.build(self.row, self.transcript, self.deadline, lambda: False)
        call.assert_not_called()
        self.assertEqual('youtube:original-fallback', result['body_provenance'])
        self.assertIn(block, result['body'])

    def test_service_local_and_unattempted_failures_never_spend_the_output_budget(self):
        for error in (OSError('disk full'), media.Interrupted('stopped'),
                      media.ToolFailure('cannot start worker', origin='local'),
                      media.ToolFailure('model transport unavailable', 'HTTP 429', origin='model'),
                      throttle.NotAttempted('model request not attempted', origin='model')):
            with self.subTest(error=error), patch.object(notes, 'model', return_value=self.selected), \
                    patch.object(notes, 'call', side_effect=error) as call:
                for attempt in range(notes.MAX_LANGUAGE_TRIES + 1):
                    with self.assertRaises(type(error)):
                        notes.build(self.row, self.transcript, self.deadline, lambda: False)
                self.assertEqual(notes.MAX_LANGUAGE_TRIES + 1, call.call_count)
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-language-failures.json').exists())

    def collector_model_failure(self, error, expected, recover=False):
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=error) as call, \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            for attempt in range(notes.MAX_LANGUAGE_TRIES + 1):
                result = collector.collect(FetchScope(force=True))
                self.assertEqual((0, 0), (result['processed'], result['added']), result)
                row, = storage.rows()
                self.assertEqual(expected, row['state'])
                self.assertFalse(row['note'])
                self.assertEqual(attempt + 1 if expected == 'failed' else 0, row['attempts'])
                if expected == 'deferred':
                    self.assertEqual(('', 0), (row['last_error'], row['retry_at']))
                self.assertFalse(collector.YouTubeCollector('youtube').materialize())
            if recover:
                call.side_effect = self.reply
                result = collector.collect(FetchScope(force=True))
                self.assertEqual((1, 1), (result['processed'], result['added']))
                row, = storage.rows()
                self.assertEqual('ready', row['state'])
                self.assertTrue(products.valid(row))
                self.assertNotIn('fallback', json.loads(row['note'])['body_provenance'])
                self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
                filename, = (self.root / 'vault' / 'source' / 'youtube').rglob('*.md')
                self.assertIn('## 中文译文（自动生成）', filename.read_text())
                self.assertNotIn('原文待校验：', filename.read_text())
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-language-failures.json').exists())

    def test_response_service_failures_preserve_each_existing_content_stage_budget(self):
        failure = SimpleNamespace(status='failed', error=SimpleNamespace(message='server had an error'),
                                  incomplete_details=None)
        for number, stage in enumerate(('translation', 'summary', 'note'), 800):
            self.row['video_id'] = f'{number:011d}'
            mode = 'output'

            def reply(prompt, *arguments):
                actual = 'translation' if prompt.startswith('将以下字幕') else 'note' if '只输出 JSON' in prompt else 'summary'
                if actual == stage and mode == 'output':
                    raise model_response.OutputError('explicit output failure')
                if actual == stage and mode == 'service':
                    return model_response.complete(None, 'fixture', 'fixture', prompt)
                return self.reply(prompt)

            path = media.cache_dir(self.row['video_id']) / 'notes-language-failures.json'
            with self.subTest(stage=stage), patch.object(model_response.llm, 'openai_conversion_enabled',
                                                       return_value=False), \
                    patch.object(model_response.llm_openai, '_create', return_value=failure):
                for attempt in range(2):
                    with self.assertRaises(model_response.OutputError):
                        self.build(reply)
                self.assertEqual([2], list(media.read_json(path).values()))
                mode = 'service'
                for attempt in range(notes.MAX_LANGUAGE_TRIES + 1):
                    with self.assertRaisesRegex(media.ToolFailure, 'server had an error') as caught:
                        self.build(reply)
                    self.assertEqual('model', caught.exception.origin)
                    self.assertEqual([2], list(media.read_json(path).values()))
                mode = 'success'
                self.assertNotIn('fallback', self.build(reply)['body_provenance'])
                self.assertEqual({}, media.read_json(path))

    def test_collector_retries_failed_response_envelopes_then_exports_a_real_recovery(self):
        failure = SimpleNamespace(status='failed', error=SimpleNamespace(message='server had an error'),
                                  incomplete_details=None)
        with patch.object(model_response.llm, 'openai_conversion_enabled', return_value=False), \
                patch.object(model_response.llm_openai, '_create', return_value=failure):
            def fail(*arguments):
                return model_response.complete(None, 'fixture', 'fixture', 'fixture')

            self.collector_model_failure(fail, 'failed', recover=True)

    def test_service_failures_preserve_existing_content_counts_without_exhausting_them(self):
        for attempt in range(2):
            with self.assertRaises(translation.LanguageError):
                self.build(self.untranslated)
        path = media.cache_dir(self.row['video_id']) / 'notes-language-failures.json'
        for error in (media.ToolFailure('model unavailable', origin='model'),
                      throttle.NotAttempted('model request not attempted', origin='model')):
            def fail(*arguments):
                raise error

            for attempt in range(notes.MAX_LANGUAGE_TRIES + 1):
                with self.subTest(error=error, attempt=attempt), self.assertRaises(type(error)):
                    self.build(fail)
                self.assertEqual([2], list(media.read_json(path).values()))
        self.assertNotIn('fallback', self.build()['body_provenance'])
        self.assertEqual({}, media.read_json(path))

    def test_collector_keeps_model_outages_retryable_after_three_rounds(self):
        self.collector_model_failure(media.ToolFailure('model unavailable', 'HTTP 429', origin='model'), 'failed')

    def test_collector_defers_unattempted_model_work_without_failure_or_publication(self):
        self.collector_model_failure(throttle.NotAttempted('model request not attempted', origin='model'), 'deferred')

    def test_all_output_failures_share_a_bounded_stage_exit(self):
        cases = [('translation', json.dumps({'translations': []})),
                 ('translation', json.dumps({'translations': [None]})),
                 ('summary', ''), ('summary', '中文内容' * 400),
                 ('note', 'not JSON'), ('note', json.dumps({'note': {'title': '嵌套笔记'}})),
                 ('note', json.dumps({'title': '笔记', 'summary': '', 'body': '正文'})),
                 ('translation', model_response.OutputError('refused'))]
        for number, (stage, failure) in enumerate(cases, 100):
            self.row['video_id'] = f'{number:011d}'
            requested = []

            def reply(prompt, *arguments):
                requested.append(prompt)
                actual = 'translation' if prompt.startswith('将以下字幕') else 'note' if '只输出 JSON' in prompt else 'summary'
                if actual == stage:
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                return self.reply(prompt)

            with self.subTest(stage=stage, failure=failure):
                for attempt in range(notes.MAX_LANGUAGE_TRIES - 1):
                    with self.assertRaises(RuntimeError):
                        self.build(reply)
                result = self.build(reply)
                self.assertIn('连续三轮未生成可用成品', result['body'])
                self.assertEqual('youtube:original-fallback' if stage == 'translation' else
                                 'youtube:translation-fallback', result['body_provenance'])
                count = len(requested)
                self.assertEqual(result, self.build(reply))
                self.assertEqual(count, len(requested))

    def test_incomplete_refused_nontext_and_empty_model_responses_are_bounded(self):
        message = lambda content='', refusal=None: SimpleNamespace(content=content, refusal=refusal)
        replies = [SimpleNamespace(choices=[SimpleNamespace(finish_reason=reason, message=message())])
                   for reason in ('length', 'tool_calls')]
        replies += [SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=value)])
                    for value in (message(refusal='refused'), message(), message(['nontext']))]
        for number, reply in enumerate(replies, 200):
            self.row['video_id'] = f'{number:011d}'
            with self.subTest(reply=reply), patch.object(model_response.llm, 'openai_conversion_enabled', return_value=True), \
                    patch.object(model_response.llm_openai, '_create', return_value=reply):
                call = lambda *arguments: model_response.complete(None, 'fixture', 'fixture', 'fixture')
                for attempt in range(notes.MAX_LANGUAGE_TRIES - 1):
                    with self.assertRaises(RuntimeError):
                        self.build(call)
                result = self.build(call)
                self.assertEqual('youtube:original-fallback', result['body_provenance'])

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
            return json.dumps(payload) if '只输出 JSON' in prompt else payload['summary'] + value

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

    def test_collector_exports_lowercase_tool_names_without_falling_back(self):
        names = 'npm、pnpm、yarn、bun、vite、esbuild、rollup、webpack、swc、turbopack、parcel'
        self.transcript['segments'][0]['text'] = 'The toolchain covers ' + names.replace('、', ', ') + '.'
        value = '工具链覆盖 ' + names + '，每个工具的缓存策略都要独立核验。'
        payload = {'title': '工具链缓存策略', 'summary': value, 'body': value}

        def reply(prompt, *arguments):
            if prompt.startswith('将以下字幕'):
                return response(prompt, value)
            return json.dumps(payload) if '只输出 JSON' in prompt else value

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        file, = (self.root / 'vault' / 'source' / 'youtube').rglob('*.md')
        self.assertIn(names, file.read_text())
        self.assertNotIn('原文待校验：', file.read_text())

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
            chinese, original = body.split('\n\n## 原文对照（未改写）\n\n')
            self.assertNotIn('Then we move on', chinese)
            self.assertIn(source, original)
            self.assertNotIn('原文待校验', body)
            self.assertEqual({}, media.read_json(directory / 'notes-language-failures.json'))

    def test_collector_exports_cached_chinese_when_only_final_note_exhausts_its_budget(self):
        prompts = []
        storage.discover(self.source, [self.entry_value])
        storage.update(self.entry_value['video_id'], attempts=20)

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if '只输出 JSON' in prompt and not prompt.startswith('将以下字幕'):
                return json.dumps({'note': {'title': '错误包装', 'summary': '错误包装', 'body': '不能采用的模型正文'}})
            return self.reply(prompt)

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            for attempt in range(2):
                result = collector.collect(FetchScope(force=True))
                self.assertEqual((0, 0), (result['processed'], result['added']))
                self.assertFalse(collector.YouTubeCollector('youtube').materialize())
            result = collector.collect(FetchScope(force=True))
            self.assertEqual((1, 1), (result['processed'], result['added']), result)
            self.assertTrue(products.valid(storage.rows()[0]))
            self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
            body, = [filename.read_text() for filename in
                     (self.root / 'vault' / 'source' / 'youtube').rglob('*.md')]
            self.assertIn('整理待校验：', body)
            self.assertIn('中文资料（待核验）', body)
            self.assertIn('作者要求独立核验修改，并记录方法和局限。', body)
            chinese, original = body.split('\n\n## 原文对照（未改写）\n\n')
            self.assertNotIn('Verify every generated change', chinese)
            self.assertIn(self.transcript['segments'][0]['text'], original)
            self.assertNotIn('English note', body)
            self.assertNotIn('不能采用的模型正文', body)
            self.assertEqual(1, sum(prompt.startswith('将以下字幕') for prompt in prompts))
            count = len(prompts)
            collector.collect(FetchScope(force=True))
            self.assertEqual(count, len(prompts))

    def mixed_source(self):
        english = ('so basically what we are going to do here is wire the agent into the editor and then '
                   'let it rewrite the failing test until the suite goes green, which sounds great until '
                   'you realise the cache gets invalidated on every single keystroke and the whole thing '
                   'grinds to a halt, so please try it yourself before you trust any of these numbers. ')
        source = ('好的，我们来看这个智能体编程的实际例子，先把环境准备好，然后一步一步演示整条流水线怎么跑起来。'
                  '下面这段是作者的原话，我先放一遍，稍后再逐句解释其中的关键限制和容易踩的坑。' + english * 2).strip()
        self.transcript['language'] = 'zh'
        self.transcript['segments'][0]['text'] = source
        return source

    def test_mixed_original_echo_caches_never_become_chinese_fallback_material(self):
        source = self.mixed_source()
        directory = media.cache_dir(self.row['video_id'])
        units = translation.units(source)
        cache = {notes.signature('translate', self.selected, source): source,
                 notes.signature(translation.STAGE, self.selected, source):
                 json.dumps({'translations': [unit['text'] for unit in units]})}
        cache.update({notes.signature('translate-unit-v1', self.selected, unit['text']):
                      json.dumps({'translations': [unit['text']]}) for unit in units})
        media.save_json(directory / 'notes-cache.json', cache)
        prompts = []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            return self.untranslated(prompt)

        for attempt in range(notes.MAX_LANGUAGE_TRIES - 1):
            with self.assertRaises(translation.LanguageError):
                self.build(reply)
        result = self.build(reply)
        self.assertEqual('youtube:original-fallback', result['body_provenance'])
        self.assertIn(source, result['body'])
        self.assertNotIn('中文资料（待核验）', result['body'])
        self.assertNotIn('## 中文译文', result['body'])
        count = len(prompts)
        self.assertEqual(result, self.build(reply))
        self.assertEqual(count, len(prompts))

    def test_collector_exports_chinese_translation_and_original_for_a_mixed_chinese_transcript(self):
        source = self.mixed_source()
        prompts = []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            return self.reply(prompt)

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            result = collector.collect(FetchScope(force=True))
            self.assertEqual((1, 1), (result['processed'], result['added']), result)
            self.assertEqual('ready', storage.rows()[0]['state'])
            self.assertTrue(products.valid(storage.rows()[0]))
            self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
            filename, = (self.root / 'vault' / 'source' / 'youtube').rglob('*.md')
            chinese, original = filename.read_text().split('\n\n## 原文对照（未改写）\n\n')
            self.assertIn('## 中文译文（自动生成）', chinese)
            self.assertIn('作者要求独立核验修改，并记录方法和局限。', chinese)
            self.assertNotIn('so basically', chinese)
            self.assertEqual(source, original.strip())
            self.assertTrue(any(prompt.startswith('将以下字幕') for prompt in prompts))
            count = len(prompts)
            collector.collect(FetchScope(force=True))
            self.assertEqual(count, len(prompts))
