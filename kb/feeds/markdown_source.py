"""Prefer public Markdown originals without changing the generic HTML/plain-text contract."""
from __future__ import annotations

import base64
import json
import re
import time
from urllib.parse import quote, unquote, urlsplit

from kb.feeds import readable, registry as reg
from kb.feeds.fetching import FeedError

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
    except (FeedError, OSError, ValueError, TypeError):
        return None
    return None


def enrich(source: dict, entries: list[dict], *, deadline: float,
           collect_deadline: float) -> list[dict]:
    if source.get('fulltext'):
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
