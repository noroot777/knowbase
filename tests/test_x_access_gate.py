"""Confirmed access gates stop acquisition batches without losing retryable content."""
import contextlib
import json
import re
import time
import unittest
from unittest.mock import patch

from kb.collectors import FetchScope
from kb.feeds import registry as reg, sync, x_browser, x_channels, x_page_state, x_post
from scripts import x_post_backfill as backfill
from tests import test_x_post_dom as dom
from tests.test_x_channels import ChannelBase
from tests.test_x_collectors import Quick, tweet
from tests.test_x_post import BODY, OLD, Page, preview, snapshot
from tests.test_x_post_backfill import backfill_entry


def gate(**kwargs):
    return dict(dict(snapshot(''), at='', id='') | dict(ready='complete', articles=0, login=False,
                empty=False, limited=False, failed=False, challenge=True), **kwargs)


class AccessGateTests(ChannelBase):
    def test_detail_gate_at_another_route_stops_after_two_reads_not_the_full_budget(self):
        page = Page(gate(url='https://x.com/account/access'))
        started = time.monotonic()
        with patch.object(x_post, 'STEP', 0), self.assertRaises(x_page_state.XAccessBlocked) as caught:
            x_post.fetch('https://x.com/Example/status/123', page)
        self.assertEqual(2, page.reads)
        self.assertLess(time.monotonic() - started, 1)
        self.assertIn('完成验证后重试', str(caught.exception))
        self.assertNotIsInstance(caught.exception, x_page_state.XRateLimited)
        self.assertNotIsInstance(caught.exception, x_post.PostGone)

    def test_one_transient_or_loading_gate_and_arbitrary_wrong_routes_are_not_terminal(self):
        for first in [gate(), gate(ready='loading'),
                      gate(challenge=False, url='https://x.com/home')]:
            page = Page(first, snapshot(BODY), snapshot(BODY))
            with self.subTest(first=first), patch.object(x_post, 'STEP', 0):
                try:
                    text = x_post.fetch('https://x.com/Example/status/123', page)
                except RuntimeError as exc:
                    self.fail(f'transient gate incorrectly stopped acquisition: {exc}')
                self.assertEqual(BODY, text)
            self.assertEqual(3, page.reads)

    def test_access_gate_is_reported_for_all_remaining_accounts_on_both_channels(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            page = Page(gate())
            with self.subTest(channel=channel), patch.object(x_browser.time, 'sleep'), \
                 patch.object(x_browser, 'browser_session', return_value=contextlib.nullcontext(page)):
                results = x_browser.harvest([('Example', OLD), ('Other', OLD)], profile='',
                                           attach=channel == x_channels.ATTACH)
            self.assertEqual(2, page.reads)
            self.assertEqual(['https://x.com/Example'], page.urls)
            for handle in ['Example', 'Other']:
                self.assertIn('安全验证', results[handle]['error'])
                self.assertEqual([], results[handle]['entries'])
                self.assertNotIn('watermark', results[handle])

    def test_detail_gate_preserves_stored_row_and_stops_the_rest_of_the_audit(self):
        source = self.sub()
        reg.put_items(source, [backfill_entry(tweet('123', text='Read https://\nexample.org'))])
        with reg.db() as conn:
            conn.execute("UPDATE feed_items SET body_zh='旧译文',translated_at='old'")
            row = dict(conn.execute('SELECT * FROM feed_items').fetchone())
        page = Page(gate())
        with patch.object(x_post, 'STEP', 0):
            stats = backfill.repair([row, row], page, apply=True, remember=True)
        self.assertEqual(1, stats['failed'])
        self.assertEqual(1, stats['deferred'])
        self.assertEqual(0, stats['updated'])
        self.assertIn('完成验证后重试', stats['stop_reason'])
        self.assertEqual([row['url']], page.urls)
        with reg.db() as conn:
            self.assertEqual(row, dict(conn.execute('SELECT * FROM feed_items').fetchone()))
            self.assertEqual([row], backfill.candidates(conn))

    def test_detail_gate_never_advances_watermark_and_a_later_round_can_save_the_content(self):
        for channel in [x_channels.BROWSER, x_channels.ATTACH]:
            source = self.sub('Example' + channel)
            entry = dict(preview(), url=f'https://x.com/Example{channel}/status/123')
            x_channels.save_watermark(source['id'], channel, OLD)
            settings = self.settings(channel)
            for blocked in [True, False]:
                source = reg.subscription('social', source['id'])
                page = Page(gate() if blocked else snapshot(BODY))
                with self.subTest(channel=channel, blocked=blocked), Quick(), \
                     patch.object(x_post, 'STEP', 0), patch.object(x_browser, 'browser_session',
                        return_value=contextlib.nullcontext(page)), patch.object(
                        x_browser, '_harvest_one', return_value=([entry], '')):
                    x_channels.prefetch([source], settings)
                if blocked:
                    with self.assertRaisesRegex(ValueError, '安全验证'):
                        x_channels.take(source, settings)
                    self.assertEqual(OLD, x_channels.watermark(reg.subscription(
                        'social', source['id']), channel))
                    with reg.db() as conn:
                        self.assertEqual(0, conn.execute(
                            'SELECT count(*) FROM subscription_items WHERE subscription_id=?',
                            (source['id'],)).fetchone()[0])
                else:
                    sync._save_result(source, x_channels.take(source, settings), FetchScope())
                    self.assertNotEqual(OLD, x_channels.watermark(reg.subscription(
                        'social', source['id']), channel))
                    with reg.db() as conn:
                        self.assertEqual(BODY, conn.execute('SELECT body FROM feed_items '
                            'JOIN subscription_items ON key=item_key WHERE subscription_id=?',
                            (source['id'],)).fetchone()[0])


@unittest.skipUnless(dom.SHELL is not None, dom.INSTALL_HINT)
class AccessGateDomTests(unittest.TestCase):
    def load(self, html):
        dom.PostDomTests.load(self, html)
        until = time.monotonic() + 3
        while self.page.eval('document.readyState') != 'complete':
            if time.monotonic() >= until:
                self.fail('fixture did not finish loading')
            time.sleep(0.01)

    @classmethod
    def setUpClass(cls):
        dom.PostDomTests.setUpClass.__func__(cls)

    def test_real_dom_component_and_access_route_are_shared_by_both_readers(self):
        for html in [
                '<script>window._cf_chl_opt = {};</script>正在进行安全验证',
                '<script src="/cdn-cgi/challenge-platform/h/flow"></script>Checking your browser',
                '<form action="/cdn-cgi/challenge-platform/h/flow">请完成验证</form>',
                '<iframe src="/cdn-cgi/challenge-platform/h/flow"></iframe>',
                '<script>history.replaceState({}, "", "/account/access");</script>安全验证']:
            with self.subTest(html=html):
                self.load(html)
                for script in [x_post.READ_JS, x_browser._STATE_JS]:
                    snap = json.loads(self.page.eval(script))
                    self.assertTrue(snap['challenge'])
                    self.assertFalse(snap['empty'])
                    self.assertEqual('challenge', x_page_state.problem(snap))
                local_url = f'http://127.0.0.1:{self.server.server_port}/Example/status/123'
                with patch.object(x_post, '_STATUS', re.compile(
                        r'^http://127\.0\.0\.1:\d+/[^/]+/status/(\d+)')), \
                     patch.object(x_post, 'STEP', 0.01), self.assertRaises(x_page_state.XAccessBlocked):
                    x_post.fetch(local_url, self.page)
                with self.assertRaises(x_page_state.XAccessBlocked):
                    x_browser._wait_state(self.page, 'Example', time.monotonic() + 3)

    def test_discussion_examples_generic_widgets_and_other_routes_are_not_gates(self):
        for html in [
                '<pre>&lt;script src="/cdn-cgi/challenge-platform/h/flow"&gt;</pre>安全验证教程',
                dom.article(text='正在进行安全验证, _cf_chl_opt 与 account/access 是如何工作的？'),
                '<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js"></script>',
                '<script src="/recaptcha/api.js"></script>',
                '<script>history.replaceState({}, "", "/home");</script>loading']:
            with self.subTest(html=html):
                self.load(html)
                for script in [x_post.READ_JS, x_browser._STATE_JS]:
                    self.assertFalse(json.loads(self.page.eval(script))['challenge'])

    def test_real_dom_uses_executable_script_types_and_case_insensitive_component_markers(self):
        for html, blocked in [
                ('<script type="application/json">{"endpoint":"/cdn-cgi/challenge-platform/h/b/"}'
                 '</script>', False),
                ('<script type="application/ld+json">{"sample":"_cf_chl_opt"}</script>', False),
                ('<script type="application/json" src="/cdn-cgi/challenge-platform/h/flow"></script>',
                 False),
                ('<template><script src="/cdn-cgi/challenge-platform/h/flow"></script></template>',
                 False),
                ('<script src="/CDN-CGI/Challenge-Platform/h/flow"></script>', True),
                ('<form action="/CDN-CGI/CHALLENGE-PLATFORM/H/flow"></form>', True),
                ('<script type="TEXT/JAVASCRIPT">window._CF_CHL_OPT = {};</script>', True),
                ('<script type="module">window._CF_CHL_OPT = {};</script>', True),
                ('<script src="" type="module">window._CF_CHL_OPT = {};</script>', False)]:
            with self.subTest(html=html):
                self.load(html)
                for script in [x_post.READ_JS, x_browser._STATE_JS]:
                    snap = json.loads(self.page.eval(script))
                    self.assertEqual(blocked, snap['challenge'])
                    self.assertEqual('challenge' if blocked else '', x_page_state.problem(snap))
                if blocked:
                    with self.assertRaises(x_page_state.XAccessBlocked):
                        x_browser._wait_state(self.page, 'Example', time.monotonic() + 3)

    def test_proven_url_repair_survives_real_inline_dom_without_legacy_break_evidence(self):
        self.load(dom.article(text='Read this:<a href="https://example.org/path">'
                              '<span aria-hidden="true">https://</span>example.org/path</a>'))
        evidence = json.loads(self.page.eval(x_post.READ_JS))
        self.assertEqual('Read this:https://example.org/path', evidence['inline_legacy'])
        stored = 'Read this:\nhttps://\nexample.org/path\n…'
        entry = dict(url='https://x.com/Example/status/123', body=stored,
                     title='Read this:', **{x_post.HINT: ''})
        state, complete, info = backfill.proposal(entry, evidence['text'], evidence)
        self.assertEqual('normalized', state)
        self.assertEqual('Read this:\nhttps://example.org/path', complete['body'])
        self.assertEqual('blocked_whitespace', info['remaining'])
