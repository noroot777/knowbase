"""Bounded public HTTP transport and RSS/Atom parsing.

正文转纯文本(`plain`)那一块的硬约束在 `docs/decisions/feed-plain.md` 第一节 —— 改这块之前先读那一份。**

No browser/login side effects live here: X's logged-in channels do their work in
`kb.feeds.x_channels` before the fetch phase fans out, and `fetch_source` only
hands back what that prefetch already collected.
"""
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
from bs4 import BeautifulSoup, NavigableString

from kb.feeds import x_channels
from kb.feeds.registry import normalize_url, uses_channel, x_handle
from kb.source_types import feed_types

MAX_BYTES = 8 * 1024 * 1024
SOURCE_SECONDS = 20
_WS = re.compile(r'\s+')
_CTRL = re.compile(r'[\x00\x02\x03]')
# 元素边界分三档,各插一个标记,相邻的标记随后合并成最强的那一档(\x02 > \x03 >
# 空格),所以「前后各插一个」不会在块与块之间留下双倍空行。
#   块级 \x02 → 空行:这是段落边界,翻译按它切块、markdown 按它分段
#   换行 \x03 → <br> 和表格行,只断行,不然一张表会被空行撑成几十段
#   单元格 → 一个空格,同一行里的几格不该各占一行
# 其余一律当行内,不插任何分隔符 —— <a>/<em>/<code> 切开的是同一个句子。
_BLOCK = frozenset((
    'address article aside blockquote caption dd details dialog div dl dt fieldset figcaption '
    'figure footer form h1 h2 h3 h4 h5 h6 header hgroup hr li main nav ol p pre section summary '
    'table tbody tfoot thead ul').split())
_LINE = frozenset('br tr'.split())
# feedparser 对 Atom 的 type="text" 和 RSS 的裸标题给的就是这几种写法。
_PLAIN_TYPES = frozenset(('text', 'plain', 'text/plain'))
_CELL = frozenset('td th'.split())
_MARKS = {**{t: '\x02' for t in _BLOCK}, **{t: '\x03' for t in _LINE}, **{t: ' ' for t in _CELL}}
_MARK_RUN = re.compile(r' *[\x02\x03][\x02\x03 ]*')


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
                                hint = '；服务限流，请稍后重试' if r.status_code == 429 else '；请检查来源或填写专用订阅链接'
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
    """HTML 正文转纯文本:只有块级元素换行,行内元素不切断句子。

    别退回 `soup.get_text('\n', strip=True)`:那个分隔符对**每一个文本节点**
    都插一次换行,而 <a>/<em>/<code> 这些行内元素正是把一句话切成多个文本节点
    的东西 —— 一句「As <a>promised</a>, Anthropic released ...」出来就是三行,
    每个链接文字独占一行。更糟的是段落边界跟着一起塌掉:<p> 之间本该是空行,
    strip=True 之下也只剩一个 \n,和句中被切开的那些换行再也分不出来
    (2026-10-08 实测:库里 738 条 RSS 条目有 677 条带换行,而带空行的只有 3 条
    —— 段落信息已经在入库那一刻丢光了)。后果是三重的:面板的阅读视图按
    `white-space: pre-wrap` 一行行照显,导出的 vault md 整篇没有一个空行,
    翻译那边 `_split` 按 '\n\n' 切块于是整篇算一段、超过 CHUNK 就从句子中间
    硬切,而 prompt 里写着「保持原有的段落与换行结构」—— 模型忠实照做,
    译文把碎行逐行译了一遍。用户看到的「翻译后排版乱」在英文原文里就已经乱了。
    """
    # 这三个控制字符不会出现在正常正文里,先清掉:下面拿 NUL 当 <pre> 的占位符,
    # \x02 / \x03 当元素边界标记。
    soup = BeautifulSoup(_CTRL.sub('', html), 'html.parser')
    for node in soup(['script', 'style', 'iframe', 'noscript']):
        node.decompose()
    # <pre> 的空白是内容(代码块的缩进和换行),先把它的内容换成占位符,免得被
    # 下面那道 HTML 空白折叠压成一行。**换掉的是内容,不是整个标签** —— 整个
    # replace_with 成一段裸文本的话,`pre` 虽然在 _BLOCK 里,后面那趟 find_all
    # 已经找不到这个标签了,它自己的块级边界跟着一起没:两个相邻的 <pre> 直接
    # 拼成一块(`more_first()second()`),<pre> 和两侧文字之间同样粘连
    # (`Beforecode` / `indentAfter`),复制出来就是错的命令。占位符本身不含空白,
    # 留在标签里一个字也不会被折叠掉。
    kept: list[str] = []
    for tag in soup.find_all('pre'):
        kept.append(tag.get_text().strip('\n'))
        tag.string = f'\x00{len(kept) - 1}\x00'
    # 按 HTML 的空白折叠规则逐个文本节点归一。必须赶在插入分隔符之前做,
    # 否则连我们自己插的换行一起折叠掉。`type(...) is` 而不是 isinstance:
    # Comment / CData / Doctype 都是 NavigableString 的子类,照这样替换会把
    # 注释内容搬进正文。
    for text in soup.find_all(string=True):
        if type(text) is NavigableString and (flat := _WS.sub(' ', text)) != text:
            text.replace_with(NavigableString(flat))
    for tag in soup.find_all(set(_MARKS)):
        mark = _MARKS[tag.name]
        tag.insert_before(NavigableString(mark))
        tag.insert_after(NavigableString(mark))
    # 一串相邻标记(连同夹在中间、折叠出来的空格)合并成一个换行,有块级标记就
    # 是空行。标记两侧的空格一并吃掉 —— 那是源码缩进,不是内容。
    text = _MARK_RUN.sub(lambda m: '\n\n' if '\x02' in m[0] else '\n', soup.get_text(''))
    # 单元格标记是空格,相邻两格各插一个就成了双空格;文本节点内部的空白上面
    # 已经折叠过,这里剩下的都是标记凑出来的。
    text = re.sub(' {2,}', ' ', text)
    # <pre> 放回来要排在最后:它内部的缩进和空行是内容,不该被上面那条规则碰。
    text = re.sub('\x00(\\d+)\x00', lambda m: kept[int(m[1])], text)
    return text.strip()[:100000]


