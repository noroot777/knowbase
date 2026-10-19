"""Expand timeline previews on the matching status page before storing a post."""
from __future__ import annotations

import json
import re
import time

from kb.feeds import x_article, x_body
from kb.feeds.x_cdp import CdpError
from kb.feeds.x_page_state import STATE_JS, XRateLimited
from kb.feeds.x_post_empty import EMPTY_JS

HINT = '_x_post_prefix'
SECONDS = 25.0
STEP = 0.25
STRUCTURE_SECONDS = 3.0
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
''' + TEXT_JS + EMPTY_JS + STATE_JS + r'''
  const at = location.pathname.match(/^\/[^/]+\/status\/(\d+)(?:\/|$)/);
  const result = {...xPageState(), at: at ? at[1] : '', id: '', text: '', truncated: true,
                  empty_kind: '', unknown_structure: false};
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    const clock = ownNode(art, 'time[datetime]');
    const href = clock?.closest('a[href*="/status/"]')?.getAttribute('href') || '';
    const identity = href.match(/^\/[^/]+\/status\/(\d+)(?:[/?#]|$)/);
    if (!identity || identity[1] !== result.at) continue;
    result.id = identity[1];
    result.text = postText(ownNode(art, '[data-testid="tweetText"]'));
    result.truncated = postMore(art);
    result.empty_kind = emptyPostKind(art, identity[1]);
    result.unknown_structure = emptyPostReady(art) && !result.empty_kind;
    break;
  }
  return JSON.stringify(result);
})()'''


class PostUnavailable(RuntimeError):
    pass


class PostGone(PostUnavailable):
    """The requested, fully loaded page explicitly says the post is unavailable."""


class PostStructureUnknown(PostUnavailable):
    """A loaded matching post has no readable body or positive empty-text proof."""


class PostEmpty(PostUnavailable):
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f'详情页已确认无自写文字({kind})')


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
    empty = None
    unknown_since = None
    try:
        navigation = session.goto(url)
        if isinstance(navigation, dict) and navigation.get('errorText'):
            raise PostUnavailable(f'推文详情页打开失败:{navigation["errorText"]}')
        while True:
            if unknown_since is not None and time.monotonic() >= until:
                raise PostStructureUnknown('详情页正文结构尚未确认,未把选择器缺失当作无自写文字')
            _check(until, should_stop)
            snap = json.loads(session.eval(READ_JS))
            if snap.get('login'):
                raise PostUnavailable('推文详情页需要重新登录 X')
            if snap.get('limited'):
                raise XRateLimited('X 暂时限制了访问频率,请稍后重试或降低同步频率')
            missing = (snap.get('at') == match[1] and not snap.get('id')
                       and snap.get('ready') == 'complete' and snap.get('empty') is True)
            if missing and absent:
                raise PostGone('X 明确答复该推文页面不存在或已不可用')
            absent = missing
            kind = snap.get('empty_kind')
            confirmed_empty = (snap.get('at') == match[1] == snap.get('id')
                               and snap.get('ready') == 'complete' and snap.get('text') == ''
                               and snap.get('truncated') is False
                               and kind in {'photo', 'video', 'quote', 'link', 'unspecified'})
            if confirmed_empty and empty == kind:
                raise PostEmpty(kind)
            empty = kind if confirmed_empty else None
            valid = (snap.get('at') == match[1] == snap.get('id')
                     and isinstance(snap.get('text'), str) and snap['text'].strip()
                     and snap.get('truncated') is False)
            text = snap['text'].strip() if valid else None
            if text is not None and text == previous:
                return text
            previous = text
            unknown = (snap.get('at') == match[1] == snap.get('id') and
                       snap.get('ready') == 'complete' and snap.get('text') == '' and
                       snap.get('truncated') is False and snap.get('unknown_structure') is True)
            unknown_since = (unknown_since if unknown_since is not None else time.monotonic()
                             ) if unknown else None
            if unknown_since is not None and time.monotonic() - unknown_since >= STRUCTURE_SECONDS:
                raise PostStructureUnknown('详情页正文结构尚未确认,未把选择器缺失当作无自写文字')
            time.sleep(min(STEP, max(0.0, until - time.monotonic())))
    except (CdpError, OSError, EOFError, ValueError, TypeError, AttributeError) as exc:
        raise PostUnavailable(f'推文详情页读取失败:{exc}') from exc


def merge(entry: dict, text: str) -> dict:
    body = x_body.parts(entry)
    if not expanded(text, body.text):
        raise PostUnavailable('详情页正文没有长于时间线预览')
    complete = x_body.with_text(entry, text)
    complete.pop(HINT)
    complete.update(title=text.splitlines()[0][:500])
    return complete


def fill(entries: list[dict], *, session, deadline=None, should_stop=None, unavailable=None):
    kept, missed = [], 0
    for entry in entries:
        if HINT not in entry:
            kept.append(entry)
            continue
        try:
            text = fetch(entry['url'], session, deadline=deadline, should_stop=should_stop)
            complete = merge(entry, text)
        except (PostGone, PostEmpty) as exc:
            if isinstance(exc, PostEmpty) and entry.get(x_article.HINT):
                kept.append(entry)
                continue
            if unavailable is not None:
                evidence = dict(url=entry['url'], reason=str(exc))
                if isinstance(exc, PostEmpty):
                    evidence['empty_kind'] = exc.kind
                unavailable.append(evidence)
            continue
        except PostUnavailable:
            if x_article.needs_article(entry):
                kept.append(entry)
                continue
            missed += 1
            continue
        kept.append(complete)
    note = f'有 {missed} 条折叠推文的完整正文没取到,留到下次同步再试' if missed else ''
    return kept, note


def unavailable_note(unavailable: list[dict]) -> str:
    if not unavailable:
        return ''
    details = ';'.join(f'{item["url"]} ({item["reason"]})' for item in unavailable)
    label = '推文' if any(item.get('empty_kind') for item in unavailable) else '远端明确不可用推文'
    return f'跳过 {len(unavailable)} 条{label}(跳过本身不冻结水位):{details}'


def unavailable_report(unavailable: list[dict]) -> dict:
    return dict(unavailable=unavailable, unavailable_count=len(unavailable),
                note=unavailable_note(unavailable))


def complete(entries: list[dict], *, session, deadline=None, should_stop=None, unavailable=None):
    entries, post_note = fill(entries, session=session, deadline=deadline,
                              should_stop=should_stop, unavailable=unavailable)
    entries, article_note = x_article.fill(
        entries, session=session, deadline=deadline, should_stop=should_stop)
    unresolved = sum(HINT in entry for entry in entries)
    entries = [entry for entry in entries if HINT not in entry]
    notes = [post_note, article_note]
    if unresolved:
        notes.append(f'有 {unresolved} 条折叠推文没有补到正文,留到下次同步再试')
    return entries, ';'.join(note for note in notes if note)
