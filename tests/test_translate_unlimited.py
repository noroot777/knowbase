"""翻译同步不按条数截断，同时保留停止、失败隔离和显式限量调用。"""
from unittest.mock import Mock, patch

from kb import llm, pipeline_state
from kb.collectors.feeds import FeedCollector
from kb.feeds import registry as reg, translate
from tests.test_feed_translate import FeedCase


OK = '###TITLE###\n中文标题\n###BODY###\n中文正文。'


class UnlimitedTranslationTests(FeedCase):

    def populate(self, count):
        for index in range(count):
            self.put(f'English post {index}', f'English body for entry {index}.')

    def test_default_translates_the_entire_queue_and_reports_full_progress(self):
        self.populate(65)
        progress = []
        chat = Mock(return_value=OK)
        with patch.object(llm, 'chat', chat):
            summary = translate.translate_pending(
                'rss', progress=lambda done, total: progress.append((done, total)))
            again = translate.translate_pending('rss')
        self.assertEqual((65, 0, 0), (summary['done'], summary['failed'], summary['left']))
        self.assertEqual(0, translate.pending('rss'))
        self.assertEqual(65, chat.call_count)
        self.assertEqual([(count, 65) for count in range(1, 66)], progress)
        self.assertEqual(0, again['done'])
        with reg.db() as connection:
            translated = connection.execute(
                "SELECT count(*) FROM feed_items WHERE translated_at<>'' AND body_zh=?",
                ('中文正文。',)).fetchone()[0]
        self.assertEqual(65, translated)

    def test_materialize_exports_every_translated_entry_in_one_sync(self):
        self.populate(45)
        with patch.object(llm, 'chat', Mock(return_value=OK)):
            FeedCollector('rss').materialize()
        paths = list((self.root / 'vault' / 'source' / 'rss').glob('*.md'))
        self.assertEqual(45, len(paths))
        self.assertTrue(all('中文正文。' in path.read_text() for path in paths))
        self.assertEqual(0, translate.pending('rss'))

    def test_explicit_limit_only_bounds_that_call(self):
        self.populate(3)
        with patch.object(llm, 'chat', Mock(return_value=OK)):
            limited = translate.translate_pending('rss', limit=1)
            rest = translate.translate_pending('rss')
        self.assertEqual((1, 2), (limited['done'], limited['left']))
        self.assertEqual((2, 0), (rest['done'], rest['left']))
        self.assertEqual(0, translate.pending('rss'))

    def test_manual_translation_handles_all_selected_entries_without_scope_leaks(self):
        self.populate(45)
        other_source = reg.save_subscription(
            'rss', {'name': 'Other blog', 'url': 'https://other.org/feed.xml'})
        reg.put_items(other_source, [{'url': 'https://other.org/post',
                                     'title': 'Other English post',
                                     'body': 'English body outside the selected subscription.'}])
        with reg.db() as connection:
            connection.execute('UPDATE feed_items SET translate_manual=1')
        with patch.object(llm, 'chat', Mock(return_value=OK)) as chat:
            summary = translate.translate_pending(
                'rss', only_manual=True, source_id=self.source['id'])
        self.assertEqual((45, 0), (summary['done'], summary['left']))
        self.assertEqual(45, chat.call_count)
        self.assertEqual(0, translate.pending('rss', self.source['id']))
        self.assertEqual(1, translate.pending('rss', other_source['id']))
        with reg.db() as connection:
            untouched = connection.execute(
                "SELECT translated_at,translate_tries,translate_manual FROM feed_items"
                " WHERE title='Other English post'").fetchone()
        self.assertEqual(('', 0, 1), tuple(untouched))

    def test_one_failure_does_not_block_or_repeatedly_retry_the_queue(self):
        self.populate(44)
        self.put('Failed entry', 'This entry cannot be translated.')

        def respond(prompt, *args, **kwargs):
            if 'Failed entry' in prompt:
                raise RuntimeError('HTTP 503')
            return OK

        with patch.object(llm, 'chat', Mock(side_effect=respond)) as chat:
            summary = translate.translate_pending('rss')
        self.assertEqual((44, 1), (summary['done'], summary['failed']))
        self.assertEqual(45, chat.call_count)
        self.assertEqual(1, translate.pending('rss'))
        with reg.db() as connection:
            failed = connection.execute(
                "SELECT translated_at,translate_tries,translate_error FROM feed_items"
                " WHERE title='Failed entry'").fetchone()
        self.assertEqual(('', 1, 'HTTP 503'), tuple(failed))

    def test_stop_leaves_unstarted_entries_for_a_later_sync(self):
        self.populate(45)

        def stop_after_first(*args, **kwargs):
            pipeline_state._state['stop_requested'] = True
            return OK

        with patch.dict(pipeline_state._state, {'stop_requested': False}), \
                patch.object(translate, 'WORKERS', 1), \
                patch.object(llm, 'chat', Mock(side_effect=stop_after_first)) as chat:
            summary = translate.translate_pending('rss')
        self.assertEqual((1, 44), (summary['done'], summary['left']))
        self.assertEqual(1, chat.call_count)
        self.assertEqual(44, translate.pending('rss'))
        with patch.object(llm, 'chat', Mock(return_value=OK)):
            resumed = translate.translate_pending('rss')
        self.assertEqual((44, 0), (resumed['done'], resumed['left']))
        self.assertEqual(0, translate.pending('rss'))
