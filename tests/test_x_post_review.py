"""Regressions for stable previews and the handoff from status text to Article recovery."""
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from kb.feeds import x_article, x_browser, x_post
from tests.test_x_post import BODY, OLD, PREVIEW, Page, preview, snapshot

ARTICLE = {'title': 'Full article', 'body': 'The full article with its final paragraph.'}


class ExpansionTests(unittest.TestCase):
    def test_equal_or_shorter_stable_text_is_not_full_text(self):
        for text in [PREVIEW, 'a shorter fragment']:
            with self.subTest(text=text), patch.object(x_post, 'STEP', 0.0):
                kept, note = x_post.complete([preview()], session=Page(snapshot(text)))
                self.assertEqual([], kept)
                self.assertIn('下次同步', note)

    def test_growth_compares_with_preview_not_with_repost_context(self):
        context = ('Reposted by ' + 'A person ' * 100).rstrip()
        with patch.object(x_post, 'STEP', 0.0):
            kept, note = x_post.complete([preview(context)], session=Page())
        self.assertEqual('', note)
        self.assertEqual('> ' + context + '\n\n' + BODY, kept[0]['body'])

    def test_stable_but_unexpanded_text_keeps_both_channel_watermarks(self):
        for attach in [False, True]:
            page = Page(snapshot(PREVIEW))
            with self.subTest(attach=attach), patch.object(x_post, 'STEP', 0.0), \
                 patch.object(x_browser, 'browser_session', return_value=nullcontext(page)), \
                 patch.object(x_browser, '_harvest_one', return_value=([preview()], '')):
                result = x_browser.harvest([('Example', OLD)], profile='', attach=attach)['Example']
            self.assertEqual([], result['entries'])
            self.assertEqual(OLD, result['watermark'])
            self.assertTrue(result['incomplete'])

    def test_two_complete_empty_reads_can_still_be_followed_by_the_full_body(self):
        empty = dict(snapshot(''), ready='complete', articles=1)
        with patch.object(x_post, 'STEP', 0.0):
            page = Page(empty, empty, snapshot(BODY), snapshot(BODY))
            kept, note = x_post.complete([preview()], session=page)
        self.assertEqual(4, page.reads)
        self.assertEqual('', note)
        self.assertEqual(BODY, kept[0]['body'])

    def test_a_stable_preview_on_one_round_can_be_completed_on_the_next(self):
        for attach in [False, True]:
            for text, expected in [(PREVIEW, []), (BODY, [BODY])]:
                page = Page(dict(snapshot(text), ready='complete', articles=1))
                with self.subTest(attach=attach, text=text), patch.object(x_post, 'STEP', 0.0), \
                     patch.object(x_browser, 'browser_session', return_value=nullcontext(page)), \
                     patch.object(x_browser, '_harvest_one', return_value=([preview()], '')):
                    result = x_browser.harvest([('Example', OLD)], profile='', attach=attach)['Example']
                self.assertEqual(expected, [entry['body'] for entry in result['entries']])
                if expected:
                    self.assertNotEqual(OLD, result['watermark'])
                else:
                    self.assertEqual(OLD, result['watermark'])


