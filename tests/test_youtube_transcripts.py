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

    def test_blocks_preserve_full_text_without_adding_timestamps(self):
        words = '完整正文' * 2200
        segments = [{'start': 123, 'end': 180, 'text': words}]
        blocks = transcripts.blocks(segments, limit=3000)
        self.assertTrue(all(len(block) <= 3000 for block in blocks))
        self.assertTrue(all('[00:02:03]' not in block for block in blocks))
        self.assertEqual(words, ''.join(blocks))

    def test_blocks_join_caption_fragments_and_prefer_sentence_boundaries(self):
        segments = [{'start': 0, 'end': 3, 'text': 'An AI agent builds code.'},
                    {'start': 3, 'end': 6, 'text': 'It needs review. ' * 10},
                    {'start': 6, 'end': 9, 'text': 'Finally verify every change.'}]
        blocks = transcripts.blocks(segments, limit=60)
        self.assertTrue(all(0 < len(block) <= 60 for block in blocks))
        self.assertTrue(all(block.endswith('.') for block in blocks))
        self.assertEqual(' '.join(segment['text'].strip() for segment in segments), ' '.join(blocks))
        self.assertIn('Finally verify every change.', blocks[-1])

    def test_blocks_handle_empty_input_and_reject_invalid_limit(self):
        self.assertEqual([], transcripts.blocks([]))
        for limit in (0, -1):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                transcripts.blocks([], limit=limit)

    def test_small_limits_and_unbroken_words_keep_every_character(self):
        text = '没有空格的正文ABCDEFGHIJK'
        for limit in (1, 2, 7):
            with self.subTest(limit=limit):
                blocks = transcripts.blocks([{'start': 0, 'end': 10, 'text': text}], limit=limit)
                self.assertTrue(all(0 < len(block) <= limit for block in blocks))
                self.assertEqual(text, ''.join(blocks))
