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
from typing import Iterable
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

import feedparser
import httpx
from bs4 import BeautifulSoup, NavigableString

from kb.feeds import page_gate, resolve, x_channels
from kb.feeds.registry import normalize_url, uses_channel, x_handle
from kb.source_types import feed_types

MAX_BYTES = 8 * 1024 * 1024
# 「压根没解析到」这一句两条解析路都要用(系统解析器报错 / DoH 说没有这个域名),
# 写两遍就会有一天只改了一处。「解析到了但全是内网」是另一句,在 public_addresses 里。
_UNRESOLVED = '无法解析订阅服务器地址'
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


class Gated(FeedError):
    """**这一次响应自己说它是一道关卡**(`page_gate.response_reason` 认出来的)。

    和普通抓取失败分开成一个类型,不是为了好看,是两处要据此分流:
    - `markdown_source.fetch` 把普通失败吞成 `None`「换条路试试」,而关卡不是
      换条路能解决的 —— 吞掉之后主路会拿同一个站点再抓一次,照样被拦,只是白敲
      一次门;
    - `fulltext._fail` 据此决定 `fulltext_hold` 那一列怎么动:关卡是**已经查清**
      (清掉待办),而超时、连接失败这些**什么都没查清**(那一列原样不动,见
      `_fail` 自己的说明)。

    **判据住在传输层**:`request` 一拿到响应就问,所以它排在任何「这是什么格式」
    的分派**之前** —— 响应身份与正文格式无关。审查第 1 轮实测过放在分派之后的
    后果:带 `cf-mitigated` 的 `text/markdown` 响应,订阅补抓和手动收录两个入口
    都把 492 字拒绝说明收成了正文、写上 `fulltext_at`、退出重试队列。
    """


class CollectionBudgetExceeded(Exception):
    """The run stopped this fetch; the source has not failed."""


def host_for_lookup(host: str | None) -> tuple[str, str | None]:
    """主机名规范化:**两条路共用这一份**,所以翻开关不改变结果。

    - `('literal', '1.1.1.1')` —— 地址字面量(含 `[::1]` 那种方括号形式去过壳的)。
      **它不是 DNS 名字,一个解析器都不该问**:实测 DoH 把 `1.1.1.1` 答成 NXDOMAIN,
      于是一条本来抓得到的订阅报「无法解析订阅服务器地址」。字面量照旧要过下面那道
      `is_global`,所以 `127.0.0.1`、内网地址仍然被拒 —— SSRF 这道一分不少。
    - `('name', 'xn--mnchen-3ya.de')` —— DNS 名字。**非 ASCII 先 IDNA**:DoH 端点对
      `münchen.de` 直接回 HTTP 400,而 `getaddrinfo` 自己会编码,于是开关一翻结果就
      不一样。**纯 ASCII 原样放过**(不走 idna codec):它会拒掉 `getaddrinfo` 本来
      认得的东西(实测 63 字符以上的标签),那种收紧不属于这一层的事。
    - `('name', None)` —— 编不出来,等同解析失败(原来 `getaddrinfo` 也失败)。
    """
    if not host:
        return 'name', None
    bare = host.strip('[]')
    try:
        return 'literal', str(ipaddress.ip_address(bare))
    except ValueError:
        pass
    if host.isascii():
        return 'name', host
    try:
        return 'name', host.encode('idna').decode('ascii')
    except (UnicodeError, UnicodeDecodeError):
        return 'name', None


def _public_only(found: Iterable[str]) -> list[str]:
    """公网地址、去重、保序。**这一族的 SSRF 判据只有这一份**,两条路和 DoH 的缓存
    (`resolve.addresses` 的 `keep`)都过它。"""
    return list(dict.fromkeys(a for a in found if ipaddress.ip_address(a).is_global))


def _require_public(public: list[str]) -> list[str]:
    if not public:
        raise FeedError('订阅域名未解析到公网地址，已拒绝连接')
    return public


def public_addresses(url: str, *, deadline: float | None = None,
                     collect_deadline: float | None = None) -> tuple[str, list[str]]:
    """「这个域名能连哪几个地址」—— 这一族的口径只有这一份,`request` 每跳都过它。

    **换解析器换的只是「地址从哪来」**(`resolve`:配了 `KB_FEED_DOH` 就改问一台可信的
    DoH 解析器,默认关着走系统解析)。下面那两道判据、去重顺序、以及调用方钉数字 IP
    的写法都没动 —— 本机解析被污染时 SSRF 这一道**还是对的**,它正是把
    `buy-european.net` 那条指向 127.0.0.1 的假答案拒掉的东西。形态见
    `docs/decisions/dns-egress.md`。

    **DoH 的预算从这一次的 deadline 里扣,不另开一份**:`remaining()` 这一下还顺带
    让整轮采集的 `collect_deadline` 继续管得住它(超了照旧抛
    `CollectionBudgetExceeded`,不会被包成一次订阅失败)。
    """
    url = normalize_url(url)
    host = urlsplit(url).hostname
    kind, name = host_for_lookup(host)
    if kind == 'literal':
        found = [name]
    elif name is None:
        raise FeedError(_UNRESOLVED)
    elif resolve.configured():
        seconds = (remaining(deadline, collect_deadline) if deadline is not None
                   else resolve.seconds_budget())
        try:
            return url, _require_public(resolve.addresses(
                name, seconds=seconds, keep=_public_only))
        except resolve.Unknown:
            raise FeedError(_UNRESOLVED) from None
        except resolve.Unavailable as exc:
            # **和「对方拦着你」分开说。** 这一句说的是我们这一侧没出口或者端点答得
            # 不对,人要去看的是 KB_FEED_DOH 和代理,不是改订阅链接。**原因带上** ——
            # HTTP 400、超时、非 JSON、预算用尽在面板上长一样的话,指不到该改什么。
            raise FeedError(f'域名解析服务不可用（{exc}），请检查 KB_FEED_DOH 与代理设置') from None
    else:
        try:
            found = [a[4][0] for a in socket.getaddrinfo(name, None, type=socket.SOCK_STREAM)]
        except OSError:
            raise FeedError(_UNRESOLVED) from None
    return url, _require_public(_public_only(found))


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
            url, addresses = public_addresses(
                url, deadline=deadline, collect_deadline=collect_deadline)
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
                            if gate := page_gate.response_reason(dict(r.headers)):
                                # **关卡身份排在这一层,而且排在状态码分类和任何
                                # 格式分派之前。** 它问的是「对方这一次拦了没有」,
                                # 和状态码是什么、正文是什么格式都无关:放到下游
                                # 去问,`text/markdown` 和专用原文抓取那两个成功
                                # 出口就会绕过它(`Gated` 的说明里有实测后果)。
                                detail = f'（HTTP {r.status_code}）' if r.status_code >= 400 else ''
                                raise Gated(f'{gate}{detail}')
                            if r.status_code >= 400:
                                # **没有关卡头的 4xx/5xx 不许说成「要过一道验证」。**
                                # 429 是限流、503 是对方不可用,它们说得出下一步
                                # (稍后重试);说成验证会把人指向一件他做不了也不
                                # 需要做的事(审查第 1 轮点出的就是这一处:关卡那
                                # 张表原先把 429/503 也算进去,这句准确的限流提示
                                # 从此不可达)。
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
