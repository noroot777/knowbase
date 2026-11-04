"""旧格式译文(`translate-coverage-v1` 那种 `{"translations": [...]}`)的验收口径。

87cc380 / e93e29c 之后新译文走 `fulltext`,这套口径**只剩一处用场**:`translation_cache.plan`
拿它校验磁盘上的旧缓存,过了才复用、不过就重译 —— 所以它判宽了,旧缓存里的英文回显会被当成
译文直接发出去。经由 `notes.translate` / 小段兜底 / 摘要与成稿那几段的断言随那些代码一起删了。
"""
import json

from kb.feeds.registry import text_language
from kb.youtube import json_output, model_response, notes, transcripts, translation, translation_language
from tests.youtube_fixture import YouTubeFixture


CODE = ('npm install --save-dev vitest && npx vitest run --coverage . '
        'export API_BASE_URL=https://api.example.test/v2/items?page=1 ; '
        'git rebase --onto main feature~3 feature && git push --force-with-lease . ')


class TranslationOutput(YouTubeFixture):
    def test_inline_result_wrappers_do_not_reintroduce_format_promises(self):
        answer = json.dumps({'translations': ['作者要求独立核验。']}, ensure_ascii=False)
        for output in ('好的，以下是译文：' + answer, answer + '  （以上为译文）',
                       '好的，以下是译文：' + answer + '（以上为译文）',
                       answer + '\n以上是翻译结果，如有需要请告诉我。',
                       answer + '\nLet me know if you would like any changes.',
                       answer + '\nI hope this will be helpful!', answer + '\n注：要点 [1]；遗漏：[]'):
            with self.subTest(output=output):
                self.assertEqual('作者要求独立核验。', translation.render('Verify every change.', output))
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

    def test_term_lists_and_source_bound_quotes_survive_validation(self):
        source = ('We compare GPT 4o, Claude 3.5 Sonnet, Gemini 1.5 Pro, Llama 3.1, Mistral Large, '
                  'Command R, DeepSeek V2, Qwen 2.5, Phi 3 and Yi 34B on the same benchmark.')
        value = ('我们在同一套基准上对比 GPT 4o、Claude 3.5 Sonnet、Gemini 1.5 Pro、Llama 3.1、'
                 'Mistral Large、Command R、DeepSeek V2、Qwen 2.5、Phi 3 和 Yi 34B。')
        for separator in ('、', '；', '，', ', ', '; ', '（说明）'):
            expected = value.replace('、', separator)
            output = json.dumps({'translations': [expected]})
            with self.subTest(separator=separator):
                self.assertEqual(expected, translation.render(source, output))
        quote = 'the server must respond with a valid JSON object containing the field name and the field value'
        source = ('The author describes the implementation constraints, the deployment plan, the rollout schedule, '
                  'and the independent verification process. The spec says: "' + quote + '". '
                  'The team measures latency and records every failure, then verifies the result '
                  'against the published requirements.')
        value = '作者说明实现约束、部署计划与独立核验流程。规范原文写的是：“' + quote + '”。团队记录延迟与失败并核对要求。'
        self.assertEqual(value, translation.render(source, json.dumps({'translations': [value]})))
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
        source = '作者说明应当核验结果。' * 20 + 'Today we build an agent from scratch and verify every generated change before use.'
        self.assertEqual('zh', text_language(source))
        self.assertFalse(notes.native_chinese(source))
        translated = '作者说明应当核验结果。' * 20 + '今天从零构建智能体，并在使用前核验每一项生成的修改。'
        self.assertEqual(translated, translation.render(source, json.dumps({'translations': [translated]})))
        with self.assertRaises(translation.LanguageError):
            translation.render(source, json.dumps({'translations': [source]}))
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

    def test_lowercase_source_bound_names_are_not_rejected_as_long_english_prose(self):
        names = ['npm', 'pnpm', 'yarn', 'bun', 'vite', 'esbuild', 'rollup', 'webpack', 'swc',
                 'turbopack', 'parcel', 'ffmpeg']
        for count in (10, 11, 12):
            source = 'The toolchain covers ' + ', '.join(names[:count]) + ', each with different caching behavior.'
            value = '工具链覆盖 ' + '、'.join(names[:count]) + '，每一个的缓存处理都不一样。'
            output = json.dumps({'translations': [value]})
            with self.subTest(count=count):
                self.assertEqual(value, translation.render(source, output))
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
                blocks = [unit['text'] for unit in translation.units(source)]
                contexts = translation_language.contexts(source, blocks, quoted=True)
                values = ['作者独立核验握手规范并对比部署失败。' + ''.join(context) for context in contexts]
                rendered = translation.render(source, json.dumps({'translations': values}))
                self.assertIn(name, rendered)
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
                self.assertEqual('作者介绍技术并要求核验。',
                                 translation.render(source, json.dumps({'translations': ['作者介绍技术并要求核验。']})))
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
        incomplete = json.dumps({'translations': ['作者介绍智能体框架。', *blocks[1:]]})
        with self.assertRaises(RuntimeError):
            translation.render(source, incomplete)
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
