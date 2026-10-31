import io
import json
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import browser_capture as capture
from kb.feeds import readable, registry as reg
from kb.web import browser_capture_api
from kb.source_types import catalog, permits_link_body
from tests.test_feed_fulltext import FullTextCase

BODY = '\n\n'.join([
    'Browser capture reads the article that a person has already opened. The user chooses a target item before any text is collected, so the content cannot silently replace an unrelated document. The preview shows the actual address rather than trusting metadata supplied by a website.',
    'An extension keeps its pairing secret in its own local storage. It sends text only to the explicitly configured loopback service, without cookies or form values. Navigation redirects are refused on that connection so a local service cannot send the credential to another server.',
    'A replacement compares both the original content fingerprint and a monotonically increasing revision. If a background worker changes the document while the person is reading, the older request loses permission to overwrite it. This also protects changes that later return to identical text.',
    'Existing translations describe an earlier version and must be invalidated when the source changes. The export receipt remains historical evidence rather than being erased. Later stages regenerate the document and its index; simply storing text is never reported as a completed knowledge-base update.',
    'A selected paragraph is not necessarily a whole article. The person must inspect the complete preview and explicitly confirm its scope. A length threshold helps reject empty pages, but cannot prove that a subscription wall or lazy-loaded section contains everything the author published.',
])


