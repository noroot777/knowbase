"""Expand timeline previews on the matching status page before storing a post."""
from __future__ import annotations

import re
import time

from kb.feeds import x_article, x_post_meta
from kb.feeds.x_cdp import CdpError
from kb.feeds.x_page_state import STATE_JS, XRateLimited, navigate, problem, read

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
''' + TEXT_JS + STATE_JS + r'''
  const at = location.pathname.match(/^\/[^/]+\/status\/(\d+)(?:\/|$)/);
  const result = {...xPageState(), at: at ? at[1] : '', id: '', text: '', truncated: true};
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


class PostGone(PostUnavailable):
    """The requested, fully loaded page explicitly says the post is unavailable."""


class PostRequestFailed(PostUnavailable):
    """The page offers a retry, without proving absence or an HTTP rate limit."""


def expanded(text: str, preview: str) -> bool:
    return len(text.strip()) > len(preview.strip())


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
    absent = False
    previous_problem = ''
    try:
        navigation = navigate(session, url)
        if isinstance(navigation, dict) and navigation.get('errorText'):
            raise PostUnavailable(f'推文详情页打开失败:{navigation["errorText"]}')
        while True:
            _check(until, should_stop)
            snap = read(session, READ_JS)
            if snap.get('login'):
                raise PostUnavailable('推文详情页需要重新登录 X')
            current_problem = problem(snap) if snap.get('at') == match[1] else ''
            if current_problem and current_problem == previous_problem:
                if current_problem == 'limited':
                    raise XRateLimited('X 暂时限制了访问频率,请稍后重试或降低同步频率')
                raise PostRequestFailed('X 页面请求出错,请稍后重试(不能据此确认限流或推文不存在)')
            previous_problem = current_problem
            missing = (snap.get('at') == match[1] and not snap.get('id')
                       and snap.get('ready') == 'complete' and snap.get('empty') is True)
            if missing and absent:
                raise PostGone('X 明确答复该推文页面不存在或已不可用')
            absent = missing
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


def merge(entry: dict, text: str) -> dict:
    prefix = entry[HINT]
    if not expanded(text, entry['body'].removeprefix(prefix)):
        raise PostUnavailable('详情页正文没有长于时间线预览')
    complete = dict(entry)
    complete.pop(HINT)
    complete.update(title=text.splitlines()[0][:500], body=prefix + text)
    return x_post_meta.stamp(complete, 'post', prefix, author_text=text)


def fill(entries: list[dict], *, session, deadline=None, should_stop=None):
    kept, missed, request_failed = [], 0, 0
    for entry in entries:
        if HINT not in entry:
            kept.append(entry)
            continue
        try:
            text = fetch(entry['url'], session, deadline=deadline, should_stop=should_stop)
            complete = merge(entry, text)
        except PostGone:
            continue
        except PostUnavailable as exc:
            if x_article.needs_article(entry):
                kept.append(entry)
                continue
            missed += 1
            request_failed += isinstance(exc, PostRequestFailed)
            continue
        kept.append(complete)
    note = f'有 {missed} 条折叠推文的完整正文没取到,留到下次同步再试' if missed else ''
    if request_failed:
        note += f';其中 {request_failed} 条为 X 页面请求出错,请稍后重试'
    return kept, note


def complete(entries: list[dict], *, session, deadline=None, should_stop=None):
    entries, post_note = fill(entries, session=session, deadline=deadline, should_stop=should_stop)
    entries, article_note = x_article.fill(
        entries, session=session, deadline=deadline, should_stop=should_stop)
    unresolved = sum(HINT in entry for entry in entries)
    entries = [entry for entry in entries if HINT not in entry]
    notes = [post_note, article_note]
    if unresolved:
        notes.append(f'有 {unresolved} 条折叠推文没有补到正文,留到下次同步再试')
    return entries, ';'.join(note for note in notes if note)
