"""Long posts must be complete before insertion, or stay retryable at the old watermark."""
import json
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from kb.feeds import grab_store, registry as reg, sync, translate, x_browser, x_post
from kb.feeds.x_cdp import CdpError
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import FRESH_ISO, tweet

URL = 'https://x.com/Example/status/123'
PREVIEW = 'Review EVERY app. Ask me which ones I still'
BODY = PREVIEW + ' use rather than guessing.\n\n2. Open details.\n\n4. Ask before changing anything.'
OLD = '2026-10-01T00:00:00Z'


def snapshot(text=BODY, **extra):
    return dict(at='123', id='123', text=text, truncated=False, login=False, **extra)


class Page:
    def __init__(self, *snaps):
        self.snaps = list(snaps or [snapshot()])
        self.urls = []
        self.reads = 0

    def open_page(self, url):
        pass

    def goto(self, url):
        self.urls.append(url)
        return {}

    def eval(self, script):
        self.reads += 1
        snap = self.snaps.pop(0) if len(self.snaps) > 1 else self.snaps[0]
        if isinstance(snap, Exception):
            raise snap
        return json.dumps(snap)


def preview(context=''):
    item = tweet('123', text=PREVIEW, context=context)
    item['truncated'] = True
    return x_browser._entry(item)


class PostTests(unittest.TestCase):
    def setUp(self):
        fast = patch.object(x_post, 'STEP', 0.0)
        fast.start()
        self.addCleanup(fast.stop)

    def test_full_post_replaces_both_body_and_preview_title(self):
        entries, note = x_post.fill([preview()], session=Page())
        self.assertEqual('', note)
        self.assertEqual(BODY, entries[0]['body'])
        self.assertEqual(BODY.splitlines()[0], entries[0]['title'])
        self.assertNotIn(x_post.HINT, entries[0])
        self.assertEqual('post', entries[0]['content_kind'])

    def test_repost_context_survives_but_pinned_context_is_not_added(self):
        for context, prefix in [('Someone reposted', '> Someone reposted\n\n'), ('Pinned', '')]:
            with self.subTest(context=context):
                entries, _ = x_post.fill([preview(context)], session=Page())
                self.assertEqual(prefix + BODY, entries[0]['body'])

    def test_short_posts_never_open_detail_pages(self):
        item = x_browser._entry(tweet('124', text='short'))
        page = Page()
        kept, note = x_post.fill([item], session=page)
        self.assertEqual([item], kept)
        self.assertEqual('', note)
        self.assertEqual([], page.urls)

    def test_waits_for_a_stable_full_body_after_the_preview(self):
        folded = dict(snapshot(PREVIEW), truncated=True)
        page = Page(folded, snapshot('partly loaded'), snapshot(), snapshot())
        self.assertEqual(BODY, x_post.fetch(URL, page))
        self.assertEqual(4, page.reads)

    def test_mismatched_page_reply_preview_and_empty_text_cannot_be_stored(self):
        for snap in [dict(snapshot(), at='999'), dict(snapshot(), id='999'),
                     dict(snapshot(), truncated=True), snapshot('')]:
            with self.subTest(snap=snap), patch.object(x_post, 'SECONDS', 0.002):
                kept, note = x_post.fill([preview()], session=Page(snap))
                self.assertEqual([], kept)
                self.assertIn('1 条折叠推文', note)

    def test_failures_are_local_and_leave_short_posts_untouched(self):
        short = x_browser._entry(tweet('124', text='short'))
        for snap in [dict(snapshot(), login=True), CdpError('disconnected')]:
            with self.subTest(snap=snap):
                kept, note = x_post.fill([preview(), short], session=Page(snap))
                self.assertEqual([short], kept)
                self.assertIn('下次同步', note)

    def test_stop_and_budget_do_not_start_any_navigation(self):
        for kwargs in [{'should_stop': lambda: True}, {'deadline': time.monotonic() - 1}]:
            with self.subTest(kwargs=kwargs):
                page = Page()
                kept, note = x_post.fill([preview()], session=page, **kwargs)
                self.assertEqual([], kept)
                self.assertTrue(note)
                self.assertEqual([], page.urls)

    def test_a_failed_detail_does_not_advance_browser_or_attach_watermarks(self):
        for attach in [False, True]:
            with self.subTest(attach=attach):
                page = Page(dict(snapshot(), login=True))
                with patch.object(x_browser, 'browser_session', return_value=nullcontext(page)), \
                     patch.object(x_browser, '_harvest_one', return_value=([preview()], '')):
                    result = x_browser.harvest([('Example', OLD)], profile='', attach=attach)
                self.assertEqual([], result['Example']['entries'])
                self.assertEqual(OLD, result['Example']['watermark'])
                self.assertIn('折叠推文', result['Example']['incomplete'])

    def test_success_advances_the_watermark_and_keeps_the_full_post(self):
        with patch.object(x_browser, 'browser_session', return_value=nullcontext(Page())), \
             patch.object(x_browser, '_harvest_one', return_value=([preview()], '')):
            result = x_browser.harvest([('Example', OLD)], profile='')['Example']
        self.assertEqual(BODY, result['entries'][0]['body'])
        self.assertEqual('', result['incomplete'])
        self.assertEqual(x_browser._utc(x_browser._as_published(FRESH_ISO)),
                         x_browser._utc(result['watermark']))


class StoredPostTests(ChannelBase):
    def test_refetch_does_not_fix_existing_rows_but_versioned_replacement_does(self):
        source = self.sub()
        reg.put_items(source, [preview()])
        sync.export('social')
        with reg.db() as conn:
            row = dict(conn.execute('SELECT * FROM feed_items').fetchone())
            conn.execute("UPDATE feed_items SET body_zh='残文译文',translated_at='old'")
        complete, _ = x_post.fill([preview()], session=Page())
        reg.put_items(source, complete)
        with reg.db() as conn:
            self.assertEqual(PREVIEW, conn.execute('SELECT body FROM feed_items').fetchone()[0])
        self.assertTrue(grab_store.replace(row, complete[0]))
        with reg.db() as conn:
            updated = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        self.assertEqual(BODY, updated['body'])
        self.assertEqual('', updated['body_zh'])
        self.assertEqual('', updated['translated_at'])
        self.assertEqual('post', updated['content_kind'])
        self.assertEqual(row['exported_hash'], updated['exported_hash'])
        self.assertEqual(1, updated['extract_stale'])
        self.assertEqual(row['content_rev'] + 1, updated['content_rev'])
        self.assertFalse(grab_store.replace(row, dict(complete[0], body='late stale body')))
        self.assertFalse(translate._save(row['key'], '旧标题', '旧译文', (row['title'], row['body'])))
        sync.export('social')
        text = (sync.VAULT_ROOT / row['path']).read_text()
        self.assertEqual(1, text.count('4. Ask before changing anything.'))
