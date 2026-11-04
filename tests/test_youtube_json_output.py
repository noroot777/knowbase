import json
import unittest

from kb.youtube import json_output, model_response, translation


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
        source, chinese = 'Verify every generated change independently before use.', '使用前独立核验每一项生成的修改。'
        answer = json.dumps({'translations': [chinese]}, ensure_ascii=False)
        for suffix in suffixes:
            with self.subTest(suffix=suffix):
                output = answer + '\n' + suffix
                self.assertEqual(chinese, translation.render(source, output))

    def test_expected_fields_ignore_non_results_but_not_same_shape_templates(self):
        decorations = ('[1]', '[]', '{"footnotes": [1]}', '{"segments": ["input"]}',
                       '{"translations": ["另一种结构"]}', '[' + self.answer + ']')
        fields = ('title', 'summary', 'body')
        for decoration in decorations:
            for output in (decoration + '\n' + self.answer, self.answer + '\n注：' + decoration):
                with self.subTest(decoration=decoration, output=output):
                    self.assertEqual(self.payload, json_output.object_payload(output, required_fields=fields))
        for template in ('{"title": "", "summary": "", "body": ""}',
                         '{"title": null, "summary": null, "body": null}'):
            with self.subTest(template=template), self.assertRaises(model_response.OutputError):
                json_output.object_payload(self.answer + '\n' + template, required_fields=fields)
        source, chinese = 'Verify every generated change independently before use.', '使用前独立核验每一项生成的修改。'
        answer = json.dumps({'translations': [chinese]}, ensure_ascii=False)
        output = json.dumps({'segments': [source]}) + '\n' + answer + '\n注：[1]；遗漏：[]'
        self.assertEqual(chinese, translation.render(source, output))
        with self.assertRaises(model_response.OutputError):
            translation.render(source, answer + '\n{"translations": []}')

    def test_translation_consumer_rejects_same_shape_drafts(self):
        source = 'Verify every generated change independently before use.'
        answer = json.dumps({'translations': ['使用前独立核验每一项生成的修改。']}, ensure_ascii=False)
        draft = json.dumps({'translations': ['这是另一份未经确认的备选译文。']}, ensure_ascii=False)
        for first, second in ((answer, draft), (draft, answer)):
            with self.subTest(first=first), self.assertRaises(model_response.OutputError):
                translation.render(source, first + '\n' + second)
