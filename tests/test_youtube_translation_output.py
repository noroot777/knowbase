import json
from types import SimpleNamespace
from unittest.mock import patch

from kb.feeds.registry import text_language
from kb.youtube import json_output, media, model_response, notes, transcripts, translation, translation_language
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
                       '好的，以下是译文：' + answer + '（以上为译文）',
                       answer + '\n以上是翻译结果，如有需要请告诉我。',
                       answer + '\nLet me know if you would like any changes.',
                       answer + '\nI hope this will be helpful!', answer + '\n注：要点 [1]；遗漏：[]'):
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

    def test_technical_narrative_uses_source_bound_structure_before_the_shared_language_ratio(self):
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
        with self.assertRaises(model_response.OutputError):
            json_output.object_payload(wrapped, example)
        wrapped = '参考 {placeholder}\n实际结果：\n```json\n' + answer + '\n```\n说明 {结束}'
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
                self.assertEqual(expected, notes.summary_output(expected, source))
                payload = {'title': '模型基准比较', 'summary': expected, 'body': expected}
                self.assertEqual(payload, notes.note_payload(json.dumps(payload), source=source))
                with patch.object(notes, 'call', return_value=output) as call:
                    self.assertEqual(expected, notes.translate(source, self.selected, 10**10, lambda: False))
                call.assert_called_once()
        quote = 'the server must respond with a valid JSON object containing the field name and the field value'
        source = ('The author describes the implementation constraints, the deployment plan, the rollout schedule, '
                  'and the independent verification process. The spec says: "' + quote + '". '
                  'The team measures latency and records every failure, then verifies the result '
                  'against the published requirements.')
        value = '作者说明实现约束、部署计划与独立核验流程。规范原文写的是：“' + quote + '”。团队记录延迟与失败并核对要求。'
        self.assertEqual(value, translation.render(source, json.dumps({'translations': [value]})))
        self.assertEqual(value, translation.unit_output(source, 0, json.dumps({'translations': [value]})))
        with self.assertRaises(translation.LanguageError):
            translation.render('The author describes the server.', json.dumps({'translations': [value]}))
        source = 'The spec says: "' + (quote + ' ') * 12 + '".'
        values = [unit['text'].replace('The spec says:', '规范原文写的是：') for unit in translation.units(source)]
        self.assertGreater(len(values), 2)
        with self.assertRaises(translation.LanguageError):
            translation.render(source, json.dumps({'translations': values}))
        translated = ['规范要求服务器返回含字段名和字段值的有效对象。'] * len(values)
        rendered = translation.render(source, json.dumps({'translations': translated}))
        self.assertNotIn(quote, rendered)

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
                self.assertEqual(value, notes.summary_output(value, value, chinese_source=True))
                payload = {'title': value, 'summary': value, 'body': value}
                self.assertEqual(payload, notes.note_payload(json.dumps(payload)))
                self.assertEqual(payload, notes.note_payload(json.dumps(payload), source=value, chinese_source=True))
                bad = value + 'the remaining explanation is still entirely in English and must not be published'
                with self.assertRaises(RuntimeError):
                    notes.summary_output(bad, value, chinese_source=True)
                with self.assertRaises(RuntimeError):
                    notes.note_payload(json.dumps(dict(payload, body=bad)), source=value, chinese_source=True)

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
        self.assertFalse(notes.native_chinese(source))
        translated = '作者说明应当核验结果。' * 20 + '今天从零构建智能体，并在使用前核验每一项生成的修改。'
        with patch.object(notes, 'call', return_value=json.dumps({'translations': [translated]})) as call:
            self.assertEqual(translated, notes.translate(source, self.selected, 10**10, lambda: False))
            call.assert_called_once()
        with patch.object(notes, 'call', return_value=json.dumps({'translations': [source]})) as call, \
                self.assertRaises(translation.LanguageError):
            notes.translate(source, self.selected, 10**10, lambda: False)
        self.assertEqual(2, call.call_count)
        self.assertTrue(translation_language.valid('先完整核对所有参数 ' + ' '.join(['token'] * 10) + '，再独立验证后使用。', [], source))
        self.assertFalse(translation_language.valid('先完整核对所有参数 ' + ' '.join(['token'] * 11) + '，再独立验证后使用。', [], source))

    def test_comma_separated_english_with_a_chinese_opening_is_not_a_translation(self):
        tail = ('Then we move on to the second part, which covers the deployment story, '
                'and after that we look at the rollout, with the numbers from last quarter, '
                'and finally we close with the roadmap, which the team has already published, '
                'so you can read it yourself, if you want the full detail, and that is all for today.')
        source = 'Welcome back to the channel. ' + tail
        self.assertEqual(1, len(translation.units(source)))
        for value in ('欢迎回到本频道。' + tail, tail + '。好'):
            output = json.dumps({'translations': [value]})
            with self.subTest(value=value):
                self.assertEqual('en', text_language(value))
                with self.assertRaises(translation.LanguageError):
                    translation.render(source, output)
                with self.assertRaises(translation.LanguageError):
                    translation.unit_output(source, 0, output)
                with patch.object(notes, 'call', return_value=output) as call, \
                        self.assertRaises(translation.LanguageError):
                    notes.translate(source, self.selected, 10**10, lambda: False)
                self.assertEqual(2, call.call_count)

    def test_quotes_are_bounded_source_evidence_not_unlimited_translation_exemptions(self):
        quote = ('We are going to ship the new runtime next quarter and the migration guide will land '
                 'at the same time so that everyone can upgrade without downtime.')
        unmatched = ('He opened with "a quick note on the rollout before we start and then the team '
                     'walked through every number from last quarter without stopping once" and then moved on.')
        for source in ('"' + quote + '"', 'The spec says: "' + quote + '".', unmatched,
                       'The spec says: "' + quote * 3 + '".'):
            self.assertEqual([], translation_language.quote_spans(source))
            for prefix in ('', '好。'):
                output = json.dumps({'translations': [prefix + unit['text'] for unit in translation.units(source)]})
                with self.subTest(source=source, prefix=prefix), self.assertRaises(translation.LanguageError):
                    translation.render(source, output)
        quote = 'the server must respond with a valid JSON object containing the field name and the field value'
        source = ('The author describes implementation constraints and independent verification. ' * 4 +
                  'The spec says: "' + quote + '".')
        spans = translation_language.quote_spans(source)
        self.assertEqual([quote], [source[start:end] for start, end in spans])
        for value in ('"' + quote + '".', '说明：“' + quote + '”' + (' "' + quote + '"') * 4):
            with self.subTest(value=value):
                self.assertFalse(translation_language.valid(value, [], source))
                self.assertFalse(translation_language.chinese_output(value, source))
        value = '作者要求明确记录实现约束，并独立核验。规范写道：“' + quote + '”。'
        self.assertTrue(translation_language.valid(value, [], source))
        self.assertTrue(translation_language.chinese_output(value, source))
        source = source.replace('"' + quote + '"', '“' + quote + '”')
        self.assertEqual(1, len(translation_language.quote_spans(source)))
        self.assertTrue(translation_language.chinese_output(value, source))
        source = ('Reviewers independently verify implementation constraints before deployment ' * 5 +
                  'The spec says: "' + quote + '". ' +
                  'Independent reviewers record every failure and compare the results. ' * 3)
        blocks = [unit['text'] for unit in translation.units(source)]
        contexts = translation_language.contexts(source, blocks, quoted=True)
        self.assertEqual(2, sum(bool(context) for context in contexts))
        values = [('作者要求独立核验部署结果。' + ' '.join(context) if position == 0 else
                   ' '.join(context) + '作者要求独立核验部署结果。') for position, context in enumerate(contexts)]
        self.assertIn(quote, translation.render(source, json.dumps({'translations': values})))
        for position, value in enumerate(values):
            self.assertEqual(value, translation.unit_output(source, position, json.dumps({'translations': [value]})))

    def test_lowercase_source_bound_names_are_not_rejected_as_long_english_prose(self):
        names = ['npm', 'pnpm', 'yarn', 'bun', 'vite', 'esbuild', 'rollup', 'webpack', 'swc',
                 'turbopack', 'parcel', 'ffmpeg']
        for count in (10, 11, 12):
            source = 'The toolchain covers ' + ', '.join(names[:count]) + ', each with different caching behavior.'
            value = '工具链覆盖 ' + '、'.join(names[:count]) + '，每一个的缓存处理都不一样。'
            output = json.dumps({'translations': [value]})
            with self.subTest(count=count):
                self.assertEqual(value, translation.render(source, output))
                self.assertEqual(value, translation.unit_output(source, 0, output))
                self.assertEqual(value, notes.summary_output(value, source))
                payload = {'title': '工具链缓存策略', 'summary': value, 'body': value}
                self.assertEqual(payload, notes.note_payload(json.dumps(payload), source=source))
        for source, value in (
                ('Tools: ' + ', '.join(names[:11]), '工具包括 ' + '、'.join(names) + '，需要核验。'),
                ('Tools: ' + ', '.join(names), '工具包括 npm、' + '、'.join(names) + '，需要核验。'),
                ('Tools: ' + ', '.join(names[1:]), '工具包括 ' + '、'.join(names) + '，需要核验。'),
                ('Tools: ' + ', '.join(['a' * 65, *names]),
                 '工具包括 ' + '、'.join(['a' * 65, *names]) + '，需要核验。'),
                ('we move on now, then we review results, next we record failures, finally we ship changes',
                 '作者说明方法和限制，需要独立核验。' * 20 +
                 'we move on now, then we review results, next we record failures, finally we ship changes')):
            with self.subTest(source=source, value=value):
                self.assertFalse(translation_language.chinese_output(value, source))

    def test_bounded_quote_evidence_survives_compression_without_licensing_new_quotes(self):
        quote = 'the server must respond with a valid JSON-RPC object containing the field name and the field value'
        source = ('Reviewers independently verify implementation constraints before deployment. ' * 4 +
                  'The spec says: "' + quote + '".')
        quotes = translation_language.contexts(source, [source], quoted=True)[0]
        self.assertEqual([quote], quotes)
        value = '规范要求：“' + quote + '”。作者要求独立核验。'
        self.assertFalse(translation_language.chinese_output(value, value))
        self.assertTrue(translation_language.chinese_output(value, value, quotes))
        self.assertEqual(value, notes.summary_output(value, value, quotes))
        payload = {'title': '核验规范', 'summary': value, 'body': value}
        self.assertEqual(payload, notes.note_payload(json.dumps(payload), source=value, quotes=quotes))
        for original, output in (('作者要求独立核验。', value), (value, value + value),
                                 (value, value + '模型补充：“a new statement that was never in the source '
                                  'and contains far too many English words to pass”。')):
            with self.subTest(source=original, output=output):
                self.assertFalse(translation_language.chinese_output(output, original, quotes))

    def test_source_bound_flags_and_identifiers_never_swallow_surrounding_prose(self):
        source = 'Use packr add package_name --frozen before independently reviewing every generated change.'
        spans = [source[start:end] for start, end in translation_language.literal_spans(source)]
        self.assertEqual(['package_name', '--frozen'], spans)
        self.assertTrue(translation_language.valid('使用 packr add package_name --frozen 后独立核验所有生成的修改。',
                                                    spans, source))
        for value in (source, '好。' + source,
                      '好。package_name --frozen then we deploy, after that we record results, and move on.'):
            with self.subTest(value=value):
                self.assertFalse(translation_language.valid(value, spans, source))
        self.assertFalse(translation_language.chinese_output('好。' + 'invented_identifier_' * 10, source))

    def test_technical_literals_inside_a_bounded_quote_do_not_cancel_the_whole_quote(self):
        narrative = ('I spent the entire weekend reading through the specification and the reference implementation, '
                     'because the behaviour of the handshake never matched anything I expected from the earlier '
                     'release notes, and the wording in the document turned out to be the real answer here. '
                     'I also compared it against three other servers, re-read the changelog twice, and asked two '
                     'maintainers about it before I finally understood what was going on with that one paragraph. '
                     'The paragraph about error handling says: ')
        tail = ' and that one sentence explains every single failure we saw in production during migration week.'
        chinese = ('我整个周末都在读规范和参考实现，因为握手的行为和早先发布说明里的预期不同。'
                   '后来发现文档措辞才是真正的答案。我还对比了三个服务端、重读变更日志并询问维护者。错误处理段落写道：')
        for name in ('JSON-RPC', 'open-source', 'end-to-end', 'field_name', '--strict', 're-read',
                     '`field_name`', 'https://example.test/error'):
            quote = 'the server must always reply with a ' + name + ' error object and never close the socket'
            source = narrative + '"' + quote + '"' + tail
            value = chinese + '“' + quote + '”，这解释了迁移时看到的失败。'
            with self.subTest(name=name):
                self.assertEqual([quote], translation_language.contexts(source, [source], quoted=True)[0])
                literals = translation_language.contexts(source, [source])[0]
                self.assertEqual(['re-read'], literals)
                self.assertTrue(translation_language.valid(value, literals, source))
                self.assertEqual(value, notes.summary_output(value, source))
                payload = {'title': '协议错误核验', 'summary': value, 'body': value}
                self.assertEqual(payload, notes.note_payload(json.dumps(payload), source=source))
                blocks = [unit['text'] for unit in translation.units(source)]
                contexts = translation_language.contexts(source, blocks, quoted=True)
                values = ['作者独立核验握手规范并对比部署失败。' + ''.join(context) for context in contexts]
                rendered = translation.render(source, json.dumps({'translations': values}))
                self.assertIn(name, rendered)
                for position, translated in enumerate(values):
                    self.assertEqual(translated, translation.unit_output(source, position,
                                                                        json.dumps({'translations': [translated]})))
        quote = 'the protocol error details are described in https://example.test/error'
        source = narrative + '「' + quote + '」' + tail
        self.assertEqual([quote], translation_language.contexts(source, [source], quoted=True)[0])
        self.assertTrue(translation_language.chinese_output('规范写道：「' + quote + '」。仍需独立核验。', source))
        fenced = '```json\n{"example": "' + quote + '"}\n```'
        self.assertEqual([], translation_language.quote_spans(fenced))

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
