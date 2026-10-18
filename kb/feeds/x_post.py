"""Expand timeline previews on the matching status page before storing a post."""
from __future__ import annotations

import json
import re
import time

from kb.feeds.x_cdp import CdpError

HINT = '_x_post_prefix'
SECONDS = 25.0
STEP = 0.25
_STATUS = re.compile(r'^https://(?:x|twitter)\.com/[^/?#]+/status/(\d+)(?:[/?#]|$)')

TEXT_JS = r'''
  const postText = node => {
    if (!node) return '';
    const copy = node.cloneNode(true);
    for (const tail of copy.querySelectorAll('a [aria-hidden="true"]')) {
      if (tail.textContent === '…') tail.remove();
    }
    for (const image of copy.querySelectorAll('img[alt]')) {
      image.replaceWith(document.createTextNode(image.getAttribute('alt')));
    }
    for (const br of copy.querySelectorAll('br')) br.replaceWith('\n');
    return copy.textContent.replace(/\u00a0/g, ' ').trim();
  };
  const ownNode = (art, selector) => [...art.querySelectorAll(selector)].find(node =>
    node.closest('article[data-testid="tweet"]') === art &&
    !node.closest('div[role="link"], [data-testid="quoteTweet"]'));
  const postMore = art => !!ownNode(art, '[data-testid="tweet-text-show-more-link"]');
'''

READ_JS = r'''(() => {
''' + TEXT_JS + r'''
  const at = location.pathname.match(/^\/[^/]+\/status\/(\d+)(?:\/|$)/);
  const result = {at: at ? at[1] : '', id: '', text: '', truncated: true,
    login: !!document.querySelector('[data-testid="loginButton"], [data-testid="login"]')
      || /^\/i\/flow\/login/.test(location.pathname)};
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    const clock = ownNode(art, 'time[datetime]');
    const href = clock?.closest('a[href*="/status/"]')?.getAttribute('href') || '';
    const identity = href.match(/^\/[^/]+\/status\/(\d+)(?:[/?#]|$)/);
    if (!identity || identity[1] !== result.at) continue;
    result.id = identity[1];
    result.text = postText(ownNode(art, '[data-testid="tweetText"]'));
    result.truncated = postMore(art);
    break;
  }
  return JSON.stringify(result);
})()'''


class PostUnavailable(RuntimeError):
    pass


def _check(deadline, should_stop):
    if should_stop and should_stop():
        raise PostUnavailable('同步已停止')
    if deadline is not None and time.monotonic() >= deadline:
        raise PostUnavailable('推文正文补全时间用完')


def fetch(url: str, session, *, deadline=None, should_stop=None) -> str:
    match = _STATUS.match(url)
    if not match:
        raise PostUnavailable('不是 X 推文地址')
    until = min(time.monotonic() + SECONDS, deadline) if deadline is not None else (
        time.monotonic() + SECONDS)
    _check(until, should_stop)
    previous = None
    try:
        navigation = session.goto(url)
        if isinstance(navigation, dict) and navigation.get('errorText'):
            raise PostUnavailable(f'推文详情页打开失败:{navigation["errorText"]}')
        while True:
            _check(until, should_stop)
            snap = json.loads(session.eval(READ_JS))
            if snap.get('login'):
                raise PostUnavailable('推文详情页需要重新登录 X')
            valid = (snap.get('at') == match[1] == snap.get('id')
                     and isinstance(snap.get('text'), str) and snap['text'].strip()
                     and snap.get('truncated') is False)
            text = snap['text'].strip() if valid else None
            if text is not None and text == previous:
                return text
            previous = text
            time.sleep(min(STEP, max(0.0, until - time.monotonic())))
    except (CdpError, OSError, EOFError, ValueError, TypeError, AttributeError) as exc:
        raise PostUnavailable(f'推文详情页读取失败:{exc}') from exc


def fill(entries: list[dict], *, session, deadline=None, should_stop=None):
    kept, missed = [], 0
    for entry in entries:
        if HINT not in entry:
            kept.append(entry)
            continue
        try:
            text = fetch(entry['url'], session, deadline=deadline, should_stop=should_stop)
        except PostUnavailable:
            missed += 1
            continue
        complete = dict(entry)
        prefix = complete.pop(HINT)
        complete.update(title=text.splitlines()[0][:500], body=prefix + text)
        kept.append(complete)
    note = f'有 {missed} 条折叠推文的完整正文没取到,留到下次同步再试' if missed else ''
    return kept, note
