import json
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.youtube import media, notes, translation
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import encoded, response


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
            return response(prompt, '本段中文译文，保留事实和限制。')
        if '只输出 JSON' in prompt:
            return json.dumps({'title': '智能体代码生成的实践', 'summary': '作者展示代码生成方法，并提示核验限制。',
                               'body': '## 具体方法\n作者认为每次修改都应该验证。'}, ensure_ascii=False)
        return '作者介绍智能体代码生成的方法和局限，需要核验结果。'

    def build(self, reply=None):
        with patch.object(notes, 'model', return_value=self.selected), patch.object(notes, 'call', side_effect=reply or self.reply):
            return notes.build(self.row, self.transcript, self.deadline, lambda: False)

    def test_full_translation_and_note_cached_without_frames(self):
        result = self.build()
        self.assertIn('中文译文（自动生成）', result['body'])
        self.assertNotIn('完整中文转录', result['body'])
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

    def test_empty_or_english_translation_not_published(self):
        for output in ('', '   ', 'Still entirely English words.'):
            with self.subTest(output=output), self.assertRaises(RuntimeError):
                self.build(lambda *args: output)
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-cache.json').exists())

    def test_batch_shape_failures_recover_by_translating_each_original_unit(self):
        literal = 'https://example.test/tools/agent/install?version=latest'
        source = 'Review generated changes before use. ' * 12 + '\n\n' + literal
        expected = [unit['text'] for unit in translation.units(source)]
        translated = '使用前必须独立审查生成的修改，下面保留原始网址。'
        for extra in (False, True):
            requested = []

            def reply(prompt, *arguments):
                segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
                requested.append(segments)
                if len(segments) > 1:
                    values = [translated, literal, '额外总结。'] if extra else [translated + literal]
                else:
                    self.assertIn(segments[0], expected)
                    values = [literal if segments[0] == literal else translated]
                return json.dumps({'translations': values})

            with self.subTest(extra=extra), patch.object(notes, 'call', side_effect=reply):
                self.assertEqual(translated + '\n\n' + literal,
                                 notes.translate(source, self.selected, self.deadline, lambda: False))
                self.assertEqual([expected, *[[unit] for unit in expected]], requested)

    def test_atomic_recovery_reuses_completed_units_without_publishing_a_missing_one(self):
        literal = 'python -m unittest tests.test_youtube_notes --verbose'
        source = 'Review generated changes before use. ' * 12 + '\n\n' + literal
        expected = [unit['text'] for unit in translation.units(source)]
        self.transcript['segments'] = [{'text': source}]
        requested = []
        broken = True

        def reply(prompt, *arguments):
            if not prompt.startswith('将以下字幕'):
                return self.reply(prompt, *arguments)
            segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
            requested.append(segments)
            if len(segments) > 1:
                values = ['合并后的中文译文，不直接使用这份覆盖未知的输出。']
            elif segments[0] == literal:
                values = [] if broken else [literal]
            else:
                self.assertEqual(expected[0], segments[0])
                values = ['使用前必须独立审查所有生成的修改，随后运行原文命令。']
            return json.dumps({'translations': values})

        with self.assertRaisesRegex(RuntimeError, '覆盖'):
            self.build(reply)
        self.assertFalse(self.prompts)
        cache = media.read_json(media.cache_dir(self.row['video_id']) / 'notes-cache.json')
        unit_key = notes.signature('translate-unit-v1', self.selected, expected[0])
        self.assertEqual({unit_key}, set(cache))
        broken = False
        result = self.build(reply)
        self.assertIn(literal, result['body'])
        self.assertNotIn('覆盖未知', result['body'])
        self.assertEqual([expected, [expected[0]], [literal], expected, [literal]], requested)
        calls = len(requested), len(self.prompts)
        self.assertEqual(result, self.build(reply))
        self.assertEqual(calls, (len(requested), len(self.prompts)))

    def test_non_chinese_atomic_results_do_not_poison_recovery_cache(self):
        literal = 'https://example.test/tools/agent/install?version=latest'
        source = 'Review generated changes before use. ' * 12 + '\n\n' + literal
        self.transcript['segments'] = [{'text': source}]
        expected = [unit['text'] for unit in translation.units(source)]
        requested = []
        broken = True

        def reply(prompt, *arguments):
            if not prompt.startswith('将以下字幕'):
                return self.reply(prompt, *arguments)
            segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
            requested.append(segments)
            if len(segments) > 1:
                values = []
            elif broken or segments[0] == literal:
                values = [segments[0]]
            else:
                values = ['使用前必须独立核验所有生成的修改，随后访问原文网址。']
            return json.dumps({'translations': values})

        with self.assertRaisesRegex(RuntimeError, '覆盖'):
            self.build(reply)
        self.assertFalse(self.prompts)
        self.assertIsNone(media.read_json(media.cache_dir(self.row['video_id']) / 'notes-cache.json'))
        broken = False
        result = self.build(reply)
        self.assertIn('必须独立核验', result['body'])
        self.assertIn(literal, result['body'])
        sequence = [expected, *[[unit] for unit in expected]]
        self.assertEqual([expected, [expected[0]]] + sequence, requested)

    def test_translation_accepts_natural_paragraphs_without_timestamp_alignment(self):
        source = ('[00:00:00] The AI agent builds code.\n'
                  '[00:00:02] It still needs review.\n'
                  '[00:00:04] Verify every generated change before using it.')
        output = '这个 AI 智能体能够编写代码，但仍需要审查；使用前必须核验每次生成的改动。'
        with patch.object(notes, 'call', return_value=encoded(source, output)):
            self.assertEqual(output, notes.translate(source, self.selected, self.deadline, lambda: False))

    def test_every_batch_validation_failure_recovers_from_the_original_units(self):
        source = 'Review every generated change before use. ' * 24
        expected = [unit['text'] for unit in translation.units(source)]
        count = len(expected)
        invalid = ['中文散文而不是 JSON。', '{"translations":',
                   json.dumps({'translations': '不是数组'}),
                   json.dumps({'translations': ['中文译文。'] * (count - 1) + [' ']}),
                   json.dumps({'translations': ['中文译文。'] * (count - 1) + [None]}),
                   json.dumps({'translations': [{'id': 'foreign', 'text': '中文译文。'}] * count}),
                   json.dumps({'translations': ['English only'] * count})]
        for batch in invalid:
            requested = []

            def reply(prompt, *arguments):
                segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
                requested.append(segments)
                if len(requested) == 1:
                    return batch
                return response(prompt, '每次生成的修改都要独立审查后才能使用。')

            with self.subTest(batch=batch), patch.object(notes, 'call', side_effect=reply):
                result = notes.translate(source, self.selected, self.deadline, lambda: False)
            self.assertEqual([expected, *[[unit] for unit in expected]], requested)
            self.assertEqual(count, result.count('独立审查'))

    def test_model_json_wrappers_are_extracted_for_translation_and_final_note(self):
        def wrapped(prompt, *arguments):
            result = self.reply(prompt, *arguments)
            if prompt.startswith('将以下字幕') or '只输出 JSON' in prompt:
                return '好的，以下是结果：\n```JSON\n' + result + '\n```\n以上为结果。'
            return result

        result = self.build(wrapped)
        self.assertIn('中文译文（自动生成）', result['body'])
        self.assertNotIn('好的，以下', result['body'])
        self.assertNotIn('以上为结果', result['body'])
        batches = [prompt for prompt in self.prompts if prompt.startswith('将以下字幕')]
        self.assertEqual(2, len(batches))
        calls = len(self.prompts)
        self.assertEqual(result, self.build(wrapped))
        self.assertEqual(calls, len(self.prompts))

    def test_transport_failure_does_not_fan_out_into_atomic_model_requests(self):
        failure = media.ToolFailure('model transport unavailable', 'HTTP 429', origin='model')
        with patch.object(notes, 'call', side_effect=failure) as call, self.assertRaises(media.ToolFailure):
            notes.translate('Review every generated change. ' * 30,
                            self.selected, self.deadline, lambda: False)
        self.assertEqual(1, call.call_count)

    def test_complete_but_compressed_translation_is_not_rejected_by_length(self):
        source = ('The information provided in this presentation should not under any circumstances '
                  'be regarded as a replacement for independent verification of generated source code. '
                  'Please make sure that every single change is checked before the generated result is used.')
        output = '本演示不能代替对生成代码的独立核验，使用前务必检查每次修改。'
        self.assertLess(sum(char.isalnum() for char in output),
                        sum(char.isalnum() for char in source) * .30)
        self.assertEqual(output, notes.translated_output(source, encoded(source, output)))

    def test_one_sentence_cannot_be_cached_or_used_to_generate_a_note(self):
        def reply(prompt, *rest):
            self.prompts.append(prompt)
            return '中文摘要。'

        with self.assertRaisesRegex(RuntimeError, '覆盖'):
            self.build(reply)
        self.assertFalse((media.cache_dir(self.row['video_id']) / 'notes-cache.json').exists())
        self.assertEqual(2, len(self.prompts))
        self.assertFalse(any('只输出 JSON' in prompt for prompt in self.prompts))

    def test_natural_chinese_numbers_and_translated_acronyms_do_not_create_quality_claims(self):
        self.transcript['segments'] = [{'start': 0, 'end': 600,
            'text': 'Check 3 things in the UI and QA workflow. The API budget is 64.'}]
        output = '检查界面和质量保证流程中的三件事。接口预算为六十四。'

        def reply(prompt, *rest):
            return response(prompt, output) if prompt.startswith('将以下字幕') else self.reply(prompt, *rest)

        result = self.build(reply)
        self.assertIn(output, result['body'])
        self.assertNotIn('翻译核验提示', result['body'])
        self.assertNotIn('translation_warnings', result)

    def test_short_closing_translation_is_not_judged_as_a_long_text_block(self):
        source = 'Thank you very much for watching this video.'
        output = '感谢观看这个视频。'
        self.assertLess(len(output), sum(char.isalnum() for char in source) * .30)
        self.assertEqual(output, notes.translated_output(source, encoded(source, output)))

    def test_compressed_translation_cache_is_reused_without_repeated_model_charges(self):
        source = 'Every generated change needs independent verification. ' * 50
        output = '每次生成的修改均须独立核验。'
        raw = encoded(source, output)
        cache = {'translation': raw}
        with patch.object(notes, 'call') as call:
            actual = notes.cached(media.cache_dir(self.row['video_id']), cache, 'translation',
                                  lambda: notes.translate(source, self.selected, self.deadline, lambda: False),
                                  lambda value: notes.translated_output(source, value))
        call.assert_not_called()
        self.assertEqual('\n\n'.join([output] * len(translation.units(source))), actual)
        self.assertEqual(raw, cache['translation'])

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

    def test_native_chinese_quotes_do_not_require_a_successful_translation(self):
        quote = ('the compiler will analyze your component tree and automatically insert memoization '
                 'boundaries so that you do not have to write useMemo by hand any more')
        for words in (9, 10, 11, 12, 30):
            source = ('今天介绍这个框架的状态管理与渲染。官方文档原话是：' +
                      ' '.join(quote.split()[:words]) + '。这句话说明编译器负责插入缓存边界，接下来实际核验。')
            with self.subTest(words=words), patch.object(notes, 'call') as call:
                self.assertTrue(notes.native_chinese(source))
                self.assertEqual(source, notes.translate(source, self.selected, self.deadline, lambda: False))
                call.assert_not_called()
        self.transcript['language'] = 'zh'
        self.transcript['segments'] = [{'start': 0, 'end': 120, 'text': source}]
        original = json.loads(json.dumps(self.transcript))
        directory = media.cache_dir(self.row['video_id'])
        key = notes.signature('translate', self.selected, source)
        media.save_json(directory / 'notes-cache.json', {key: source})
        result = self.build()
        self.assertIn(source, result['body'])
        self.assertFalse(any(prompt.startswith('将以下字幕') for prompt in self.prompts))
        self.assertEqual(original, self.transcript)
        count = len(self.prompts)
        self.assertEqual(result, self.build())
        self.assertEqual(count, len(self.prompts))
        self.assertEqual(source, media.read_json(directory / 'notes-cache.json')[key])

    def test_japanese_summaries_and_note_fields_are_not_chinese_products(self):
        foreign = '今日は状態管理と描画の方法を説明します。'
        with self.assertRaises(RuntimeError):
            notes.summary_output(foreign)
        with self.assertRaises(RuntimeError):
            notes.translated_output('作者介绍状态管理。', foreign)
        for field in ('title', 'summary', 'body'):
            payload = {'title': '状态管理的方法', 'summary': '作者说明核验限制。', 'body': '正文说明实际方法。'}
            payload[field] = foreign
            with self.subTest(field=field), self.assertRaises(RuntimeError):
                notes.note_payload(json.dumps(payload))

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
                if attempts >= 2:
                    return ''
            return self.reply(prompt, *rest)

        with self.assertRaisesRegex(RuntimeError, '中文译文'):
            self.build(failed)
        self.assertEqual(3, attempts)
        blocks = notes.transcripts.blocks(self.transcript['segments'])
        cache = media.read_json(media.cache_dir(self.row['video_id']) / 'notes-cache.json')
        self.assertEqual({notes.signature(translation.STAGE, self.selected, blocks[0])}, set(cache))
        self.assertTrue(all(prompt.startswith('将以下字幕') for prompt in self.prompts))
        self.build()
        self.assertEqual(2, sum(prompt.startswith('将以下字幕') for prompt in self.prompts))

    def test_legacy_unstructured_cache_is_not_silently_promoted_to_coverage_evidence(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'transcript.json', self.transcript)
        blocks = notes.transcripts.blocks(self.transcript['segments'])
        legacy = {notes.signature('translate', self.selected, block): '中文摘要。' for block in blocks}
        media.save_json(directory / 'notes-cache.json', legacy)
        result = self.build()
        self.assertNotIn('中文摘要。', result['body'])
        self.assertEqual(len(blocks), sum(prompt.startswith('将以下字幕') for prompt in self.prompts))
        saved = media.read_json(directory / 'notes-cache.json')
        self.assertTrue(legacy.items() <= saved.items())
        self.assertEqual(self.transcript, media.read_json(directory / 'transcript.json'))
        self.assertEqual(result, self.build())

    def test_corrupt_cached_coverage_is_rechecked_before_summary_or_publication(self):
        self.build()
        directory = media.cache_dir(self.row['video_id'])
        block = notes.transcripts.blocks(self.transcript['segments'])[0]
        key = notes.signature(translation.STAGE, self.selected, block)
        cache = media.read_json(directory / 'notes-cache.json')
        broken = json.loads(cache[key])
        broken['translations'].pop()
        cache[key] = json.dumps(broken)
        media.save_json(directory / 'notes-cache.json', cache)
        self.prompts.clear()
        self.build()
        self.assertEqual(1, sum(prompt.startswith('将以下字幕') for prompt in self.prompts))

    def test_segment_identity_coverage_rejects_missing_duplicate_foreign_and_empty_outputs(self):
        source = 'Check every generated change before use. ' * 40
        valid = json.loads(encoded(source, '每次修改都要核验。'))
        rows = valid['translations']
        invalid = [rows[:-1], rows + [rows[0]], [rows[0]] * len(rows),
                   [{**row, 'id': 'foreign'} for row in rows],
                   [{**row, 'text': ''} for row in rows],
                   [{**row, 'text': 'English only'} for row in rows]]
        for outputs in invalid:
            with self.subTest(outputs=outputs), self.assertRaisesRegex(RuntimeError, '覆盖'):
                notes.translated_output(source, json.dumps({'translations': outputs}))
        shuffled = json.dumps({'translations': list(reversed(rows))})
        self.assertEqual(notes.translated_output(source, json.dumps(valid)),
                         notes.translated_output(source, shuffled))

    def test_segment_manifest_is_lossless_bounded_and_bound_to_source_content(self):
        source = 'First fact: check all changes. ' * 30 + 'Final budget is 64.'
        segments = translation.units(source)
        self.assertGreater(len(segments), 1)
        self.assertEqual(source, ' '.join(segment['text'] for segment in segments))
        self.assertTrue(all(len(segment['text']) <= 450 for segment in segments))
        translated = encoded(source, '核验所有修改，末尾预算为六十四。')
        with self.assertRaisesRegex(RuntimeError, '覆盖'):
            notes.translated_output(source.replace('64', '65'), translated)

    def test_ordered_translation_accepts_literal_units_and_keeps_legacy_cache_compatible(self):
        for literal in ('https://example.test/tools/agent/install?version=latest',
                        'python -m unittest tests.test_youtube_notes --verbose'):
            source = 'Review generated changes before use. ' * 12 + '\n\n' + literal
            segments = translation.units(source)
            values = ['使用前需要独立审查所有生成的修改，随后执行原文中的命令或访问原网址。', literal]
            with self.subTest(literal=literal):
                self.assertEqual(2, len(segments))
                manifest = json.loads(translation.prompt(source).split('\n\n', 1)[1])['segments']
                self.assertEqual([segment['text'] for segment in segments], manifest)
                rendered = notes.translated_output(source, json.dumps({'translations': values}))
                self.assertEqual('\n\n'.join(values), rendered)
                legacy = {'translations': [{'id': segment['id'], 'text': value}
                                           for segment, value in zip(segments, values)]}
                self.assertEqual(rendered, notes.translated_output(source, json.dumps(legacy)))

    def test_ordered_translation_still_rejects_missing_extra_empty_or_non_chinese_blocks(self):
        source = 'Review generated changes before use. ' * 40
        values = ['每次修改都需要核验。'] * len(translation.units(source))
        invalid = [values[:-1], values + ['额外的总结。'], ['', *values[1:]],
                   ['English only'] * len(values), [{'text': value} for value in values],
                   [123, *values[1:]]]
        for output in invalid:
            with self.subTest(output=output), self.assertRaisesRegex(RuntimeError, '覆盖'):
                notes.translated_output(source, json.dumps({'translations': output}))

    def test_ordered_translation_cache_remains_bound_to_source_and_model(self):
        source = 'Verify every generated change before use. ' * 20
        key = notes.signature(translation.STAGE, self.selected, source)
        cache = {key: response(translation.prompt(source), '每次修改都需要独立核验。')}
        with patch.object(notes, 'call') as call:
            actual = notes.cached(media.cache_dir(self.row['video_id']), cache, key,
                                  lambda: notes.translation_request(source, self.selected, self.deadline, lambda: False),
                                  lambda value: notes.translated_output(source, value))
        call.assert_not_called()
        self.assertIn('独立核验', actual)
        self.assertNotEqual(key, notes.signature(translation.STAGE, self.selected, source + ' Another fact.'))
        self.selected.model_id = 'another-model'
        self.assertNotEqual(key, notes.signature(translation.STAGE, self.selected, source))

    def test_translation_cache_is_reused_without_adding_heuristic_text_to_notes(self):
        self.transcript['segments'][-1]['text'] += ' The final API budget is 64.'
        result = self.build()
        self.assertNotIn('translation_warnings', result)
        self.assertNotIn('翻译核验提示', result['body'])
        self.assertNotIn('需核对', result['body'])
        self.assertEqual(result, self.build())

    def test_short_reduce_tail_may_expand_without_rejecting_the_whole_note(self):
        self.assertEqual('可用的中文分段要点。', notes.summary_output('可用的中文分段要点。', '短尾。'))
        with self.assertRaisesRegex(RuntimeError, '压缩'):
            notes.summary_output('中' * 1001, '原' * 12000)

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