class ArticleHandoffTests(unittest.TestCase):
    def entry(self):
        return dict(preview('Someone reposted'), **{x_article.HINT: 'https://x.com/i/article/999'})

    def test_explicitly_deleted_posts_do_not_enter_article_recovery(self):
        with patch.object(x_post, 'fetch', side_effect=x_post.PostGone('页面不存在')), \
             patch.object(x_article, 'fetch') as request, \
             patch.object(x_article, 'browser_fetch') as fallback:
            kept, note = x_post.complete([self.entry()], session=Page())
        self.assertEqual([], kept)
        self.assertEqual('', note)
        request.assert_not_called()
        fallback.assert_not_called()

    def test_rate_limits_escape_article_handoff_as_browser_channel_errors(self):
        with patch.object(x_post, 'fetch', side_effect=x_post.XRateLimited('访问频率受限')), \
             patch.object(x_article, 'fetch') as request, \
             patch.object(x_article, 'browser_fetch') as fallback:
            with self.assertRaisesRegex(x_browser.XBrowserError, '频率'):
                x_post.complete([self.entry()], session=Page())
        request.assert_not_called()
        fallback.assert_not_called()

    def test_a_failed_status_read_still_reaches_the_article_api(self):
        entry = self.entry()
        with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('no detail')), \
             patch.object(x_article, 'API', 'https://article.test'), \
             patch.object(x_article, 'fetch', return_value=ARTICLE) as request:
            kept, note = x_post.complete([entry], session=Page())
        self.assertEqual('', note)
        self.assertIn(ARTICLE['body'], kept[0]['body'])
        self.assertTrue(kept[0]['body'].startswith('> Someone reposted\n\n'))
        self.assertNotIn(x_post.HINT, kept[0])
        self.assertNotIn(x_article.HINT, kept[0])
        request.assert_called_once()

    def test_request_error_still_allows_article_recovery_without_freezing_a_recovered_post(self):
        with patch.object(x_post, 'fetch', side_effect=x_post.PostRequestFailed('X 页面请求出错')), \
             patch.object(x_article, 'API', 'https://article.test'), \
             patch.object(x_article, 'fetch', return_value=ARTICLE):
            kept, note = x_post.complete([self.entry()], session=Page())
        self.assertEqual('', note)
        self.assertEqual(1, len(kept))
        self.assertIn(ARTICLE['body'], kept[0]['body'])
        self.assertNotIn(x_post.HINT, kept[0])

    def test_the_article_browser_fallback_is_reachable_too(self):
        with patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('no detail')), \
             patch.object(x_article, 'API', 'https://article.test'), \
             patch.object(x_article, 'BROWSER_FALLBACK', True), \
             patch.object(x_article, 'fetch', side_effect=x_article.ArticleUnavailable('API down')), \
             patch.object(x_article, 'browser_fetch', return_value=ARTICLE) as fallback:
            kept, note = x_post.complete([self.entry()], session=Page())
        self.assertEqual('', note)
        self.assertIn(ARTICLE['body'], kept[0]['body'])
        fallback.assert_called_once()

    def test_no_article_or_disabled_recovery_cannot_let_the_preview_through(self):
        for api in ['', 'https://article.test']:
            with self.subTest(api=api), \
                 patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('no detail')), \
                 patch.object(x_article, 'API', api), \
                 patch.object(x_article, 'BROWSER_FALLBACK', False), \
                 patch.object(x_article, 'fetch', return_value=None):
                kept, note = x_post.complete([self.entry()], session=Page())
            self.assertEqual([], kept)
            self.assertIn('下次同步', note)

    def test_both_detail_and_article_failures_keep_the_watermark(self):
        for attach in [False, True]:
            with self.subTest(attach=attach), \
                 patch.object(x_browser, 'browser_session', return_value=nullcontext(Page())), \
                 patch.object(x_browser, '_harvest_one', return_value=([self.entry()], '')), \
                 patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('no detail')), \
                 patch.object(x_article, 'API', 'https://article.test'), \
                 patch.object(x_article, 'BROWSER_FALLBACK', False), \
                 patch.object(x_article, 'fetch', side_effect=x_article.ArticleUnavailable('API down')):
                result = x_browser.harvest([('Example', OLD)], profile='', attach=attach)['Example']
            self.assertEqual([], result['entries'])
            self.assertEqual(OLD, result['watermark'])
            self.assertTrue(result['incomplete'])

    def test_successful_handoff_releases_the_watermark(self):
        with patch.object(x_browser, 'browser_session', return_value=nullcontext(Page())), \
             patch.object(x_browser, '_harvest_one', return_value=([self.entry()], '')), \
             patch.object(x_post, 'fetch', side_effect=x_post.PostUnavailable('no detail')), \
             patch.object(x_article, 'API', 'https://article.test'), \
             patch.object(x_article, 'fetch', return_value=ARTICLE):
            result = x_browser.harvest([('Example', OLD)], profile='')['Example']
        self.assertEqual('', result['incomplete'])
        self.assertNotEqual(OLD, result['watermark'])
        self.assertIn(ARTICLE['body'], result['entries'][0]['body'])
