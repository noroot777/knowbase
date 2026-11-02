import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.youtube import collector, discovery, json_output, media, model_response, notes, products, storage, translation
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import response


class UniqueModelObject(unittest.TestCase):
    def setUp(self):
        self.payload = {'title': '智能体如何自检生成的代码', 'summary': '使用前核验结果。',
                        'body': '## 具体方法\n每次修改都需要验证。'}
        self.answer = json.dumps(self.payload, ensure_ascii=False)
        self.draft = json.dumps({'title': '草稿标题', 'summary': '另一份中文摘要。',
                                 'body': '这是先写的一版草稿，下面才是正式版。'}, ensure_ascii=False)

    def test_unique_objects_keep_wrappers_nested_data_and_literal_braces(self):
        for output in (self.answer, '以下是笔记：' + self.answer + '（以上为笔记）',
                       '好的，以下是结果：\n```JSON\n' + self.answer + '\n```\n以上为结果。'):
            with self.subTest(output=output):
                self.assertEqual(self.payload, json_output.object_payload(output))
        payload = {'translations': [{'id': 'one', 'text': '正文提到 {花括号}、草稿、example 和正式版。'}]}
        self.assertEqual(payload, json_output.object_payload(json.dumps(payload, ensure_ascii=False)))

    def test_multiple_results_are_rejected_regardless_of_order_or_labels(self):
        template = json.dumps({'title': '', 'summary': '', 'body': ''})
        schema = json.dumps({'title': 'string', 'summary': 'string', 'body': 'string'})
        for other in (self.draft, template, schema, self.answer):
            for first, second in ((self.answer, other), (other, self.answer)):
                for label in ('', '示例：', '格式示例如下\n', 'example:\n', '草稿：\n', '正式版：\n'):
                    output = first + '\n\n' + label + second
                    with self.subTest(other=other, label=label, first=first), \
                            self.assertRaises(model_response.OutputError):
                        json_output.object_payload(output, other)

    def test_adjacent_labels_apply_across_lines_without_reading_payload_text(self):
        for label in ('我会按这个格式输出', '我将给出结果', '示例：', '模板：', '草稿：', '备选：',
                      '以上只是示例', '正式版将另行给出', 'I will output', 'example:', 'template:',
                      'draft:', 'alternative:', '这是草稿。', 'This is just an example.'):
            for output in (label + '\n\n```json\n' + self.answer + '\n```',
                           self.answer + '\n\n' + label):
                with self.subTest(label=label, output=output), self.assertRaises(ValueError):
                    json_output.object_payload(output)

    def test_arrays_prompt_echoes_and_missing_objects_stay_invalid(self):
        for output in ('正文没有结构化结果', '[' + self.answer + ']', None, ''):
            with self.subTest(output=output), self.assertRaises(ValueError):
                json_output.object_payload(output)
        with self.assertRaises(ValueError):
            json_output.object_payload(self.answer, '输入示例：\n' + self.answer)
        self.assertEqual(self.payload, json_output.object_payload('[' + self.answer + ']\n' + self.answer))

    def test_neutral_closings_and_content_descriptions_do_not_label_the_result(self):
        prefixes = ('', '以下是按要求的格式输出的笔记：', '以下是视频中示例代码部分整理后的笔记：',
                    'Here is the note in JSON:\n', '视频中有一个草稿示例。\n以下是结果：', '正式版：')
        suffixes = ('以上是翻译结果，如有需要请告诉我。', '以上就是整理好的笔记。',
                    'Let me know if you would like any changes.', 'I hope this will be helpful!',
                    '注：要点 [1] 来自视频开头。', '（无遗漏段落：[]）',
                    '以上是结果。\n示例代码将在视频后半段讲解。')
        for prefix in prefixes:
            for suffix in suffixes:
                with self.subTest(prefix=prefix, suffix=suffix):
                    output = prefix + '\n```json\n' + self.answer + '\n```\n' + suffix
                    self.assertEqual(self.payload, json_output.object_payload(output))
                    self.assertEqual(self.payload, notes.note_payload(output))
        source, chinese = 'Verify every generated change independently before use.', '使用前独立核验每一项生成的修改。'
        answer = json.dumps({'translations': [chinese]}, ensure_ascii=False)
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                output = answer + '\n' + suffix
                self.assertEqual(chinese, translation.render(source, output))
                self.assertEqual(chinese, translation.unit_output(source, 0, output))

    def test_expected_fields_ignore_non_results_but_not_same_shape_templates(self):
        decorations = ('[1]', '[]', '{"footnotes": [1]}', '{"segments": ["input"]}',
                       '{"translations": ["另一种结构"]}', '[' + self.answer + ']')
        fields = ('title', 'summary', 'body')
        for decoration in decorations:
            for output in (decoration + '\n' + self.answer, self.answer + '\n注：' + decoration):
                with self.subTest(decoration=decoration, output=output):
                    self.assertEqual(self.payload, json_output.object_payload(output, required_fields=fields))
                    self.assertEqual(self.payload, notes.note_payload(output))
        for template in ('{"title": "", "summary": "", "body": ""}',
                         '{"title": null, "summary": null, "body": null}'):
            with self.subTest(template=template), self.assertRaises(model_response.OutputError):
                notes.note_payload(self.answer + '\n' + template)
        source, chinese = 'Verify every generated change independently before use.', '使用前独立核验每一项生成的修改。'
        answer = json.dumps({'translations': [chinese]}, ensure_ascii=False)
        output = json.dumps({'segments': [source]}) + '\n' + answer + '\n注：[1]；遗漏：[]'
        self.assertEqual(chinese, translation.render(source, output))
        self.assertEqual(chinese, translation.unit_output(source, 0, output))
        with self.assertRaises(model_response.OutputError):
            translation.render(source, answer + '\n{"translations": []}')

    def test_note_and_both_translation_consumers_reject_same_shape_drafts(self):
        source = 'Verify every generated change independently before use.'
        answer = json.dumps({'translations': ['使用前独立核验每一项生成的修改。']}, ensure_ascii=False)
        draft = json.dumps({'translations': ['这是另一份未经确认的备选译文。']}, ensure_ascii=False)
        for first, second in ((self.answer, self.draft), (self.draft, self.answer)):
            with self.subTest(first=first), self.assertRaises(model_response.OutputError):
                notes.note_payload(first + '\n' + second)
        for first, second in ((answer, draft), (draft, answer)):
            for consumer in (translation.render, lambda text, output: translation.unit_output(text, 0, output)):
                with self.subTest(first=first, consumer=consumer), self.assertRaises(model_response.OutputError):
                    consumer(source, first + '\n' + second)


