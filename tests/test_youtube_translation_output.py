import json
from types import SimpleNamespace
from unittest.mock import patch

from kb.feeds.registry import text_language
from kb.youtube import json_output, media, notes, transcripts, translation, translation_language
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

    def test_term_lists_and_source_bound_quotes_survive_batch_and_unit_validation(self):
        source = ('We compare GPT 4o, Claude 3.5 Sonnet, Gemini 1.5 Pro, Llama 3.1, Mistral Large, '
                  'Command R, DeepSeek V2, Qwen 2.5, Phi 3 and Yi 34B on the same benchmark.')
        value = ('我们在同一套基准上对比 GPT 4o、Claude 3.5 Sonnet、Gemini 1.5 Pro、Llama 3.1、'
                 'Mistral Large、Command R、DeepSeek V2、Qwen 2.5、Phi 3 和 Yi 34B。')
        for separator in ('、', '；', '，', ', ', '; ', '（说明）'):
            expected = value.replace('、', separator)
            output = json.dumps({'translations': [expected]})
            with self.subTest(separator=separator):
                self.assertEqual(expected, translation.render(source, output))
                self.assertEqual(expected, translation.unit_output(source, 0, output))
                with patch.object(notes, 'call', return_value=output) as call:
                    self.assertEqual(expected, notes.translate(source, self.selected, 10**10, lambda: False))
                call.assert_called_once()
        quote = 'the server must respond with a valid JSON object containing the field name and the field value'
        source = 'The spec says: "' + quote + '".'
        value = '规范原文写的是：“' + quote + '”。'
        self.assertEqual(value, translation.render(source, json.dumps({'translations': [value]})))
        with self.assertRaises(translation.LanguageError):
            translation.render('The author describes the server.', json.dumps({'translations': [value]}))
        source = 'The spec says: "' + (quote + ' ') * 12 + '".'
        values = [unit['text'].replace('The spec says:', '规范原文写的是：') for unit in translation.units(source)]
        self.assertGreater(len(values), 2)
        rendered = translation.render(source, json.dumps({'translations': values}))
        self.assertIn((quote + ' ') * 10, rendered)

    def test_translated_names_do_not_share_the_native_input_script_gate(self):
        for name in ('ソフトバンク', '삼성전자', 'ｿﾌﾄﾊﾞﾝｸ'):
            source = '今日は' + name + 'の投資について説明します。今後の方法を検証します。'
            value = '作者介绍' + name + '的投资，并要求独立核验结果。'
            output = json.dumps({'translations': [value]})
            with self.subTest(name=name):
                self.assertFalse(notes.native_chinese(source))
                self.assertEqual(value, translation.render(source, output))
                self.assertEqual(value, translation.unit_output(source, 0, output))
                self.assertEqual(value, notes.summary_output(value))
                payload = {'title': value, 'summary': value, 'body': value}
                self.assertEqual(payload, notes.note_payload(json.dumps(payload)))

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

    def test_chinese_padding_and_partial_translation_do_not_hide_foreign_prose(self):
        source = ('Welcome back to the channel. Today we build an agent from scratch. '
                  'You will need node and a terminal. The planner decomposes every goal into steps '
                  'and the executor verifies each result before proceeding to the next step. ' * 5).strip()
        blocks = [unit['text'] for unit in translation.units(source)]
        for transform in (lambda block: '好。' + block, lambda block: block + '完',
                          lambda block: '欢迎回来。' + block.partition('.')[2]):
            values = [transform(block) for block in blocks]
            with self.subTest(value=values[0]):
                with self.assertRaises(RuntimeError):
                    translation.render(source, json.dumps({'translations': values}))
                for position, value in enumerate(values):
                    with self.assertRaises(RuntimeError):
                        translation.unit_output(source, position, json.dumps({'translations': [value]}))
                def reply(prompt, *arguments):
                    segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
                    return json.dumps({'translations': [transform(block) for block in segments]})
                with patch.object(notes, 'call', side_effect=reply), self.assertRaises(RuntimeError):
                    notes.translate(source, self.selected, 10**10, lambda: False)
        source = '作者说明应当核验结果。' * 20 + 'Today we build an agent from scratch and verify every generated change before use.'
        self.assertEqual('zh', text_language(source))
        with patch.object(notes, 'call') as call:
            self.assertEqual(source, notes.translate(source, self.selected, 10**10, lambda: False))
            call.assert_not_called()
        self.assertTrue(translation_language.valid('先核对 ' + ' '.join(['token'] * 10) + '，再使用。', [], source))
        self.assertFalse(translation_language.valid('先核对 ' + ' '.join(['token'] * 11) + '，再使用。', [], source))

    def test_non_chinese_cjk_narrative_must_be_translated_not_echoed(self):
        sources = ('今日は新しい技術を紹介します。状態管理と描画を分離するのが基本の考え方です。',
                   '오늘은 人工智能 技術과 狀態管理를 설명합니다. 실제로 확인해 보겠습니다.',
                   'ｱｲｳｴｵ 人工知能の実装を紹介します。')
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual('zh', text_language(source))
                self.assertFalse(notes.native_chinese(source))
                for value in (source, '作者介绍：' + source):
                    output = json.dumps({'translations': [value]})
                    with self.assertRaises(RuntimeError):
                        translation.render(source, output)
                    with self.assertRaises(RuntimeError):
                        translation.unit_output(source, 0, output)
                with patch.object(notes, 'call', return_value=json.dumps({'translations': ['作者介绍技术并要求核验。']})) as call:
                    self.assertEqual('作者介绍技术并要求核验。', notes.translate(source, self.selected, 10**10, lambda: False))
                    call.assert_called_once()
        literal = 'https://example.test/人工知能/アイウエオ'
        self.assertTrue(notes.native_chinese('作者给出原始地址：' + literal))
        self.assertTrue(notes.native_chinese('作者说明状态管理・渲染分离。'))
        self.assertEqual(literal, translation.render(literal, json.dumps({'translations': [literal]})))

    def test_cleaned_single_line_captions_do_not_turn_command_tails_into_literals(self):
        lines = ['Welcome back to the channel. Today we examine an AI agent framework.',
                 'First run npm install to get the deps.',
                 'The planner decomposes the goal into steps and each step gets its own budget.',
                 'The executor runs those steps and checks the result before proceeding.',
                 'Every generated change requires independent verification before use.'] * 6
        segments = transcripts.parse(json.dumps({'segments': [
            {'start': position * 20, 'end': (position + 1) * 20, 'text': line + '\n'}
            for position, line in enumerate(lines)]}), '.json')
        source, = transcripts.blocks(segments)
        self.assertNotIn('\n', source)
        blocks = [unit['text'] for unit in translation.units(source)]
        self.assertGreaterEqual(len(blocks), 5)
        for position, block in enumerate(blocks):
            with self.subTest(position=position), self.assertRaises(RuntimeError):
                translation.unit_output(source, position, json.dumps({'translations': [block]}))
        incomplete = json.dumps({'translations': ['作者介绍智能体框架。', *blocks[1:]]})
        with self.assertRaises(RuntimeError):
            translation.render(source, incomplete)
        with patch.object(notes, 'call', return_value=incomplete), self.assertRaises(RuntimeError):
            notes.translate(source, self.selected, 10**10, lambda: False)
        spans = translation_language.literal_spans(source)
        self.assertTrue(spans)
        self.assertTrue(all('The planner' not in source[start:end] for start, end in spans))

    def test_commands_end_before_prose_even_without_caption_newlines(self):
        for source in ('npm install to get the deps and then verify every generated change',
                       'npm install packages verify requirements independently without running unsafe commands',
                       'npm install. Verify every generated change before use.',
                       'First run npm install. Review 测试 results independently.'):
            with self.subTest(source=source), self.assertRaises(RuntimeError):
                translation.render(source, json.dumps({'translations': [source]}))
        command = 'npm install --save-dev vitest && npx vitest run --coverage'
        tail = ('The planner checks every generated change before using it. ' * 12).strip()
        source = command + '. ' + tail
        translated = [unit['text'].replace('The planner checks every generated change before using it.',
                                          '规划器在使用前核验每次生成的修改。')
                      for unit in translation.units(source)]
        rendered = translation.render(source, json.dumps({'translations': translated}))
        self.assertIn(command, rendered)
        self.assertIn('\n\n', rendered)
        self.assertNotIn('The planner', rendered)

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
