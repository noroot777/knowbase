import json
import sys
import time
from unittest.mock import patch

from kb.youtube import media
from tests.youtube_fixture import YouTubeFixture


class YouTubeMedia(YouTubeFixture):
    def setUp(self):
        super().setUp()
        self.row = {'video_id': '12345678901', 'entry': {'url': 'https://www.youtube.com/watch?v=12345678901'}}
        self.metadata = {'id': self.row['video_id'], 'duration': 60, 'language': 'en'}
        self.transcript = {'origin': 'uploaded_captions', 'language': 'en',
                           'segments': [{'start': 0, 'end': 59, 'text': 'An AI agent writes useful code.'}]}
        self.deadline = time.monotonic() + 30

    def obtain(self):
        return media.obtain(self.row, self.metadata, {'youtube': {}}, self.deadline, lambda: False)

    def test_cache_before_captions_or_audio(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'transcript.json', {**self.transcript, 'video_id': self.row['video_id']})
        with patch.object(media, 'captions') as captions, patch.object(media, 'asr') as asr:
            self.assertEqual(self.transcript['segments'], self.obtain()['segments'])
        captions.assert_not_called()
        asr.assert_not_called()

    def test_bad_cache_recovers_and_uploaded_precedes_automatic(self):
        directory = media.cache_dir(self.row['video_id'])
        media.save_json(directory / 'transcript.json', {'video_id': self.row['video_id'],
                        'origin': 'uploaded_captions', 'language': 'en', 'segments': [{}]})
        with patch.object(media, 'captions', return_value=self.transcript) as captions, patch.object(media, 'asr') as asr:
            result = self.obtain()
        self.assertEqual('uploaded_captions', result['origin'])
        self.assertFalse(captions.call_args.args[2])
        self.assertEqual(1, captions.call_count)
        asr.assert_not_called()

    def test_caption_failure_falls_back_then_caches(self):
        with patch.object(media, 'captions', side_effect=[ValueError('坏字幕'), None]) as captions, \
                patch.object(media, 'asr', return_value={**self.transcript, 'origin': 'local_asr'}) as asr:
            result = self.obtain()
        self.assertEqual([False, True], [call.args[2] for call in captions.call_args_list])
        self.assertEqual('local_asr', result['origin'])
        self.assertEqual(['坏字幕'], result['caption_warnings'])
        asr.assert_called_once()
        self.assertEqual(result, media.read_json(media.cache_dir(self.row['video_id']) / 'transcript.json'))

    def test_original_tracks_never_use_auto_translation(self):
        metadata = {**self.metadata, 'automatic_captions': {
            'zh': [{'url': 'https://example.org/caption?tlang=zh'}],
            'en-orig': [{'url': 'https://example.org/caption?lang=en'}]}}
        self.assertEqual(['en-orig'], media.tracks(metadata, True))
        metadata['automatic_captions'].pop('en-orig')
        self.assertEqual([], media.tracks(metadata, True))

    def test_matching_local_artifact_and_corrupt_metadata(self):
        directory = media.VIDEOS_ROOT / ('review-' + self.row['video_id'])
        (directory / 'metadata').mkdir(parents=True)
        (directory / 'subtitles').mkdir()
        media.save_json(directory / 'metadata/youtube_metadata.json', self.metadata)
        (directory / 'subtitles/source.srt').write_text('1\n00:00:00,000 --> 00:00:59,000\nAn AI agent writes useful code.\n')
        with patch.object(media, 'ytdlp') as download, patch.object(media, 'captions') as captions:
            self.assertEqual(self.metadata, media.probe(self.row, self.deadline, lambda: False))
            self.assertEqual('local_cache', self.obtain()['origin'])
        download.assert_not_called()
        captions.assert_not_called()
        media.save_json(media.cache_dir(self.row['video_id']) / 'metadata.json', [])
        media.save_json(directory / 'metadata/youtube_metadata.json', [])
        with patch.object(media, 'ytdlp', side_effect=lambda args, output, *rest: output.write_text(json.dumps(self.metadata))):
            self.assertEqual(self.metadata, media.probe(self.row, self.deadline, lambda: False))

    def test_audio_only_asr_is_thread_capped_and_cleans_media(self):
        directory = media.cache_dir(self.row['video_id'])
        model = directory / 'ggml-base.en.bin'
        model.touch()
        commands = []

        def download(arguments, *rest):
            commands.append(arguments)
            (directory / 'audio.webm').write_bytes(b'fake audio')

        def run(arguments, *rest):
            commands.append(arguments)
            if arguments[0] == 'ffmpeg':
                (directory / 'speech.wav').touch()
            else:
                media.save_json(directory / 'whisper.json', {'segments': self.transcript['segments']})

        with patch.object(media, 'tools', return_value=dict(asr_enabled=True, ffmpeg=True, whisper=True, model=True)), \
                patch.object(media, 'model_path', return_value=model), patch.object(media, 'ytdlp', side_effect=download), \
                patch.object(media, 'run', side_effect=run):
            result = media.asr(self.row, self.metadata, {'youtube': {}}, self.deadline, lambda: False)
            self.assertEqual('local_asr', result['origin'])
            self.assertEqual('bestaudio[abr<=160]/bestaudio', commands[0][1])
            self.assertEqual(['-t', '2'], commands[-1][-2:])
            self.assertFalse((directory / 'audio.webm').exists())
            self.assertFalse((directory / 'speech.wav').exists())
            with self.assertRaisesRegex(RuntimeError, '多语言模型'):
                media.asr(self.row, {**self.metadata, 'language': 'ja'}, {'youtube': {}}, self.deadline, lambda: False)

    def test_public_tool_arguments_and_stop(self):
        with patch.object(media, 'run') as run:
            media.ytdlp(['--skip-download', 'https://www.youtube.com/watch?v=12345678901'],
                         self.root / 'log', self.deadline, lambda: False)
        arguments = run.call_args.args[0]
        self.assertIn('--ignore-config', arguments)
        self.assertNotIn('--cookies-from-browser', arguments)
        started = time.monotonic()
        with self.assertRaises(media.Interrupted):
            media.run([sys.executable, '-c', 'import time; time.sleep(20)'], self.root / 'child.log',
                      started + .3, lambda: False)
        self.assertLess(time.monotonic() - started, 3)
