"""Prefer public Markdown originals without changing the generic HTML/plain-text contract."""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import quote, unquote, urlsplit

from kb.feeds import readable, registry as reg
from kb.feeds.fetching import FeedError, Gated
from kb.source_types import permits_link_body

_SUFFIXES = ('.md', '.markdown', '.mdown', '.mkdn')
_COMPONENT = re.compile(r'[A-Za-z0-9_.-]+\Z')
_TEXT_TYPES = frozenset(('text/plain', 'text/markdown', 'text/x-markdown',
                         'application/octet-stream'))


def accept(headers: dict) -> dict:
    return {**headers, 'Accept':'text/markdown, ' + headers.get('Accept', 'text/html')}


def from_response(response) -> str | None:
    headers = {key.lower():value for key, value in response.headers.items()}
    if headers.get('content-type', '').split(';', 1)[0].strip().lower() not in (
            'text/markdown', 'text/x-markdown'):
        return None
    return _text(response.body, headers)


def entry(url: str, body: str) -> dict:
    host = (urlsplit(url).hostname or '').lower().removeprefix('www.')
    title = next((line.lstrip('#').strip() for line in body.splitlines()
                  if line.startswith('# ')), '')
    return {'url':url, 'title':title or urlsplit(url).path.strip('/') or host,
            'body':body, 'author':host, 'published':'', 'content_kind':'content'}


def target(url: str) -> tuple[str, bool] | None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme not in ('https', 'http') or not parsed.hostname:
        return None
    if parsed.username or parsed.password or port not in (None, 80, 443):
        return None
    host = parsed.hostname.lower()
    pieces = parsed.path.strip('/').split('/')
    if host in ('github.com', 'www.github.com'):
        if len(pieces) < 2 or not all(_COMPONENT.fullmatch(part) for part in pieces[:2]):
            return None
        owner, repository = pieces[:2]
        repository = repository.removesuffix('.git')
        if not repository or repository in ('.', '..') or owner in ('.', '..'):
            return None
        root = f'https://api.github.com/repos/{owner}/{repository}'
        if len(pieces) == 2:
            return root + '/readme', True
        if len(pieces) >= 5 and pieces[2] == 'blob' and parsed.path.lower().endswith(_SUFFIXES):
            reference = quote(unquote(pieces[3]), safe='')
            file_path = '/'.join(quote(unquote(part), safe='') for part in pieces[4:])
            return root + '/contents/' + file_path + '?ref=' + reference, True
        return None
    if parsed.path.lower().endswith(_SUFFIXES):
        return url, False
    return None


def _text(body: bytes, headers: dict) -> str | None:
    headers = {key.lower():value for key, value in headers.items()}
    mime = headers.get('content-type', '').split(';', 1)[0].strip().lower()
    if mime not in _TEXT_TYPES:
        return None
    checked = {**headers, 'content-type':'text/plain'}
    if readable.html_problem(body, checked):
        return None
    text = readable.decode(body, headers).replace('\r\n', '\n').replace('\r', '\n')
    if not text.strip() or re.match(r'\s*<(?:!doctype\s+html|html)(?:\s|>)', text, re.I):
        return None
    return text[:100000]


def fetch(url: str, *, transport=None, deadline=None, collect_deadline=None) -> str | None:
    candidate = target(url)
    if candidate is None:
        return None
    if transport is None:
        from kb.feeds.fetching import request
        transport = request
    address, github = candidate
    headers = {'Accept':'application/vnd.github+json' if github else 'text/markdown, text/plain',
               'User-Agent':'knowbase'}
    options = {key:value for key, value in (('deadline', deadline),
               ('collect_deadline', collect_deadline)) if value is not None}
    try:
        response = transport(address, headers, **options)
        if not github:
            return _text(response.body, response.headers)
        data = json.loads(response.body)
        if (not isinstance(data, dict) or data.get('type') != 'file'
                or not str(data.get('name', '')).lower().endswith(_SUFFIXES)):
            return None
        if data.get('encoding') == 'base64' and isinstance(data.get('content'), str):
            content = base64.b64decode(''.join(data['content'].split()), validate=True)
            return _text(content, {'content-type':'text/plain; charset=utf-8'})
        download = data.get('download_url')
        if isinstance(download, str) and urlsplit(download).hostname == 'raw.githubusercontent.com':
            raw = transport(download, {'Accept':'text/plain'}, **options)
            return _text(raw.body, raw.headers)
    except Gated:
        # **关卡不是「换条路试试」能解决的,所以它必须穿透出去。** 别的失败吞成
        # `None` 是对的(专用原文抓不到就走通用 HTML 那条路),但对方明说拦了这一
        # 次请求时,吞掉只会让主路拿同一个站点再敲一次门、照样被拦 —— 而那两次之
        # 间任何一次「恰好没带关卡头」都会把拒绝说明收成正文(`Gated` 的说明)。
        raise
    except (FeedError, OSError, ValueError, TypeError):
        return None
    return None


def enrich(source: dict, entries: list[dict], *, deadline: float,
           collect_deadline: float) -> list[dict]:
    """采集时顺手把能直取的 Markdown 原文换上来 —— **先过来源能力那一道**。

    **这是「拿 `url` 指向的那一页替换本条正文」的第三个入口**,和补抓队列
    (`fulltext`)、浏览器采集(`kb.browser_capture`)同族,判据只有
    `permits_link_body()` 这一份(口径见 `kb/source_types.py`)。

    **漏掉它的后果在生产库里实现过一次。** 原先这儿只有下面那道
    `source['fulltext']` 早退 —— 那不是资格判据,是**分工**:开了补抓的订阅让
    补抓阶段去取(它带重试、挂起和准入八道),这儿不抢。于是闸门方向恰好是反的:
    简报按 `docs/decisions/digest.md` 规定 `fulltext` 必须为 0,**正好落在打开的
    那一侧**,而它的 `url` 指向的是别人家的文章。TLDR AI 里一条指向 GitHub 仓库
    的简讯因此被整篇 README 顶掉(9242 字,`content_kind` 由 `summary` 变
    `content`,已导出进 vault),而同一期另外 4 条同样指向 github.com 的没被换 ——
    差别只是那一刻 `api.github.com` 放不放行(`fetch` 把普通失败吞成 `None`),
    所以它表现为**间歇性**。存量出路见 `scripts/feed_fulltext_recheck.py`。
    """
    if not permits_link_body(source['kind']) or source.get('fulltext'):
        return entries
    candidates = [entry for entry in entries if entry.get('content_kind') != 'post'
                  and target(entry['url']) is not None]
    if not candidates:
        return entries
    with reg.db() as connection:
        known = {row['url'] for row in connection.execute(
            'SELECT url FROM feed_items WHERE kind=?', (source['kind'],))}
    resolved = {}
    for entry in candidates:
        if time.monotonic() >= min(deadline, collect_deadline):
            break
        url = entry['url']
        if url in known:
            continue
        if url not in resolved:
            resolved[url] = fetch(url, deadline=deadline, collect_deadline=collect_deadline)
        if resolved[url] is not None:
            entry['body'] = resolved[url]
            entry['content_kind'] = 'content'
    return entries
