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


def item_published(node, memo: dict | None = None) -> str:
    """这条记录的发布时间。`<time>` 优先,它是"这是个时间"的标准语义。

    这一步要把 `node` **整片子树**的文字拼起来,所以同一个大祖先被反复问就是
    `O(链接数 × 子树)`:一个导航区里几百条同址标题链接各自往上走、都走到同
    一个 `<nav>`/`<body>`,实测 500/1000/2000 条分别要 0.55/2.19/8.51 秒,而
    单来源采集预算只有 20 秒。`memo` 按节点**身份**记住算过的答案,让同一片
    子树在一次解析里只被遍历固定次数。

    **`memo` 的寿命必须跟那棵 soup 一样长,一天都不许多。** 键是 `id(node)`,
    而 CPython 的 id 在对象被回收之后**会被下一个对象复用** —— 做成模块级缓存
    的话,上一页某个 `<div>` 的日期就会发给这一页某条毫不相干的记录,而且只在
    内存碰巧撞上时才出现。所以它由 `parse_page_list` 当场建、随调用结束一起
    丢;并发跑的几次解析各有一份,互不相干(`soup` 在整个调用期间一直被引用,
    期间 id 不会被复用)。
    """
    key = id(node)
    if memo is not None and key in memo:
        return memo[key]
    stamp = ''
    for tag in node.find_all('time'):
        if stamp := (read_published(tag.get('datetime') or '')
                     or read_published(tag.get_text(' '))):
            break
    stamp = stamp or read_published(node.get_text(' '))
    if memo is not None:
        memo[key] = stamp
    return stamp


def _date_shaped(node) -> bool:
    """这片子树里有没有**像日期**的文字 —— 只问形状,不问合不合法。

    `item_published` 读不出日期有两种原因:**压根没有日期**,或者**有、但第一
    个日期形状是非法的**(它把整片子树的文字拼成一串、只认第一个匹配)。两者
    对"能不能跳过这片子树"的答案正相反:前者跳了也不会错过什么,后者里面的
    小容器往往正常读得出来 —— 实测父容器带一个 `2026-02-30` 编号时,跳过内部
    会把那两篇真文章一起漏掉。所以 `_dated_boxes` 用这个谓词当闸,不拿"解析
    失败"当"没有日期"。
    """
    if node.find('time') is not None:
        return True
    text = ' '.join(node.get_text(' ').split())
    return any(pattern.search(text) for pattern in (_ISO, _CN, _MDY, _DMY))


def _date_hosts(box, rank: dict) -> list:
    """`box` 里面那几个**可能**读出日期的祖先,从深到浅。

    **逐层问每个祖先是 `O(深度²)`** —— 每一层都把同一片子树重扫一遍,而
    `_date_shaped` 为真(有日期形状、但 `item_published` 读不出来)时正好要往
    里面找。实测 16000 层嵌套、里面只有一个非法编号的页面要 29 秒,而单来源
    采集预算只有 20 秒。

    换个问法就不用逐层:**只有子树里有日期形状文字的祖先才可能读出日期**,
    而日期形状通常只出现在一两个地方。所以反过来从那几处文字出发,各自往上
    走到第一个"是这条链接的祖先"的节点(`rank` 里有的就是),那几个节点就是
    全部候选 —— 别的祖先一个日期形状都不含,`item_published` 必然是空。

    **等价性**:`item_published(node)` 非空 ⇒ node 子树里有能解析的日期形状
    文字 t,而包着 t 和本链接的最内层祖先 `L ⊆ node` 也含 t;`L` 的文字是
    node 文字的连续一段,所以 node 文字里 t 之前没有同类形状时 `L` 里也没有
    —— `L` 同样读得出来,原来那套"从内往外第一个读出日期的"也会先返回 `L`。
    两边给的都是 `L`,没有哪个祖先被漏掉。

    **一个形状跨了两个文本节点时这里看不出来**(`<span>Oct 18,</span>
    `<span>2026</span>` 拼起来才是日期),所以调用方拿到空候选、而
    `_date_shaped` 又说有形状时**退回逐层问** —— 慢,但不会漏。
    """
    hosts = {}
    for shaped in _shaped_nodes(box):
        host = shaped
        while host is not None and id(host) not in rank:
            host = host.parent
        if host is not None:
            hosts[id(host)] = host
    return sorted(hosts.values(), key=lambda node: rank[id(node)])


