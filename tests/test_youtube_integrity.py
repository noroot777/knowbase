import json
import os
import time
from types import SimpleNamespace
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage, transcripts, translation
from tests.youtube_fixture import YouTubeFixture
from tests.youtube_translation_fixture import response


class YouTubeIntegrity(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.isolate()
        self.source = self.channel()
        self.entry_value = self.entry()
        self.row = {'video_id': self.entry_value['video_id'], 'entry': self.entry_value}
        self.metadata = {'id': self.row['video_id'], 'duration': 600, 'language': 'en'}
        self.complete = [{'start': 0, 'end': 300, 'text': 'The AI agent generates code and runs tests.'},
                         {'start': 300, 'end': 600, 'text': 'Always verify the result and its limitations.'}]

    def test_tail_only_middle_holes_sparse_and_overlap_not_complete(self):
        for periods in ([(590, 600)], [(0, 100), (500, 600)],
                        [(position, position + 5) for position in range(0, 600, 60)],
                        [(0, 100)] * 8 + [(590, 600)]):
            segments = [{'start': start, 'end': end, 'text': 'Thank you for watching this AI tutorial.'}
                        for start, end in periods]
            with self.subTest(periods=periods), self.assertRaises(ValueError):
                transcripts.validate(segments, 600)
        self.assertEqual(self.complete, transcripts.validate(self.complete, 600))
        speech = [{'start': start, 'end': end, 'text': 'Readable speech with natural short pauses.'}
                  for start, end in [(0, 200), (220, 400), (410, 590)]]
        self.assertEqual(speech, transcripts.validate(speech, 600))

    def test_partial_transcript_cache_is_rejected_at_shared_boundary(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'transcript.json', {
            'video_id': self.row['video_id'], 'language': 'en', 'origin': 'local_asr',
            'segments': [{'start': 590, 'end': 600, 'text': 'Thank you for watching this AI tutorial.'}]})
        with patch.object(media, 'captions', return_value={'origin': 'uploaded_captions',
                          'language': 'en', 'segments': self.complete}) as captions:
            result = media.obtain(self.row, self.metadata, {'youtube': {}}, time.monotonic() + 5, lambda: False)
        captions.assert_called_once()
        self.assertEqual(self.complete, result['segments'])

    def test_translation_must_have_substance_for_each_text_block(self):
        text = 'Every change needs verification and testing. ' * 40 + 'Important final conclusion.'
        for output in ('中文摘要。结论。', 'Important final conclusion.'):
            with self.subTest(output=output), patch.object(notes, 'call', return_value=output), self.assertRaises(RuntimeError):
                notes.translate(text, None, time.monotonic() + 5, lambda: False)

    def test_timestamp_free_notes_are_published_and_exported(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        translated = '作者演示智能体生成代码并运行测试，强调必须核验结果与限制。'
        payload = {'title': '智能体代码生成的实践', 'summary': '作者展示方法并提示核验限制。',
                   'body': '## 核验要求\n每次生成的改动都需要验证。'}
        prompts = []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if prompt.startswith('将以下字幕'):
                return response(prompt, translated)
            return json.dumps(payload, ensure_ascii=False) if '只输出 JSON' in prompt else translated

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                                                           'segments': self.complete}):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']))
        self.assertFalse(result['errors'])
        self.assertNotRegex('\n'.join(prompts), r'\[\d{2}:\d{2}:\d{2}\]')
        exported = collector.YouTubeCollector('youtube').materialize()
        self.assertEqual(1, len(exported))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        self.assertIn('## 中文译文（自动生成）\n\n' + translated, files[0].read_text())
        self.assertNotIn('&t=', files[0].read_text())

    def test_merged_translation_recovers_original_units_then_reaches_ready_and_export(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        literal = 'python -m unittest tests.test_youtube_notes --verbose'
        source = 'Review generated changes before use. ' * 12 + '\n\n' + literal
        translated = '作者提醒使用前独立审查全部生成的修改，下面的命令必须保持原样。'
        requested = []

        def reply(prompt, *arguments):
            if prompt.startswith('将以下字幕'):
                segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
                requested.append(segments)
                if len(segments) > 1:
                    self.assertEqual([source.split('\n\n')[0].strip(), literal], segments)
                    return json.dumps({'translations': [translated + literal]})
                self.assertIn(segments[0], [source.split('\n\n')[0].strip(), literal])
                return json.dumps({'translations': [literal if segments[0] == literal else translated]})
            if '只输出 JSON' in prompt:
                return json.dumps({'title': '独立核验生成结果', 'summary': '运行命令前先审查生成的修改。',
                                   'body': '## 核验步骤\n先审查，再运行测试。'})
            return '作者要求独立审查生成的代码，然后运行测试命令。'

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                    'segments': [{'start': 0, 'end': 600, 'text': source}]}):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertEqual([[source.split('\n\n')[0].strip(), literal],
                          [source.split('\n\n')[0].strip()], [literal]], requested)
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        self.assertIn(translated + '\n\n' + literal, files[0].read_text())

    def test_invalid_batch_format_recovers_units_before_publication_and_export(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        source = ' '.join(f'Fact {position} requires reviewing the generated code before using it.'
                          for position in range(30))
        translated = '作者要求在使用代码前独立核验每个事实和每次生成的改动。'
        requested = []

        def reply(prompt, *arguments):
            if prompt.startswith('将以下字幕'):
                segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
                requested.append(segments)
                return '以下是一段散文，不是有效的批次结果。' if len(segments) > 1 else response(prompt, translated)
            if '只输出 JSON' in prompt:
                return '好的，以下是笔记：\n```json\n' + json.dumps({
                    'title': '核验代码中的每个事实', 'summary': '先核验再使用生成的代码。',
                    'body': '## 方法\n逐项验证生成的修改。'}) + '\n```\n笔记结束。'
            return '作者要求逐项核验生成的代码。'

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                    'segments': [{'start': 0, 'end': 600, 'text': source}]}):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertGreater(len(requested[0]), 1)
        self.assertEqual([[unit] for unit in requested[0]], requested[1:])
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        body = files[0].read_text()
        self.assertEqual(len(requested[0]), body.count(translated))
        self.assertNotIn('以下是笔记', body)
        self.assertNotIn('笔记结束', body)

    def test_format_echo_cannot_become_a_ready_note_or_export(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        source = 'Verify every generated change before use. ' * 18
        echo = '好的，我会按 {"translations":["第一段译文","第二段译文"]} 的格式输出。'
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', return_value=echo) as call, \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                    'segments': [{'start': 0, 'end': 600, 'text': source}]}):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((0, 0), (result['processed'], result['added']), result)
        self.assertEqual('failed', storage.rows()[0]['state'])
        self.assertEqual(2, call.call_count)
        self.assertEqual(frozenset(), collector.YouTubeCollector('youtube').materialize())
        self.assertFalse(list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md')))

    def test_cleaned_caption_command_echo_is_rejected_from_cache_and_recovers_before_export(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        sentences = {
            'Welcome back to the channel. Today we examine an AI agent framework.': '欢迎回来，今天介绍智能体框架。',
            'First run npm install to get the deps.': '先运行 npm install 安装依赖。',
            'The planner decomposes the goal into steps and each step gets its own budget.': '规划器拆分目标，为每步分配预算。',
            'The executor runs those steps and checks the result before proceeding.': '执行器逐步运行并核验结果。',
            'Every generated change requires independent verification before use.': '使用每次生成的修改前都要独立核验。'}
        segments = transcripts.parse(json.dumps({'segments': [
            {'start': position * 20, 'end': (position + 1) * 20, 'text': sentence + '\n'}
            for position, sentence in enumerate(list(sentences) * 6)]}), '.json')
        source, = transcripts.blocks(segments)
        self.assertNotIn('\n', source)
        blocks = [unit['text'] for unit in translation.units(source)]
        directory = media.cache_dir(self.row['video_id'])
        key = notes.signature(translation.STAGE, selected, source)
        broken, prompts = lambda value: value, []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if prompt.startswith('将以下字幕'):
                values = json.loads(prompt.split('\n\n', 1)[1])['segments']
                if broken:
                    return json.dumps({'translations': [broken(value) for value in values]})
                for original, translated in sentences.items():
                    values = [value.replace(original, translated) for value in values]
                return json.dumps({'translations': values})
            if '只输出 JSON' in prompt:
                return json.dumps({'title': '核验智能体的逐步执行', 'summary': '说明规划与执行的验证边界。',
                                   'body': '## 方法\n规划器分配预算，执行器核验每步结果。'})
            return '作者说明规划器分配预算，执行器逐步核验结果。'

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                                                           'segments': segments}), \
                patch.object(notes, 'MAX_LANGUAGE_TRIES', 5):
            for broken in (lambda value: value, lambda value: '好。' + value,
                           lambda value: value + '完', lambda value: '欢迎回来。' + value.partition('.')[2]):
                incomplete = json.dumps({'translations': [broken(block) for block in blocks]})
                media.save_json(directory / 'notes-cache.json', {key: incomplete})
                prompts.clear()
                result = collector.collect(FetchScope(force=True))
                self.assertEqual(0, result['added'])
                self.assertEqual('failed', storage.rows()[0]['state'])
                self.assertNotIn(key, media.read_json(directory / 'notes-cache.json'))
                self.assertTrue(prompts)
                self.assertTrue(all(prompt.startswith('将以下字幕') for prompt in prompts))
                self.assertFalse(collector.YouTubeCollector('youtube').materialize())
            broken = False
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        body = files[0].read_text()
        self.assertIn('规划器拆分目标', body)
        self.assertIn('npm install', body)
        self.assertNotIn('The planner', body)
        self.assertNotIn('The executor', body)

    def test_native_chinese_quote_reaches_ready_and_export_without_translation(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        source = ('今天介绍状态管理与渲染的分离。官方文档原话是：the compiler will analyze your component '
                  'tree and automatically insert memoization boundaries so that you do not have to write '
                  'useMemo by hand any more in most of the cases we have measured so far。'
                  '这意味着编译器会自己插入缓存边界，下面实际核验结果。')
        transcript = {'origin': 'uploaded_captions', 'language': 'zh',
                      'segments': [{'start': 0, 'end': 600, 'text': source}]}
        prompts = []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if prompt.startswith('将以下字幕'):
                raise AssertionError('中文原文不依赖翻译模型配合')
            if '只输出 JSON' in prompt:
                return json.dumps({'title': '状态管理与渲染分离', 'summary': '作者说明缓存边界并要求实际核验。',
                                   'body': '## 方法\n保留文档原话，实际核验编译器的缓存行为。'})
            return '作者说明编译器插入缓存边界，需要独立核验结果。'

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value=transcript):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertFalse(any(prompt.startswith('将以下字幕') for prompt in prompts))
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        self.assertIn(source, files[0].read_text())

    def test_japanese_echo_cache_cannot_publish_and_recovers_with_chinese_translation(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        source = '今日は新しい技術を紹介します。状態管理と描画を分離するのが基本の考え方です。'
        transcript = {'origin': 'uploaded_captions', 'language': 'ja',
                      'segments': [{'start': 0, 'end': 600, 'text': source}]}
        directory = media.cache_dir(self.row['video_id'])
        old_key = notes.signature('translate', selected, source)
        key = notes.signature(translation.STAGE, selected, source)
        media.save_json(directory / 'notes-cache.json', {
            old_key: source, key: json.dumps({'translations': [source]})})
        broken, prompts = True, []

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if prompt.startswith('将以下字幕'):
                segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
                return json.dumps({'translations': segments if broken else ['作者说明状态管理与渲染的分离。']})
            if '只输出 JSON' in prompt:
                return json.dumps({'title': '状态管理的方法', 'summary': '作者说明状态管理与渲染的分离。',
                                   'body': '## 方法\n先分离状态与渲染，再实际核验。'})
            return '作者说明状态管理与渲染的分离，要求实际核验。'

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value=transcript):
            result = collector.collect(FetchScope(force=True))
            self.assertEqual(0, result['added'])
            self.assertEqual('failed', storage.rows()[0]['state'])
            self.assertTrue(prompts)
            self.assertTrue(all(prompt.startswith('将以下字幕') for prompt in prompts))
            self.assertNotIn(key, media.read_json(directory / 'notes-cache.json'))
            self.assertFalse(collector.YouTubeCollector('youtube').materialize())
            broken = False
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        body = files[0].read_text()
        self.assertIn('作者说明状态管理与渲染的分离。', body)
        self.assertNotIn(source, body)

    def test_code_heavy_translation_reaches_ready_and_export_without_language_ratio_failure(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        narrative = ('The author explains why every generated change requires independent verification. ' * 4).strip()
        command = 'npm install --save-dev vitest && npx vitest run --coverage'
        source = narrative + '\n\n' + '\n'.join([command] * 16)
        requested = []

        def reply(prompt, *arguments):
            if prompt.startswith('将以下字幕'):
                segments = json.loads(prompt.split('\n\n', 1)[1])['segments']
                requested.append(segments)
                values = ['作者解释每次生成的修改都需要独立核验。' if 'The author' in segment else segment
                          for segment in segments]
                return json.dumps({'translations': values})
            if '只输出 JSON' in prompt:
                return json.dumps({'title': '核验命令执行结果', 'summary': '先独立核验，再采用生成结果。',
                                   'body': '## 方法\n运行原始命令并核验结果。'})
            return '作者要求先独立核验命令和生成的修改。'

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value={'origin': 'uploaded_captions', 'language': 'en',
                    'segments': [{'start': 0, 'end': 600, 'text': source}]}):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (result['processed'], result['added']), result)
        self.assertEqual(1, len(requested))
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))
        files = list((self.root / 'vault' / 'source' / 'youtube').rglob('*.md'))
        self.assertEqual(1, len(files))
        body = files[0].read_text()
        self.assertEqual(16, body.count(command))
        self.assertNotIn('The author', body)

    def test_missing_translation_coverage_leaves_no_note_and_recovers_on_retry(self):
        selected = SimpleNamespace(provider='fixture', model_id='fixture')
        complete = {'origin': 'uploaded_captions', 'language': 'en', 'segments': [
            {'start': 0, 'end': 600, 'text': 'Verify every generated change before use. ' * 35}]}
        prompts = []
        broken = True

        def reply(prompt, *arguments):
            prompts.append(prompt)
            if prompt.startswith('将以下字幕'):
                payload = json.loads(response(prompt, '每次修改都要独立核验。'))
                if broken:
                    payload['translations'].pop()
                return json.dumps(payload)
            if '只输出 JSON' in prompt:
                return json.dumps({'title': '逐项核验生成结果', 'summary': '每次修改需要核验。',
                                   'body': '## 方法\n使用前独立核验。'})
            return '作者强调每次修改需要独立核验。'

        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(collector.login, 'readiness', return_value={'ready': True, 'browser': 'chrome'}), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(notes, 'model', return_value=selected), patch.object(notes, 'call', side_effect=reply), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value=complete):
            for attempt in range(3):
                prompts.clear()
                first = collector.collect(FetchScope(force=True))
                self.assertEqual((0, 0), (first['processed'], first['added']))
                self.assertNotEqual('ready', storage.rows()[0]['state'])
                self.assertTrue(all(prompt.startswith('将以下字幕') for prompt in prompts))
                self.assertEqual(frozenset(), collector.YouTubeCollector('youtube').materialize())
                self.assertFalse(list((self.root / 'vault').rglob('*.md')))
            broken = False
            recovered = collector.collect(FetchScope(force=True))
        self.assertEqual((1, 1), (recovered['processed'], recovered['added']))
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertEqual(1, len(collector.YouTubeCollector('youtube').materialize()))

    def test_aliases_share_content_and_survive_disable_delete_and_repoint(self):
        alias = reg.save_subscription('youtube', {'name': '别名频道', 'url': 'https://www.youtube.com/@same-channel'})
        storage.discover(self.source, [self.entry_value])
        storage.discover(alias, [self.entry_value])
        self.assertEqual(1, len(storage.rows()))
        self.assertEqual(1, len(storage.rows([alias['id']])))
        reg.save_subscription('youtube', {'enabled': False}, self.source['id'])
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=self.metadata) as probe, \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope())
        self.assertEqual(1, result['processed'])
        probe.assert_called_once()
        reg.delete_subscription('youtube', self.source['id'])
        self.assertEqual('ready', storage.rows([alias['id']])[0]['state'])
        reg.save_subscription('youtube', {'url': 'https://www.youtube.com/@different-channel'}, alias['id'])
        self.assertEqual([], storage.rows())
        storage.discover(alias, [self.entry_value])
        self.assertEqual([], storage.rows())
        fresh = reg.subscription('youtube', alias['id'])
        storage.discover(fresh, [self.entry_value])
        self.assertEqual('ready', storage.rows()[0]['state'])
        self.assertFalse(storage.rows()[0]['committed'])

    def test_both_aliases_receive_one_globally_processed_note(self):
        alias = reg.save_subscription('youtube', {'name': '别名频道', 'url': 'https://www.youtube.com/@same-channel'})
        with patch.object(discovery, 'fetch', return_value=[self.entry_value]), \
                patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=self.metadata), \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), patch.object(notes, 'build', side_effect=self.note) as build:
            collector.collect(FetchScope())
        build.assert_called_once()
        for source in (self.source, alias):
            self.assertTrue(storage.rows([source['id']])[0]['committed'])
        with reg.db() as connection:
            self.assertEqual(1, connection.execute("SELECT count(*) FROM feed_items WHERE kind='youtube'").fetchone()[0])
            self.assertEqual(2, connection.execute('SELECT count(*) FROM subscription_items').fetchone()[0])

    def test_existing_candidate_migrates_without_losing_error_or_ownership(self):
        with reg.db() as connection:
            connection.execute('''CREATE TABLE youtube_candidates (
                video_id TEXT PRIMARY KEY,source_id INTEGER,source_url TEXT,entry TEXT,
                views INTEGER DEFAULT 0,heat REAL DEFAULT 0,value REAL DEFAULT 0,
                heat_rank INTEGER DEFAULT 0,value_rank INTEGER DEFAULT 0,state TEXT,
                reason TEXT,attempts INTEGER DEFAULT 0,retry_at REAL DEFAULT 0,
                duration REAL DEFAULT 0,note TEXT DEFAULT '',committed INTEGER DEFAULT 0,updated_at TEXT)''')
            connection.execute("DELETE FROM feed_meta WHERE key='youtube-ownership-v2'")
            connection.execute('''INSERT INTO youtube_candidates(video_id,source_id,source_url,entry,state,reason,updated_at)
                VALUES(?,?,?,?,?,?,?)''', (self.entry_value['video_id'], self.source['id'], self.source['url'],
                json.dumps(self.entry_value), 'failed', '旧格式失败', reg.now()))
        self.assertEqual('旧格式失败', storage.rows()[0]['last_error'])
        self.assertEqual(1, len(storage.rows()))
        with reg.db() as connection:
            self.assertEqual(1, connection.execute('SELECT count(*) FROM youtube_candidates').fetchone()[0])

    def test_broken_preferred_author_track_tries_other_author_track(self):
        metadata = {**self.metadata, 'subtitles': {'en': [{'url': 'https://example.org/en'}],
                                                 'zh': [{'url': 'https://example.org/zh'}]}}
        selected = []

        def download(arguments, log, *rest, **kwargs):
            selected.append(arguments[arguments.index('--sub-langs') + 1])
            output = arguments[arguments.index('-o') + 1].replace('%(ext)s', 'vtt')
            text = ('WEBVTT\n\n00:09:50.000 --> 00:10:00.000\nThank you for watching this AI tutorial.'
                    if selected[-1] == 'en' else
                    'WEBVTT\n\n00:00:00.000 --> 00:10:00.000\n完整视频字幕，介绍智能体实践、测试方法和结果验证。')
            from pathlib import Path
            Path(output).write_text(text)
            # 真实现返回「这次调用自己的」那份日志(`media.unique`),调用方拿它去 unlink。
            actual = media.unique(log)
            actual.write_bytes(b'')
            return actual

        with patch.object(media, 'ytdlp', side_effect=download):
            result = media.captions(self.row, metadata, {'youtube': {}}, False,
                                    time.monotonic() + 5, lambda: False)
        self.assertEqual(['en', 'zh'], selected)
        self.assertEqual('zh', result['language'])

    def fail_source(self):
        storage.discover(self.source, [self.entry_value])
        storage.failed(storage.rows()[0], RuntimeError('旧字幕错误'))
        with reg.db() as connection:
            connection.execute('UPDATE subscriptions SET last_error=?,failures=1 WHERE id=?',
                               ('旧字幕错误', self.source['id']))

    def assert_unresolved(self, summary):
        source = reg.subscription('youtube', self.source['id'])
        self.assertEqual('', source['last_error'])
        self.assertEqual('', source['last_success'])
        self.assertEqual('旧字幕错误', storage.rows([self.source['id']])[0]['last_error'])
        self.assertNotIn(str(self.source['id']), summary['succeeded_ids'])
        self.assertGreater(summary['deferred'], 0)
        with patch.object(collector, 'collect', return_value=summary):
            self.assertFalse(collector.YouTubeCollector('youtube').fetch(FetchScope()).whole)

    def test_failed_source_not_cleared_when_other_source_spends_count_budget(self):
        self.fail_source()
        other = self.channel(2)
        storage.discover(other, [self.entry(2, views=1000000)])
        self.settings(max_videos=1)
        with patch.object(discovery, 'fetch', return_value=[]), patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=self.metadata) as probe, \
                patch.object(media, 'obtain', return_value=self.complete_transcript()), patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual(1, probe.call_count)
        self.assert_unresolved(result)

    def test_failed_source_not_cleared_by_missing_models_duration_or_interrupt(self):
        self.fail_source()
        for available, metadata, interruption in [((False, '没有模型'), self.metadata, None),
                ((True, ''), {**self.metadata, 'duration': 3000}, None),
                ((True, ''), self.metadata, media.Interrupted('处理时间预算用尽'))]:
            with self.subTest(metadata=metadata, available=available), \
                    patch.object(discovery, 'fetch', return_value=[]), patch.object(notes, 'available', return_value=available), \
                    patch.object(media, 'probe', return_value=metadata), patch.object(media, 'obtain', side_effect=interruption):
                self.assert_unresolved(collector.collect(FetchScope(force=True)))

    def test_failed_source_outside_date_range_not_cleared(self):
        self.fail_source()
        with patch.object(discovery, 'fetch', return_value=[]), patch.object(media, 'probe') as probe:
            self.assert_unresolved(collector.collect(FetchScope(since='2026-01-01', until='2026-01-02', force=True)))
        probe.assert_not_called()

    def test_real_success_is_the_only_way_to_clear_content_error(self):
        self.fail_source()
        with patch.object(discovery, 'fetch', return_value=[]), patch.object(notes, 'available', return_value=(True, '')), \
                patch.object(media, 'probe', return_value=self.metadata), patch.object(media, 'obtain', return_value=self.complete_transcript()), \
                patch.object(notes, 'build', side_effect=self.note):
            result = collector.collect(FetchScope(force=True))
        self.assertEqual('', reg.subscription('youtube', self.source['id'])['last_error'])
        self.assertEqual('', storage.rows()[0]['last_error'])
        self.assertIn(str(self.source['id']), result['succeeded_ids'])

    def test_metadata_expiry_force_and_missing_subtitles_recover(self):
        directory = media.cache_dir(self.row['video_id'])
        path = directory / 'metadata.json'
        updated = {**self.metadata, 'subtitles': {'en': [{'url': 'https://example.org/sub'}]}}
        for expired, force in ((True, False), (False, True)):
            media.save_json(path, self.metadata)
            if expired:
                os.utime(path, (time.time() - 200, time.time() - 200))
            def dumped(arguments, output, *rest, **keywords):
                actual = media.unique(output)
                actual.write_text(json.dumps(updated), encoding='utf-8')
                return actual

            with self.subTest(expired=expired, force=force), \
                    patch.object(media, 'ytdlp', side_effect=dumped) as probe:
                self.assertEqual(updated, media.probe(self.row, {'youtube': {}},
                                                      time.monotonic() + 5, lambda: False, force))
            probe.assert_called_once()
        with patch.object(media, 'captions', return_value=None), patch.object(media, 'asr', side_effect=RuntimeError('无字幕')):
            with self.assertRaises(RuntimeError):
                media.obtain(self.row, self.metadata, {'youtube': {}}, time.monotonic() + 5, lambda: False)
        self.assertFalse(path.exists())

    def test_completed_asr_survives_transcript_save_failure(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'whisper.json', {'segments': self.complete})
        audio = directory / 'audio.webm'
        audio.write_bytes(b'audio')
        with patch.object(media, 'captions', return_value=None), patch.object(media, 'save_json', side_effect=OSError('写盘失败')):
            with self.assertRaises(OSError):
                media.obtain(self.row, self.metadata, {'youtube': {}}, time.monotonic() + 5, lambda: False)
        self.assertTrue(audio.exists())
        with patch.object(media, 'captions', return_value=None), patch.object(media, 'ytdlp') as download, patch.object(media, 'run') as asr:
            media.obtain(self.row, self.metadata, {'youtube': {}}, time.monotonic() + 5, lambda: False)
        download.assert_not_called()
        asr.assert_not_called()
        self.assertFalse(audio.exists())

    def test_partial_settings_preserve_other_budgets(self):
        self.settings(max_minutes=120, run_seconds=180, asr_threads=4)
        self.client.put('/api/intake/youtube/settings', json={'youtube': {'max_videos': 1}})
        values = reg.get_settings('youtube')['youtube']
        self.assertEqual((1, 120, 180, 4), tuple(values[key] for key in ('max_videos', 'max_minutes', 'run_seconds', 'asr_threads')))

    def test_excluded_video_does_not_occupy_rank_admission(self):
        storage.discover(self.source, [self.entry_value, self.entry(2, views=1)])
        storage.update(self.entry_value['video_id'], state='excluded', reason='直播', gate_code='replay')
        rows = ranking.score(storage.rows(), self.settings()['youtube'], FetchScope())
        self.assertEqual([self.entry(2)['video_id']], [row['video_id'] for row in rows])