def _plain_lines(text: str) -> str:
    """纯文本正文:原样保留换行,只做最轻的归一(统一行尾、去行末空白、并空行)。

    不能拿 `plain()` 去处理它 —— 那里那道 HTML 空白折叠会把真正的换行当成源码
    缩进抹掉,一篇本来分成几段的纯文本文章被压成一整段(译文、导出的 md、阅读页
    一路继承,长正文还会退回「整篇算一段、超过 CHUNK 从句中硬切」那个老问题)。
    """
    lines = [line.rstrip() for line in text.replace('\r\n', '\n').replace('\r', '\n').split('\n')]
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()[:100000]


def _is_plain(kind: str | None) -> bool:
    """这份正文该不该按纯文本处理 —— **只问类型,不看内容长什么样**。

    别再往这里加「内容里有标签就按 HTML 处理」那种嗅探:**「这段文字能被解析成
    标签」证明不了作者标错了类型**。技术文章里的 `List<T>`、尖括号写的链接、
    一段字面 HTML 示例全是这个形态,而它们正是作者声明 `type="text"` 要原样
    显示的东西 —— 嗅探会把 `<T>` 当标签删掉、把真实段落按 HTML 规则折叠掉,
    一篇正文只有 `<script>alert(1)</script>` 示例的文章更会被脚本清理整个掏空,
    连条目都收录不进来(2026-10-08 第 2 轮审查三种形态各实测一次)。
    """
    return (kind or '').lower() in _PLAIN_TYPES


def body_text(value: str, kind: str | None = '') -> str:
    """按 feedparser 给出的正文类型选解析方式。取正文/标题/作者都走这个入口。"""
    return _plain_lines(value) if _is_plain(kind) else plain(value)