def _shaped_nodes(box) -> list:
    """`box` 里带日期形状的那几处 —— `<time>` 标签,和含日期形状文字的元素。"""
    out = list(box.find_all('time'))
    for raw in box.descendants:
        if type(raw) is not NavigableString:
            continue
        if text := ' '.join(raw.split()):
            if any(pattern.search(text) for pattern in (_ISO, _CN, _MDY, _DMY)):
                out.append(raw.parent)
    return out


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


def _list_group(soup: BeautifulSoup, base_url: str, memo: dict | None = None):
    """页面上的文章区 → `[(容器, 绝对地址)]`,真列表排在单独的头条卡片前面。

    `memo` 是这一次解析的日期备忘(见 `item_published`):它要跨分组活着 ——
    各组的链接常常共享同一批外层祖先。缺省自己建一个,寿命仍只有这一次调用。
    """
    if memo is None:
        memo = {}
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
    # 上移的定界同样按**整页**算,理由和上面那段一样,见 `_title_bounds`。
    bounds = _title_bounds(pairs)
    buckets: dict[tuple, list] = {}
    for (anchor, url), box in zip(pairs, boxes):
        buckets.setdefault(_record_path(anchor), []).append((anchor, box, url))
    groups, reach = [], Counter()
    for key, items in buckets.items():
        fixed = [(anchor, box, url) for (anchor, _, url), box
                 in zip(items, _dated_boxes(items, bounds, memo))]
        dated = sum(1 for _, box, _ in fixed if item_published(box, memo))
        # **整页这道闸按结构路径数,不按分组数。** 签名里那个"是不是标题链接"
        # 是**卡片里的分工**,它该管的是上移资格和选主;拿它来数"这一页算不算
        # 列表"就会把同一个列表拆散 —— 一行标题写 `<h3>`、另一行写 `<span>`
        # (两条都原生带日期、都是实打实的文章)时,两条各成一个单条分组、
        # 一起够不着 `MIN_DATED`,整页当场拒收。实测基线收 2 篇、`errors: []`,
        # 那一版收 0 篇并给这条订阅记一次失败、进退避。
        reach[key[0]] += dated
        # **零日期的分组也要留着当候选,不在这里就丢掉。** 丢了的话下面那条
        # "这条结构路径已经是个列表"的入选条件压根补不回来 —— 实测一个列表里
        # 两篇带日期(写 `<h3>`)、第三篇没标日期(写 `<span>`)时,第三篇连候选
        # 都进不来,基线收 3 条、丢掉候选的那版收 2 条,而且 `errors` 为空、
        # 来源记成功。`published` 为空是**合法条目**(历史下限对未知日期照收),
        # 不该因为"这一组里没人带日期"就把它判出去。
        groups.append((dated, fixed, key[0]))
    if not groups or max(reach.values()) < MIN_DATED:
        # 整页一条结构路径都不够 `MIN_DATED` = 这压根不是列表页。这道闸只问
        # 一次,不逐组问 —— 逐组问就把单条头条卡片永久关在门外。
        return []
    # 够 `MIN_DATED` 条带日期的是列表区;**每一条都带日期**的也算(单独展示
    # 的头条常常只有一条,够不着 `MIN_DATED`,而"这一组里每条都有发布时间"
    # 本身就是"这是文章区"的证据)。代价是导航里一个孤立的、容器里又恰好有
    # 日期的链接会被收进来 —— 那顶多多一条条目,而漏掉头条是静默丢数据,
    # 两个方向差着一个量级。
    #
    # **第三种入选:整页那道闸已经认定这条结构路径是个列表**(`reach` 够数)。
    # 这一条是必须的 —— 上面两道都是**按分组**问的,于是同一个"分工"会在这里
    # 第二次把列表切断:一行标题写 `<h3>`、另两行写 `<span>`(其中一行没日期)
    # 时,`<h3>` 那组 1/1 入选,`<span>` 那组 1/2 两道都不满足,于是那篇**原生
    # 带日期**的真文章跟着没日期的那条一起落选。实测基线收 3 篇、只改整页计数
    # 的那版收 1 篇,而且 `errors` 为空、同步显示成功 —— 静默漏收,比报错坏。
    # 展示分工该管的是上移资格和选主,不该两次否决同一个列表。
    picked = [(d, items) for d, items, path in groups
              if d >= MIN_DATED or d == len(items) or reach[path] >= MIN_DATED]
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

    1. **一个记录容器只能属于一条记录**,同一个容器被多条链接吸到时取文字最
       长的那条。这一道现在是**冗余带,不是承重判据**:角标抢容器那个形态已经
       由 `_dated_boxes` 的第一道闸(只有标题链接够资格上移)挡在前面了,于是
       真正共享容器的只剩"同一行上的几条标题链接",而它们**指向同一篇文章**
       —— 实测把这一道整条短路成"取先遇到的那条",两份样本的产出一条不变。
       留着是因为异址共享容器时它保证一个容器只产出一条记录;**别指望它挡
       角标**。
    2. **容器互相嵌套时,外层的是记录、内层的是它的一个字段。** featured
       那几张卡片是这个形态:分类链接自己那一小块里就带着日期,够不着第一
       道,但它落在卡片容器里面。**这一道才是挡住 featured 角标的那个**,
       撤掉它实测红 8 条、8 个分类索引页进库。

    **这两道都不许依赖组的先后。** 实测 featured 卡片上,分类链接和文章链接
    的组在两个排序键上完全并列,谁先谁后只取决于它俩在文档里谁排前面(分类
    链接在卡片头部,所以它赢),于是那些分类页照旧进库。

    代价是真·嵌套列表(卡片里再嵌一个"相关文章")会被当成字段漏掉 —— 换来
    的是不必去猜哪种地址长得像分类页。
    """
    items = [triple for _, group in picked for triple in group]
    owners: dict[int, str] = {}
    ranks: dict[int, tuple] = {}
    for anchor, box, url in items:
        rank = len(' '.join(anchor.get_text(' ').split()))
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


def titled(anchor) -> bool:
    """这条链接是不是**标题链接** —— 标题标签在 `<a>` 里面或外面都算。

    **两个方向都得认。** `<a><h3>标题</h3></a>` 和 `<h3><a>标题</a></h3>` 在站点
    上一样常见,而 `item_title` 读的是整个记录容器、两种都看得见 —— 只认
    `<a>` 内部的话,标题照样取对,但"谁是这条记录"会判给同一张卡片里文字更
    长的那个角标,于是输出「真标题 + 错地址」(审查 F2 实测:`First news` 配
    上了 `/news/announcements`)。那比报错更坏,报错看得见。

    **这是"链接在卡片里的分工"**:标题链接 / 缩略图链接 / 角标链接。分工是
    记录身份的一部分,所以它既进分组签名(`_record_path`),也决定**谁够资格
    上移去找日期**(`_dated_boxes`)—— 两处同一个口径,而 `item_title` 读的是
    整个容器、两种写法都看得见,三者因此对得上。
    """
    return (anchor.find(HEADINGS) is not None
            or anchor.find_parent(HEADINGS) is not None)


def _record_path(anchor) -> tuple:
    """这条链接的结构签名 = 从根到 `<a>` 的路径 + 它是不是标题链接。

    **必须算进"是不是标题链接"。** 改版后的 Anthropic 把一行写成三个平级的
    `<a>`(标题 / 摘要 / 分类),到 `<a>` 为止的路径逐字相同 —— 只算路径的话
    这三者落进同一组,于是组内混着两个不同地址,后面的记录边界和字段剔除就
    都失效了。

    **只能算这一个特征。** 第一版还算了"有没有 `<img>`",结果真列表里"这一
    行配了缩略图、那一行没配"被拆成两个单条分组,两条都够不着 `MIN_DATED`,
    整页当场报"找不到文章列表"(审查 F3 实测:基线返回 2 篇,那一版抛异常)。
    缩略图是**显示变体**不是记录身份;而它本来也不必算 —— 图片链接没有标题
    标签,靠 `titled` 就已经和标题链接分开了。
    """
    return _tag_path(anchor), titled(anchor)


def _title_bounds(pairs: list) -> dict:
    """每个节点下,**整页**的标题链接指向哪个地址;不止一个地址记 `''`。

    这是上移的定界。**它必须在整页尺度上算,不在组内算** —— 跟记录容器那条
    是同一句话(见 `_list_group`)。组内算的话,别的分组的记录在 DOM 上就在
    那儿、却看不见:实测一个导航区里几百条同址标题链接,组内看上去"这一层
    只属于我",于是一路上移穿过 `<nav>` 走到 `<body>`,把 `<main>` 里真文章
    的日期借了过来;再靠"文字最长者当宿主"把那两篇真文章剔成自己的字段 ——
    基线收 2 篇真文章,那一版只收 1 条导航链接,而且 `errors` 为空、同步显示
    成功。那是静默漏收。

    **只数标题链接,不数全部链接。** 这和上移的第一道闸是同一句话的两个方向:
    日期是**记录**的属性,所以只有标题链接够资格上移;反过来,**别人的标题
    链接就标出了别人的记录边界**,上移不许跨过去。数全部链接会把同一张卡片
    里的角标、摘要、`Read more` 当成"别人",上移在读到日期之前就停手 ——
    那正是用户报的那个故障的形态。
    """
    bounds: dict[int, str] = {}
    for anchor, url in pairs:
        if not titled(anchor):
            continue
        node = anchor
        while node is not None and getattr(node, 'name', None):
            if id(node) not in bounds:
                bounds[id(node)] = url
            elif bounds[id(node)] != url:
                bounds[id(node)] = ''  # 这一层下不止一条记录
            node = node.parent
    return bounds


def _dated_boxes(items: list, bounds: dict, memo: dict | None = None):
    """一组同构链接各自的记录容器;容器里读不到日期就上移到读得到的那一层。

    **日期跟链接是兄弟而不是祖孙,这是改版后的常见形态。** Anthropic 现在
    一行是 `<div><a>标题</a></div><div><a>摘要</a></div><div><time>日期</time></div>`
    —— 同一篇文章被链了四次(收起态/展开态各两条),`_containers` 那条"只属于
    这条链接的最后一个祖先"于是停在包着 `<a>` 的那个 `<div>` 上,`<time>` 在
    它外面。整组因此 0 条带日期、被判为不是文章区,而页面上 20 篇文章的标题
    和日期都明明在那儿。

    上移有三道闸,缺一道就会把日期发错人:

    **一、只有标题链接够资格上移**(`titled`)。日期是**记录**的属性,字段链接
    不配拿。少了这道闸,卡片里那条分类角标会一路长到整张卡片、把真文章的日期
    据为己有,再靠宿主规则把真文章本人剔成自己的字段 —— 审查 F1 实测:基线
    返回 2 篇真文章,那一版只剩 1 条 `/news/announcements`,顶着真文章的标题
    和日期。角标**原生**就带日期时(改版后那一页上 24 条角标都如此)它压根
    不需要上移,所以这道闸不会挡掉任何本来读得到的日期。

    **二、走到第一个"下面不止我这一条**记录**"的祖先就停手**,所以最多长到
    自己那一行,不会把下一条记录的日期抢过来。边界按**整页的标题链接地址**算
    (`_title_bounds`):按链接条数记会在读到日期之前就停手(同一篇文章在一张
    卡片里被同签名链两次,改版后那一页就是这个形态),只在组内算则看不见别的
    分组的记录、会一路上移到半个页面那么大 —— 两种都实测过,各自的代价写在
    `_title_bounds` 的说明里。

    **没有第三道:"不许跨过另一条自带日期的异址链接"这条加不上去。** 它想
    挡的是"无日期的标题链接吞掉并排那条带日期的记录",但那个形态跟改版后那
    一页上的**精选卡片**结构同构 —— 两边都是「titled 链接要上移,路上有一条
    非 titled 的链接、它的原生容器自带日期」,区别只在语义(一边是并列的另一
    条记录,一边是同一张卡片的分类角标),DOM 上找不出判据。实测过五种收紧
    方式(异址自带日期即停 / 自带日期者豁免被剔 / 兄弟容器即停 / 宿主只认
    原生容器 / 上移得来的日期不计入选组),代价一律是**用户报的那个故障当场
    复发**:最轻的一种(只挡兄弟容器)也丢了 4 篇真文章,并把 `/news/safeguards`
    等 8 个分类索引页重新收进库。所以这里按元规则「漏报是静默丢数据,误报顶多
    重抓一次」留宽:那种页面上会多收两条借了日期的导航链接,面板上看得见、
    删得掉。

    只有组内 ≥ `MIN_DATED` 条才允许上移。单独展示的头条那一组常常只有一条
    链接,同组定界对它形同虚设(每个祖先都只属于它,一路长到根),而它本来
    就靠整页尺度的 `_containers` 正确停在自己那张卡片上 —— 对它不动,正是
    不把那个坑重新挖开。
    """
    if memo is None:
        memo = {}
    if len(items) < MIN_DATED:
        return [box for _, box, _ in items]
    chains = []
    for anchor, _, _ in items:
        chain, node = [], anchor
        while node is not None and getattr(node, 'name', None):
            chain.append(node)
            node = node.parent
        chains.append(chain)
    out = []
    for (anchor, box, url), chain in zip(items, chains):
        if item_published(box, memo) or not titled(anchor):
            out.append(box)
            continue
        # **从原容器之外接着往上走,不从 `<a>` 重新开始。** 原容器刚刚被判过
        # 没有日期,而 `<a>` 到原容器之间那些祖先的子树都在原容器**里面** ——
        # 从 `<a>` 起逐层重扫等于把同一片已排除的子树按深度扫了一遍又一遍,
        # `O(深度²)`:3200 层嵌套的无日期页面实测 1.83 秒,改成这样 0.125 秒。
        # 这一段祖先只属于本链接,所以跳过它们不会错过"这一层是不是别人的"。
        #
        # **但"压根没有日期形状的文字"才敢整段跳**(`_date_shaped`)。"大的解析
        # 不出来、里面的小的也不会有"这个推理**不成立**:`item_published` 把整
        # 片子树的文字拼成一串、只认其中第一个日期形状,所以父容器里一个非法
        # 编号(`2026-02-30` 这种)就能让它返回空,而里面某个小容器的正常日期
        # 本来读得出来。按"解析失败"去跳会把那两篇真文章一起漏掉。
        #
        # **里面有形状时也不逐层问,只问那几处形状所在的祖先**(`_date_hosts`)。
        # 逐层问同样是 `O(深度²)`:16000 层里一个非法编号实测 29 秒,而单来源
        # 采集预算只有 20 秒。拿到空候选说明那个形状跨了两个文本节点,这时才
        # 退回逐层。
        #
        # **走 parent 链,不在 chain 里找下标。** `box in chain` / `chain.index`
        # 用的是 bs4 的 `Tag.__eq__` —— 那是**内容相等**,会递归比较名字、属性
        # 和整棵子树;深层同名 `<div>` 容器上实测直接 `RecursionError`,整页报错、
        # 一篇都收不到。这个模块里认节点一律按**身份**(别处都是 `id(node)`)。
        outward = []
        node = box.parent
        while node is not None and getattr(node, 'name', None):
            outward.append(node)
            node = node.parent
        if not _date_shaped(box):
            above = outward
        else:
            rank = {id(node): i for i, node in enumerate(chain)}
            inside = _date_hosts(box, rank)
            above = (inside or chain[:rank.get(id(box), 0)]) + outward
        for node in above:
            # 先问"这一层是不是别人的"再问日期:共享那一层上的日期属于
            # 下一条记录,取过来就是整组抄同一个日期。
            if bounds[id(node)] != url:
                break
            if item_published(node, memo):
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
    # 这一次解析的日期备忘,随 `soup` 一起活、一起丢(寿命那条见 `item_published`)。
    memo: dict[int, str] = {}
    found = _list_group(soup, base_url, memo)
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
        published = item_published(box, memo)
        entries.append({'url': url, 'title': title,
                        'body': shell_body(item_subject(box, title), published, url),
                        'author': '', 'published': published, 'content_kind': 'summary'})
    if not entries:
        raise FeedError('这一页上有文章列表，但一条标题都没解析出来 —— 站点结构'
                        '可能变了，需要适配')
    entries.sort(key=lambda e: e['published'], reverse=True)
    return entries[:MAX_ITEMS]
