"""All X ingestion body conventions obey the same empty-text and attribution contract."""
import time
from unittest.mock import patch

from kb.feeds import grab, grab_store, registry as reg, x_body, x_browser, x_feedgrab, x_post
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import tweet
from tests.test_x_empty_post import empty
from tests.test_x_post import BODY, URL, Page, snapshot


class BodyContractTests(ChannelBase):
    def setUp(self):
        super().setUp()
        self.source = self.sub()
        fast = patch.object(x_post, 'STEP', 0.0)
        fast.start()
        self.addCleanup(fast.stop)

    def seed(self, entry):
        with reg.db() as conn:
            existing = conn.execute('SELECT * FROM feed_items').fetchone()
        if existing:
            self.assertTrue(grab_store.replace(dict(existing), entry))
        else:
            reg.put_items(self.source, [entry])
        return self.row()

    def row(self):
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items').fetchone())

    def manual(self, media=None, quote=None, text=''):
        payload = dict(author={'name': 'Example Person'}, text=text, media=media, quote=quote)
        with patch.object(grab, '_tweet_payload', return_value=payload), \
             patch.object(grab.x_article, 'needs_article', return_value=False):
            return grab._grab_x(URL, time.monotonic() + 10)

    def test_actual_channel_outputs_are_empty_without_rewriting_any_original_column(self):
        entries = [x_browser._entry(tweet('123', text='', context='向阳乔木 转推了')),
                   x_feedgrab._entry(dict(source=URL, author_name='Example Person'), '', 'Example'),
                   self.manual(media={'photos': [{'url': 'https://pbs.twimg.com/media/photo.jpg'}]}),
                   self.manual(media={'videos': [{'url': 'https://video.twimg.com/clip.mp4',
                                                 'thumbnail_url': 'https://pbs.twimg.com/cover.jpg'}]}),
                   self.manual(quote={'author': {'screen_name': 'Other'}, 'text': 'Quoted words'})]
        for entry in entries:
            with self.subTest(body=entry['body']):
                stored = self.seed(entry)
                stats = backfill.repair([stored], Page(empty()), apply=True)
                self.assertEqual(1, stats['empty'])
                self.assertEqual(0, stats['blocked_length'])
                self.assertEqual(0, stats['updated'])
                self.assertEqual(stored, self.row())

    def test_both_recovery_and_empty_audit_recognize_raw_and_normalized_attribution(self):
        entry = x_browser._entry(tweet('123', text='', context='ctx'))
        for body in [entry['body'], entry['body'].rstrip()]:
            with self.subTest(body=body):
                stored = self.seed(dict(entry, body=body))
                self.assertEqual('> ctx\n\n', backfill.entry_of(stored)[x_post.HINT])
                stats = backfill.repair([stored], Page(snapshot(BODY)), apply=True)
                self.assertEqual(1, stats['updated'])
                self.assertEqual('> ctx\n\n' + BODY, self.row()['body'])

    def test_placeholder_and_media_are_not_a_length_gate_on_recovered_text(self):
        for entry in [x_feedgrab._entry(dict(source=URL, author='Example Person'), '', 'Example'),
                      self.manual(media={'photos': [{'url': 'https://pbs.twimg.com/media/a.jpg'}]})]:
            with self.subTest(body=entry['body']):
                stored = self.seed(entry)
                stats = backfill.repair([stored], Page(snapshot('new')), apply=True)
                self.assertEqual(1, stats['updated'])
                expected = 'new\n\n' + entry['body'] if entry['body'].startswith('![') else 'new'
                self.assertEqual(expected, self.row()['body'])

    def test_real_authored_quotes_urls_and_custom_titles_remain_nonempty(self):
        texts = ['> keep my own words', 'https://example.org', '![图片](https://pbs.twimg.com/a.jpg)']
        for text in texts:
            for context in ['', 'ctx']:
                with self.subTest(text=text, context=context):
                    stored = self.seed(x_browser._entry(tweet('123', text=text, context=context)))
                    stats = backfill.repair([stored], Page(empty()), apply=True)
                    self.assertEqual(1, stats['blocked_length'])
                    self.assertEqual(stored, self.row())
        custom = x_feedgrab._entry(dict(source=URL, author='Example Person', title='custom'), '', 'Example')
        self.assertEqual('custom', x_body.parts(custom).text)

    def test_generated_media_suffix_is_shared_and_preserved_on_successful_recovery(self):
        for text in ['own', 'own\n\nmore']:
            with self.subTest(text=text):
                entry = self.manual(text=text, media={'photos': [{'url': 'https://pbs.twimg.com/a.jpg'}]},
                                    quote={'author': {'screen_name': 'Other'}, 'text': 'quoted'})
                stored = self.seed(entry)
                detail = text + '\n\n' + BODY
                stats = backfill.repair([stored], Page(snapshot(detail)), apply=True)
                self.assertEqual(1, stats['updated'])
                self.assertEqual('> 引用 @Other：quoted\n\n' + detail + '\n\n![图片](https://pbs.twimg.com/a.jpg)',
                                 self.row()['body'])

    def test_loaded_unknown_structure_is_not_empty_and_has_a_short_retryable_budget(self):
        snap = dict(snapshot(''), ready='complete', unknown_structure=True)
        with patch.object(x_post, 'STRUCTURE_SECONDS', 0.01), \
             self.assertRaises(x_post.PostStructureUnknown):
            x_post.fetch(URL, Page(snap))
        page = Page(snap, snapshot(BODY), snapshot(BODY))
        self.assertEqual(BODY, x_post.fetch(URL, page))

    def test_batch_sentinel_reports_drift_and_defers_the_rest_without_writes(self):
        stored = self.seed(x_browser._entry(tweet('123', text='keep original')))
        with patch.object(x_post, 'fetch', side_effect=x_post.PostStructureUnknown('unknown')) as fetch:
            stats = backfill.repair([stored] * 8, Page(), apply=True)
        self.assertEqual(3, fetch.call_count)
        self.assertEqual(3, stats['unknown_structure'])
        self.assertEqual(3, stats['failed'])
        self.assertEqual(5, stats['deferred'])
        self.assertIn('结构变化', stats['stop_reason'])
        self.assertEqual('batch', stats['results'][-1]['scope'])
        self.assertEqual(stored, self.row())

    def test_low_unknown_ratio_and_ordinary_failures_do_not_trigger_structure_sentinel(self):
        stored = self.seed(x_browser._entry(tweet('123', text='keep original')))
        outcomes = [BODY] * 4 + [x_post.PostStructureUnknown('unknown')] * 3
        for effects, count in [(outcomes, 3), ([x_post.PostUnavailable('timeout')] * 7, 0)]:
            with self.subTest(count=count), patch.object(x_post, 'fetch', side_effect=effects) as fetch:
                stats = backfill.repair([stored] * 7, Page(), apply=False)
            self.assertEqual(7, fetch.call_count)
            self.assertEqual(count, stats['unknown_structure'])
            self.assertNotIn('stop_reason', stats)
            self.assertEqual(0, stats['deferred'])
