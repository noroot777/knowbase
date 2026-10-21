"""Deliver actual localhost CDP responses across a controlled navigation boundary."""
import json
import threading
import time
import unittest
from collections import deque
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from kb.feeds import x_browser, x_page_state
from kb.feeds.x_cdp import Chrome
from kb.feeds.x_timeline_state import TimelineState
from tests.headless import SHELL, requires_shell
from tests.test_x_post import OLD
from tests.test_x_post_dom import Handler, article


@requires_shell   # 这一道闸的口径只有一份(见 tests/headless.requires_shell)
class ResponseDomTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.thread.join)
        cls.addClassCleanup(cls.server.shutdown)

    def test_real_response_identity_survives_a_delayed_delivery_after_navigation_reply(self):
        for target in ['/Next', '/Example/status/123']:
            for status in [429, 503]:
                with self.subTest(target=target, status=status), TemporaryDirectory() as root:
                    self.server.api_status = status
                    marker = str(time.monotonic_ns())
                    self.server.html = (f'<meta name="ownership-marker" content="{marker}">'
                                        + article(text='Complete cached body'))
                    with Chrome(str(Path(root) / 'profile'), headless=True, chrome_path=SHELL) as page:
                        page.open_page('about:blank')
                        process = page.proc
                        base = f'http://127.0.0.1:{self.server.server_port}'
                        x_page_state.navigate(page, base + '/Example')
                        deadline = time.monotonic() + 5
                        while not page.eval('document.querySelector("meta[name=ownership-marker]")?.content === '
                                            + json.dumps(marker)):
                            if time.monotonic() > deadline:
                                self.fail('owned localhost timeline did not render')
                            time.sleep(0.01)
                        owner = page._x_responses
                        state = x_page_state.read(page, x_browser._STATE_JS)
                        timeline = TimelineState(page, 'Example', state, x_browser._STATE_JS)
                        result = dict(entries=[dict(body='cached')], error='', incomplete='', watermark='new')
                        timeline.finalize(result, OLD)
                        original_take = page._take
                        parked = deque()
                        request_ids = set()
                        received = []
                        deliver = False

                        def take(deadline):
                            if deliver and parked:
                                return parked.popleft()
                            while True:
                                message = original_take(deadline)
                                method = message.get('method')
                                params = message.get('params', {})
                                if (method == 'Network.requestWillBeSent'
                                        and '/i/api/graphql/hash/UserTweets' in params['request']['url']):
                                    request_ids.add(params['requestId'])
                                if (not deliver and params.get('requestId') in request_ids and method in {
                                        'Network.responseReceived', 'Network.loadingFinished',
                                        'Network.loadingFailed'}):
                                    parked.append(message)
                                    if method == 'Network.responseReceived':
                                        received.append(message)
                                    continue
                                return message

                        with patch.object(page, '_take', side_effect=take):
                            page.eval('fetch("/i/api/graphql/hash/UserTweets").then(response => response.text())')
                            page.eval('0')
                            self.assertEqual(1, len(received))
                            params = received[0]['params']
                            self.assertEqual(status, params['response']['status'])
                            self.assertEqual(owner.loader, params['loaderId'])
                            request_key = (page._sid, params['requestId'])
                            self.assertIs(owner, page._x_response_scopes.requests[request_key])
                            x_page_state.navigate(page, base + target)
                            self.assertNotEqual(owner.loader, page._x_responses.loader)
                            self.assertEqual('', result['error'])
                            self.assertEqual('', result['incomplete'])
                            deliver = True
                            if status == 429:
                                with self.assertRaises(x_page_state.XRateLimited):
                                    x_page_state.read(page, x_browser._STATE_JS)
                                self.assertIn('频率', result['error'])
                                self.assertEqual([], result['entries'])
                            else:
                                snapshot = x_page_state.read(page, x_browser._STATE_JS)
                                self.assertFalse(x_page_state.problem(snapshot))
                                self.assertIn('请求出错', result['incomplete'])
                                self.assertEqual(OLD, result['watermark'])
                                self.assertTrue(result['entries'])
                            self.assertFalse(parked)
                            self.assertNotIn(request_key, page._x_response_scopes.requests)
                    self.assertIsNotNone(process.poll())
                    self.assertTrue(page.closed_cleanly)
