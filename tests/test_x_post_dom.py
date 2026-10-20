"""Exercise the timeline and status readers against real DOM in a fresh headless shell."""
import json
import re
import threading
import time
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from kb.feeds import x_body, x_browser, x_page_state, x_post
from kb.feeds.x_cdp import Chrome
from scripts import x_post_backfill as backfill
from tests.headless import INSTALL_HINT, SHELL
from tests.x_empty_dom import MORE, EmptyDomCases

LINK = ('<a href="https://t.co/example"><span aria-hidden="true">https://</span>'
        'example.com/<span aria-hidden="true">connections</span>'
        '<span aria-hidden="true">…</span></a>')
TEXT = 'Audit ' + LINK + '<br>Every app<img alt="🙂" src="data:,">\n\n4. Ask first.'
EXPECTED = 'Audit https://example.com/connections\nEvery app🙂\n\n4. Ask first.'
RETRY_PATH = ('M12 4c-4.418 0-8 3.58-8 8s3.582 8 8 8c3.806 0 6.993-2.66 '
              '7.802-6.22l1.95.44C20.742 18.67 16.76 22 12 22 6.477 22 2 '
              '17.52 2 12S6.477 2 12 2c3.272 0 6.176 1.57 8 4V3.5h2v6h-6v-2'
              'h2.616C17.175 5.39 14.749 4 12 4z')
RETRY = f'<button role="button"><div><svg><g><path d="{RETRY_PATH}"></path></g></svg></div></button>'


def article(identity='123', text='short', more='', quoted=''):
    return (f'<article data-testid="tweet"><div data-testid="User-Name">Example</div>'
            f'<a href="/Example/status/{identity}">'
            '<time datetime="2026-10-18T00:00:00Z">now</time></a>'
            f'<div data-testid="tweetText">{text}</div>{more}{quoted}</article>')


def stored_entry(text):
    return x_body.record(dict(body=text, **{x_post.HINT: ''}), text)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.lower().startswith('/cdn-cgi/'):
            self.send_response(200)
            self.end_headers()
            return
        if self.path.startswith('/i/api/graphql/'):
            self.send_response(self.server.api_status)
            self.end_headers()
            self.wfile.write(b'{}')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.end_headers()
        self.wfile.write(('<!doctype html><meta charset="utf-8">' + self.server.html).encode())

    def log_message(self, *args):
        pass


