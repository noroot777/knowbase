# -*- coding: utf-8 -*-
"""没有 RSS 的站点:把它的文章列表页拆成条目。

**硬约束在 `docs/decisions/page-list.md` 第一节 —— 改这块之前先读那一份。**

Anthropic 的 news 是典型形态 —— 整站一处 feed 都没有(五个常见 feed 路径
全是 404、页面上没有 `<link rel=alternate>`、公共 RSS 桥接实例要么 403
要么连不上),而它恰恰是最该收的来源之一。

**判据只有一条:带发布时间的那一组链接就是文章列表。** 不认 CSS 类名
(Next.js 的类名带构建哈希,每次发版都变)、不认 tag 名(`li` / `article` /
`div` 都有站点在用)、**也不认"哪一组链接最多"** —— 实测 Anthropic 那一页
上最大的链接组是 90 条页脚菜单,真正的新闻列表只有 10 条、排在第二。日期
是文章列表的本质特征(导航、页脚、推荐位都没有),而且它本来就是我们必须
拿到的字段(`sync._within_history` 要用),所以这条判据不额外欠任何东西。

分组按**结构路径**(从根到 `<a>` 的 tag 序列,**再加上 `<a>` 里装的是标题
还是图**)而不是按父节点:同一个列表里的链接路径逐字相同,而 featured 大
卡、导航、页脚各在自己的路径上,自动分开 —— 实测 Anthropic 那张页面上
featured 那条正因此落在另一组里,不会把它那段摘要当成标题。

**日期不一定在链接的祖先上。** 改版后的列表把一行写成「标题链接 / 摘要
链接 / 分类角标 / 日期」四个平级的兄弟,同一篇文章被链四次(收起态、展开
态各两条),于是"只属于这条链接的最后一个祖先"停在包着 `<a>` 的那个
`<div>` 上、`<time>` 在它外面,整组 0 条带日期 —— 20 篇文章的标题和日期都
明明在那儿,却被判成"这不是文章区"。所以容器读不到日期时**允许上移到读得
到的那一层**,边界是同组的下一条链接(`_dated_boxes`)。

**卡片里的字段链接不是记录。** 分类角标、作者页、`Read more` 各自成组,又
跟日期包在同一层,于是分类索引页顶着真文章的日期进了库 —— 而它们压根没有
正文。两道判定按**从属关系**剔(`_drop_subordinate`),都不看谁在文档里排
前面。

**但分开之后要全都收下,不是只取胜出那一组。** 只留带日期条目最多的那一
组,等于把"单独展示的头条"整组丢掉,而漏掉的恰恰是最新那几条 —— 实测
Anthropic 当前页面上 `Introducing Claude Haiku 5.5` / `Sonnet 5.5` /
`Opus 5.5` 三条发布公告就在 featured 那一组里(它们的地址不在 `/news/`
下,胜出的 10 条一条都不含),而同步照常报成功、没有任何提示。所以判据拆
成两层:**整页至少有一组够 `MIN_DATED` 条带日期**(这一页是不是列表页),
然后**收下每一个"够 `MIN_DATED` 条带日期、或者每条都带日期"的组**(这一
组算不算文章区)。后半句那个"每条都带"是给单条头条卡片留的门。

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
    """页面上的文章区 → `[(容器, 绝对地址)]`,真列表排在单独的头条卡片前面。"""
    pairs = []
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
        pairs.append((anchor, url))
    if not pairs:
        return []
    # 记录容器在**整页**的尺度上算,不在组内算。组内计数时,只有一条链接的
    # 那一组里**每一个祖先都"只属于它"**,容器于是一路长到根节点 —— 单独展
    # 示的头条既认不出自己那张卡片,又会把页面别处的日期和最长的那段文字当
    # 成自己的(审查那份最小样本实测:`<section>` 里那条头条的容器是
    # `[document]`)。放到整页上算,它正好停在自己那张卡片上;反过来,真正
    # 的孤立噪音(页头 logo、`Newsroom` 这种栏目名)容器跟着收缩之后读不到
    # 日期,自己就落选了 —— 实测 Anthropic 那一页上 3 条这样的噪音因此消失。
    boxes = _containers([a for a, _ in pairs])
    buckets: dict[tuple, list] = {}
    for (anchor, url), box in zip(pairs, boxes):
        buckets.setdefault(_record_path(anchor), []).append((anchor, box, url))
    groups = []
    for items in buckets.values():
        fixed = [(anchor, box, url) for (anchor, _, url), box
                 in zip(items, _dated_boxes(items))]
        dated = sum(1 for _, box, _ in fixed if item_published(box))
        if dated:
            groups.append((dated, fixed))
    if not groups or max(d for d, _ in groups) < MIN_DATED:
        # 整页一组都不够 `MIN_DATED` = 这压根不是列表页。这道闸只问一次,
        # 不逐组问 —— 逐组问就把单条头条卡片永久关在门外。
        return []
    # 够 `MIN_DATED` 条带日期的是列表区;**每一条都带日期**的也算(单独展示
    # 的头条常常只有一条,够不着 `MIN_DATED`,而"这一组里每条都有发布时间"
    # 本身就是"这是文章区"的证据)。代价是导航里一个孤立的、容器里又恰好有
    # 日期的链接会被收进来 —— 那顶多多一条条目,而漏掉头条是静默丢数据,
    # 两个方向差着一个量级。
    picked = [(d, items) for d, items in groups if d >= MIN_DATED or d == len(items)]
    # 带日期的条目越多越像"主列表",排在前面:`parse_page_list` 按地址先到
    # 先得去重,而 featured 卡片的容器里连摘要一起,标题判定不如列表里那份准。
    picked.sort(key=lambda g: (-g[0], len(g[1])))
    return _drop_subordinate(picked)


def _drop_subordinate(picked: list):
    """收下所有入选记录,把**卡片里的字段链接**剔出去。

    一张卡片里除了文章本身,常常还挂着指向别处的链接:分类标签、作者页、
    `Read more`。它们各自成组(`<a>` 内部结构不同),又跟日期包在同一层 ——
    改版后的 Anthropic 就是这个形态,每行写着

        <div><a><h3>标题</h3></a></div><div><a>Announcements</a></div>
        <div><time>Oct 18, 2026</time></div>

    于是 9 个分类索引页顶着真文章的日期被当成了文章列表(越过历史下限的
    那 5 条真进了库),而它们压根没有正文。

    **判据是从属关系,不是"像不像文章"**,两道:

    1. **一个记录容器只能属于一条记录。** 上面那一行里标题链接和分类链接
       吸到的是同一个容器,谁是记录用的就是 `item_title` 那条判据(`<a>` 里
       装着标题标签的优先,都没有就取文字最长的那条)—— 口径只有一份。
    2. **容器互相嵌套时,外层的是记录、内层的是它的一个字段。** featured
       那几张卡片是这个形态:分类链接自己那一小块里就带着日期,够不着第一
       道,但它落在卡片容器里面。

    **这两道都不许依赖组的先后。** 实测 featured 卡片上,分类链接和文章链接
    的组在两个排序键上完全并列,谁先谁后只取决于它俩在文档里谁排前面(分类
    链接在卡片头部,所以它赢),于是 5 个分类页照旧进库。

    代价是真·嵌套列表(卡片里再嵌一个"相关文章")会被当成字段漏掉 —— 换来
    的是不必去猜哪种地址长得像分类页。
    """
    items = [triple for _, group in picked for triple in group]
    owners: dict[int, str] = {}
    ranks: dict[int, tuple] = {}
    for anchor, box, url in items:
        rank = (1 if anchor.find(HEADINGS) else 0,
                len(' '.join(anchor.get_text(' ').split())))
        if id(box) not in ranks or rank > ranks[id(box)]:
            ranks[id(box)], owners[id(box)] = rank, url
    out = []
    for _, box, url in items:
        if owners[id(box)] != url:
            continue
        # 从 `box.parent` 起走,不从 box 自己起 —— 否则每条记录都是自己的宿主。
        host = _host_url(box.parent, owners)
        if host is None or host == url:
            out.append((box, url))
    return out


def _host_url(node, owners: dict):
    """这条链接落在谁的记录容器里面;没有宿主返回 None(它自己就是一条记录)。"""
    while node is not None and getattr(node, 'name', None):
        if (url := owners.get(id(node))) is not None:
            return url
        node = node.parent
    return None


def _tag_path(node) -> tuple:
    """从根到这个节点的 tag 序列。同一个列表里的链接,这条路径逐字相同。"""
    parts = []
    while node is not None and getattr(node, 'name', None):
        parts.append(node.name)
        node = node.parent
    return tuple(reversed(parts))


def _record_path(anchor) -> tuple:
    """这条链接的结构签名 = 从根到 `<a>` 的路径 + `<a>` 里装的是标题还是图。

    **必须算进 `<a>` 内部。** 改版后的 Anthropic 把一行写成三个平级的 `<a>`
    (标题 / 摘要 / 分类),到 `<a>` 为止的路径逐字相同 —— 只算到 `<a>` 的话
    这三者落进同一组,于是组内混着两个不同地址,下面那两道(同组定界、字段
    链接剔除)就都失效了。

    **但只能算这两个特征,不能把内部 tag 整串拿来比。** 整串比的话,真列表
    里"这一行有角标、那一行没有"就被拆成两组,各自够不着 `MIN_DATED`,整页
    当场报"找不到文章列表"(回归 `test_heading_tag_wins` 与
    `test_a_blurb_is_not_a_title` 实测红)。标题标签和图片是**卡片里那几个
    `<a>` 的分工**(标题链接 / 缩略图链接 / 角标链接),同一个列表的同一个
    分工在每一行上都一样,而行与行之间的差异(多一个角标、少一段摘要)不落
    在这两个特征上。
    """
    return (_tag_path(anchor), anchor.find(HEADINGS) is not None,
            anchor.find('img') is not None)


def _dated_boxes(items: list):
    """一组同构链接各自的记录容器;容器里读不到日期就上移到读得到的那一层。

    **日期跟链接是兄弟而不是祖孙,这是改版后的常见形态。** Anthropic 现在
    一行是 `<div><a>标题</a></div><div><a>摘要</a></div><div><time>日期</time></div>`
    —— 同一篇文章被链了四次(收起态/展开态各两条),`_containers` 那条"只属于
    这条链接的最后一个祖先"于是停在包着 `<a>` 的那个 `<div>` 上,`<time>` 在
    它外面。整组因此 0 条带日期、被判为不是文章区,而页面上 20 篇文章的标题
    和日期都明明在那儿。

    上移的边界是**同组的下一条链接**:走到第一个"同组里不止我一条"的祖先就
    停手,所以最多长到自己那一行,不会把下一条记录的日期抢过来。

    只有组内 ≥ `MIN_DATED` 条才允许上移。单独展示的头条那一组常常只有一条
    链接,同组定界对它形同虚设(每个祖先都只属于它,一路长到根),而它本来
    就靠整页尺度的 `_containers` 正确停在自己那张卡片上 —— 对它不动,正是
    不把那个坑重新挖开。
    """
    if len(items) < MIN_DATED:
        return [box for _, box, _ in items]
    chains, seen = [], Counter()
    for anchor, _, _ in items:
        chain, node = [], anchor
        while node is not None and getattr(node, 'name', None):
            chain.append(node)
            node = node.parent
        chains.append(chain)
        for node in chain:
            seen[id(node)] += 1
    out = []
    for (_, box, _), chain in zip(items, chains):
        if item_published(box):
            out.append(box)
            continue
        for node in chain:
            # 先问"是不是同组共享的祖先"再问日期:共享那一层上的日期属于
            # 下一条记录,取过来就是整组抄同一个日期。
            if seen[id(node)] > 1:
                break
            if item_published(node):
                box = node
                break
        out.append(box)
    return out


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
