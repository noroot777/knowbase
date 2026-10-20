"""Evaluate the actual state script in Node with selector fixtures, not a browser."""
import json
import subprocess
import unittest

from kb.feeds import x_page_state

RUN = r'''
const fs = require('node:fs');
const {script, state} = JSON.parse(fs.readFileSync(0, 'utf8'));
const primary = {
  innerText: state.main || '',
  querySelector(selector) {
    if (selector.includes('progressbar')) return state.busy ? {} : null;
    if (selector.includes('alert')) return state.alert ? {} : null;
    return null;
  },
};
const document = {
  body: {
    innerText: state.body || state.sidebar || '',
    querySelector: selector => selector.includes('progressbar') && state.busy ? {} : null,
  },
  readyState: state.ready || 'loading',
  querySelector(selector) {
    if (selector.includes('sidebarColumn') && state.sidebar) return {};
    return selector === '[data-testid="primaryColumn"]' && !state.missing ? primary : null;
  },
  querySelectorAll(selector) {
    return selector.startsWith('article') ? Array(state.articles || 0).fill({}) : [];
  },
};
const location = {href: 'https://x.com/home', pathname: '/home'};
const evaluate = new Function('document', 'location', script + '; return xPageState();');
console.log(JSON.stringify(evaluate(document, location)));
'''


class DomEvidenceTests(unittest.TestCase):
    def snapshot(self, **state):
        raw = subprocess.check_output(['node', '-e', RUN], input=json.dumps(
            dict(script=x_page_state.STATE_JS, state=state)), text=True)
        return json.loads(raw)

    def test_sidebar_messages_cannot_classify_any_stage_of_the_main_content(self):
        for ready in ('loading', 'complete'):
            for text in ('Something went wrong. Try reloading', 'Rate limit exceeded'):
                with self.subTest(ready=ready, text=text):
                    snap = self.snapshot(ready=ready, main='Loading timeline', sidebar=text, busy=True)
                    self.assertFalse(snap['failed'])
                    self.assertFalse(snap['limited'])
                    self.assertEqual('', x_page_state.problem(snap))

    def test_main_error_text_during_loading_requires_terminal_content_evidence(self):
        for busy, alert in [(True, False), (True, True), (False, False)]:
            with self.subTest(busy=busy, alert=alert):
                snap = self.snapshot(main='Something went wrong. Try reloading', busy=busy, alert=alert)
                self.assertTrue(snap['failed'])
                self.assertEqual('', x_page_state.problem(snap))
        settled = self.snapshot(ready='complete', main='Something went wrong. Try reloading')
        self.assertEqual('', x_page_state.problem(settled))

    def test_a_terminal_main_error_or_rate_limit_does_not_wait_for_global_ready(self):
        for text, changes, verdict in [('Rate limit exceeded', {}, 'limited'),
                ('Something went wrong. Try reloading', dict(alert=True), 'failed')]:
            with self.subTest(verdict=verdict):
                snap = self.snapshot(main=text, **changes)
                self.assertEqual('loading', snap['ready'])
                self.assertEqual(verdict, snap['dom_problem'])
                self.assertEqual(verdict, x_page_state.problem(snap))

    def test_a_busy_rate_limit_or_missing_primary_is_not_an_authoritative_loading_verdict(self):
        for changes in [dict(busy=True), dict(missing=True)]:
            with self.subTest(changes=changes):
                snap = self.snapshot(main='Rate limit exceeded', sidebar='Rate limit exceeded', **changes)
                self.assertEqual('', x_page_state.problem(snap))

    def test_shell_less_error_pages_keep_classification_without_admitting_content(self):
        for ready in ('loading', 'complete'):
            for text, verdict in [('Rate limit exceeded', 'limited'),
                    ('Something went wrong. Try reloading', 'failed'),
                    ('出错了。请尝试重新加载。', 'failed')]:
                with self.subTest(ready=ready, text=text):
                    snap = self.snapshot(missing=True, ready=ready, body=text)
                    self.assertEqual(verdict, x_page_state.problem(snap))
                    self.assertEqual(0, snap['articles'])
                    self.assertFalse(snap['empty'])

    def test_missing_shell_without_causal_text_still_has_no_error_evidence(self):
        for body in ('', 'Please wait', 'A page we do not recognize'):
            with self.subTest(body=body):
                snap = self.snapshot(missing=True, ready='complete', body=body)
                self.assertEqual('', x_page_state.problem(snap))
                self.assertFalse(snap['empty'])

    def test_shell_less_copy_with_a_loading_indicator_is_not_terminal_evidence(self):
        for body in ('Rate limit exceeded', 'Something went wrong. Try reloading'):
            with self.subTest(body=body):
                snap = self.snapshot(missing=True, body=body, busy=True)
                self.assertEqual('', x_page_state.problem(snap))

    def test_rendered_posts_prevent_dom_error_text_from_becoming_a_failure(self):
        snap = self.snapshot(main='Rate limit exceeded. Something went wrong. Try reloading',
                             articles=1, alert=True, ready='complete')
        self.assertEqual('', x_page_state.problem(snap))
