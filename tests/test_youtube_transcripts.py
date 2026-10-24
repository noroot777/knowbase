import json
import unittest

from kb.youtube import transcripts


class YouTubeTranscripts(unittest.TestCase):
    def test_vtt_markup_and_rolling_captions(self):
        source = '''WEBVTT

00:00:00.000 --> 00:00:04.000
<c>This is an AI agent</c>

00:00:02.000 --> 00:00:06.000
an AI agent that writes code
'''
        segments = transcripts.validate(transcripts.parse(source, '.vtt'), 6)
        self.assertEqual('This is an AI agent', segments[0]['text'])
        self.assertEqual('that writes code', segments[1]['text'])

    def test_json3_and_whisper_json(self):
        source = {'events': [{'tStartMs': 1000, 'dDurationMs': 2000,
                              'segs': [{'utf8': 'Hello '}, {'utf8': 'AI developers'}]}]}
        segments = transcripts.parse(json.dumps(source), '.json3')
        self.assertEqual(1, segments[0]['start'])
        self.assertEqual(3, segments[0]['end'])
        whisper = {'transcription': [{'offsets': {'from': 1000, 'to': 3000}, 'text': 'Hello AI developers'}]}
        self.assertEqual(segments, transcripts.parse(json.dumps(whisper), '.json'))

    def test_invalid_and_truncated_subtitles(self):
        with self.assertRaises(ValueError):
            transcripts.validate([], 60)
        with self.assertRaises(ValueError):
            transcripts.validate([{'start': 0, 'end': 500, 'text': 'This is a readable sentence'}], 60)
        with self.assertRaises(ValueError):
            transcripts.validate([{'start': 0, 'end': 10, 'text': 'Only an intro sentence'}], 600)

    def test_blocks_preserve_full_text_and_timestamps(self):
        words = '完整正文' * 2200
        segments = [{'start': 123, 'end': 180, 'text': words}]
        blocks = transcripts.blocks(segments, limit=3000)
        self.assertTrue(all(len(block) <= 3000 for block in blocks))
        self.assertTrue(all(block.startswith('[00:02:03] ') for block in blocks))
        self.assertEqual(words, ''.join(block.split('] ', 1)[1] for block in blocks))
