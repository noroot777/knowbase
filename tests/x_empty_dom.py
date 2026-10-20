"""Positive empty-authorship DOM contracts, mixed into the shared headless reader suite."""
import json
import re
import time
from pathlib import Path
from unittest.mock import patch

from kb.feeds import x_page_state, x_post

MORE = '<button data-testid="tweet-text-show-more-link">显示更多</button>'


class EmptyDomCases:
    def test_real_core_http_error_does_not_veto_twice_confirmed_target_empty_content(self):
        payload = dict(id_str='123', full_text='', display_text_range=[0, 0], entities={})
        content = self.media('<button data-testid=reply></button><button data-testid=like></button>')
        content += ('<script>document.querySelector("article").__reactFiber$fixture = '
                    + json.dumps(dict(memoizedProps=dict(tweet=payload))) + '</script>')
        for status in [429, 503]:
            with self.subTest(status=status):
                self.server.api_status = status
                self.server.html = content + '<script>fetch("/i/api/graphql/hash/TweetDetail")</script>'
                url = f'http://127.0.0.1:{self.server.server_port}/Example/status/123'
                with patch.object(x_post, '_STATUS', re.compile(r'^http://[^/]+/[^/]+/status/(\d+)')), \
                     patch.object(x_post, 'STEP', 0.01), patch.object(x_post, 'SECONDS', 3):
                    with self.assertRaises(x_post.PostEmpty) as caught:
                        x_post.fetch(url, self.page)
                self.assertEqual('unspecified', caught.exception.kind)
                state = x_page_state.read(self.page, x_post.READ_JS)
                self.assertEqual({'TweetDetail': status}, state['http_content'])
                self.assertEqual('123', state['id'])

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
