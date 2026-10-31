from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import browser_capture as capture
from kb.feeds import readable, registry as reg
from kb.web import browser_capture_api
from tests.test_feed_fulltext import FullTextCase

BODY = '\n\n'.join([
    'Browser capture reads the article that a person has already opened. The user chooses a target item before any text is collected, so the content cannot silently replace an unrelated document. The preview shows the actual address rather than trusting metadata supplied by a website.',
    'An extension keeps its pairing secret in its own local storage. It sends text only to the explicitly configured loopback service, without cookies or form values. Navigation redirects are refused on that connection so a local service cannot send the credential to another server.',
    'A replacement compares both the original content fingerprint and a monotonically increasing revision. If a background worker changes the document while the person is reading, the older request loses permission to overwrite it. This also protects changes that later return to identical text.',
    'Existing translations describe an earlier version and must be invalidated when the source changes. The export receipt remains historical evidence rather than being erased. Later stages regenerate the document and its index; simply storing text is never reported as a completed knowledge-base update.',
    'A selected paragraph is not necessarily a whole article. The person must inspect the complete preview and explicitly confirm its scope. A length threshold helps reject empty pages, but cannot prove that a subscription wall or lazy-loaded section contains everything the author published.',
])


class CaptureCase(FullTextCase):
    """浏览器采集两组用例共用的脚手架:临时库 + 一个 TestClient + 配对头。

    **自己不带断言。** 拆成两个文件是因为原来那一份顶着 700 行上限,而
    「我站着这一页能做什么」(`here`)本来就是独立的一块;共用的这几个辅助方法
    要是各抄一份,两边的 payload 迟早长得不一样,而那正是这些用例在钉的东西。
    继承它的类不许再带用例,否则 unittest 会把父类的用例也收走跑两遍。
    """

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
                    title='Browser title', body=BODY, method='article')
        body.update(overrides)
        return self.client.post('/api/browser-capture/upload', json=body, headers=self.auth)

    def here(self, url='https://e.org/post', auth=True, path='/here', payload=None):
        """扩展开起来那一下问的那一次:我站着这一页,此刻能做什么。"""
        return self.client.post('/api/browser-capture' + path,
                                json=payload if payload is not None else {'url': url},
                                headers=self.auth if auth else {})

