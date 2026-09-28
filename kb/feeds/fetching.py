"""Bounded public HTTP transport and RSS/Atom parsing. No browser/login side effects."""
from __future__ import annotations

import calendar
import ipaddress
import re
import socket
import time
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
SOURCE_SECONDS = 20


class FeedError(ValueError):
    pass


class CollectionBudgetExceeded(Exception):
    """The run stopped this fetch; the source has not failed."""


def public_addresses(url: str) -> tuple[str, list[str]]:
    url = normalize_url(url)
    host = urlsplit(url).hostname
    try:
        addresses = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        raise FeedError('无法解析订阅服务器地址') from None
    public = list(dict.fromkeys(a[4][0] for a in addresses
                               if ipaddress.ip_address(a[4][0]).is_global))
    if not public:
        raise FeedError('订阅域名未解析到公网地址，已拒绝连接')
    return url, public


def public_url(url: str) -> str:
    return public_addresses(url)[0]


def remaining(deadline: float, collect_deadline: float | None = None) -> float:
    now = time.monotonic()
    # Keep the cause of the limiting deadline, even if a blocking operation
    # returns after both deadlines. A tie belongs to the collection budget.
    if collect_deadline is not None and collect_deadline <= deadline:
        if now >= collect_deadline:
            raise CollectionBudgetExceeded
        deadline = collect_deadline
    seconds = deadline - now
    if seconds <= 0:
        raise FeedError('本次订阅读取超时，请稍后重试')
    return seconds


@dataclass
class Response:
    status: int
    url: str
    body: bytes
    headers: dict


def tls_context(hostname: str):
    context = httpx.create_ssl_context()
    wrap_socket = context.wrap_socket

    def wrap_for_origin(*args, server_hostname=None, **kwargs):
        return wrap_socket(*args, server_hostname=hostname, **kwargs)

    # httpcore's CONNECT/SOCKS transports ignore the sni_hostname extension and
    # otherwise verify the numeric IP. Bind this one context to the real origin;
    # certificate verification stays enabled, including when using an env proxy.
    context.wrap_socket = wrap_for_origin
    return context


def request(url: str, headers: dict | None = None, *, deadline: float | None = None,
            collect_deadline: float | None = None) -> Response:
    deadline = deadline if deadline is not None else time.monotonic() + SOURCE_SECONDS
    try:
        for _ in range(6):
            remaining(deadline, collect_deadline)
            url, addresses = public_addresses(url)
            original = httpx.URL(url)
            # Direct sockets and proxy CONNECT targets use validated numeric IPs.
            # A separate origin context preserves Host/SNI/cert verification and
            # prevents connection/cookie reuse across redirect origins.
            with httpx.Client(verify=tls_context(original.raw_host.decode('ascii')),
                              trust_env=True, follow_redirects=False,
                              limits=httpx.Limits(max_keepalive_connections=0),
                              headers={'User-Agent': 'KnowledgeBaseFeedReader/1.0',
                                       'Accept': 'application/atom+xml, application/rss+xml, application/xml, text/xml;q=0.9, text/html;q=0.5'}) as client:
                for index, address in enumerate(addresses):
                    try:
                        budget = remaining(deadline, collect_deadline)
                        with client.stream('GET', original.copy_with(host=address),
                                           headers={**(headers or {}), 'Host': original.netloc.decode('ascii')},
                                           extensions={'sni_hostname': original.raw_host.decode('ascii')},
                                           timeout=httpx.Timeout(min(8, budget), connect=min(6, budget))) as r:
                            remaining(deadline, collect_deadline)
                            if r.status_code in (301, 302, 303, 307, 308):
                                dest = urljoin(url, r.headers.get('location', ''))
                                if urlsplit(dest)[:2] != urlsplit(url)[:2]:
                                    headers = {}  # Clear validators across origins.
                                url = dest
                                break  # Re-resolve and validate every redirect.
                            if r.status_code == 304:
                                return Response(304, url, b'', dict(r.headers))
                            if r.status_code >= 400:
                                hint = '；服务限流，请稍后重试' if r.status_code == 429 else '；请检查来源或更换订阅桥接'
                                raise FeedError(f'订阅服务返回 HTTP {r.status_code}{hint}')
                            content = bytearray()
                            for block in r.iter_bytes():
                                remaining(deadline, collect_deadline)
                                content.extend(block)
                                if len(content) > MAX_BYTES:
                                    raise FeedError('订阅内容超过 8 MB，请改用较小的订阅源')
                            return Response(r.status_code, url, bytes(content), dict(r.headers))
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if index == len(addresses) - 1:
                            raise
        raise FeedError('订阅地址重定向次数过多')
    except httpx.TimeoutException:
        remaining(deadline, collect_deadline)
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


def fetch_source(source: dict, settings: dict, *, unconditional: bool = False,
                 deadline: float | None = None,
                 collect_deadline: float | None = None) -> tuple[list[dict], dict]:
    deadline = deadline if deadline is not None else time.monotonic() + SOURCE_SECONDS
    adapter = ADAPTERS[feed_types()[source['kind']].adapter]
    errors = []
    for endpoint in adapter.endpoints(source, settings):
        remaining(deadline, collect_deadline)
        headers = {}
        if not unconditional and endpoint == source.get('resolved_url'):
            if source.get('etag'):
                headers['If-None-Match'] = source['etag']
            if source.get('modified'):
                headers['If-Modified-Since'] = source['modified']
        try:
            response = request(endpoint, headers, deadline=deadline, collect_deadline=collect_deadline)
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
                response = request(discovered, deadline=deadline, collect_deadline=collect_deadline)
                entries = adapter.parse(response.body, response.url)
            return entries, {'etag': response.headers.get('etag',''),
                             'modified': response.headers.get('last-modified',''),
                             'resolved_url': response.url}
        except (ValueError, OSError) as e:
            errors.append(f'{urlsplit(endpoint).hostname}: {e}')
    raise FeedError('；'.join(errors)[:1200] or '未配置可用的订阅地址')
