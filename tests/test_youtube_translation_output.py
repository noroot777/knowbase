import json
from types import SimpleNamespace
from unittest.mock import patch

from kb.feeds.registry import text_language
from kb.youtube import json_output, media, notes, translation
from tests.youtube_fixture import YouTubeFixture


CODE = ('npm install --save-dev vitest && npx vitest run --coverage . '
        'export API_BASE_URL=https://api.example.test/v2/items?page=1 ; '
        'git rebase --onto main feature~3 feature && git push --force-with-lease . ')


class TranslationOutput(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.selected = SimpleNamespace(provider='fixture', model_id='fixture')

    def test_inline_result_wrappers_do_not_reintroduce_format_promises(self):
        answer = json.dumps({'translations': ['作者要求独立核验。']}, ensure_ascii=False)
        for output in ('好的，以下是译文：' + answer, answer + '  （以上为译文）',
                       '好的，以下是译文：' + answer + '（以上为译文）'):
            with self.subTest(output=output):
                self.assertEqual('作者要求独立核验。', translation.render('Verify every change.', output))
        note = {'title': '独立核验', 'summary': '核验改动。', 'body': '先核验，再使用。'}
        self.assertEqual(note, notes.note_payload('以下是笔记：' + json.dumps(note) + '（以上为笔记）'))
        for output in ('我会按 ' + answer + ' 的格式输出。', 'I will output ' + answer,
                       '以下是结果：[' + answer + ']'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                json_output.object_payload(output)
        with self.assertRaises(ValueError):
            json_output.object_payload('以下是结果：' + answer, answer)

    def test_technical_narrative_is_not_blocked_by_a_chinese_character_ratio(self):
        cases = (
            ('okay so first you do pip install -U yt-dlp and then pip install -r requirements.txt '
             'after that uv sync --frozen and uv run pytest -q, and finally pnpm add drizzle-orm '
             'postgres drizzle-kit, then pnpm dlx drizzle-kit generate and pnpm dlx drizzle-kit push',
             '好，首先执行 pip install -U yt-dlp，然后 pip install -r requirements.txt，'
             '接着 uv sync --frozen 和 uv run pytest -q，最后 pnpm add drizzle-orm postgres '
             'drizzle-kit，再执行 pnpm dlx drizzle-kit generate 和 pnpm dlx drizzle-kit push'),
            ('the repo is github.com/anthropics/claude-code and the docs live at '
             'docs.anthropic.com/en/docs/claude-code/overview, the registry is '
             'registry.npmjs.org/@anthropic-ai/claude-code and mirrors sit on jsdelivr and unpkg',
             '仓库在 github.com/anthropics/claude-code，文档在 '
             'docs.anthropic.com/en/docs/claude-code/overview，'
             '注册表是 registry.npmjs.org/@anthropic-ai/claude-code，镜像放在 jsdelivr 和 unpkg'))
        for source, value in cases:
            output = json.dumps({'translations': [value]}, ensure_ascii=False)
            with self.subTest(source=source):
                self.assertEqual('en', text_language(value))
                self.assertEqual(value, translation.render(source, output))
                self.assertEqual(value, translation.unit_output(source, 0, output))
                with patch.object(notes, 'call', return_value=output) as call:
                    self.assertEqual(value, notes.translate(source, self.selected, 10**10, lambda: False))
                self.assertEqual(1, call.call_count)

    def test_only_independent_result_objects_are_extracted_and_prompt_echoes_are_rejected(self):
        example = json.dumps({'translations': ['第一段译文', '第二段译文']}, ensure_ascii=False)
        answer = json.dumps({'translations': ['作者要求逐项验证。', '最后核验结果。']}, ensure_ascii=False)
        for output in ('好的，我会按 ' + example + ' 的格式输出。', '[' + example + ']'):
            with self.subTest(output=output), self.assertRaises(ValueError):
                json_output.object_payload(output)
        with self.assertRaises(ValueError):
            json_output.object_payload(example, '格式示例：\n' + example)
        wrapped = '参考 {placeholder}\n' + example + '\n实际结果：\n```json\n' + answer + '\n```\n说明 {结束}'
        self.assertEqual(json.loads(answer), json_output.object_payload(wrapped, example))
        note = json.dumps({'title': '示例标题', 'summary': '示例摘要', 'body': '示例正文'}, ensure_ascii=False)
        with self.assertRaisesRegex(RuntimeError, '格式'):
            notes.note_payload(note, '示例：\n' + note)

    def test_translation_prompt_has_no_parseable_answer_template(self):
        source = 'Verify every generated change before use. ' * 18
        self.assertEqual(2, len(translation.units(source)))
        instructions = translation.prompt(source).split('\n\n', 1)[0]
        self.assertNotIn('{', instructions)
        self.assertNotIn('第一段译文', instructions)
        echo = '好的，我会按 {"translations":["第一段译文","第二段译文"]} 的格式输出。'
        with patch.object(notes, 'call', return_value=echo) as call, self.assertRaises(RuntimeError):
            notes.translate(source, self.selected, 10**10, lambda: False)
        self.assertEqual(2, call.call_count)

    def test_source_bound_code_urls_and_commands_do_not_dilute_translated_narrative(self):
        narrative = 'The author explains how to verify the command before using it.'
        source = narrative + '\n\n' + CODE * 10
        translated = [unit['text'].replace(narrative, '作者要求在使用命令前核验。')
                      for unit in translation.units(source)]
        output = json.dumps({'translations': translated})
        self.assertEqual('en', text_language('\n\n'.join(translated)))
        self.assertIn(CODE.split(' && ')[0], translation.render(source, output))
        pure = json.dumps({'translations': [unit['text'] for unit in translation.units(CODE * 10)]})
        self.assertEqual(len(translation.units(CODE * 10)), len(translation.values(CODE * 10, pure)))
        self.assertIn('git push --force-with-lease', translation.render(CODE * 10, pure))
        for source in ('https://example.test/tools/install', 'python -m unittest tests.test_youtube_notes',
                       '```python\nresult = build_agent("demo")\nprint(result)\n```', 'result = build_agent("demo")'):
            with self.subTest(source=source):
                self.assertEqual(source, translation.render(source, json.dumps({'translations': [source]})))

    def test_untranslated_narrative_and_foreign_literals_are_not_exempted(self):
        for source, value in (('Review every generated change before use.', 'Review every generated change before use.'),
                              ('Read https://example.test before use.', 'Read https://example.test before use.'),
                              ('https://example.test/install', 'https://unrelated.test/other')):
            with self.subTest(source=source), self.assertRaises(RuntimeError):
                translation.render(source, json.dumps({'translations': [value]}))

    def test_valid_narrative_and_literal_unit_caches_survive_another_unit_failure(self):
        first = ('Review every generated change before use. ' * 8).strip()
        last = ('Check the generated result independently. ' * 8).strip()
        literal = 'npm install --save-dev ' + 'package_' * 50
        source = first + '\n\n' + literal + '\n\n' + last
        expected = [unit['text'] for unit in translation.units(source)]
        self.assertEqual([first, literal, last], expected)
        requested, cache = [], {}
        broken = True
        directory = media.cache_dir('00000000001')

        def reply(prompt, *arguments):
            segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
            requested.append(segments)
            if len(segments) > 1:
                values = []
            elif segments[0] == expected[1] or broken and segments[0] == last:
                values = segments
            else:
                values = ['作者要求独立核验所有生成的改动。']
            return json.dumps({'translations': values})

        with patch.object(notes, 'call', side_effect=reply):
            for attempt in range(2):
                with self.subTest(attempt=attempt), self.assertRaises(RuntimeError):
                    notes.translation_request(source, self.selected, 10**10, lambda: False, directory, cache)
                self.assertEqual({notes.signature('translate-unit-v1', self.selected, unit) for unit in expected[:2]},
                                 set(cache))
            broken = False
            output = notes.translation_request(source, self.selected, 10**10, lambda: False, directory, cache)
        self.assertIn(expected[1], translation.render(source, output))
        self.assertEqual([expected, *[[unit] for unit in expected], expected, [last], expected, [last]], requested)
        self.assertEqual(3, len(cache))
