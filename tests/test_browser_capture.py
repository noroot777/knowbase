import json
import time
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import browser_capture as capture
from kb.feeds import registry as reg
from kb.web import browser_capture_api
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

    def upload(self, intent, **overrides):
        body = dict(intent_id=intent['id'], page_url='https://e.org/post',
                    title='Browser title', body=BODY, method='article', confirmed_complete=True)
        body.update(overrides)
        return self.client.post('/api/browser-capture/upload', json=body, headers=self.auth)

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