def entry_body(item) -> tuple[str, bool]:
    """条目正文:content 优先,其次 summary,都没有退回标题;各自按自己的类型解析。

    类型必须跟着值一起取 —— 同一个条目里 content 可能是 `text/plain` 而 summary
    是 `text/html`,把它们混成一个 `a or b or c` 再统一按 HTML 解析,纯文本那一份
    的分段就没了。
    """
    for c in item.get('content', []):
        if c.get('value'):
            return body_text(c['value'], c.get('type')), True
    if summary := item.get('summary', ''):
        return body_text(summary, item.get('summary_detail', {}).get('type')), False
    return oneline(item.get('title', ''), item.get('title_detail', {}).get('type')), False


def oneline(value: str, kind: str | None = '') -> str:
    """标题、作者这类单行字段:块级元素同样不该在里面留下换行。"""
    return ' '.join(body_text(value, kind).split())


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
        text, has_content = entry_body(item)
        if not text:
            continue
        title = (oneline(item.get('title', ''), item.get('title_detail', {}).get('type'))
                 or text.splitlines()[0][:100])
        date = item.get('published_parsed') or item.get('updated_parsed')
        published = ''
        if date:
            try:
                published = datetime.fromtimestamp(calendar.timegm(date), timezone.utc).astimezone(
                    ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M')
            except (ValueError, OverflowError):
                pass
        entries.append({'url': link, 'title': title[:500], 'body': text,
                        'author': oneline(item.get('author', '')), 'published': published,
                        'content_kind': 'post' if social else ('content' if has_content else 'summary')})
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
        # X 账号没有 HTTP 端点可言:填了专用订阅链接就用它,没填的那些由通道
        # 接管(`fetch_source` 开头那一道),压根走不到这里。剩下的是非 X 的
        # social 订阅(Bluesky 的 RSS 之类),它们的地址就是自己那条。
        return [source['feed_url'] or source['url']]

    def parse(self, body: bytes, url: str) -> list[dict]:
        return parse_feed(body, url, social=True)


class PageAdapter(RSSAdapter):
    """没有 feed 的站点:把列表页拆成条目(见 `kb.feeds.page_list`)。"""

    def parse(self, body: bytes, url: str) -> list[dict]:
        # 延迟导入:`page_list` 要用这个模块的 FeedError / canonical_url /
        # discover_feed,顶层导入就是个环(方向同 `readable` 导入 `plain`)。
        from kb.feeds import page_list
        return page_list.parse_page_list(body, url)


ADAPTERS = {'rss': RSSAdapter(), 'social': SocialAdapter(), 'page': PageAdapter()}


def fetch_source(source: dict, settings: dict, *, unconditional: bool = False,
                 deadline: float | None = None,
                 collect_deadline: float | None = None) -> tuple[list[dict], dict]:
    # X 的非 HTTP 通道(feedgrab / 浏览器登录态)不走下面这套 endpoint + etag:
    # 它们在采集阶段开始时已经整批抓完,这里只是按订阅取自己那一份(见
    # `kb.feeds.x_channels`)。放在最前面是因为这条路连 endpoint 都没有 ——
    # `SocialAdapter.endpoints()` 会把账号主页本身当地址返回,那是一个 HTML 页,
    # 解析必然失败,报出来的还是一句跟真实原因无关的"没有返回 RSS / Atom"。
    #
    # 只接管「没填专用订阅链接的那些 X 账号」:填了的照旧走 HTTP
    # (见 `registry.uses_channel`)。
    if x_channels.supports_channels(source['kind']) and uses_channel(source):
        return x_channels.take(source, settings)
    # 简报同理不走 endpoint + etag:它要两跳(RSS 给期号 → 每期落地页给正文),
    # 而这里只发一次 HTTP。整批在采集开头抓完,这里只取自己那一份(见
    # `kb.feeds.digest` 的模块注释,三条"为什么绕不过去"都在那儿)。
    # 延迟导入:`digest` 要用本模块的 request / parse_feed / plain,顶层导入
    # 就是个环(方向同 `PageAdapter.parse` 里那句)。
    from kb.feeds import digest
    if digest.supports(source['kind']):
        return digest.take(source, settings)
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
