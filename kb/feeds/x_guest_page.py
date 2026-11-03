# -*- coding: utf-8 -*-
"""不登录通道的第二件工具:X 自己的未登录页面(个人主页、单条详情页)。

**不起浏览器**:没登录时 x.com 回的是服务端直出的整页 HTML(2026-11-03 实测:普通 HTTP
请求和无 cookie 的无头 Chrome 拿到的是同一份 —— 同样的 `<article>`、同样的正文),
所以一次 HTTP 就够,不必为它启动 Chrome。要带一个普通浏览器的 UA:带 `HeadlessChrome`
的直接 403。

**它在这条通道里干两件事,各有射程**(选谁当主路见 `x_guest` 的模块注释):
- 个人主页:**只露最新四五条他自己发的**(不含转推),正文是截断预览(带「显示更多」
  按钮)。所以它只是 FxTwitter 问不到时的后路,而且多半证明不了读到了下界。
- 单条详情页:主推文是**全文**,链接在 `title` 里给出完整地址(显示文字是缩短的)。
  后路读到的截断预览由它补全。

**这套标记跟登录后的页面完全不同**(没有 `data-testid`,时间不在 `<time>` 里而在一段
内联脚本 `formatRelativePastTimestamp` / `formatFullTimestamp` 的参数里,脚本第一个参数
就是那个时间链接的 id),所以浏览器通道的收集脚本一行都用不上。认不出来一律抛
`PageError`:有 `<article>` 却一条都配不上时间,就是页面换了形状,不许当成「没有推文」。
只有一部分配不上时,认得出来的照给,认不出来的条数一并交出去(`posts` 返回的第二项),由调用方
记成「没读全」—— 不许拿剩下几条把它们证明掉。
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup, NavigableString, Tag

from kb.feeds.fetching import FeedError, request

UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36')
_HEADERS = {'User-Agent': UA, 'Accept': 'text/html,application/xhtml+xml',
            'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8'}
_STAMP = re.compile(r'__xClientTextFormatters\?\.run\(\s*"([^"]+)"\s*,\s*"format\w*Timestamp"\s*,\s*(\{.*?\})\s*\)')
# 时间所在的那个链接。编辑过的推文,时间挂在指向 `/history` 的链接里(实测)。
_STATUS = re.compile(r'^/([A-Za-z0-9_]{1,20})/status/(\d+)(?:/history)?$')
_ARTICLE = re.compile(r'/(?:i|[A-Za-z0-9_]{1,20})/article/(\d+)')
_MORE = ('显示更多', 'Show more')
_PINNED = ('已置顶', 'Pinned')
SHANGHAI = ZoneInfo('Asia/Shanghai')


class PageError(RuntimeError):
    """这一页没读成。文案直接给用户。"""


@dataclass(frozen=True)
class Post:
    author: str
    ident: str
    when: datetime
    name: str
    text: str
    truncated: bool
    quoted: bool
    article: str
    pinned: bool

    @property
    def url(self) -> str:
        return f'https://x.com/{self.author}/status/{self.ident}'


def fetch(url: str, deadline: float) -> str:
    try:
        resp = request(url, _HEADERS, deadline=deadline)
    except FeedError as e:
        raise PageError(f'X 页面没答复({re.split(r"[;；]", str(e))[0].strip()})') from None
    return resp.body.decode('utf-8', 'replace')


def _own(article: Tag) -> list[Tag]:
    """这一条自己的节点 —— 不含嵌在里面的引用推。"""
    return [n for n in article.find_all(True) if n.find_parent('article') is article]


def _text(node: Tag) -> str:
    """按文本节点读;链接换成 `title` 里的完整地址,表情图换成 alt,「显示更多」按钮不算正文。"""
    out = []
    for child in node.children:
        if isinstance(child, NavigableString):
            out.append(str(child))
        elif child.name == 'a' and (child.get('title') or '').startswith('http'):
            out.append(child['title'])
        elif child.name == 'img':
            out.append(child.get('alt') or '')
        elif child.name == 'br':
            out.append('\n')
        elif isinstance(child, Tag) and child.name not in ('script', 'style', 'button'):
            out.append(_text(child))
    return ''.join(out)


def _with_card(text: str, card: str) -> str:
    if not card or card in text:
        return text
    return f'{text} {card}' if text else card


def instant(value, per_second: int = 1) -> datetime | None:
    """远端给的时间戳 → UTC 时间;**两件工具共用这一份**(FxTwitter 给秒、X 页面给毫秒)。

    认得出来的标准是**整条日期表示链都走得通**:是数(不是布尔)、有限、为正、能构造 UTC 时间,
    而且能换成项目时区写成入库的那个字符串(`x_guest._build` 的 `published`)。任何一步不行都是
    `None`,那一条按认不出来算。有限性判断、单位换算也都在保护里 —— 几百位的整数是合法 JSON,
    转浮点那一步就抛;UTC 落在 9999-12-31 下午的,换到上海时区才越界。
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        if not math.isfinite(value) or value <= 0:
            return None
        when = datetime.fromtimestamp(value / per_second, timezone.utc)
        when.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')
    except (OverflowError, ValueError, OSError):
        return None
    return when


