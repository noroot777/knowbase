"""Exercise the timeline and status readers against real DOM in a fresh headless shell."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory

from kb.feeds import x_browser, x_post
from kb.feeds.x_cdp import Chrome
from tests.headless import INSTALL_HINT, SHELL

MORE = '<button data-testid="tweet-text-show-more-link">显示更多</button>'
LINK = ('<a href="https://t.co/example"><span aria-hidden="true">https://</span>'
        'example.com/<span aria-hidden="true">connections</span>'
        '<span aria-hidden="true">…</span></a>')
TEXT = 'Audit ' + LINK + '<br>Every app<img alt="🙂" src="data:,">\n\n4. Ask first.'
EXPECTED = 'Audit https://example.com/connections\nEvery app🙂\n\n4. Ask first.'


def article(identity='123', text='short', more='', quoted=''):
    return (f'<article data-testid="tweet"><div data-testid="User-Name">Example</div>'
            f'<a href="/Example/status/{identity}">'
            '<time datetime="2026-10-18T00:00:00Z">now</time></a>'
            f'<div data-testid="tweetText">{text}</div>{more}{quoted}</article>')


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
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
        html = article(quoted='<div role="link"><div data-testid="tweetText">quote</div></div>')
        self.load(html.replace('<div data-testid="tweetText">short</div>', ''))
        self.assertEqual('', self.collect()[0]['text'])

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
            ('请稍后再试', False, True),
            ('Loading', False, False),
        ]:
            with self.subTest(text=text):
                self.load('<main>' + text + '</main>')
                for script in [x_post.READ_JS, x_browser._STATE_JS]:
                    snap = json.loads(self.page.eval(script))
                    self.assertIs(empty, snap.get('empty'))
                    self.assertIs(limited, snap.get('limited'))

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

    def test_unknown_language_and_renamed_handles_still_have_a_structural_terminal(self):
        self.load('<main><div data-testid=primaryColumn><div data-testid=emptyState>'
                  '<div data-testid=empty_state_header_text>Unbekannt</div></div></div></main>',
                  '/old_handle')
        snap = json.loads(self.page.eval(x_browser._STATE_JS))
        self.assertEqual('unavailable', snap['account']['state'])
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

    def test_real_zero_text_external_card_is_not_a_loading_failure(self):
        content = ('<div data-testid="card.wrapper"><div data-testid="card.layoutLarge.media">'
                   '<a href="https://t.co/ldgND9KkGu" aria-label="anthropic.com GLM-5.3 and the spread of advanced cyber capabilities">'
                   'GLM-5.3 and the spread of advanced cyber capabilities</a></div></div>')
        self.load(self.media(content).replace('</article>',
                  '<button data-testid=reply></button><button data-testid=like></button></article>'))
        self.assertEqual('link', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_loaded_photo_and_quote_are_empty_only_with_their_own_controls(self):
        image = ('<div data-testid=tweetPhoto><img src="data:image/gif;base64,'
                 'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></div>')
        quote = ('<div role=link><a href=/Other/status/456><time datetime=2026-10-01>now</time></a>'
                 '<div data-testid=tweetText>quoted text is not authored text</div></div>')
        controls = '<button data-testid=reply></button><button data-testid=like></button>'
        for content, kind in [(image, 'photo'), (quote, 'quote')]:
            with self.subTest(kind=kind):
                self.load(self.media(content + controls))
                self.assertEqual(kind, json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])
                self.load(self.media(content))
                self.assertEqual('', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_missing_media_empty_text_nodes_and_article_cards_are_not_legal_empty_posts(self):
        controls = '<button data-testid=reply></button><button data-testid=like></button>'
        for content in ['', '<div data-testid=tweetText></div>',
                        '<div data-testid=tweetPhoto><img src=/missing-image></div>',
                        '<div data-testid=videoPlayer><video></video></div>',
                        '<div data-testid=card.wrapper><a href=/i/article/999>Article</a></div>',
                        '<div data-testid=card.wrapper></div>']:
            with self.subTest(content=content):
                self.load(self.media(content + controls))
                self.assertEqual('', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_real_photo_permalink_is_proof_even_when_media_downloads_are_disabled(self):
        self.load(self.media('<a href=/Example/status/123/photo/1 role=link><div></div></a>'
                             '<button data-testid=reply></button><button data-testid=like></button>'))
        self.assertEqual('photo', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])
        self.load(self.media('<a href=/Other/status/456/photo/1 role=link><div></div></a>'
                             '<button data-testid=reply></button><button data-testid=like></button>'))
        self.assertEqual('', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_video_poster_is_media_evidence_but_an_uninitialized_player_is_not(self):
        self.load(self.media('<div data-testid=videoPlayer><video poster="data:image/gif;base64,'
                             'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">'
                             '</video></div><button data-testid=reply></button>'
                             '<button data-testid=like></button>'))
        self.assertEqual('video', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_a_previously_liked_media_post_is_still_a_legal_empty_post(self):
        self.load(self.media('<a href=/Example/status/123/photo/1></a>'
                             '<button data-testid=reply></button><button data-testid=unlike></button>'))
        self.assertEqual('photo', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])
