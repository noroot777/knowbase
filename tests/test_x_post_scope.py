"""A local detail failure must not discard other accounts or a verified result."""
import contextlib
import io
import socket
import time
import unittest
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import cdp_attach, registry as reg, sync, x_browser, x_channels, x_page_state, x_post
from kb.feeds.ws_client import OP_CONT, OP_TEXT, WebSocket, WsTimeoutError, _frame
from kb.feeds.x_cdp import CdpError, CdpTransportError
from scripts import x_post_backfill as backfill
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import Quick, Timeline, tweet
from tests.test_x_page_state import EventSession, response
from tests.test_x_post import BODY, OLD, URL, Page, preview, snapshot
from tests.test_x_post_backfill import backfill_entry


class OpenTimeline(Timeline):
    def open_page(self, url):
        pass


class TransportScopeTests(unittest.TestCase):
    def connection(self):
        client, peer = socket.socketpair()
        self.addCleanup(client.close)
        self.addCleanup(peer.close)
        session = cdp_attach.Attached()
        session._ws = WebSocket(client)
        return session, peer

    def test_read_timeout_keeps_a_partial_frame_and_the_next_reply_usable(self):
        for cut in [1, 2, 4, 8]:
            with self.subTest(cut=cut):
                session, peer = self.connection()
                packet = _frame(OP_TEXT, b'{"id":1,"result":{"ok":true}}')
                peer.sendall(packet[:cut])
                with self.assertRaises(CdpError) as caught:
                    session._take(time.monotonic() + 0.005)
                self.assertNotIsInstance(caught.exception, CdpTransportError)
                peer.sendall(packet[cut:])
                self.assertEqual({'id': 1, 'result': {'ok': True}},
                                 session._take(time.monotonic() + 1))

    def test_read_timeout_keeps_message_fragments(self):
        session, peer = self.connection()
        first = bytearray(_frame(OP_TEXT, b'{"id":'))
        first[0] &= 0x7f
        peer.sendall(first)
        with self.assertRaises(WsTimeoutError):
            session._ws.recv(time.monotonic() + 0.005)
        peer.sendall(_frame(OP_CONT, b'1}'))
        self.assertEqual({'id': 1}, session._take(time.monotonic() + 1))

    def test_eof_and_failed_writes_are_connection_failures_not_command_timeouts(self):
        session, peer = self.connection()
        peer.close()
        with self.assertRaises(EOFError):
            session._take(time.monotonic() + 1)
        with self.assertRaises(CdpTransportError):
            session._post({'id': 1}, time.monotonic() + 1)

    def test_command_rejection_leaves_the_shared_session_usable(self):
        session = EventSession([])
        original = session._post

        def reject_once(message, deadline):
            session._post = original
            session.incoming.append(dict(id=message['id'], error=dict(message='Target navigated')))

        session._post = reject_once
        with self.assertRaises(CdpError) as caught:
            session.call('Runtime.evaluate')
        self.assertNotIsInstance(caught.exception, CdpTransportError)
        self.assertEqual({}, session.call('Page.enable'))


class ResultPrecedenceTests(unittest.TestCase):
    def test_unrelated_cards_cannot_erase_core_http_failure_evidence(self):
        state = x_page_state.Responses('owned')
        state.reset(URL)
        state.loader = 'current'
        state.receive(response(429))
        snap = dict(articles=1, ready='complete', failed=False, limited=False)
        applied = state.apply(snap)
        self.assertTrue(applied['limited'])
        self.assertTrue(applied['http_only'])
        self.assertTrue(applied['http_content_limited'])

    def test_full_stable_text_wins_over_every_http_failure(self):
        for status in [429, 503, 404]:
            with self.subTest(status=status), patch.object(x_post, 'STEP', 0):
                session = EventSession([response(status, operation='TweetDetail')],
                                       available=True)
                self.assertEqual(BODY, x_post.fetch(URL, session))
                self.assertEqual(2, session.reads)

    def test_flags_cannot_veto_a_result_but_cannot_relax_its_identity_or_expansion(self):
        for changes in [dict(id='999'), dict(at='999'), dict(truncated=True), dict(text='')]:
            with self.subTest(changes=changes), patch.object(x_post, 'STEP', 0), \
                 patch.object(x_post, 'SECONDS', 0.005):
                page = Page(dict(snapshot(), ready='complete', failed=True, **changes))
                with self.assertRaises(x_post.PostUnavailable):
                    x_post.fetch(URL, page)
        with patch.object(x_post, 'STEP', 0):
            page = Page(dict(snapshot(), ready='complete', failed=True, limited=True))
            try:
                text = x_post.fetch(URL, page)
            except (x_post.PostUnavailable, x_page_state.XBrowserError) as exc:
                self.fail(f'Failure flags vetoed an already verified body: {exc}')
            self.assertEqual(BODY, text)
            self.assertEqual(2, page.reads)

    def test_late_timeline_content_survives_an_ancillary_http_error(self):
        for status in [429, 503]:
            with self.subTest(status=status), patch.object(x_browser.time, 'sleep'):
                session = EventSession([response(status, operation='UserByScreenName')])
                original = session._post

                def delayed(message, deadline):
                    session.available = session.reads >= 2
                    if session.available:
                        session.incoming.append(response(200, operation='UserByScreenName'))
                    original(message, deadline)

                session._post = delayed
                x_page_state.navigate(session, 'https://x.com/Example')
                try:
                    state = x_browser._wait_state(session, 'Example', time.monotonic() + 0.1)
                except x_page_state.XBrowserError as exc:
                    self.fail(f'HTTP evidence vetoed the pending timeline: {exc}')
                self.assertEqual(1, state['articles'])
                self.assertEqual(3, session.reads)