class BrowserCaptureTests(FullTextCase):
    def setUp(self):
        super().setUp()
        capture._intents.clear()
        self.addCleanup(capture._intents.clear)
        app = FastAPI()
        app.include_router(browser_capture_api.router)
        self.client = TestClient(app, base_url='http://127.0.0.1',
                                 client=('127.0.0.1', 54321),
                                 headers={'Sec-Fetch-Site': 'same-origin'})
        self.key = self.put()
        self.auth = {'Authorization': 'Bearer ' + capture.pairing_key(),
                     'Origin': 'chrome-extension://fixture'}

    def intent(self):
        result = self.client.post('/api/browser-capture/intents',
                                  json={'key': self.key, 'page_url': 'https://e.org/post'})
        self.assertEqual(200, result.status_code)
        return result.json()

    def precheck(self, intent, body='', method='article', **overrides):
        payload = dict(intent_id=intent['id'], page_url='https://e.org/post',
                       body=body, method=method)
        payload.update(overrides)
        return self.client.post('/api/browser-capture/precheck', json=payload, headers=self.auth)

    def upload(self, intent, **overrides):
        body = dict(intent_id=intent['id'], page_url='https://e.org/post',
                    title='Browser title', body=BODY, method='article', confirmed_complete=True)
        body.update(overrides)
        return self.client.post('/api/browser-capture/upload', json=body, headers=self.auth)

    def here(self, url='https://e.org/post', auth=True, path='/here', payload=None):
        """扩展开起来那一下问的那一次:我站着这一页,此刻能做什么。"""
        return self.client.post('/api/browser-capture' + path,
                                json=payload if payload is not None else {'url': url},
                                headers=self.auth if auth else {})

    def test_standing_on_the_article_is_enough_to_fill_it(self):
        """**站在那篇文章上就够了** —— 不必先去补抓页发起一个待办。

        原先补一篇正文要跨三个界面走七步,其中「补回哪个待办」那一下是让人把
        服务端已经知道的事再答一遍:地址栏里那一行就是答案。判据落在
        「待办是服务端自己建的」「正文真的写进了原条目」两件事上,**而且全程
        一次 `/intents` 都没调**。
        """
        answer = self.here()
        self.assertEqual(200, answer.status_code, answer.text)
        state = answer.json()
        self.assertEqual('fill', state['situation'])
        self.assertEqual(self.key, state['item']['key'])
        self.assertTrue(state['intent']['id'])
        self.assertEqual('https://e.org/post', state['intent']['page_url'])
        # 反复开扩展不该堆出一串待办:`create` 幂等,同一(条目, 页面)复用那一个
        self.assertEqual(state['intent']['id'], self.here().json()['intent']['id'])
        self.assertEqual(1, len(capture.pending()))
        # 拿它直接提交,补抓页一次都没经过
        saved = self.upload(state['intent'])
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(BODY, self.row(self.key)['body'])

    def test_the_page_is_found_under_either_canonical_form(self):
        """库里那一行的 `url` 是**收它的那条路**存下来的,所以要按两份规范形问。

        订阅类来源原样存 feed 给的地址,手动收录存的是 `grab.normalize`(它去掉
        `utm_*` 这类跟踪参数)。只按一份问,另一条路收的那一半永远答「不在库里」
        —— 而那一句的下一步是**收录**,于是同一篇在库里变成两行。
        反过来钉一边:真正另一篇文章照旧答「不在库里」,不许靠模糊匹配认亲。
        """
        self.assertEqual('fill', self.here('https://e.org/post?utm_source=newsletter')
                         .json()['situation'], '带跟踪参数的同一页要认出来')
        self.assertEqual('absent', self.here('https://e.org/post-two').json()['situation'])
        self.assertEqual('absent', self.here('https://e.org/zh-Hans-CN/post').json()['situation'],
                         '语言前缀是站点自己的约定,不许在这里剥掉认成同一页')

    def test_a_page_outside_the_library_creates_nothing(self):
        """不在库里的那一页:只回一句话,**一个待办都不建**。

        建了的话额度(`MAX_PENDING`)会被随手逛过的页面占满,而它们手上没有任何
        产物 —— 真要补正文的那一条反倒被顶掉。
        """
        state = self.here('https://elsewhere.example.org/x').json()
        self.assertEqual('absent', state['situation'])
        self.assertIsNone(state['item'])
        self.assertIsNone(state['intent'])
        self.assertEqual([], capture.pending())

    def test_a_settled_body_is_not_quietly_offered_for_refilling(self):
        """正文已经定下来的那一条:答「齐了」,**不自动武装一条写入路径**。

        「把缺的那份补上」和「我手上这份是错的、重新采一次」是两件事:后者在补抓
        页上有自己的流程、自己的警示和回执,挤成同一颗按钮会让人分不出点的是哪件。
        反过来钉一边:正文欠着的那一条照旧给(上面那条)。
        """
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET fulltext_at=? WHERE key=?", (reg.now(), self.key))
        state = self.here().json()
        self.assertEqual('complete', state['situation'])
        self.assertIsNone(state['intent'])
        self.assertEqual([], capture.pending())

    def test_an_item_no_subscription_wants_fulltext_for_is_not_owed(self):
        """`_OWED` 的两半各钉一次 —— 这一条钉的是**资格**那一半。

        「正文还欠着」不等于「`fulltext_at` 是空的」:一条内容归属的订阅都没开补抓
        (或者最后一条开着的被关掉、被删掉)时,它压根不该等全文 —— 自动补抓队列
        (`fulltext.queued_sql`)也正是这么判的。只看 `fulltext_at` 的话,这一页会被
        说成「正文还缺着」并自动武装一条写入路径,而面板上它一个待办都没有。
        """
        plain = self.put(self.plain, url='https://e.org/plain')
        state = self.here('https://e.org/plain').json()
        self.assertEqual(plain, state['item']['key'])
        self.assertEqual('complete', state['situation'], '没有订阅要它的全文 = 不欠')
        self.assertEqual([], capture.pending())

    def test_a_request_pointing_at_another_address_is_handed_to_the_person(self):
        """等着的待办指着另一个地址时,**把它摆在人眼前**,而不是只说一句「不在库里」。

        事故形态:站点按浏览器语言跳转(`openai.com/index/x` 在中文浏览器里落到
        `/zh-Hans-CN/index/x`)。按网址一问,这一页确实「不在库里」—— 只凭这一句
        请人收录,库里立刻多出**第二行同一篇文章**,而两行各有各的 key、各导出
        一篇 md。摆出那个待办并给一颗「改用这一页的地址」,他一眼就看出是同一篇。

        反过来钉一边:站在待办指定的那一页上时,这一项是空的(不然每次都挂着一句
        与此刻无关的提示)。
        """
        self.intent()                                  # 指着 https://e.org/post
        landed = self.here('https://e.org/zh-Hans-CN/post').json()
        self.assertEqual('absent', landed['situation'])
        self.assertEqual(['https://e.org/post'], [one['page_url'] for one in landed['elsewhere']])
        self.assertEqual([], self.here().json()['elsewhere'], '就站在它指定的那一页上时不提')

        adopted = self.here(path='/here/adopt',
                            payload={'key': self.key, 'page_url': 'https://e.org/zh-Hans-CN/post'})
        self.assertEqual(200, adopted.status_code, adopted.text)
        self.assertEqual('https://e.org/zh-Hans-CN/post', adopted.json()['page_url'])
        # 改用**不是放行**:正文照旧要过那一整套准入,这里只验「它现在认这一页」
        self.assertEqual(200, self.precheck(adopted.json(), body=BODY,
                                            page_url='https://e.org/zh-Hans-CN/post').status_code)

    def test_elsewhere_never_lists_a_request_that_was_just_superseded(self):
        """`elsewhere` 要在建完待办**之后**算。

        `here()` 自己那次 `create` 会把同一条目上指着别处的旧待办标成
        `superseded`。先算就会把刚被顶掉的那个摆出来、请人「改用这一页的地址」,
        而它已经作废 —— 点下去只会被拒,屏幕上却看不出为什么。
        """
        stale = self.intent()                          # 指着 https://e.org/post
        landed = self.here('https://e.org/post?utm_source=x').json()
        self.assertEqual('fill', landed['situation'])
        self.assertNotEqual(stale['id'], landed['intent']['id'], '指着另一个地址,该另建一个')
        self.assertEqual([], landed['elsewhere'], '刚被顶掉的那个不许摆出来')

    def test_the_page_question_needs_the_pairing_key(self):
        """两个新端点都由调用方指定地址,所以鉴权和 `/upload` 同一档。

        反过来钉一边:带着密钥的那一次照旧放行(否则「一律拒」也能让上面几条绿)。
        """
        for path in ('/here', '/here/adopt'):
            with self.subTest(path=path):
                payload = ({'url': 'https://e.org/post'} if path == '/here'
                           else {'key': self.key, 'page_url': 'https://e.org/post'})
                self.assertEqual(401, self.here(auth=False, path=path, payload=payload).status_code)
                self.assertEqual(200, self.here(path=path, payload=payload).status_code)

    def test_saves_original_item_with_provenance_and_idempotent_receipt(self):
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET exported_hash='old-receipt',body_zh='old translation',"
                         "fulltext_hold='unconfirmed' WHERE key=?", (self.key,))
        intent = self.intent()
        result = self.upload(intent)
        self.assertEqual(200, result.status_code)
        row = self.row(self.key)
        self.assertEqual(BODY, row['body'])
        self.assertEqual('Yes, and', row['title'])
        self.assertEqual('old-receipt', row['exported_hash'])
        self.assertEqual('', row['body_zh'])
        self.assertEqual('', row['fulltext_hold'])
        self.assertEqual('content', row['content_kind'])
        self.assertTrue(row['fulltext_at'])
        self.assertEqual('browser_article', json.loads(row['body_provenance'])['method'])
        self.assertEqual(result.json(), self.upload(intent).json())
        self.assertEqual(row['content_rev'], self.row(self.key)['content_rev'])
        with reg.db() as conn:
            self.assertEqual(1, conn.execute('SELECT count(*) FROM browser_capture_audit').fetchone()[0])

    def test_browser_normalised_address_is_the_same_page(self):
        """人填的网址和浏览器交回来的那一份,规范化之后必须是同一页。

        事故形态:服务端原来直接拼回原字符串、扩展自己 `new URL()` 比一遍 ——
        网址带中文的订阅**每一次**都是人把整篇文章读完、勾完确认才被 409 拒。
        默认端口、路径空格、末尾 `?` 是同一形态的另外三种。反过来钉一边:
        真的是另一篇照旧拒(否则「宽一点」会变成拿别的文章覆盖这一条)。
        """
        shapes = (
            ('https://e.org/文章/第一篇', 'https://e.org/%E6%96%87%E7%AB%A0/%E7%AC%AC%E4%B8%80%E7%AF%87'),
            ('https://e.org/q?k=中文', 'https://e.org/q?k=%E4%B8%AD%E6%96%87'),
            ('https://e.org:443/post2', 'https://e.org/post2'),
            ('https://e.org/a b', 'https://e.org/a%20b'),
            ('https://e.org/trailing?', 'https://e.org/trailing?'),
            ('https://e.org/d/../post3', 'https://e.org/post3'),
            ('https://E.ORG/upper', 'https://e.org/upper'),
        )
        for typed, from_browser in shapes:
            with self.subTest(typed=typed):
                key = self.put(url=typed)
                request = capture.create(key, typed)
                self.assertEqual(200, self.client.post(
                    '/api/browser-capture/precheck', headers=self.auth,
                    json={'intent_id': request['id'], 'page_url': from_browser,
                          'method': 'article'}).status_code)
                self.assertEqual(200, self.upload(request, page_url=from_browser).status_code)
                self.assertEqual(BODY, self.row(key)['body'])
        other = self.put(url='https://e.org/apart')
        request = capture.create(other, 'https://e.org/apart')
        for wrong in ('https://e.org/apart-two', 'https://e.org/Apart', 'http://e.org/apart',
                      'https://e.org/apart/', 'https://e.org/%2Fapart'):
            with self.subTest(wrong=wrong):
                self.assertEqual(409, self.client.post(
                    '/api/browser-capture/precheck', headers=self.auth,
                    json={'intent_id': request['id'], 'page_url': wrong,
                          'method': 'article'}).status_code)
                self.assertEqual(409, self.upload(request, page_url=wrong).status_code)
        self.assertNotEqual(BODY, self.row(other)['body'])

    def test_refused_page_is_handed_back_so_the_person_can_switch_to_it(self):
        """浏览器停在另一个地址:拒绝那一刻的真实网址要带回补抓页,**可一键改用**。

        事故形态:站点按浏览器语言跳转(`openai.com/index/x` 在中文浏览器里落到
        `/zh-Hans-CN/index/x`),这一类订阅**每一次**都被拒,而人只看到一句
        「网址不同」—— 真实网址服务端此刻就拿着,却丢掉了;更坏的是原来那句
        「回知识库核对最终网址并重新发起」在页面上**当场做不到**(有待办时那一栏
        和「发起」都禁用,得先取消待办)。
        **记下来不等于放行**:原请求照旧只认它绑定的那一页,改用它要人另发一次。
        反过来钉一边:没撞上网址不同时那一栏一直是空的(否则页面上会常驻一颗
        指向别处的按钮)。
        """
        key = self.put(url='https://openai.com/index/openai-models-google-cloud/')
        request = capture.create(key, 'https://openai.com/index/openai-models-google-cloud/')
        self.assertEqual(200, self.precheck(request, page_url=request['page_url']).status_code)
        self.assertEqual('', capture.item_state(key)['pending'][0]['seen_url'],
                         '没撞上就不该记下任何「另一页」')
        landed = 'https://openai.com/zh-Hans-CN/index/openai-models-google-cloud/'
        self.assertEqual(409, self.precheck(request, page_url=landed).status_code)
        self.assertEqual(landed, capture.item_state(key)['pending'][0]['seen_url'])
        # 记下来**不是**放行:这个请求仍然只写得了它绑定的那一页,正文一个字没动
        self.assertEqual(409, self.upload(request, page_url=landed).status_code)
        self.assertNotEqual(BODY, self.row(key)['body'])
        # `save` 那条路共用同一份判据,也记同一处(扩展先读后传的那一次)
        self.assertEqual(409, self.upload(request, page_url=landed + '?utm=1').status_code)
        self.assertEqual(landed + '?utm=1', capture.item_state(key)['pending'][0]['seen_url'])
        # 人点下「改用这个地址」= 拿它另发一个请求,旧的被服务端顶掉
        switched = capture.create(key, landed)
        self.assertEqual('', switched['seen_url'])
        self.assertEqual([switched['id']],
                         [one['id'] for one in capture.item_state(key)['pending']])
        self.assertEqual(200, self.precheck(switched, page_url=landed).status_code)
        self.assertEqual(200, self.upload(switched, page_url=landed).status_code)
        self.assertEqual(BODY, self.row(key)['body'])
        # 被顶掉的那个不再改写它记下的那一份(页面上只显示 pending 的那一个)
        self.assertEqual(409, self.precheck(request, page_url='https://openai.com/ja-JP/x').status_code)
        self.assertEqual(landed + '?utm=1', capture._intents[request['id']]['seen_url'])

    def test_precheck_refuses_before_reading_with_the_same_verdict_as_upload(self):
        """读之前先问一次,判据和 `save` 同一组 —— 否则人白读一整页。

        三种在扩展侧曾经放行、服务端却拒的形状:同一段铺满整页的提示页(扩展
        数原始长度过线、服务端按行去重只剩两百来字)、读的那几分钟里条目被
        同步改过、请求已经过期。`precheck` 一个字都不落库。
        """
        repeated = 'Sign in to continue reading this story. Subscribe to unlock it now.\n' * 17
        # 这条用例的**前提**:原始长度过得了那道门槛(扩展曾经那么数,所以放行),
        # 按行去重之后过不了(服务端拒)。门槛的唯一源头是 `readable.MIN_CHARS` ——
        # 原来这里写的是实测记下的魔数 1300,而这段文字只有 1156 字,前提断言从
        # 引入那天起就恒假,底下那十来条**一条都没跑到**(基线既有)。
        self.assertGreater(len(repeated), readable.MIN_CHARS)
        self.assertLess(readable.substance(repeated), readable.MIN_CHARS)
        request = self.intent()
        before = self.row(self.key)
        thin = self.precheck(request, body=repeated)
        self.assertEqual(409, thin.status_code)
        self.assertEqual(thin.json()['detail'], self.upload(request, body=repeated).json()['detail'])
        self.assertEqual(200, self.precheck(request, body=BODY).status_code)
        self.assertEqual(len(BODY), self.precheck(request, body=BODY).json()['characters'])
        self.assertEqual(before, self.row(self.key))
        with reg.db() as conn:
            self.assertEqual(0, conn.execute('SELECT count(*) FROM browser_capture_audit').fetchone()[0])
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET content_rev=content_rev+1 WHERE key=?', (self.key,))
        stale = self.precheck(request)
        self.assertEqual(409, stale.status_code)
        self.assertEqual(capture._STALE, stale.json()['detail'])
        expired = self.intent()
        with patch.object(capture.time, 'time', return_value=time.time() + capture.TTL + 1):
            self.assertEqual(404, self.precheck(expired).status_code)
        self.assertEqual(422, self.precheck(self.intent(), method='screenshot').status_code)

    def test_wrong_page_unconfirmed_and_short_body_cannot_write(self):
        intent = self.intent()
        before = self.row(self.key)
        for overrides in ({'page_url': 'https://e.org/another'},
                          {'confirmed_complete': False}, {'body': 'short'}):
            self.assertGreaterEqual(self.upload(intent, **overrides).status_code, 400)
        self.assertEqual(before['body'], self.row(self.key)['body'])

    def test_revision_change_even_with_same_hash_blocks_overwrite(self):
        intent = self.intent()
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET content_rev=content_rev+2 WHERE key=?', (self.key,))
        self.assertEqual(409, self.upload(intent).status_code)
        self.assertEqual('stale', capture.get(intent['id'])['status'])
        self.assertNotEqual(BODY, self.row(self.key)['body'])

    def test_excluded_deleted_cancelled_and_expired_targets_are_not_revived(self):
        intent = self.intent()
        with reg.db() as conn:
            conn.execute('INSERT INTO feed_item_exclusions(key,kind,url,created_at) VALUES (?,?,?,?)',
                         (self.key, 'rss', 'https://e.org/post', reg.now()))
        self.assertEqual(409, self.upload(intent).status_code)
        with reg.db() as conn:
            conn.execute('DELETE FROM feed_item_exclusions WHERE key=?', (self.key,))
        cancelled = self.intent()
        self.client.delete('/api/browser-capture/intents/' + cancelled['id'])
        self.assertEqual(409, self.upload(cancelled).status_code)
        expired = self.intent()
        with patch.object(capture.time, 'time', return_value=time.time() + capture.TTL + 1):
            self.assertEqual(404, self.upload(expired).status_code)
        deleted = self.intent()
        with reg.db() as conn:
            conn.execute('DELETE FROM subscription_items WHERE item_key=?', (self.key,))
            conn.execute('DELETE FROM feed_items WHERE key=?', (self.key,))
        self.assertEqual(404, self.upload(deleted).status_code)

    def test_requires_local_same_origin_and_paired_extension(self):
        intent = self.intent()
        data = dict(intent_id=intent['id'], page_url='https://e.org/post', title='title',
                    body=BODY, method='article', confirmed_complete=True)
        self.assertEqual(401, self.client.post('/api/browser-capture/upload', json=data).status_code)
        self.assertEqual(403, self.client.get('/api/browser-capture/pairing',
                                             headers={'Origin': 'https://evil.example'}).status_code)
        self.assertEqual(403, self.client.get('/api/browser-capture/pairing',
                                             headers={'Sec-Fetch-Site': 'none'}).status_code)
        self.assertEqual(403, self.client.get('/api/browser-capture/pairing',
                                             headers={'Host': 'evil.example'}).status_code)
        self.assertEqual(403, self.client.get('/api/browser-capture/pending',
                                             headers={**self.auth, 'Origin': 'https://evil.example'}).status_code)

    def test_post_items_and_unsafe_urls_are_not_capture_targets(self):
        self.assertEqual(409, self.client.post('/api/browser-capture/intents',
            json={'key': self.key, 'page_url': 'javascript:alert(1)'}).status_code)
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET content_kind='post' WHERE key=?", (self.key,))
        self.assertEqual(409, self.client.get('/api/browser-capture/target/' + self.key).status_code)

    def test_continuation_is_separate_and_refuses_busy_pipeline_without_losing_body(self):
        intent = self.intent()
        self.assertEqual(409, self.client.post('/api/browser-capture/intents/' + intent['id'] + '/continue').status_code)
        self.assertEqual(200, self.upload(intent).status_code)
        with patch.object(browser_capture_api.pipeline, 'start_pipeline', return_value=False):
            self.assertEqual(409, self.client.post('/api/browser-capture/intents/' + intent['id'] + '/continue').status_code)
        self.assertEqual(BODY, self.row(self.key)['body'])

    def test_source_capability_blocks_digest_even_when_subscription_fulltext_is_enabled(self):
        self.assertTrue(permits_link_body('rss'))
        self.assertTrue(permits_link_body('web'))
        self.assertTrue(permits_link_body('inbox'))
        self.assertFalse(permits_link_body('digest'))
        self.assertFalse(permits_link_body('social'))
        # YouTube 的正文是转录稿,原文链接后面那一页是播放页:从浏览器补抓会拿
        # 页面文字把转录稿整篇换掉,而那一步不可逆。
        self.assertFalse(permits_link_body('youtube'))
        self.assertFalse(permits_link_body('unknown'))
        modules = {item['id']: item for item in catalog()}
        self.assertFalse(modules['digest']['body_from_link'])
        self.assertTrue(modules['rss']['body_from_link'])
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET kind='digest',content_kind='summary' WHERE key=?",
                         (self.key,))
            conn.execute('UPDATE subscriptions SET fulltext=1')
        before = self.row(self.key)
        self.assertEqual(409, self.client.get('/api/browser-capture/target/' + self.key).status_code)
        result = self.client.post('/api/browser-capture/intents',
                                  json={'key': self.key, 'page_url': before['url']})
        self.assertEqual(409, result.status_code)
        self.assertEqual(before, self.row(self.key))

    def test_saved_receipt_survives_expiry_restart_and_idempotent_upload(self):
        intent = self.intent()
        receipt = self.upload(intent).json()
        revision = self.row(self.key)['content_rev']
        with patch.object(capture.time, 'time', return_value=intent['expires_at'] + 1):
            restored = self.client.get('/api/browser-capture/intents/' + intent['id'])
            self.assertEqual(200, restored.status_code)
            self.assertEqual('saved', restored.json()['status'])
            self.assertEqual(receipt, restored.json()['receipt'])
            self.assertEqual(receipt, self.upload(intent).json())
        capture._intents.clear()
        self.assertEqual('saved', capture.get(intent['id'])['status'])
        with patch.object(browser_capture_api.pipeline, 'start_pipeline', return_value='continued') as start:
            result = self.client.post('/api/browser-capture/intents/' + intent['id'] + '/continue')
        self.assertEqual(200, result.status_code)
        self.assertEqual('continued', result.json()['run'])
        self.assertEqual({'channel': 'rss'}, start.call_args.kwargs['crawl_filter'])
        self.assertEqual(revision, self.row(self.key)['content_rev'])

    def test_persisted_continuation_refuses_a_newer_revision(self):
        intent = self.intent()
        self.assertEqual(200, self.upload(intent).status_code)
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET content_rev=content_rev+1 WHERE key=?', (self.key,))
        with patch.object(browser_capture_api.pipeline, 'start_pipeline') as start:
            result = self.client.post('/api/browser-capture/intents/' + intent['id'] + '/continue')
        self.assertEqual(409, result.status_code)
        start.assert_not_called()

    def test_terminal_requests_do_not_consume_capacity_and_a_full_pool_refuses_nobody(self):
        """终态请求不占额度;池满时顶掉**最老的那个**,不拒人。

        额度是全局的,而取消的出口只开在各自那篇文章的补抓页上 —— 撞上额度就拒的话,
        人得挨个翻完那 100 篇才发得出第 101 次。等待中的请求手上没有任何产物,顶掉
        最老的那个(它也最接近过期)只值一次重新点击,而它那篇文章一个字都没被写。
        反过来钉一边:后面那 99 个照旧等着,已经保存的回执压根不在池子里。
        """
        for iteration in range(100):
            intent = self.intent()
            if iteration % 2:
                self.assertEqual(200, self.upload(intent).status_code)
            else:
                self.client.delete('/api/browser-capture/intents/' + intent['id'])
        self.assertEqual([], capture.pending())
        receipt = self.client.get('/api/browser-capture/target/' + self.key).json()['saved']
        oldest = oldest_key = None
        for iteration in range(capture.MAX_PENDING):
            key = self.put(url=f'https://e.org/pending-{iteration}')
            request = capture.create(key, f'https://e.org/pending-{iteration}')
            if oldest is None:
                oldest, oldest_key = request, key
        self.assertEqual(capture.MAX_PENDING, len(capture.pending()))
        mine = self.intent()
        self.assertEqual('pending', mine['status'])
        waiting = [request['id'] for request in capture.pending()]
        self.assertEqual(capture.MAX_PENDING, len(waiting))
        self.assertIn(mine['id'], waiting)
        self.assertNotIn(oldest['id'], waiting)
        displaced = self.upload(oldest, page_url='https://e.org/pending-0')
        self.assertEqual(capture._NOT_PENDING, displaced.json()['detail'])
        self.assertNotEqual(BODY, self.row(oldest_key)['body'])
        self.assertEqual(receipt, self.client.get('/api/browser-capture/target/' + self.key).json()['saved'])

    def test_reingested_item_does_not_inherit_the_previous_life_receipt(self):
        """清理掉又重新收录的那篇文章,页面上不许留着上一世那张回执。

        事故形态:`item_key` 按网址算,删掉再从同一个链接收回来还是同一个 key,而
        回执按 key 取最新一行 —— 新条目的 `content_rev` 从 0 起,跟回执记的那一版
        怎么比都不等,于是那一页常驻一句假的「本次保存后正文已被修改」:回执区被
        藏掉、继续处理按钮是灰的,重抓一次也去不掉(那一行记的是上一世)。
        两种收回来的形状各走一条判据:隔了一秒看 `fetched_at`,同一秒里看倒退的
        `content_rev`(`fetched_at` 只记到秒)。反过来钉两边:新的一世照旧补得进去,
        而同一世里正文真被别人换掉,那句红字照旧要出来。
        """
        intent = self.intent()
        self.assertEqual(200, self.upload(intent).status_code)
        original = self.row(self.key)
        self.assertEqual(intent['id'],
                         self.client.get('/api/browser-capture/target/' + self.key).json()['saved']['id'])

        def reingest(fetched_at):
            capture._intents.clear()
            # 存量清理只删条目,它不认识审计表(`scripts/feed_trim_history.py`)。
            with reg.db() as conn:
                conn.execute('DELETE FROM subscription_items WHERE item_key=?', (self.key,))
                conn.execute('DELETE FROM feed_items WHERE key=?', (self.key,))
            self.assertEqual(self.key, self.put())
            with reg.db() as conn:
                conn.execute('UPDATE feed_items SET fetched_at=? WHERE key=?', (fetched_at, self.key))
            return self.client.get('/api/browser-capture/target/' + self.key).json()

        for label, fetched_at in (('同一秒收回来', original['fetched_at']),
                                  ('隔一段时间收回来', '2026-10-20T09:00:00+00:00')):
            with self.subTest(label):
                state = reingest(fetched_at)
                self.assertIsNone(state['saved'])
                self.assertEqual('', state['saved_error'])
                self.assertEqual([], state['pending'])
        fresh = self.intent()
        self.assertEqual(200, self.upload(fresh).status_code)
        state = self.client.get('/api/browser-capture/target/' + self.key).json()
        self.assertEqual(fresh['id'], state['saved']['id'])
        # 老库里那些没记身份的回执行同样按「没有回执」办,不是「被改过」。
        with reg.db() as conn:
            conn.execute("UPDATE browser_capture_audit SET item_fetched_at='' WHERE id=?", (fresh['id'],))
        legacy = self.client.get('/api/browser-capture/target/' + self.key).json()
        self.assertIsNone(legacy['saved'])
        self.assertEqual('', legacy['saved_error'])
        with reg.db() as conn:
            conn.execute('UPDATE browser_capture_audit SET item_fetched_at=? WHERE id=?',
                         (self.row(self.key)['fetched_at'], fresh['id']))
        with reg.db() as conn:
            self.assertEqual([fresh['id']], [row['id'] for row in conn.execute(
                'SELECT id FROM browser_capture_audit WHERE item_key=?', (self.key,))])
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET body=?,content_rev=content_rev+1 WHERE key=?',
                         ('另一个写入方换掉的正文', self.key))
        state = self.client.get('/api/browser-capture/target/' + self.key).json()
        self.assertIsNone(state['saved'])
        self.assertIn('已被修改', state['saved_error'])

    def test_continue_runs_export_and_index_but_never_a_whole_library_extract(self):
        """这颗按钮的射程必须等于它承诺的那些,`extract` 不在里面。

        事故形态:按钮写着「该来源」,而 `extract` 这一段的队列
        (`kb.extract.pending_docs`)压根没有来源参数 —— 点一下买下的是**全库**那一轮
        条目化提取,钱按全库待办数花。反过来钉一边:导出仍然只限这一类来源,而三处
        文案(页面、确认框、回执)不许再承诺提取。
        """
        self.assertNotIn('extract', browser_capture_api.CONTINUE_STAGES)
        intent = self.intent()
        self.assertEqual(200, self.upload(intent).status_code)
        with patch.object(browser_capture_api.pipeline, 'start_pipeline', return_value='run-7') as start:
            result = self.client.post('/api/browser-capture/intents/' + intent['id'] + '/continue')
        self.assertEqual(200, result.status_code)
        self.assertEqual(['export', 'index'], start.call_args.args[0])
        self.assertEqual({'channel': 'rss'}, start.call_args.kwargs['crawl_filter'])
        page = (browser_capture_api.ROOT / 'kb/static/browser-capture.html').read_text(encoding='utf-8')
        script = (browser_capture_api.ROOT / 'kb/static/js/browser_capture.js').read_text(encoding='utf-8')
        for place in (page, script, result.json()['detail']):
            self.assertIn('条目化提取不在这一步', place)
            self.assertNotIn('提取模型', place)

    def test_extension_bundle_and_both_pages_carry_the_shared_error_reader(self):
        """下发的 ZIP 要带上扩展目录里的每个文件;两个页面都要在 `ui.js` 之前引 `api.js`。

        `apiFail` / `apiResponse` 只有一份实现,住在扩展目录(扩展装在浏览器里,取不到
        `kb/static/js/` 下的文件)。页面漏引它的后果是**所有失败请求当场
        ReferenceError**:一句话都不弹,而成功路径一切正常;ZIP 漏一个文件的后果是
        装进浏览器的扩展当场打不开,而这边一切正常。
        """
        folder = browser_capture_api.ROOT / 'kb/static/browser-extension/knowbase-capture'
        shipped = sorted(path.name for path in folder.iterdir()
                         if path.is_file() and not path.name.startswith('.'))
        archive = self.client.get('/api/browser-capture/extension.zip')
        self.assertEqual(200, archive.status_code)
        with zipfile.ZipFile(io.BytesIO(archive.content)) as bundle:
            self.assertEqual(shipped, sorted(name.split('/')[-1] for name in bundle.namelist()))
        for page in ('kb/static/index.html', 'kb/static/browser-capture.html'):
            text = (browser_capture_api.ROOT / page).read_text(encoding='utf-8')
            self.assertLess(text.index('knowbase-capture/api.js'), text.index('/static/js/ui.js'), page)

    def test_item_state_restores_latest_saved_receipt_without_an_intent_id(self):
        original = self.intent()
        self.assertEqual(200, self.upload(original).status_code)
        newer = self.intent()
        self.assertEqual(200, self.upload(newer, body=BODY + '\nNewer article version.').status_code)
        capture._intents.clear()
        result = self.client.get('/api/browser-capture/target/' + self.key)
        self.assertEqual(200, result.status_code)
        self.assertEqual(newer['id'], result.json()['saved']['id'])
        self.assertEqual([], result.json()['pending'])

    def test_same_item_concurrent_create_reuses_one_visible_cancellable_request(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            requests = list(pool.map(lambda attempt: capture.create(self.key, 'https://e.org/post'), range(8)))
        self.assertEqual(1, len({request['id'] for request in requests}))
        state = self.client.get('/api/browser-capture/target/' + self.key).json()
        self.assertEqual(1, len(state['pending']))
        self.assertIn('created_at', state['pending'][0])
        self.assertEqual(200, self.client.delete('/api/browser-capture/intents/' + requests[0]['id']).status_code)
        self.assertEqual([], self.client.get('/api/browser-capture/target/' + self.key).json()['pending'])

    def test_changed_address_or_item_version_supersedes_pending(self):
        original = self.intent()
        replacement = capture.create(self.key, 'https://e.org/final')
        self.assertNotEqual(original['id'], replacement['id'])
        self.assertEqual('superseded', capture.get(original['id'])['status'])
        self.assertEqual([replacement['id']], [request['id'] for request in capture.pending()])
        self.assertEqual(409, self.upload(original).status_code)
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET content_rev=content_rev+1 WHERE key=?', (self.key,))
        self.assertEqual([], capture.item_state(self.key)['pending'])
        self.assertEqual('stale', capture.get(replacement['id'])['status'])

    def test_saved_validation_is_identical_with_or_without_memory(self):
        request = self.intent()
        self.assertEqual(200, self.upload(request).status_code)
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET body=? WHERE key=?', ('Another body without a revision bump.', self.key))
        warm = self.client.get('/api/browser-capture/intents/' + request['id'])
        self.assertEqual(409, warm.status_code)
        capture._intents.clear()
        cold = self.client.get('/api/browser-capture/intents/' + request['id'])
        self.assertEqual(warm.json(), cold.json())
        state = self.client.get('/api/browser-capture/target/' + self.key).json()
        self.assertIsNone(state['saved'])
        self.assertEqual(warm.json()['detail'], state['saved_error'])

    def test_extension_pending_rejects_stale_items_without_opening_the_capture_page(self):
        changes = (
            "UPDATE feed_items SET content_rev=content_rev+1 WHERE key=?",
            "UPDATE feed_items SET content_hash='new-hash' WHERE key=?",
            "UPDATE feed_items SET kind='web' WHERE key=?",
            "UPDATE feed_items SET kind='digest' WHERE key=?",
            "UPDATE feed_items SET url='https://e.org/changed' WHERE key=?",
            "UPDATE feed_items SET content_kind='post' WHERE key=?",
            "UPDATE feed_items SET x_post_meta='{}' WHERE key=?",
            "INSERT INTO feed_item_exclusions(key,kind,url,created_at) "
            "SELECT key,kind,url,'2026-10-26 12:00' FROM feed_items WHERE key=?",
            "DELETE FROM feed_items WHERE key=?",
        )
        healthy = self.intent()
        for index, statement in enumerate(changes):
            with self.subTest(statement=statement):
                key = self.put(url=f'https://e.org/stale-{index}')
                request = capture.create(key, 'https://e.org/post')
                with reg.db() as conn:
                    conn.execute('DELETE FROM subscription_items WHERE item_key=?', (key,))
                    conn.execute(statement, (key,))
                response = self.client.get('/api/browser-capture/pending', headers=self.auth)
                self.assertEqual(200, response.status_code)
                self.assertEqual([healthy['id']], [item['id'] for item in response.json()['intents']])
                self.assertEqual('stale', capture.get(request['id'])['status'])
                self.assertEqual(409, self.upload(request).status_code)
                with reg.db() as conn:
                    self.assertEqual(0, conn.execute('SELECT count(*) FROM browser_capture_audit '
                                                     'WHERE id=?', (request['id'],)).fetchone()[0])
        self.assertEqual(200, self.upload(healthy).status_code)
        self.assertEqual(BODY, self.row(self.key)['body'])

    def test_extension_pending_reads_all_current_items_in_one_connection(self):
        first = self.intent()
        key = self.put(url='https://e.org/second')
        second = capture.create(key, 'https://e.org/second')
        with patch.object(reg, 'db', wraps=reg.db) as database:
            waiting = capture.pending()
        self.assertEqual(1, database.call_count)
        self.assertEqual({first['id'], second['id']}, {request['id'] for request in waiting})

    def test_save_keeps_transactional_revision_guard_even_if_preflight_allows_it(self):
        request = self.intent()
        before = self.row(self.key)['body']
        with reg.db() as conn:
            conn.execute('UPDATE feed_items SET content_rev=content_rev+1 WHERE key=?', (self.key,))
        with patch.object(capture, 'writable', return_value=True):
            result = self.upload(request)
        self.assertEqual(409, result.status_code)
        self.assertEqual(before, self.row(self.key)['body'])
        self.assertEqual('stale', capture.get(request['id'])['status'])