def _post(article: Tag, stamps: dict[str, datetime]) -> Post | None:
    own = _own(article)
    # **身份按「带时间的那个节点」认**:脚本第一个参数就是它的 id,它自己或它外面那层链接
    # 指向这条推文。不按「第一条 status 链接」认 —— 那可能是引用卡片或别人的回复。
    stamp, anchor = None, None
    for node in own:
        if node.get('id') in stamps:
            link = node if node.name == 'a' else node.find_parent('a')
            if link is not None and _STATUS.match(link.get('href', '')):
                stamp, anchor = stamps[node['id']], link
                break
    if anchor is None:
        return None
    author, ident = _STATUS.match(anchor['href']).groups()
    body = next((n for n in own if n.name == 'div' and 'whitespace-pre-wrap' in (n.get('class') or [])), None)
    # 末尾那条链接被 X 渲染成卡片时,正文里就没有它了,只剩卡片上那条 t.co(没有 `title`)。
    # 推文原文里写着的正是这条 t.co,把它接回去,不让正文悄悄少一条链接。
    card = next((n['href'] for n in own if n.name == 'a' and n.get('href', '').startswith('https://t.co/')
                 and not n.get('title') and not any(p is body for p in n.parents)), '')
    name = next((n.get_text(strip=True) for n in own if n.name == 'a' and n.get('href') == f'/{author}'
                 and n.get_text(strip=True) and not n.get_text(strip=True).startswith('@')), author)
    head = article.get_text(' ', strip=True)[:40]
    long_form = next((m.group(1) for n in own if n.name == 'a' and (m := _ARTICLE.search(n.get('href', '')))), '')
    return Post(author=author, ident=ident,
                when=stamp,
                name=name, text=_with_card(_text(body).strip() if body else '', card),
                truncated=any(n.name == 'button' and n.get_text(strip=True) in _MORE for n in own),
                quoted=article.find('article') is not None, article=long_form,
                pinned=any(head.startswith(p) for p in _PINNED))


def posts(html: str) -> tuple[list[Post], int]:
    """页面上那几条推文(按页面顺序),和认不出来的顶层推文条数。有推文却一条都认不出来就抛。"""
    stamps = {}
    for ident, args in _STAMP.findall(html):
        try:
            when = instant(json.loads(args).get('timestamp'), 1000)
        except (ValueError, AttributeError, RecursionError):
            continue
        if when is not None:
            stamps[ident] = when
    try:
        soup = BeautifulSoup(html, 'html.parser')
        tops = [a for a in soup.find_all('article') if a.find_parent('article') is None]
        got = [p for p in (_post(a, stamps) for a in tops) if p]
    except RecursionError:
        raise PageError('X 页面嵌套深得读不下去') from None
    if not tops:
        raise PageError('X 页面上一条推文都没有(登录墙、限流或账号不可见)')
    if not got:
        raise PageError(f'X 页面换了形状,{len(tops)} 条推文一条都认不出来')
    return got, len(tops) - len(got)


def timeline(handle: str, deadline: float) -> tuple[list[Post], int]:
    """未登录主页上露出来的那几条(含置顶,不含转推),和认不出来的条数。"""
    return posts(fetch(f'https://x.com/{handle}', deadline))


def detail(handle: str, ident: str, deadline: float) -> Post:
    """单条详情页上的那条主推文。身份对不上、或者仍是截断的,一律抛。"""
    for post in posts(fetch(f'https://x.com/{handle}/status/{ident}', deadline))[0]:
        if post.ident == ident:
            if post.truncated:
                raise PageError('X 详情页上的正文仍是截断的')
            return post
    raise PageError('X 详情页上没有这条推文')
