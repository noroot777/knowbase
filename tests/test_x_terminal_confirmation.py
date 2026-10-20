"""One authoritative terminal signal, confirmed consistently across X readers."""
import json
import time
import unittest
from unittest.mock import patch

from kb.feeds import x_browser, x_page_state, x_post
from tests import test_x_page_dom_evidence as dom_fixture

URL = 'https://x.com/Example/status/123'


class SequencePage:
    def __init__(self, frames):
        self.frames, self.reads = frames, 0

    def goto(self, _url):
        return {}

    def eval(self, _script):
        frame = self.frames[min(self.reads, len(self.frames) - 1)]
        self.reads += 1
        return json.dumps(frame)


class TerminalConfirmationTests(unittest.TestCase):
    def test_shared_confirmation_requires_two_matching_signals_and_resets_on_unknown(self):
        errors = x_page_state.ProblemConfirmation()
        sequence = ['failed', '', 'failed', 'limited', 'limited', '', 'limited', 'limited']
        self.assertEqual(['', '', '', '', 'limited', '', '', 'limited'],
                         [errors.check(dict(dom_problem=signal)) for signal in sequence])

    def dom(self, **changes):
        return dom_fixture.DomEvidenceTests().snapshot(**changes)

    def consume(self, reader, frames, expected, reads):
        healthy = dict(at='123', id='123', text='The complete detail', truncated=False)
        frames = [dict(frame, **(healthy if frame['articles'] else
                  dict(at='123', id='', text='', truncated=True))) for frame in frames]
        page = SequencePage(frames)
        with patch.object(time, 'sleep'):
            if reader == 'verify':
                self.assertEqual(expected, x_browser.verify_timeline(page, timeout=1)[0])
            elif expected == 'ok':
                if reader == 'timeline':
                    self.assertEqual(1, x_browser._wait_state(page, 'Example', time.monotonic() + 1)['articles'])
                else:
                    self.assertEqual(healthy['text'], x_post.fetch(URL, page, deadline=time.monotonic() + 1))
            else:
                error = (x_browser.XRateLimited if expected == 'limited' else
                         x_browser.XBrowserError if reader == 'timeline' else x_post.PostRequestFailed)
                with self.assertRaises(error):
                    if reader == 'timeline':
                        x_browser._wait_state(page, 'Example', time.monotonic() + 1)
                    else:
                        x_post.fetch(URL, page, deadline=time.monotonic() + 1)
        self.assertEqual(reads, page.reads)

    def test_busy_or_unconfirmed_main_errors_are_not_terminal_even_after_document_complete(self):
        for ready in ('loading', 'complete'):
            for busy, alert in [(True, True), (True, False), (False, False)]:
                snap = self.dom(ready=ready, busy=busy, alert=alert,
                                main='Something went wrong. Try reloading')
                with self.subTest(ready=ready, busy=busy, alert=alert):
                    self.assertEqual('', snap['dom_problem'])
                    self.assertEqual('', x_page_state.problem(snap))

    def test_busy_failed_or_limited_frames_can_recover_in_every_reader(self):
        good = self.dom(ready='complete', articles=1)
        for text in ('Something went wrong. Try reloading', 'Rate limit exceeded'):
            busy = self.dom(ready='complete', busy=True, alert=True, main=text)
            for reader in ('verify', 'timeline', 'detail'):
                with self.subTest(reader=reader, text=text):
                    self.consume(reader, [busy, good, good], 'ok', 3 if reader == 'detail' else 2)

    def test_one_terminal_frame_is_not_enough_for_any_reader(self):
        good = self.dom(ready='complete', articles=1)
        for kind, text in [('failed', 'Something went wrong. Try reloading'),
                           ('limited', 'Rate limit exceeded')]:
            bad = self.dom(ready='complete', alert=True, main=text)
            self.assertEqual(kind, bad['dom_problem'])
            for reader in ('verify', 'timeline', 'detail'):
                with self.subTest(reader=reader, kind=kind):
                    self.consume(reader, [bad, good, good], 'ok', 3 if reader == 'detail' else 2)
                    self.consume(reader, [bad, bad], kind, 2)

    def test_busy_frame_or_a_changed_error_breaks_consecutive_confirmation(self):
        failed = self.dom(ready='complete', alert=True, main='Something went wrong. Try reloading')
        busy = self.dom(ready='complete', busy=True, alert=True, main='Something went wrong. Try reloading')
        limited = self.dom(ready='complete', main='Rate limit exceeded')
        good = self.dom(ready='complete', articles=1)
        for middle in (busy, limited):
            for reader in ('verify', 'timeline', 'detail'):
                with self.subTest(reader=reader, middle=middle['dom_problem']):
                    self.consume(reader, [failed, middle, failed, good, good],
                                 'ok', 5 if reader == 'detail' else 4)
