"""Exercise the timeline and status readers against real DOM in a fresh headless shell."""
import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_real_zero_text_external_card_is_not_a_loading_failure(self):
        content = ('<div data-testid="card.wrapper"><div data-testid="card.layoutLarge.media">'
                   '<a href="https://t.co/ldgND9KkGu" aria-label="anthropic.com GLM-5.3 and the spread of advanced cyber capabilities">'
                   'GLM-5.3 and the spread of advanced cyber capabilities</a></div></div>')
        self.load(self.media(content).replace('</article>',
                  '<button data-testid=reply></button><button data-testid=like></button></article>'))
        self.bind_payload('link')
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
                self.bind_payload(kind)
                self.assertEqual(kind, json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])
                self.load(self.media(content))
                self.bind_payload(kind)
                self.assertEqual('', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_captured_cta_gif_and_late_media_use_the_target_payload_not_ui_enumeration(self):
        samples = json.loads((Path(__file__).parent / 'fixtures/x_empty_detail_states.json').read_text())
        for sample in samples:
            with self.subTest(name=sample['name']):
                self.load(sample['html'], '/' + sample['url'].split('/', 3)[3])
                self.bind_payload(tweet=sample['tweet'])
                snap = json.loads(self.page.eval(x_post.READ_JS))
                self.assertEqual(sample['expected_kind'], snap['empty_kind'])
                self.assertIs(not bool(sample['expected_kind']) and not sample.get('loading', False),
                              snap['unknown_structure'])
                with patch.object(self.page, 'goto', return_value={}), \
                     patch.object(x_post, 'STEP', 0.001), patch.object(x_post, 'STRUCTURE_SECONDS', 0.01):
                    if sample['expected_kind']:
                        with self.assertRaises(x_post.PostEmpty) as caught:
                            x_post.fetch(sample['url'], self.page)
                        self.assertEqual(sample['expected_kind'], caught.exception.kind)
                    elif not sample.get('loading'):
                        self.assertTrue(sample['tweet']['full_text'].strip())
                        with self.assertRaises(x_post.PostStructureUnknown):
                            x_post.fetch(sample['url'], self.page)
                    else:
                        with self.assertRaises(x_post.PostUnavailable) as caught:
                            x_post.fetch(sample['url'], self.page, deadline=time.monotonic() + 0.01)
                        self.assertNotIsInstance(caught.exception, (x_post.PostEmpty, x_post.PostStructureUnknown))

    def test_empty_data_proof_is_independent_of_media_and_interface_text(self):
        controls = '<button data-testid=reply></button><button data-testid=like></button>'
        for kind in ['photo', 'video', 'quote', 'link', 'unspecified']:
            with self.subTest(kind=kind):
                self.load(self.media('<div>任意语言的关注/订阅提示</div>' + controls))
                if kind == 'unspecified':
                    self.bind_payload(tweet=dict(id_str='123', full_text='', display_text_range=[0, 0]))
                else:
                    self.bind_payload(kind)
                self.assertEqual(kind, json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_gif_player_shell_without_native_video_or_loaded_images_is_not_a_failure(self):
        self.load(self.media('<div data-testid=tweetPhoto><div data-testid=placementTracking>'
                             '<div data-testid=videoPlayer><div data-testid=videoComponent></div>'
                             '</div></div></div><button data-testid=reply></button>'
                             '<button data-testid=like></button>'))
        self.bind_payload('video')
        self.assertEqual(0, self.page.eval('document.querySelectorAll("video,img").length'))
        self.assertEqual('video', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_only_structured_hidden_reply_mentions_are_non_authored_context(self):
        samples = json.loads((Path(__file__).parent / 'fixtures/x_empty_detail_states.json').read_text())
        sample = next(sample for sample in samples if sample['name'] == 'photo-lennysan')
        self.load(sample['html'], '/' + sample['url'].split('/', 3)[3])
        self.bind_payload(tweet=sample['tweet'])
        self.assertEqual('photo', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])
        good = sample['tweet']
        invalid = [dict(good, in_reply_to_status_id_str=None), dict(good, in_reply_to_user_id_str=None),
                   dict(good, display_text_range=[0, 11]),
                   dict(good, entities={'media': good['entities']['media']}),
                   dict(good, full_text=good['full_text'] + ' keep my words')]
        for tweet in invalid:
            with self.subTest(tweet=tweet):
                self.bind_payload(tweet=tweet)
                self.assertEqual('', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_missing_or_conflicting_data_cannot_approve_a_missing_body_container(self):
        controls = '<button data-testid=reply></button><button data-testid=like></button>'
        self.load(self.media('<div data-testid=postTextVNext>Keep my own words</div>' + controls))
        self.bind_payload('link')
        good = self.page.eval('document.querySelector("article").__reactFiber$fixture.memoizedProps.tweet')
        invalid = [dict(good, id_str='999'), dict(good, full_text='Keep my own words'),
                   dict(good, article={'id': '999'}), dict(good, note_tweet={'text': 'long text'}),
                   dict(good, display_text_range=None), dict(good, display_text_range=[0, 999]),
                   dict(good, entities={'media': [None]}),
                   dict(good, entities={'urls': [{'url': good['full_text'], 'indices': [0, 999]}]}),
                   dict(good, entities={'urls': [{'url': good['full_text'], 'indices': [0, 1]}]}),
                   dict(good, card=None)]
        for tweet in invalid:
            with self.subTest(tweet=tweet):
                self.bind_payload(tweet=tweet)
                self.assertEqual('', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_payload_proof_never_overrides_loading_folding_or_article_evidence(self):
        controls = '<button data-testid=reply></button><button data-testid=like></button>'
        for blocker in ['<div role=progressbar></div>', '<div aria-busy=true></div>', MORE,
                        '<a href=/i/article/999>Article</a>', '<div data-testid=tweetText>own text</div>']:
            with self.subTest(blocker=blocker):
                self.load(self.media(blocker + controls))
                self.bind_payload('photo')
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

    def test_photo_payload_is_proof_even_when_media_downloads_are_disabled(self):
        self.load(self.media('<a href=/Example/status/123/photo/1 role=link><div></div></a>'
                             '<button data-testid=reply></button><button data-testid=like></button>'))
        self.bind_payload('photo')
        self.assertEqual('photo', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])
        self.load(self.media('<a href=/Other/status/456/photo/1 role=link><div></div></a>'
                             '<button data-testid=reply></button><button data-testid=like></button>'))
        self.assertEqual('', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_video_payload_does_not_require_an_initialized_player(self):
        self.load(self.media('<div data-testid=videoPlayer><video poster="data:image/gif;base64,'
                             'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7">'
                             '</video></div><button data-testid=reply></button>'
                             '<button data-testid=like></button>'))
        self.bind_payload('video')
        self.assertEqual('video', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_a_previously_liked_media_post_is_still_a_legal_empty_post(self):
        self.load(self.media('<a href=/Example/status/123/photo/1></a>'
                             '<button data-testid=reply></button><button data-testid=unlike></button>'))
        self.bind_payload('photo')
        self.assertEqual('photo', json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])

    def test_unrecognized_authored_content_blocks_every_empty_media_kind(self):
        controls = '<button data-testid=reply></button><button data-testid=like></button>'
        samples = [
            '<a href=/Example/status/123/photo/1></a>',
            '<div data-testid=videoPlayer><video poster="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></video></div>',
            '<div data-testid=quoteTweet><a href=/Other/status/456>source</a>'
            '<div data-testid=tweetText>quoted text</div></div>',
            '<div data-testid=card.wrapper><a href=https://example.org>linked title</a></div>',
        ]
        for media in samples:
            for authored in ['<div data-testid=postTextVNext>Do not discard my text</div>',
                             '<img alt="🙂" src="data:,">']:
                with self.subTest(media=media, authored=authored):
                    self.load(self.media(authored + media + controls))
                    self.bind_payload('photo', text='Do not discard my text 🙂')
                    snap = json.loads(self.page.eval(x_post.READ_JS))
                    self.assertEqual('', snap['text'])
                    self.assertEqual('', snap['empty_kind'])
                    with patch.object(self.page, 'goto', return_value={}), \
                         patch.object(x_post, 'SECONDS', 0.03), patch.object(x_post, 'STEP', 0.001):
                        with self.assertRaises(x_post.PostUnavailable) as caught:
                            x_post.fetch('https://x.com/Example/status/123', self.page)
                    self.assertNotIsInstance(caught.exception, x_post.PostEmpty)

    def test_known_detail_metadata_does_not_block_a_proved_empty_photo(self):
        self.load('<article data-testid=tweet><div data-testid=Tweet-User-Avatar></div>'
                  '<div data-testid=User-Name>Example @Example</div>'
                  '<div><div><a href=/Example/status/123><time datetime=2026-10-18>now</time></a></div>'
                  '<div><span>·</span></div><a href=/Example/status/123/analytics>1,857 查看</a></div>'
                  '<a href=/Example/status/123/photo/1><div></div></a>'
                  '<button data-testid=reply>2</button><button data-testid=like>22</button>'
                  '<button>相关</button><a href=/Example/status/123/quotes>查看引用</a></article>')
        self.bind_payload('photo')
        with patch.object(self.page, 'goto', return_value={}), patch.object(x_post, 'STEP', 0.001):
            with self.assertRaises(x_post.PostEmpty) as caught:
                x_post.fetch('https://x.com/Example/status/123', self.page)
        self.assertEqual('photo', caught.exception.kind)

    def test_external_card_source_footer_and_multiple_photos_are_known_media_content(self):
        image = ('<div data-testid=tweetPhoto><img alt="图像" src="data:image/gif;base64,'
                 'R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"></div>')
        for content, kind in [
            ('<div><div data-testid=card.wrapper><a href=https://example.org>title</a></div>'
             '<div><a href=https://example.org>来自 example.org</a></div></div>', 'link'),
            (image + image, 'photo'),
        ]:
            with self.subTest(kind=kind):
                self.load(self.media(content + '<button data-testid=reply></button>'
                                    '<button data-testid=like></button>'))
                self.bind_payload(kind)
                self.assertEqual(kind, json.loads(self.page.eval(x_post.READ_JS))['empty_kind'])
