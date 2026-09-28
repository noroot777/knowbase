"""Bounded public HTTP transport and RSS/Atom parsing. No browser/login side effects."""
from __future__ import annotations

import calendar
import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import httpx
from bs4 import BeautifulSoup

from kb.feeds.registry import normalize_url, x_handle
from kb.source_types import feed_types

MAX_BYTES = 8 * 1024 * 1024


class FeedError(ValueError):
    pass


def public_url(url: str) -> str:
    url = normalize_url(url)
    host = urlsplit(url).hostname
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        raise FeedError('无法解析订阅服务器地址') from None
    if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
        raise FeedError('订阅服务器必须使用公开网络地址')
    return url


@dataclass
class Response:
    status: int
    url: str
    body: bytes
    headers: dict


def request(url: str, headers: dict | None = None) -> Response:
    try:
        with httpx.Client(timeout=httpx.Timeout(18, connect=8), follow_redirects=False,
                          headers={'User-Agent': 'KnowledgeBaseFeedReader/1.0',
                                   'Accept': 'application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, text/html;q=0.5'}) as client:
            for _ in range(6):
                url = public_url(url)
                with client.stream('GET', url, headers=headers or {}) as r:
                    if r.status_code in (301, 302, 303, 307, 308):
                        dest = urljoin(url, r.headers.get('location', ''))
                        if urlsplit(dest).netloc != urlsplit(url).netloc:
                            headers = {}  # Do not forward credentials/validators to another origin.
                        url = dest
                        continue
                    if r.status_code == 304:
                        return Response(304, url, b'', dict(r.headers))
                    if r.status_code >= 400:
                        hint = '；服务限流，请稍后重试' if r.status_code == 429 else '；请检查来源或更换订阅桥接'
                        raise FeedError(f'订阅服务返回 HTTP {r.status_code}{hint}')
                    content = bytearray()
                    for block in r.iter_bytes():
                        content.extend(block)
                        if len(content) > MAX_BYTES:
                            raise FeedError('订阅内容超过 8 MB，请改用较小的订阅源')
                    return Response(r.status_code, url, bytes(content), dict(r.headers))
            raise FeedError('订阅地址重定向次数过多')
    except httpx.TimeoutException:
        raise FeedError('订阅连接超时，请稍后重试或修改链接') from None
    except httpx.HTTPError:
        raise FeedError('无法连接订阅服务，请检查网络或修改链接') from None


def plain(html: str) -> str:
    soup = BeautifulSoup(html, 'html.parser')
    for node in soup(['script', 'style', 'iframe', 'noscript']):
        node.decompose()
    return re.sub(r'\n[ \t]*\n+', '\n\n', soup.get_text('\n', strip=True)).strip()[:100000]


def canonical_url(url: str, social: bool = False) -> str:
    url = normalize_url(url)
    p = urlsplit(url)
    if social and (m := re.fullmatch(r'/([^/]+)/status/(\d+)/?', p.path)):
        return f'https://x.com/{m[1].lower()}/status/{m[2]}'
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.startswith('utm_') and k not in ('fbclid', 'gclid')]
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ''))


def parse_feed(body: bytes, base_url: str, *, social: bool = False) -> list[dict]:
    parsed = feedparser.parse(body)
    if not parsed.get('version'):
        raise FeedError('该链接没有返回 RSS / Atom；可能是登录页或验证页面，请修改订阅链接')
    if parsed.get('bozo') and not parsed.entries:
        raise FeedError('订阅 XML 无法解析，请检查订阅服务')
    entries = []
    # Feeds need not be ordered newest-first (some author feeds are ascending).
    def date_key(item):
        value = item.get('published_parsed') or item.get('updated_parsed')
        try:
            return calendar.timegm(value) if value else 0
        except (ValueError, OverflowError):
            return 0
    for item in sorted(parsed.entries, key=date_key, reverse=True)[:500]:
        link = item.get('link') or ''
        if not link:
            # A permalink-like GUID is usable; an opaque GUID is not a source URL.
            link = item.get('id', '') if str(item.get('id', '')).startswith(('http://', 'https://')) else ''
        if not link:
            continue
        try:
            link = canonical_url(urljoin(base_url, link), social)
        except ValueError:
            continue
        content = next((c.get('value', '') for c in item.get('content', []) if c.get('value')), '')
        text = plain(content or item.get('summary', '') or item.get('title', ''))
        if not text:
            continue
        title = plain(item.get('title', '')) or text.splitlines()[0][:100]
        date = item.get('published_parsed') or item.get('updated_parsed')
        published = ''
        if date:
            try:
                published = datetime.fromtimestamp(calendar.timegm(date), timezone.utc).astimezone(
                    ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
            except (ValueError, OverflowError):
                pass
        entries.append({'url': link, 'title': title[:500], 'body': text,
                        'author': plain(item.get('author', '')), 'published': published,
                        'content_kind': 'post' if social else ('content' if content else 'summary')})
    if parsed.entries and not entries:
        raise FeedError('订阅有条目，但没有可保存的正文和原文链接')
    return entries


def discover_feed(body: bytes, url: str) -> str:
    soup = BeautifulSoup(body, 'html.parser')
    for link in soup.select('link[rel~=alternate][href]'):
        if link.get('type') in ('application/rss+xml', 'application/atom+xml'):
            return normalize_url(urljoin(url, link['href']))
    return ''


class RSSAdapter:
    def endpoints(self, source: dict, settings: dict) -> list[str]:
        return [source['feed_url'] or source['url']]

    def parse(self, body: bytes, url: str) -> list[dict]:
        return parse_feed(body, url)


class SocialAdapter(RSSAdapter):
    def endpoints(self, source: dict, settings: dict) -> list[str]:
        if source['feed_url']:
            return [source['feed_url']]
        handle = x_handle(source['url'])
        return ([s.replace('{handle}', handle) for s in settings['bridges']]
                if handle else [source['url']])

    def parse(self, body: bytes, url: str) -> list[dict]:
        return parse_feed(body, url, social=True)


ADAPTERS = {'rss': RSSAdapter(), 'social': SocialAdapter()}


def fetch_source(source: dict, settings: dict, *, unconditional: bool = False) -> tuple[list[dict], dict]:
    adapter = ADAPTERS[feed_types()[source['kind']].adapter]
    errors = []
    for endpoint in adapter.endpoints(source, settings):
        headers = {}
        if not unconditional and endpoint == source.get('resolved_url'):
            if source.get('etag'):
                headers['If-None-Match'] = source['etag']
            if source.get('modified'):
                headers['If-Modified-Since'] = source['modified']
        try:
            response = request(endpoint, headers)
            if response.status == 304:
                return [], {'etag': source['etag'], 'modified': source['modified'], 'resolved_url': endpoint}
            try:
                entries = adapter.parse(response.body, response.url)
            except FeedError:
                # User may paste a blog homepage advertising a genuine feed. We do
                # not scrape arbitrary blog pages or classify them as RSS entries.
                discovered = discover_feed(response.body, response.url)
                if not discovered:
                    raise
                response = request(discovered)
                entries = adapter.parse(response.body, response.url)
            return entries, {'etag': response.headers.get('etag',''),
                             'modified': response.headers.get('last-modified',''),
                             'resolved_url': response.url}
        except (ValueError, OSError) as e:
            errors.append(f'{urlsplit(endpoint).hostname}: {e}')
    raise FeedError('；'.join(errors)[:1200] or '未配置可用的订阅地址')
