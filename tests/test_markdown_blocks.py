import re
import unittest
from unittest.mock import patch

from kb.feeds import markdown_blocks, translate


class MarkdownBlockTranslationTests(unittest.TestCase):
    def test_fenced_and_indented_code_never_enters_model_prompts(self):
        code = '```text\n/setup          /scrape\n  |                |\n  v                v\n```'
        indented = '    const keep = "English code";'
        body = 'English paragraph.\n\n' + code + '\n\nAnother paragraph.\n\n' + indented
        prompts = []

        def answer(prompt, model):
            prompts.append(prompt)
            content = prompt.rsplit('正文:\n', 1)[1]
            return '###TITLE###\n中文标题\n###BODY###\n' + content.replace('English paragraph.', '中文段落。')

        with patch.object(translate, '_call', side_effect=answer):
            title, translated = translate.translate_item({'title':'English title', 'body':body}, None)
        self.assertEqual('中文标题', title)
        self.assertIn(code, translated)
        self.assertIn(indented, translated)
        self.assertIn('中文段落。', translated)
        self.assertNotIn('KB_PRESERVE_', translated)
        self.assertNotIn('/setup', '\n'.join(prompts))
        self.assertNotIn('const keep', '\n'.join(prompts))

    def test_many_code_blocks_have_non_overlapping_marker_names(self):
        blocks = [f'```python\nprint({number})\n```' for number in range(15)]
        protected, saved, cutoff = markdown_blocks.prepare('\n\n'.join(blocks), 12000)
        restored = markdown_blocks.restore(protected, saved)
        self.assertEqual(15, len(saved))
        for block in blocks:
            self.assertIn(block, restored)
        self.assertGreater(cutoff, 0)

    def test_unicode_separators_do_not_shift_markdown_source_line_offsets(self):
        code = '```text\nkeep        spaces\n```'
        for separator in ('\u2028', '\u2029', '\u0085', '\r\n', '\r', '\n'):
            with self.subTest(separator=repr(separator)):
                body = 'Intro' + separator + 'continued\n\n' + code + '\n\nEnd.'
                prepared, saved, _ = markdown_blocks.prepare(body, 12000)
                self.assertIn(code, markdown_blocks.restore(prepared, saved))
                self.assertNotIn('keep        spaces', prepared)

    def test_missing_duplicated_wrapped_and_reordered_markers_fail_closed(self):
        protected, saved, _ = markdown_blocks.prepare('```\nfirst\n```\n\n```\nsecond\n```', 12000)
        markers = re.findall(r'KB_PRESERVE_\w+', protected)
        bad_results = [protected.replace(markers[0], ''), protected + '\n' + markers[0],
                       protected.replace(markers[0], '`' + markers[0] + '`'),
                       '\n\n'.join(reversed(markers))]
        for result in bad_results:
            with self.subTest(result=result), self.assertRaises(RuntimeError):
                markdown_blocks.restore(result, saved)

    def test_long_code_is_one_protected_block_not_several_translation_chunks(self):
        code = '```bash\n' + 'echo keep-this-command\n' * 200 + '```'
        protected, saved, _ = markdown_blocks.prepare('Introduction.\n\n' + code, 12000)
        self.assertEqual(1, len(saved))
        self.assertLess(len(protected), translate.CHUNK)
        self.assertIn(code, markdown_blocks.restore(protected, saved))

    def test_cutoff_never_leaves_half_a_code_fence(self):
        body = 'Introduction.\n\n```bash\n' + 'echo hello\n' * 200 + '```\n\nLast section.'
        protected, saved, cutoff = markdown_blocks.prepare(body, 100)
        self.assertEqual('Introduction.\n\n', protected)
        self.assertEqual({}, saved)
        self.assertEqual(body.index('```'), cutoff)

    def test_plain_prose_keeps_the_existing_chunk_and_limit_behavior(self):
        original = 'First paragraph.\n\nSecond paragraph with List<T> and a URL.'
        protected, saved, cutoff = markdown_blocks.prepare(original, 12000)
        self.assertEqual(original, protected)
        self.assertEqual({}, saved)
        self.assertEqual(len(original), cutoff)

    def test_an_oversized_first_code_block_is_not_saved_as_an_empty_translation(self):
        body = '```python\n' + 'print("hello")\n' * 200 + '```'
        with patch.object(translate, 'MAX_BODY', 100), patch.object(translate, '_call') as call:
            with self.assertRaisesRegex(RuntimeError, '代码块超过'):
                translate.translate_item({'title':'Code example', 'body':body}, None)
        call.assert_not_called()
