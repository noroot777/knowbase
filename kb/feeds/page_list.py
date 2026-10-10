# -*- coding: utf-8 -*-
"""没有 RSS 的站点:把它的文章列表页拆成条目。

Anthropic 的 news 是典型形态 —— 整站一处 feed 都没有(五个常见 feed 路径
全是 404、页面上没有 `<link rel=alternate>`、公共 RSS 桥接实例要么 403
要么连不上),而它恰恰是最该收的来源之一。

**判据只有一条:带发布时间的那一组链接就是文章列表。** 不认 CSS 类名
(Next.js 的类名带构建哈希,每次发版都变)、不认 tag 名(`li` / `article` /
`div` 都有站点在用)、**也不认"哪一组链接最多"** —— 实测 Anthropic 那一页
上最大的链接组是 90 条页脚菜单,真正的新闻列表只有 10 条、排在第二。日期
是文章列表的本质特征(导航、页脚、推荐位都没有),而且它本来就是我们必须
拿到的字段(`sync._within_history` 要用),所以这条判据不额外欠任何东西。

分组按**结构路径**(从根到 `<a>` 的 tag 序列)而不是按父节点:同一个列表里
的链接路径逐字相同,而 featured 大卡、导航、页脚各在自己的路径上,自动分
开 —— 实测 Anthropic 那张页面上 featured 那条正因此落在另一组里,不会把
它那段摘要当成标题。

**"这一页上一条都没解析出来"一律抛异常,不返回空列表。** 口径同
`x_browser` 那句 `if seen_articles and not parsed: raise`:静默返回空会让
这条订阅从某天起永远是"今天没有新文章",而面板上一切正常 —— 站点改版、
地址填成了文章页、页面改成要 JS 渲染,三种都是这个形态,而它们都需要人
去改一下,不是等下一轮。

正文这一层不碰:列表页只有标题和日期,正文靠**订阅级的 `fulltext` 补抓**
(`kb.feeds.fulltext`)去文章页取 —— 那条链已经有重试计次、二进制拦截、
样板区判定和存量复查,这里再写一遍只会多一份会漂的口径。所以这里给的是
一份**壳**(分类 · 日期 + 链接,形态同 HN Buzzing):它有字,不会落进
`body_gap` 那个"需回填"的永动清单,补抓不动最坏就是停在壳上。
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup, NavigableString

from kb.feeds import readable
from kb.feeds.fetching import FeedError, canonical_url, discover_feed

TZ = ZoneInfo('Asia/Shanghai')

# 一组里至少几条带日期才算"文章列表"。1 条太容易是巧合,而**宁可报错也不要
# 把导航当列表**:误报用户看得见、改一下就好,漏报是往知识库里灌垃圾。
MIN_DATED = 2
# 比这更长的文本块是摘要而不是标题(featured 卡片那种);比下限更短的是角标。
MAX_TITLE, MIN_TITLE = 300, 4
MAX_ITEMS = 500                     # 同 `fetching.parse_feed`
HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']

_MONTH = {m: i for i, m in enumerate(
    ['jan', 'feb', 'mar', 'apr', 'may', 'jun',
     'jul', 'aug', 'sep', 'oct', 'nov', 'dec'], 1)}
_ISO = re.compile(r'\b(\d{4})-(\d{1,2})-(\d{1,2})'
                  r'(?:[T ](\d{1,2}):(\d{2})(?::\d{2})?(?:\.\d+)?'
                  r'(Z|z|[+-]\d{2}:?\d{2})?)?')
_CN = re.compile(r'\b(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日')
_MDY = re.compile(r'\b([A-Za-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b')
_DMY = re.compile(r'\b(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})\b')


def _day(y: int, m: int, d: int) -> str:
    """`YYYY-MM-DD`,算不成一个真实日期就返回空串(挡住 2 月 30 日那类)。"""
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return ''


def read_published(text: str) -> str:
    """一段文字里的发布时间 → `YYYY-MM-DD HH:MM` / `YYYY-MM-DD` / 空串。

    精度**如实保留**:列表页给到日就只给到日,不补一个 00:00 假装知道几点
    (`sync._within_history` 和 `daily.parse_ts` 对只有日期的写法都成立,
    库里本来就有 92 篇是这种)。

    不带时区的写法一律按**项目时区**补齐,不按部署机器的 —— 口径同
    `x_browser._utc`:被比较的那一头(`parse_feed` 的 `published`)固定按
    Asia/Shanghai 格式化,两头不同时区的话每天有 8 小时窗口会差一整天。
    """
    t = ' '.join((text or '').split())
    if not t:
        return ''
    if m := _ISO.search(t):
        day = _day(int(m[1]), int(m[2]), int(m[3]))
        if not day or not m[4]:
            return day
        hour, minute = int(m[4]), int(m[5])
        if hour > 23 or minute > 59:
            return day
        stamp = datetime.fromisoformat(f'{day}T{hour:02d}:{minute:02d}')
        if zone := m[6]:
            if zone in ('Z', 'z'):
                offset = timezone.utc
            else:
                sign = -1 if zone[0] == '-' else 1
                raw = zone[1:].replace(':', '')
                offset = timezone(sign * timedelta(hours=int(raw[:2]), minutes=int(raw[2:])))
            stamp = stamp.replace(tzinfo=offset).astimezone(TZ)
        return stamp.strftime('%Y-%m-%d %H:%M')
    if m := _CN.search(t):
        return _day(int(m[1]), int(m[2]), int(m[3]))
    if m := _MDY.search(t):
        if month := _MONTH.get(m[1][:3].lower()):
            return _day(int(m[3]), month, int(m[2]))
    if m := _DMY.search(t):
        if month := _MONTH.get(m[2][:3].lower()):
            return _day(int(m[3]), month, int(m[1]))
    return ''


def item_published(node) -> str:
    """这条记录的发布时间。`<time>` 优先,它是"这是个时间"的标准语义。"""
    for tag in node.find_all('time'):
        if stamp := (read_published(tag.get('datetime') or '')
                     or read_published(tag.get_text(' '))):
            return stamp
    return read_published(node.get_text(' '))


def _blocks(node, skip=frozenset()) -> list[str]:
    """这条记录里的文本块,按出现顺序、已归一空白;`skip` 里那些容器不算。"""
    out = []
    for raw in node.descendants:
        # `type(...) is NavigableString` 而不是 isinstance:`Comment` / `CData` /
        # `Doctype` 都是它的子类,照收会把注释内容当成标题(同 `fetching.plain`)。
        if type(raw) is not NavigableString:
            continue
        parent, dropped = raw.parent, False
        while parent is not None and not dropped:
            dropped = id(parent) in skip
            parent = parent.parent
        if not dropped and (text := ' '.join(raw.split())):
            out.append(text)
    return out


def _meta_box(node) -> set:
    """`<time>` 所在的那个最小子容器 —— 日期和角标在里面,标题不在。

    **这是结构信息,比"哪个块最长"可靠。** 长度判据在一个真实的边角上会取
    错:标题**可能比分类还短**(`First thing` 11 字 vs `Announcements` 13 字),
    那时"最长块"拿回来的是分类,整条订阅的标题全变成同一个词。实测
    Anthropic 的标题是 24~96 字所以碰不到,但那是运气、不是判据。
    """
    out = set()
    for tag in node.find_all('time'):
        box = tag.parent if tag.parent is not None and tag.parent is not node else tag
        out.add(id(box))
    return out


def item_title(node) -> str:
    """这条记录的标题。

    `h1`-`h6` 优先:那是"这是个标题"的标准语义,大多数站点都这么写。没有
    标题标签时**先摘掉 `<time>` 那个 meta 容器**(日期、分类、阅读时长都在
    里面),剩下的块里取最长的 —— 装标题的那个 `<span>` 类名带构建哈希、
    认不得,只能按位置认。

    摘完一块不剩时退回全部块(`<time>` 和标题平级的那种结构,那时容器里本来
    就没有 meta 区):退路保证这一道只会比纯长度判据更准,不会更差。
    """
    for tag in node.find_all(HEADINGS):
        if title := ' '.join(tag.get_text(' ').split()):
            return title[:500]
    blocks = _blocks(node, _meta_box(node)) or _blocks(node)
    best = ''
    for text in blocks:
        if MIN_TITLE <= len(text) <= MAX_TITLE and len(text) > len(best) \
                and not read_published(text):
            best = text
    return best[:500]


def item_subject(node, title: str) -> str:
    """分类、栏目这类角标(Anthropic 的 `Announcements`)。拿不到就空着。"""
    out = [t for t in _blocks(node)
           if t != title and len(t) <= 40 and not read_published(t)]
    return ' · '.join(dict.fromkeys(out))[:120]


def shell_body(subject: str, published: str, url: str) -> str:
    """还没有正文时写进库的那份壳。

    **不重复标题**:面板上标题本来就显示在正文正上方,壳里再写一遍就是同一
    句话读两遍。**也不写"正文待补抓"这类状态**:补抓失败时它会永久留在
    vault 里,而"还在等正文"该由面板那两档状态说(见 `registry.items`)。
    """
    head = ' · '.join(x for x in (subject, published) if x)
    return f'{head}\n{url}' if head else url


def _list_group(soup: BeautifulSoup, base_url: str):
    """页面上的文章列表 → `[(容器, 绝对地址)]`,按带日期的条目数取胜出组。"""
    buckets: dict[tuple, list] = {}
    for anchor in soup.find_all('a', href=True):
        href = (anchor.get('href') or '').strip()
        if not href or href.startswith(('#', 'mailto:', 'javascript:', 'tel:')):
            continue
        try:
            url = canonical_url(urljoin(base_url, href))
        except ValueError:
            continue
        if urlsplit(url).scheme not in ('http', 'https'):
            continue
        buckets.setdefault(_tag_path(anchor), []).append((anchor, url))
    best, best_dated = [], 0
    for items in buckets.values():
        paired = list(zip(_containers([a for a, _ in items]), items))
        dated = sum(1 for box, _ in paired if item_published(box))
        if dated > best_dated:
            best, best_dated = [(box, url) for box, (_, url) in paired], dated
    return best if best_dated >= MIN_DATED else []


def _tag_path(node) -> tuple:
    """从根到这个节点的 tag 序列。同一个列表里的链接,这条路径逐字相同。"""
    parts = []
    while node is not None and getattr(node, 'name', None):
        parts.append(node.name)
        node = node.parent
    return tuple(reversed(parts))


def _containers(anchors: list):
    """每条链接的记录容器 = 它祖先链上最后一个「只属于它」的节点。

    **按祖先计数算,不对每个祖先 `find_all` 一遍**:后者是 O(n²),一张有
    几千个链接的门户页会把采集预算吃光。这里一次扫完所有祖先链再比计数,
    是线性的 —— 而且没有递归,畸形 HTML 嵌套几千层也只是慢。
    """
    chains, seen = [], Counter()
    for anchor in anchors:
        chain, node = [], anchor
        while node is not None and getattr(node, 'name', None):
            chain.append(node)
            node = node.parent
        chains.append(chain)
        for node in chain:
            seen[id(node)] += 1
    out = []
    for chain in chains:
        box = chain[0]
        for node in chain:
            if seen[id(node)] > 1:
                break
            box = node
        out.append(box)
    return out


def parse_page_list(body: bytes, base_url: str, headers: dict | None = None) -> list[dict]:
    """一张 HTML 列表页 → 条目,字段与 `fetching.parse_feed` 一致。"""
    if why := readable.html_problem(body, headers):
        raise FeedError(why)
    if feedparser.parse(body).get('version'):
        raise FeedError('这个地址返回的是 RSS / Atom，请改用「RSS」来源添加它')
    soup = BeautifulSoup(readable.decode(body, headers), 'html.parser')
    found = _list_group(soup, base_url)
    if not found:
        if feed := discover_feed(body, base_url):
            raise FeedError('这一页上找不到带发布时间的文章列表，但它声明了一个订阅'
                            f'地址 {feed} —— 请改用「RSS」来源添加它')
        raise FeedError('这一页上找不到带发布时间的文章列表。请确认填的是列表页'
                        '（而不是某一篇文章），以及这一页不需要登录或由脚本渲染')
    entries, seen = [], set()
    for box, url in found:
        if url in seen:
            continue
        title = item_title(box)
        if not title:
            continue
        seen.add(url)
        published = item_published(box)
        entries.append({'url': url, 'title': title,
                        'body': shell_body(item_subject(box, title), published, url),
                        'author': '', 'published': published, 'content_kind': 'summary'})
    if not entries:
        raise FeedError('这一页上有文章列表，但一条标题都没解析出来 —— 站点结构'
                        '可能变了，需要适配')
    entries.sort(key=lambda e: e['published'], reverse=True)
    return entries[:MAX_ITEMS]
