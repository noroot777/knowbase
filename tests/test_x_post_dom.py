"""Exercise the timeline and status readers against real DOM in a fresh headless shell."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from kb.feeds import x_browser, x_page_state, x_post
from kb.feeds.x_cdp import Chrome
from tests.headless import INSTALL_HINT, SHELL

MORE = '<button data-testid="tweet-text-show-more-link">显示更多</button>'
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


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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
class PostDomTests(unittest.TestCase):
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

    def load(self, html):
        marker = str(time.monotonic_ns())
        self.server.html = f'<meta name="fixture-version" content="{marker}">' + html
        self.page.goto(f'http://127.0.0.1:{self.server.server_port}/Example/status/123')
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
            with self.subTest(status=status):
                self.server.api_status = status
                self.server.html = ('<script>fetch("/i/api/graphql/hash/TweetDetail")'
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

    def test_error_words_in_a_real_post_cannot_make_it_absent_or_rate_limited(self):
        text = '有人说「该页面不存在」或「请稍后再试」，但这是一条正常推文。'
        self.load(article(text=text))
        for script in [x_post.READ_JS, x_browser._STATE_JS]:
            snap = json.loads(self.page.eval(script))
            self.assertIs(False, snap.get('empty'))
            self.assertIs(False, snap.get('limited'))
