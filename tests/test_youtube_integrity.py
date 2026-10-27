import json
import os
import time
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg
from kb.youtube import collector, discovery, media, notes, ranking, storage, transcripts
from tests.youtube_fixture import YouTubeFixture


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

    def test_translation_must_have_substance_for_each_marker(self):
        text = '[00:00:00] ' + 'Every change needs verification and testing. ' * 40 + '\n[00:05:00] Important final conclusion.'
        for output in ('[00:00:00] 中文摘要。\n[00:05:00] 结论。',
                       '[00:00:00] ' + '完整中文内容。' * 80 + '\n[00:05:00] '):
            with self.subTest(output=output), patch.object(notes, 'call', return_value=output), self.assertRaises(RuntimeError):
                notes.translate(text, None, time.monotonic() + 5, lambda: False)

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
        self.assertEqual('旧字幕错误', source['last_error'])
        self.assertEqual('', source['last_success'])
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