class UniqueObjectPublication(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()
        self.row = {'video_id': self.entry_value['video_id'], 'entry': self.entry_value, 'channel': '测试频道'}
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')
        self.transcript = self.complete_transcript()
        self.deadline = time.monotonic() + 30
        self.payload = {'title': '智能体如何自检生成的代码', 'summary': '使用前核验结果。',
                        'body': '## 具体方法\n每次修改都需要验证。'}
        self.answer = json.dumps(self.payload, ensure_ascii=False)
        self.draft = json.dumps({'title': '草稿标题', 'summary': '另一份中文摘要。',
                                 'body': '这是先写的一版草稿，下面才是正式版。'}, ensure_ascii=False)
        self.mode = 'success'
        self.prompts = []
        self.wrappers = {'translation': ('', ''), 'note': ('', '')}

    def reply(self, prompt, *arguments):
        self.prompts.append(prompt)
        if prompt.startswith('将以下字幕'):
            value = response(prompt, '作者说明智能体生成代码后，需要独立核验结果和局限。')
            output = value + '\n' + value if self.mode == 'translation' else value
            prefix, suffix = self.wrappers['translation']
        elif '只输出 JSON' in prompt:
            output = self.answer + '\n' + self.draft if self.mode == 'note' else self.answer
            prefix, suffix = self.wrappers['note']
        else:
            return '作者说明智能体生成代码后，需要独立核验结果和局限。'
        return prefix + output + suffix

    def build(self):
        with patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=self.reply):
            return notes.build(self.row, self.transcript, self.deadline, lambda: False)

    def test_cached_ambiguous_note_is_revalidated_before_it_can_be_returned(self):
        first = self.build()
        path = media.cache_dir(self.row['video_id']) / 'notes-cache.json'
        cache = media.read_json(path)
        key, = [key for key, value in cache.items() if value == self.answer]
        cache[key] = self.answer + '\n' + self.draft
        media.save_json(path, cache)
        self.prompts.clear()
        self.assertEqual(first, self.build())
        self.assertEqual(1, len(self.prompts))
        self.assertIn('只输出 JSON', self.prompts[0])
        self.assertEqual(self.answer, media.read_json(path)[key])

    def test_ambiguous_note_spends_only_the_note_budget_and_never_caches_a_draft(self):
        self.mode = 'note'
        directory = media.cache_dir(self.row['video_id'])
        path = directory / 'notes-language-failures.json'
        for attempt in range(2):
            with self.assertRaises(model_response.OutputError):
                self.build()
            self.assertEqual([attempt + 1], list(media.read_json(path).values()))
            self.assertNotIn(self.draft, media.read_json(directory / 'notes-cache.json').values())
        result = self.build()
        self.assertEqual('youtube:translation-fallback', result['body_provenance'])
        self.assertNotIn('草稿标题', json.dumps(result, ensure_ascii=False))
        self.assertEqual([3], list(media.read_json(path).values()))
        count = len(self.prompts)
        self.assertEqual(result, self.build())
        self.assertEqual(count, len(self.prompts))

    def test_batch_and_unit_ambiguity_require_retry_before_accepting_translation(self):
        self.mode = 'translation'
        with self.assertRaises(model_response.OutputError):
            self.build()
        self.assertEqual(2, len(self.prompts))
        directory = media.cache_dir(self.row['video_id'])
        self.assertFalse(media.read_json(directory / 'notes-cache.json'))
        self.assertEqual([1], list(media.read_json(directory / 'notes-language-failures.json').values()))
        self.mode = 'success'
        result = self.build()
        self.assertEqual(self.payload['title'], result['title'])
        self.assertNotIn('fallback', result['body_provenance'])
        self.assertEqual({}, media.read_json(directory / 'notes-language-failures.json'))

    def test_neutral_wrappers_succeed_without_spending_any_stage_budget(self):
        self.wrappers = {
            'translation': ('以下是按要求的格式输出的译文：\n',
                            '\nLet me know if you would like any changes.\n注：[1]；遗漏：[]'),
            'note': ('以下是视频中示例代码部分整理后的笔记：\n',
                     '\n以上是整理好的笔记，如需调整请告诉我。')}
        first = self.build()
        self.assertEqual(self.payload['title'], first['title'])
        self.assertNotIn('fallback', first['body_provenance'])
        self.assertEqual(3, len(self.prompts))
        for _attempt in range(3):
            self.assertEqual(first, self.build())
        self.assertEqual(3, len(self.prompts))
        directory = media.cache_dir(self.row['video_id'])
        self.assertFalse((directory / 'notes-language-failures.json').exists())
        self.assertNotIn('Let me know', first['body'])
        self.assertNotIn('如需调整', first['body'])

    def test_old_neutral_wrapped_caches_survive_revalidation_and_fallback(self):
        first = self.build()
        directory = media.cache_dir(self.row['video_id'])
        path = directory / 'notes-cache.json'
        cache = media.read_json(path)
        note_key, = [key for key, value in cache.items() if value == self.answer]
        translations = {key: value + '\nI hope this will be helpful!\n注：[1]；遗漏：[]'
                        for key, value in cache.items() if '"translations"' in value}
        self.assertTrue(translations)
        cache.update(translations)
        cache[note_key] += '\n以上就是整理好的笔记。'
        media.save_json(path, cache)
        self.prompts.clear()
        self.assertEqual(first, self.build())
        self.assertEqual([], self.prompts)
        self.assertEqual(cache, media.read_json(path))
        cache.pop(note_key)
        media.save_json(path, cache)
        self.mode = 'note'
        for _attempt in range(2):
            with self.assertRaises(model_response.OutputError):
                self.build()
        result = self.build()
        self.assertEqual('youtube:translation-fallback', result['body_provenance'])
        self.assertIn('作者说明智能体生成代码后，需要独立核验结果和局限。', result['body'])
        for key, value in translations.items():
            self.assertEqual(value, media.read_json(path)[key])
        self.assertEqual(3, len(self.prompts))
        self.assertEqual(result, self.build())
        self.assertEqual(3, len(self.prompts))

    def test_collector_publishes_neutral_wrapped_results_without_downgrading(self):
        self.wrappers = {
            'translation': ('Here is the translation in JSON:\n',
                            '\nLet me know if you would like any changes.\n注：[1]'),
            'note': ('以下是按要求的格式输出的笔记：\n',
                     '\n以上是翻译结果，如有需要请告诉我。\n（无遗漏段落：[]）')}
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=self.reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            result = collector.collect(FetchScope(force=True))
            self.assertEqual((1, 1), (result['processed'], result['added']))
            for _attempt in range(3):
                collector.collect(FetchScope(force=True))
                row, = storage.rows()
                self.assertEqual('ready', row['state'])
                self.assertTrue(products.valid(row))
                self.assertNotIn('fallback', json.loads(row['note'])['body_provenance'])
            self.assertEqual(3, len(self.prompts))
            self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
            filename, = (self.root / 'vault' / 'source' / 'youtube').rglob('*.md')
            body = filename.read_text()
            self.assertIn(self.payload['body'], body)
            self.assertNotIn('Let me know', body)
            self.assertNotIn('如有需要', body)
            directory = media.cache_dir(self.row['video_id'])
            self.assertFalse((directory / 'notes-language-failures.json').exists())

    def test_collector_never_publishes_a_trailing_draft_and_recovers_next_round(self):
        self.mode = 'note'
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=self.selected), \
                patch.object(notes, 'call', side_effect=self.reply), \
                patch.object(media, 'probe', return_value={'duration': 600}), \
                patch.object(media, 'obtain', return_value=self.transcript):
            result = collector.collect(FetchScope(force=True))
            self.assertEqual((0, 0), (result['processed'], result['added']))
            row, = storage.rows()
            self.assertEqual('failed', row['state'])
            self.assertFalse(row['note'])
            self.assertFalse(collector.YouTubeCollector('youtube').materialize())
            self.mode = 'success'
            result = collector.collect(FetchScope(force=True))
            self.assertEqual((1, 1), (result['processed'], result['added']))
            row, = storage.rows()
            self.assertEqual('ready', row['state'])
            self.assertTrue(products.valid(row))
            self.assertEqual(self.payload['title'], json.loads(row['note'])['title'])
            self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
            filename, = (self.root / 'vault' / 'source' / 'youtube').rglob('*.md')
            self.assertIn(self.payload['body'], filename.read_text())
            self.assertNotIn('草稿标题', filename.read_text())