@unittest.skipUnless(SHELL is not None, '缺 chrome-headless-shell —— ' + INSTALL_HINT)
class PostDomTests(EmptyDomCases, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join)
        cls.addClassCleanup(cls.server.shutdown)
        cls.page = Chrome(str(Path(cls.temp.name) / 'profile'), headless=True, chrome_path=SHELL)
        cls.page.__enter__()
        cls.addClassCleanup(cls.page.__exit__, None, None, None)
        cls.page.open_page('about:blank')

    def load(self, html, route='/Example/status/123'):
        marker = str(time.monotonic_ns())
        self.server.html = f'<meta name="fixture-version" content="{marker}">' + html
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}{route}')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.page.eval('document.querySelector("meta[name=fixture-version]")?.content === '
                              + json.dumps(marker)):
                self.page.eval(x_browser._RESET_JS)
                return
            time.sleep(0.02)
        self.fail('fixture did not render')

    def collect(self):
        return json.loads(self.page.eval(x_browser._COLLECT_JS))['items']

    def test_collect_marks_only_the_authors_own_show_more(self):
        quote = '<div role="link"><div data-testid="tweetText">quote</div>' + MORE + '</div>'
        self.load(article(text='preview', more=MORE) + article('124', quoted=quote))
        items = self.collect()
        self.assertTrue(items[0]['truncated'])
        self.assertIn(x_post.HINT, x_browser._entry(items[0]))
        self.assertFalse(items[1]['truncated'])
        self.assertNotIn(x_post.HINT, x_browser._entry(items[1]))

    def test_links_emoji_and_paragraphs_survive_both_readers(self):
        self.load(article(text=TEXT))
        self.assertEqual(EXPECTED, self.collect()[0]['text'])
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual(EXPECTED, snap['text'])
        self.assertEqual('123', snap['id'])
        self.assertFalse(snap['truncated'])

    def test_blockified_inline_anchors_have_source_evidence_without_losing_paragraphs(self):
        self.load(article(text='Read <a style="display:block" href="/AsideAI">@AsideAI</a>'
                          ' today<br><br>Keep this paragraph.'))
        legacy = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertIn('\n@AsideAI\n', legacy)
        self.assertIn('\n\nKeep this paragraph.', snap['text'])
        entry = stored_entry(legacy)
        state, complete, _ = backfill.proposal(entry, snap['text'], snap)
        self.assertEqual('normalized', state)
        self.assertEqual(snap['text'], complete['body'])
        self.assertEqual('blocked_whitespace', backfill.proposal(entry, snap['text'])[0])

    def test_block_paragraphs_are_not_mistaken_for_inline_anchor_artifacts(self):
        for markup in ['<p>First paragraph.</p><p>Second paragraph.</p>',
                       '<div>First paragraph.</div><a href="/AsideAI">@AsideAI</a> today']:
            with self.subTest(markup=markup):
                self.load(article(text=markup))
                legacy = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
                snap = json.loads(self.page.eval(x_post.READ_JS))
                self.assertIn('\n', legacy)
                entry = stored_entry(legacy)
                self.assertEqual('blocked_whitespace', backfill.proposal(entry, snap['text'], snap)[0])

    def test_actual_x_mention_wrappers_repair_legacy_text_and_longer_details(self):
        text = ('Prediction: you are going to use '
                '<div style="display:inline-flex"><span style="display:block">'
                '<a href="/AsideAI">@AsideAI</a></span></div> with agents.'
                '<br><br>Keep this paragraph.')
        self.load(article(text=text))
        legacy = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertIn('\n@AsideAI\n', legacy)
        for preview, expected in [(legacy, 'normalized'), (legacy.split(' with agents.')[0], 'expanded')]:
            with self.subTest(preview=preview):
                state, complete, _ = backfill.proposal(stored_entry(preview),
                                                      snap['text'], snap)
                self.assertEqual(expected, state)
                self.assertEqual(snap['text'], complete['body'])
                self.assertIn('\n\nKeep this paragraph.', complete['body'])

    def test_layout_and_canonical_evidence_share_emoji_and_url_text_semantics(self):
        for emoji in ['<img alt="😂" src="data:,">', '<span><img alt="👩🏽‍💻" src="data:,"></span>']:
            with self.subTest(emoji=emoji):
                self.load(article(text='Prediction 🙂 ' + emoji + ' read '
                                  '<div style="display:inline-flex"><span style="display:block">'
                                  '<a href="/AsideAI">@AsideAI</a></span></div> with ' + LINK
                                  + '<br><br>Keep this paragraph.'))
                snap = json.loads(self.page.eval(x_post.READ_JS))
                self.assertEqual(re.sub(r'\s+', '', snap['text']),
                                 re.sub(r'\s+', '', snap['inline_legacy']))
                self.assertIn('\n@AsideAI\n', snap['inline_legacy'])
                self.assertIn('https://example.com/connections', snap['inline_legacy'])
                state, complete, _ = backfill.proposal(
                    stored_entry(snap['inline_legacy']), snap['text'], snap)
                self.assertEqual('normalized', state)
                self.assertEqual(snap['text'], complete['body'])
                self.assertIn('\n\nKeep this paragraph.', complete['body'])
                raw = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
                state, complete, info = backfill.proposal(
                    stored_entry(raw), snap['text'], snap)
                self.assertEqual('blocked_content', state)
                self.assertIsNone(complete)
                self.assertEqual('detail_does_not_cover_stored', info['reason'])

    def test_unrelated_blocks_do_not_invalidate_a_proven_link_elsewhere(self):
        self.load(article(text='Read <div style="display:inline-flex"><span style="display:block">'
                          '<a href="/AsideAI">@AsideAI</a></span></div> today<br><br>'
                          '<span style="display:block">A later paragraph.</span>'))
        legacy = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
        snap = json.loads(self.page.eval(x_post.READ_JS))
        preview = legacy.split(' today')[0]
        state, complete, _ = backfill.proposal(stored_entry(preview),
                                              snap['text'], snap)
        self.assertEqual('expanded', state)
        self.assertEqual(snap['text'], complete['body'])
        self.assertIn('\n\nA later paragraph.', complete['body'])

    def test_hidden_block_wrappers_do_not_create_layout_removal_evidence(self):
        self.load(article(text='Read <div style="display:none"><span style="display:block">'
                          '<a href="/AsideAI">@AsideAI</a></span></div> today'))
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual(snap['text'], snap['inline_legacy'])
        self.assertEqual('blocked_whitespace', backfill.proposal(
            stored_entry('Read \n@AsideAI\n today'), snap['text'], snap)[0])

    def test_out_of_flow_wrappers_cannot_authorize_deleting_paragraphs(self):
        for style in ['position:absolute', 'position:fixed', 'float:left', 'visibility:hidden']:
            with self.subTest(style=style):
                self.load(article(text='Read <span style="display:block;' + style
                                  + '"><a href="/AsideAI">@AsideAI</a></span> today'))
                snap = json.loads(self.page.eval(x_post.READ_JS))
                self.assertEqual(snap['text'], snap['inline_legacy'])
                self.assertEqual('blocked_whitespace', backfill.proposal(
                    stored_entry('Read \n@AsideAI\n today'), snap['text'], snap)[0])

    def test_proven_multiple_layout_breaks_can_be_removed_without_removing_source_paragraphs(self):
        anchors = ['<a style="display:block" href="https://example.org">https://example.org</a>',
                   '<div style="display:inline-flex"><span style="display:block">'
                   '<a href="https://example.org">https://example.org</a></span></div>']
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                html = article(text='Read\n' + anchor + ' today<br><br>Keep this paragraph.')
                self.load(html.replace('data-testid="tweetText"',
                                       'data-testid="tweetText" style="white-space:pre-wrap"'))
                legacy = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
                snap = json.loads(self.page.eval(x_post.READ_JS))
                self.assertIn('\n\nhttps://', legacy)
                self.assertIn('Read\nhttps://', snap['text'])
                state, complete, _ = backfill.proposal(stored_entry(legacy),
                                                      snap['text'], snap)
                self.assertEqual('normalized', state)
                self.assertEqual(snap['text'], complete['body'])
                self.assertIn('\n\nKeep this paragraph.', complete['body'])

    def test_unproven_extra_paragraph_at_a_real_wrapper_is_still_blocked(self):
        self.load(article(text='Read <div style="display:inline-flex"><span style="display:block">'
                          '<a href="/AsideAI">@AsideAI</a></span></div> today'))
        legacy = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
        snap = json.loads(self.page.eval(x_post.READ_JS))
        preview = legacy.replace('\n@AsideAI', '\n\n@AsideAI')
        self.assertEqual('blocked_whitespace', backfill.proposal(
            stored_entry(preview), snap['text'], snap)[0])

    def test_inline_anchor_without_a_layout_break_cannot_authorize_removing_one(self):
        self.load(article(text='Read <a href="/AsideAI">@AsideAI</a> today'))
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual('blocked_whitespace', backfill.proposal(
            stored_entry('Read\n@AsideAI\ntoday'), snap['text'], snap)[0])

    def test_real_protocol_split_is_repaired_but_url_following_paragraphs_survive(self):
        self.load(article(text='Read <a href="https://t.co/example">'
                          '<span style="display:block">https://</span>'
                          'example.org/path</a>'
                          '<br><br>Keep this paragraph.'))
        legacy = self.page.eval('document.querySelector("[data-testid=tweetText]").innerText')
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertIn('https://\nexample.org/path', legacy)
        state, complete, _ = backfill.proposal(stored_entry(legacy),
                                              snap['text'], snap)
        self.assertEqual('normalized', state)
        self.assertEqual('Read https://example.org/path\n\nKeep this paragraph.', complete['body'])

    def test_anchor_offsets_count_unicode_characters_and_do_not_trust_unlinked_mentions(self):
        self.load(article(text='🙂 Read <a style="display:block" href="/AsideAI">@AsideAI</a> today'))
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual([5, 13], snap['inline_breaks'])
        entry = stored_entry('🙂 Read \n@AsideAI\n today')
        self.assertEqual('normalized', backfill.proposal(entry, snap['text'], snap)[0])
        self.load(article(text='🙂 Read @AsideAI today'))
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual([], snap['inline_breaks'])
        self.assertEqual('blocked_whitespace', backfill.proposal(entry, snap['text'], snap)[0])

    def test_a_late_show_more_signal_is_not_lost_on_repeated_collection(self):
        self.load(article())
        self.assertFalse(self.collect()[0]['truncated'])
        self.page.eval('document.querySelector("article").insertAdjacentHTML("beforeend", '
                       + json.dumps(MORE) + '); true')
        self.assertTrue(self.collect()[0]['truncated'])
        self.page.eval('document.querySelector("[data-testid=tweetText]").textContent = '
                       '"a longer preview"; true')
        self.assertTrue(self.collect()[0]['truncated'])

    def test_status_reader_excludes_replies_and_quoted_text(self):
        quote = '<div role="link"><div data-testid="tweetText">quote</div>' + MORE + '</div>'
        self.load(article('999', text='reply') + article(text=TEXT, quoted=quote))
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual(EXPECTED, snap['text'])
        self.assertFalse(snap['truncated'])
        self.load(article('999', text='only a reply'))
        snap = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual('', snap['id'])
        self.assertEqual('', snap['text'])

    def test_a_quote_without_author_text_is_not_promoted_to_the_post_body(self):
        for quote in ['<div role="link"><div data-testid="tweetText">quote</div></div>',
                      '<div data-testid="quoteTweet"><div data-testid="tweetText">quote</div></div>']:
            with self.subTest(quote=quote):
                html = article(quoted=quote)
                self.load(html.replace('<div data-testid="tweetText">short</div>', ''))
                item = self.collect()[0]
                self.assertEqual('', item['text'])
                self.assertTrue(item['quoted'])
                self.assertIsNone(x_browser._entry(item))
                self.assertEqual([], x_browser._select([item], None, 'Example'))

    def test_late_quote_detection_does_not_leave_a_storable_empty_post(self):
        self.load(article(text=''))
        self.assertFalse(self.collect()[0]['quoted'])
        quote = '<div role="link"><div data-testid="tweetText">quote</div></div>'
        self.page.eval('document.querySelector("article").insertAdjacentHTML("beforeend", '
                       + json.dumps(quote) + '); true')
        self.assertIsNone(x_browser._entry(self.collect()[0]))

    def test_author_text_and_own_article_survive_while_media_contract_is_unchanged(self):
        quote = '<div role="link"><div data-testid="tweetText">quote</div></div>'
        self.load(article(text='Own text', quoted=quote))
        self.assertEqual('Own text', x_browser._entry(self.collect()[0])['body'])
        self.load(article(text='', quoted=quote + '<a href="/i/article/999">Article</a>'))
        self.assertIn(x_browser.x_article.HINT, x_browser._entry(self.collect()[0]))
        self.load(article(text=''))
        self.assertIsNotNone(x_browser._entry(self.collect()[0]))

    def test_stable_text_before_show_more_appears_is_not_complete(self):
        self.load(article(text='a preview that stops mid'))
        first = json.loads(self.page.eval(x_post.READ_JS))
        second = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual(first['text'], second['text'])
        self.assertFalse(second['truncated'])
        entry = dict(x_browser._entry(self.collect()[0]), **{x_post.HINT: ''})
        with self.assertRaises(x_post.PostUnavailable):
            x_post.merge(entry, second['text'])
        full = second['text'] + ', and the rest arrived after the first two reads.'
        self.page.eval('document.querySelector("[data-testid=tweetText]").textContent = '
                       + json.dumps(full) + '; true')
        third = json.loads(self.page.eval(x_post.READ_JS))
        fourth = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual(first['at'], third['at'])
        self.assertEqual(first['id'], third['id'])
        self.assertEqual(third['text'], fourth['text'])
        self.assertEqual(full, x_post.merge(entry, fourth['text'])['body'])

    def test_status_and_timeline_share_explicit_absence_and_rate_limit_detection(self):
        for text, empty, limited in [
            ('唔...该页面不存在。请尝试搜索别的内容。', True, False),
            ('这个页面不存在', True, False),
            ('Hmm...this page doesn’t exist.', True, False),
            ('Rate limit exceeded', False, True),
            ('请稍后再试', False, False),
            ('Loading', False, False),
        ]:
            with self.subTest(text=text):
                self.load('<main>' + text + '</main>')
                for script in [x_post.READ_JS, x_browser._STATE_JS]:
                    snap = json.loads(self.page.eval(script))
                    self.assertIs(empty, snap.get('empty'))
                    self.assertIs(limited, snap.get('limited'))

    def test_real_request_error_copy_is_retryable_not_absent_or_proven_rate_limit(self):
        for text in ['出错了。请尝试重新加载。', 'Something went wrong. Try reloading.', '请稍后再试']:
            with self.subTest(text=text):
                self.load('<main>' + text + '</main>')
                for script in [x_post.READ_JS, x_browser._STATE_JS]:
                    snap = json.loads(self.page.eval(script))
                    self.assertIs(True, snap.get('failed'))
                    self.assertIs(False, snap.get('empty'))
                    self.assertIs(False, snap.get('limited'))

    def test_primary_column_retry_icon_detects_errors_without_matching_any_ui_language(self):
        self.load('<main><div data-testid="primaryColumn"><div><div>別の言語</div>'
                  + RETRY + '</div></div></main>')
        for script in [x_post.READ_JS, x_browser._STATE_JS]:
            snap = json.loads(self.page.eval(script))
            self.assertIs(True, snap.get('failed'))
            self.assertIs(False, snap.get('empty'))
            self.assertIs(False, snap.get('limited'))

    def test_retry_icons_in_sidebars_or_tweets_and_regular_buttons_are_not_page_errors(self):
        fixtures = [
            '<div data-testid="sidebarColumn">' + RETRY + '</div>',
            '<div data-testid="primaryColumn">' + article(text=RETRY) + '</div>',
            '<div data-testid="primaryColumn"><button><svg><path d="M0 0"/></svg></button></div>',
            article(text='出错了。请尝试重新加载。'),
        ]
        for html in fixtures:
            with self.subTest(html=html):
                self.load(html)
                for script in [x_post.READ_JS, x_browser._STATE_JS]:
                    snap = json.loads(self.page.eval(script))
                    self.assertIs(False, snap.get('failed'))

    def test_actual_http_status_events_are_seen_in_a_fresh_headless_browser(self):
        for status in [429, 503, 200]:
            for cards in ['', article(identity='999', text='Someone else replied')]:
                self.check_http_status(status, cards)

    def check_http_status(self, status, cards):
        with self.subTest(status=status, cards=bool(cards)):
            self.server.api_status = status
            self.server.html = (cards + '<script>fetch("/i/api/graphql/hash/TweetDetail")'
                                '.finally(() => window.finished = true)</script>')
            marker = str(time.monotonic_ns())
            x_page_state.navigate(self.page, f'http://127.0.0.1:{self.server.server_port}'
                                            '/Example/status/123?fixture=' + marker)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.page.eval('location.search === ' + json.dumps('?fixture=' + marker)
                                  + ' && window.finished === true'):
                    break
                time.sleep(0.02)
            else:
                self.fail('the real browser did not complete its fixture request')
            snap = x_page_state.read(self.page, x_post.READ_JS)
            self.assertIs(status == 429, snap.get('limited'))
            self.assertIs(status >= 400, snap.get('failed'))
            self.assertIs(False, snap.get('empty'))

    def test_real_http_limit_with_replies_stops_detail_reads_before_the_local_budget(self):
        self.server.api_status = 429
        self.server.html = (article(identity='999')
                            + '<script>fetch("/i/api/graphql/hash/TweetDetail")</script>')
        url = f'http://127.0.0.1:{self.server.server_port}/Example/status/123'
        started = time.monotonic()
        with (patch.object(x_post, '_STATUS', re.compile(r'^http://[^/]+/[^/]+/status/(\d+)')),
              patch.object(x_post, 'STEP', 0.01), patch.object(x_post, 'SECONDS', 3)):
            with self.assertRaises(x_page_state.XRateLimited):
                x_post.fetch(url, self.page)
        self.assertLess(time.monotonic() - started, 2.5)

    def http_page(self, status, operation, content, route='/Example'):
        self.server.api_status = status
        self.server.html = (content + '<script>fetch("/i/api/graphql/hash/' + operation + '")'
                            '.finally(() => window.finished = true)</script>')
        marker = str(time.monotonic_ns())
        x_page_state.navigate(self.page, f'http://127.0.0.1:{self.server.server_port}'
                                        + route + '?fixture=' + marker)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.page.eval('location.search === ' + json.dumps('?fixture=' + marker)
                              + ' && window.finished === true'):
                return
            time.sleep(0.02)
        self.fail('the real page fixture did not finish its HTTP request')

    def test_real_auxiliary_errors_leave_ready_timeline_content_usable(self):
        for operation in ['UserByScreenName', 'UserTweetsAndReplies']:
            for status in [429, 403, 404, 503]:
                with self.subTest(operation=operation, status=status):
                    self.http_page(status, operation, article())
                    started = time.monotonic()
                    try:
                        state = x_browser._wait_state(self.page, 'Example', started + 3)
                    except x_browser.XBrowserError as exc:
                        self.fail(f'The real timeline was vetoed by an auxiliary response: {exc}')
                    self.assertEqual(1, state['articles'])
                    self.assertEqual({operation: status}, state.get('http_auxiliary'))
                    self.assertEqual('', x_page_state.problem(state))
                    self.assertLess(time.monotonic() - started, 2.5)

    def test_real_auxiliary_detail_error_is_not_evidence_that_the_target_request_failed(self):
        for status in [429, 503]:
            with self.subTest(status=status):
                self.http_page(status, 'TweetResultByRestId', '', '/Example/status/123')
                snap = x_page_state.read(self.page, x_post.READ_JS)
                self.assertEqual({'TweetResultByRestId': status}, snap.get('http_auxiliary'))
                self.assertEqual('', x_page_state.problem(snap))
                url = f'http://127.0.0.1:{self.server.server_port}/Example/status/123'
                with (patch.object(x_post, '_STATUS', re.compile(r'^http://[^/]+/[^/]+/status/(\d+)')),
                      patch.object(x_post, 'STEP', 0.01), patch.object(x_post, 'SECONDS', 0.15)):
                    with self.assertRaises((x_post.PostUnavailable, x_page_state.XBrowserError)) as caught:
                        x_post.fetch(url, self.page)
                self.assertIsInstance(caught.exception, x_post.PostUnavailable)
                self.assertNotIsInstance(caught.exception, x_post.PostRequestFailed)
                self.assertNotIsInstance(caught.exception, x_post.PostGone)

    def test_real_dom_terminal_evidence_is_not_erased_by_current_http_errors(self):
        for status in [404, 429, 503]:
            for operation in ['UserByScreenName', 'UserTweets']:
                with self.subTest(status=status, operation=operation):
                    self.http_page(status, operation,
                        '<main><div data-testid=primaryColumn><div data-testid=emptyState>'
                        '<div data-testid=empty_state_header_text>Account suspended</div>'
                        '<a href="https://help.x.com/en/rules-and-policies/x-rules">Help</a>'
                        '</div></div></main>')
                    snap = x_page_state.read(self.page, x_browser._STATE_JS)
                    self.assertTrue(snap['empty'])
                    self.assertEqual('', x_page_state.problem(snap))
                    started = time.monotonic()
                    with self.assertRaisesRegex(x_browser.XBrowserError, '账号已被冻结'):
                        x_browser._wait_state(self.page, 'Example', started + 3)
                    self.assertLess(time.monotonic() - started, 2.5)

    def test_error_words_in_a_real_post_cannot_make_it_absent_or_rate_limited(self):
        text = '有人说「该页面不存在」或「请稍后再试」，但这是一条正常推文。'
        self.load(article(text=text))
        for script in [x_post.READ_JS, x_browser._STATE_JS]:
            snap = json.loads(self.page.eval(script))
            self.assertIs(False, snap.get('empty'))
            self.assertIs(False, snap.get('limited'))

    def test_real_profile_terminal_shapes_are_independent_of_body_wide_regexes(self):
        fixtures = json.loads((Path(__file__).parent / 'fixtures/x_account_states.json').read_text())
        for sample in fixtures:
            with self.subTest(state=sample['state']):
                route = sample.get('landed', '/' + sample['source'].rsplit('/', 1)[1])
                self.load(sample['html'], route)
                snap = json.loads(self.page.eval(x_browser._STATE_JS))
                self.assertEqual(sample['state'], snap['account']['state'])
                self.assertTrue(snap['account']['evidence'])

    def test_unknown_profile_structure_is_not_positive_evidence_of_account_absence(self):
        for profile in ['', '<div data-testid=ProfileHeaderVNext>Healthy account</div>']:
            self.load('<main><div data-testid=primaryColumn>' + profile + '<div data-testid=emptyState>'
                      '<div data-testid=empty_state_header_text>Unbekannt</div></div></div></main>',
                      '/old_handle')
            snap = json.loads(self.page.eval(x_browser._STATE_JS))
            self.assertEqual('unknown', snap['account']['state'])
            self.assertEqual('old_handle', snap['account']['handle'])

    def test_protected_profile_uses_its_help_link_not_translated_words(self):
        self.load('<main><div data-testid=primaryColumn><div data-testid=UserName>Example</div>'
                  '<div data-testid=emptyState><div data-testid=empty_state_header_text>非英文</div>'
                  '<a href="https://help.x.com/en/safety-and-security/public-and-protected-posts">'
                  'Help</a></div></div></main>', '/Example')
        snap = json.loads(self.page.eval(x_browser._STATE_JS))
        self.assertEqual('protected', snap['account']['state'])

    def test_sidebar_bio_empty_profile_and_loading_do_not_prove_account_absence(self):
        missing = ('<div data-testid=emptyState><div data-testid=empty_state_header_text>'
                   '此账号不存在</div></div>')
        for html in ['<div data-testid=sidebarColumn>' + missing + '</div>',
                     '<div data-testid=primaryColumn><div data-testid=UserName>正常账号</div>'
                     + missing + '</div>',
                     '<div data-testid=primaryColumn><div role=progressbar></div>' + missing + '</div>',
                     '<div data-testid=primaryColumn>' + article(text='此账号不存在') + missing + '</div>']:
            with self.subTest(html=html):
                self.load('<main>' + html + '</main>', '/Example')
                self.assertEqual('', json.loads(self.page.eval(x_browser._STATE_JS))['account']['state'])

    def media(self, content):
        return article(text='').replace('<div data-testid="tweetText"></div>', content)

    def bind_payload(self, kind='photo', *, text='', identity='123', tweet=None):
        link = 'https://t.co/fixture'
        data = dict(id_str=identity, full_text=text + (' ' if text else '') + link,
                    display_text_range=[0, len(text)], entities={})
        entity = dict(url=link, indices=[len(data['full_text']) - len(link), len(data['full_text'])])
        if kind in {'photo', 'video'}:
            data['entities']['media'] = [dict(entity, type=kind)]
        else:
            data['entities']['urls'] = [entity]
            if kind == 'link':
                data['card'] = dict(url=link)
            elif kind == 'quote':
                data.update(quoted_status_id_str='456', quoted_status_permalink=dict(url=link))
        self.page.eval('document.querySelector("article").__reactFiber$fixture = '
                       + json.dumps(dict(memoizedProps=dict(tweet=tweet or data))) + '; true')
