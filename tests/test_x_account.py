"""Account lifecycle states must finish early without inferring a rename destination."""
import time
import unittest
from contextlib import nullcontext
from unittest.mock import patch

from kb.feeds import x_account, x_browser
from tests.test_x_post import OLD, Page


def state(name='missing', handle='example', **extra):
    return dict(url=f'https://x.com/{handle}', ready='complete', articles=0,
                login=False, limited=False, empty=False,
                account=dict(state=name, handle=handle, evidence='empty_state_without_profile'),
                **extra)


class AccountTests(unittest.TestCase):
    def test_all_explicit_states_finish_after_two_observations_not_the_account_budget(self):
        for name, reason in [('missing', '改名或注销'), ('suspended', '被冻结'),
                             ('protected', '无权读取'), ('unavailable', '明确不可用'),
                             ('login', '重新登录')]:
            page = Page(state(name))
            with self.subTest(name=name), patch.object(x_browser.time, 'sleep'):
                with self.assertRaises(x_account.XAccountUnavailable) as caught:
                    x_browser._wait_state(page, 'Example', time.monotonic() + 5)
            self.assertEqual(name, caught.exception.state)
            self.assertIn(reason, str(caught.exception))
            self.assertEqual(2, page.reads)

    def test_stale_loading_or_transient_account_states_do_not_become_terminal(self):
        good = dict(state(''), articles=1)
        for first in [state(handle='previous'), dict(state(), ready='loading'), state()]:
            page = Page(first, good)
            with self.subTest(first=first), patch.object(x_browser.time, 'sleep'):
                self.assertEqual(good, x_browser._wait_state(page, 'Example', time.monotonic() + 5))

    def test_empty_body_and_sidebar_error_words_are_not_terminal_evidence(self):
        snap = state('')
        snap['empty'] = True
        page = Page(snap)
        with patch.object(x_browser.time, 'sleep'), self.assertRaisesRegex(
                x_browser.XBrowserError, '没有读到推文'):
            x_browser._wait_state(page, 'Example', time.monotonic() + 0.003)

    def test_login_redirect_with_home_tweets_cannot_be_accepted_as_the_profile(self):
        page = Page(dict(state('login'), articles=4))
        with patch.object(x_browser.time, 'sleep'), self.assertRaisesRegex(
                x_account.XAccountUnavailable, '登录态'):
            x_browser._wait_state(page, 'Example', time.monotonic() + 5)
        self.assertEqual(2, page.reads)

    def test_previous_profiles_visible_tweets_do_not_complete_the_new_navigation(self):
        old = dict(state('', handle='previous'), articles=5)
        good = dict(state(''), articles=1)
        page = Page(old, good)
        with patch.object(x_browser.time, 'sleep'):
            self.assertEqual(good, x_browser._wait_state(page, 'Example', time.monotonic() + 5))
        self.assertEqual(2, page.reads)

    def test_account_failure_is_structured_and_does_not_abort_other_accounts(self):
        for attach in [False, True]:
            page = Page(state())
            with self.subTest(attach=attach), \
                 patch.object(x_browser.time, 'sleep'), \
                 patch.object(x_browser, 'browser_session', return_value=nullcontext(page)), \
                 patch.object(x_browser, '_harvest_one', side_effect=[
                     x_account.XAccountUnavailable('Example', 'missing', 'empty_state_without_profile'),
                     ([], '')]):
                result = x_browser.harvest([('Example', OLD), ('Next', OLD)],
                                           profile='', attach=attach)
            self.assertEqual('missing', result['Example']['account_state'])
            self.assertIn('改名或注销', result['Example']['error'])
            self.assertNotIn('watermark', result['Example'])
            self.assertEqual('', result['Next']['error'])

    def test_only_complete_target_profile_snapshots_can_classify_an_account(self):
        for snap in [dict(state(), articles=1), dict(state(), ready='loading'),
                     state(handle='another')]:
            self.assertIsNone(x_account.terminal(snap, 'Example'))