class StoredScopeTests(ChannelBase):
    def seed(self, body='A paragraph.\n\nKeep this paragraph.', identity='123'):
        source = self.sub()
        reg.put_items(source, [backfill_entry(tweet(identity, text=body))])
        with reg.db() as conn:
            return dict(conn.execute('SELECT * FROM feed_items WHERE url=?',
                                     (f'https://x.com/Example/status/{identity}',)).fetchone())

    def test_one_command_error_withholds_one_post_but_saves_other_accounts(self):
        sources = [self.sub(), self.sub('Other')]
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            with self.subTest(channel=channel), patch.object(x_post, 'STEP', 0):
                for source in sources:
                    x_channels.save_watermark(source['id'], channel, OLD)
                sources = [reg.subscription('social', source['id']) for source in sources]
                short = x_browser._entry(tweet('124', text='a complete short post'))
                page = Page(CdpError('Runtime.evaluate timed out'), snapshot())
                with patch.object(x_browser, 'browser_session',
                                  return_value=contextlib.nullcontext(page)), \
                     patch.object(x_browser, '_harvest_one',
                                  side_effect=[([short, preview()], ''), ([preview()], '')]):
                    settings = self.settings(channel)
                    x_channels.prefetch(sources, settings)
                for source in sources:
                    try:
                        result = x_channels.take(source, settings)
                    except ValueError as exc:
                        self.fail(f'A local command failure escaped its post: {exc}')
                    sync._save_result(source, result, FetchScope())
                with reg.db() as conn:
                    bodies = {row['body'] for row in conn.execute('SELECT body FROM feed_items')}
                self.assertEqual({'a complete short post', BODY}, bodies)
                first = reg.subscription('social', sources[0]['id'])
                second = reg.subscription('social', sources[1]['id'])
                self.assertEqual('', first['last_error'])
                self.assertIn('1 条折叠推文', first['last_note'])
                self.assertEqual(OLD, x_channels.watermark(first, channel))
                self.assertNotEqual(OLD, x_channels.watermark(second, channel))

    def test_quote_only_skips_are_informational_and_do_not_stall_either_channel(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            with self.subTest(channel=channel), Quick():
                handle = 'Example' + channel
                source = self.sub(handle)
                x_channels.save_watermark(source['id'], channel, OLD)
                source = reg.subscription('social', source['id'])
                item = dict(tweet('123', handle=handle, text=''), quoted=True)
                page = OpenTimeline([item])
                settings = self.settings(channel)
                with patch.object(x_browser, 'browser_session',
                                  return_value=contextlib.nullcontext(page)):
                    x_channels.prefetch([source], settings)
                sync._save_result(source, x_channels.take(source, settings), FetchScope())
                saved = reg.subscription('social', source['id'])
                self.assertIn('跳过 1 条本人无正文的引用推文', saved['last_note'])
                observed = x_browser._newest(x_browser._select([item], None, handle), OLD)
                self.assertEqual(observed, x_channels.watermark(saved, channel))
                self.assertNotEqual(OLD, observed)
                with reg.db() as conn:
                    self.assertEqual(0, conn.execute('SELECT count(*) FROM feed_items WHERE url=?',
                                                    (item['url'],)).fetchone()[0])
                page = OpenTimeline([item])
                with patch.object(x_browser, 'browser_session',
                                  return_value=contextlib.nullcontext(page)):
                    x_channels.prefetch([saved], settings)
                self.assertEqual(0, sync._save_result(
                    saved, x_channels.take(saved, settings), FetchScope()))
                saved = reg.subscription('social', source['id'])
                self.assertEqual(observed, x_channels.watermark(saved, channel))
                self.assertEqual('', saved['last_note'])

    def test_halt_inside_fetch_is_deferred_and_stops_further_rows(self):
        row = self.seed()
        with patch.object(x_post, 'fetch', side_effect=x_post.PostHalted('同步已停止')) as fetch:
            stats = backfill.repair([row, row], Page(), apply=True)
        self.assertEqual(0, stats['failed'])
        self.assertEqual(2, stats['deferred'])
        self.assertEqual('deferred', stats['results'][0]['state'])
        self.assertEqual(1, fetch.call_count)
        with reg.db() as conn:
            self.assertEqual(row, dict(conn.execute('SELECT * FROM feed_items').fetchone()))

    def test_all_intentional_withholding_states_exit_successfully_without_writing(self):
        row = self.seed()
        states = ['blocked_length', 'blocked_artifacts', 'blocked_whitespace', 'blocked_content',
                  'blocked_evidence']
        for state in states:
            with self.subTest(state=state), contextlib.redirect_stdout(io.StringIO()) as output, \
                 patch.object(backfill.x_browser, 'browser_session',
                              return_value=contextlib.nullcontext(Page())), \
                 patch.object(backfill, 'proposal', return_value=(state, None, {})):
                self.assertEqual(0, backfill.main(['run', '--yes', '--recheck']))
                self.assertIn('失败 0 条', output.getvalue())
        with reg.db() as conn:
            self.assertEqual(row, dict(conn.execute('SELECT * FROM feed_items').fetchone()))
