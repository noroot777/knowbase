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

from bisect import bisect_left
from collections import Counter
from urllib.parse import urljoin, urlsplit

import feedparser
from bs4 import BeautifulSoup, NavigableString

from kb.feeds import readable
from kb.feeds.fetching import FeedError, canonical_url, discover_feed
# 日期怎么读、以及整页一次把它们索引起来,都在 `page_date` —— 这一份已经顶到
# 行数上限,而"日期怎么读"不认识链接、分组和记录容器,是条独立职责。
# `read_published` / `item_published` 在这里转出一次,老调用方照旧能用。
from kb.feeds.page_date import DateIndex, item_published, read_published  # noqa: F401

# 一组里至少几条带日期才算"文章列表"。1 条太容易是巧合,而**宁可报错也不要
# 把导航当列表**:误报用户看得见、改一下就好,漏报是往知识库里灌垃圾。
MIN_DATED = 2
# 比这更长的文本块是摘要而不是标题(featured 卡片那种);比下限更短的是角标。
MAX_TITLE, MIN_TITLE = 300, 4
MAX_ITEMS = 500                     # 同 `fetching.parse_feed`
HEADINGS = ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']

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


def _list_group(soup: BeautifulSoup, base_url: str, index: DateIndex | None = None):
    """页面上的文章区 → `[(容器, 绝对地址)]`,真列表排在单独的头条卡片前面。

    `index` 是整页的日期索引(见 `page_date.DateIndex`)。缺省自己建一个 ——
    它必须**整页**建一次、跨分组用,各组的链接常常共享同一批外层祖先。
    """
    if index is None:
        index = DateIndex(soup)
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
    # 整页"够资格当一条记录"的那些链接在哪 —— 上移要靠它判断"这一层是不是
    # 已经装着不止我一条记录"(见 `_dated_boxes`)。
    lists = _dated_spots(pairs, boxes, index)
    buckets: dict[tuple, list] = {}
    for (anchor, url), box in zip(pairs, boxes):
        buckets.setdefault(_record_path(anchor), []).append((anchor, box, url))
    groups, reach = [], Counter()
    for key, items in buckets.items():
        fixed = [(anchor, box, url) for (anchor, _, url), box
                 in zip(items, _dated_boxes(items, index, lists))]
        # **数的是"几条**记录**带日期",按去重后的地址算,不按链接条数。**
        # 一个导航区里几百条**同址**的标题链接,上移时在组内看上去"这一层只
        # 属于我"(都是同一个地址),于是一路走到 `<body>`、把别处真文章的
        # 日期借过来,这一组于是"几百条都带日期"、排在真列表前面,再靠"文字
        # 最长者当宿主"把真文章剔成自己的字段 —— 实测基线收 2 篇真文章、按
        # 链接条数记的那版只收 1 条导航链接,`errors` 为空、同步显示成功。
        # 按地址数记时这一组只有 1 条记录,够不着 `MIN_DATED`、也不满足"每条
        # 都带日期",自己就落选了。**同一篇文章在一张卡片里被链两次**(改版
        # 后那一页的常见形态)时去重也正是想要的:那本来就是一条记录。
        dated = len({url for _, box, url in fixed if item_published(box, index)})
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
        groups.append((dated, fixed, key[0], len({url for _, _, url in fixed})))
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
    # 三道入选条件里的"每条都带日期"同样按**记录**算(`d == urls`),理由同
    # 上面那条:按链接条数算的话,几百条同址导航链接会因为"每条都借到了日期"
    # 整组入选。
    picked = [(d, items) for d, items, path, urls in groups
              if d >= MIN_DATED or d == urls or reach[path] >= MIN_DATED]
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


def _dated_boxes(items: list, index: DateIndex, lists: tuple):
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

    **二、走到第一个"同组里不止我一条**记录**"的祖先就停手**,所以最多长到
    自己那一行,不会把下一条记录的日期抢过来。**按地址定界,不按链接条数** ——
    同一篇文章在一张卡片里被同签名链两次(改版后那一页就是这个形态)时,按
    条数记会在读到日期之前就停手,整组 0 条带日期、整页报"找不到文章列表",
    这条源从此一条都收不到。那是漏报方向,比多收一条坏得多。

    **定界只问同组,不许扩到整页。** 扩到整页(数全页的标题链接地址)看着
    更严、实际上把**同一张卡片里的字段**也算成了"别人":一行里分类角标写成
    `<h4><a>Announcements</a></h4>` 时它也是"标题链接",于是真文章在读到本
    卡片的日期之前就被挡住,日期归了那条分类链接 —— 实测基线和组内定界都收
    2 篇真文章,整页定界那版只收 1 条 `/news/announcements`,`errors` 为空、
    同步显示成功。`titled`(链接内外有 heading)标的是"够不够资格上移",不
    等于"这是一条独立记录",这两件事不许合并。**同址导航链接一路上移到半个
    页面那么大**那个形态由选组那道闸按**记录数**挡住(见 `_list_group`),
    不在这里按"别人的标题链接"去猜。

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
    if len(items) < MIN_DATED:
        return [box for _, box, _ in items]
    chains = []
    for anchor, _, _ in items:
        chain, node = [], anchor
        while node is not None and getattr(node, 'name', None):
            chain.append(node)
            node = node.parent
        chains.append(chain)
    owner = _group_owners(items, index)
    out = []
    for (anchor, box, url), chain in zip(items, chains):
        if item_published(box, index) or not titled(anchor):
            out.append(box)
            continue
        # **从内往外逐层问,原容器里面那一段也要问。** "大的解析不出来、里面
        # 的小的也不会有"这个推理**不成立**:日期提取器把整片子树的文字拼成一
        # 串、只认其中第一个日期形状,所以父容器里一个非法编号(`2026-02-30`
        # 这种)就能让它返回空,而里面某个小容器的正常日期本来读得出来 —— 跳过
        # 原容器内部会把那两篇真文章一起漏掉。
        #
        # **逐层问不再是二次的**:日期查询走整页那一份索引,一次 `O(log n)`
        # (见 `page_date.DateIndex`)。上一版为了躲开二次在这里加了两道剪枝
        # (先问"有没有日期形状"、再"只问形状所在的那几个祖先"),两道都是按
        # **单个文本节点**看形状,而提取按**拼接后的子树文字** —— `<span>Oct
        # 20,</span><span>2026</span>` 拼起来才是日期,于是那一层压根不进候选、
        # 读得出日期的容器一个都没被问到,整页报"找不到带发布时间的文章列表"。
        # 剪枝连同那道语义裂缝一起撤掉了,**别再按形状猜该问谁**。
        #
        # **走 parent 链,不在 chain 里找下标。** `box in chain` / `chain.index`
        # 用的是 bs4 的 `Tag.__eq__` —— 那是**内容相等**,会递归比较名字、属性
        # 和整棵子树;深层同名 `<div>` 容器上实测直接 `RecursionError`,整页报错、
        # 一篇都收不到。这个模块里认节点一律按**身份**(别处都是 `id(node)`)。
        rank = {id(node): i for i, node in enumerate(chain)}
        above = chain[:rank.get(id(box), 0)]
        node = box.parent
        while node is not None and getattr(node, 'name', None):
            above.append(node)
            node = node.parent
        for node in above:
            # 先问"这一层是不是已经装着别人的记录"再问日期:共享那一层上的
            # 日期属于下一条记录,取过来就是整组抄同一个日期。
            if owner.get(id(node), url) != url:
                break
            if _holds_a_list(node, url, index, lists):
                break
            if item_published(node, index):
                box = node
                break
        out.append(box)
    return out


def _group_owners(items: list, index: DateIndex) -> dict:
    """组内定界:`{节点: 这一层下面只有谁}`,有第二个地址就记成空串。

    上移走到第一个"同组里不止我一条记录"的祖先就停手,所以最多长到自己那
    一行、不会把下一条记录的日期抢过来 —— 少了这道闸,一个两行的列表里没
    标日期的那行会借到另一行的日期,再靠"文字最长者当宿主"把那一行真记录
    剔成自己的字段(实测 2 篇真文章变 1 篇,`errors` 为空、同步显示成功)。

    **一个地址只在"它的全部副本都在这一层里面"时才算这一层的一条记录**,
    所以从那几份副本的**最近共同祖先**开始往上标,不从每个链接自己往上标。
    两个方向各踩过一次:

    - **按链接条数记**(同一个地址的两份副本算两条记录)时,同一篇文章在
      一张卡片里被同签名链两次 —— 改版后那一页的常见形态 —— 会让上移在读到
      日期之前就停手,整组 0 条带日期、整页报"找不到文章列表"。所以按地址。
    - **只按地址、不问副本在哪**时,一行里的分类角标和文章标题同签名
      (`<a><h4>分类</h4></a>` 和 `<a><h3>标题</h3></a>` 到 `<a>` 的路径逐字
      相同、`titled` 都为真)的页面上,角标成了"同组另一条记录",真文章在
      读到本卡片的日期之前就被它挡住,整页拒收 —— 实测前一版收 2 篇真文章
      + 1 条分类页,这一版 0 篇、给这条订阅记一次失败。而**同一个分类角标
      会出现在好几张卡片上**:它的副本跨出了任何一张卡片,所以它不是哪一张
      卡片里的记录,是整页共享的一个字段。只出现在一张卡片里的角标仍然挡着
      (DOM 上它和"并排的另一条记录"不可区分,按元规则留在漏报更轻的那侧)。
    """
    spread: dict[str, list] = {}
    for anchor, _, url in items:
        spread.setdefault(url, []).append(anchor)
    owner: dict[int, str] = {}
    for url, anchors in spread.items():
        # 从**最靠前**那份副本往上走,走到这一段也盖住最靠后那份为止 ——
        # 换成别的起点会走到根之上去(`parent` 成 None),所以别省这次排序。
        spread[url] = anchors = sorted(anchors, key=lambda a: index.subtree(a)[0])
        last = index.subtree(anchors[-1])[0]
        node = anchors[0]
        while index.subtree(node)[1] <= last:  # 还没盖住最后那份副本
            node = node.parent
        while node is not None and getattr(node, 'name', None):
            if owner.setdefault(id(node), url) != url:
                owner[id(node)] = ''  # 这一层下不止一条记录
            node = node.parent
    return owner


def _dated_spots(pairs: list, boxes: list, index: DateIndex) -> tuple:
    """整页**原生容器就读得出日期**的链接在哪:`(每层几条, 按地址分开)`。

    **一个地址算一条记录**,它那几份副本只算这一次 —— `subtree_tally` 按标签
    去重,所以每份副本各挂一个标记就够,不必先求它们的共同祖先(求了实测红
    0 条:两种写法在任何用例上产出一致,**没人钉得住的改动不如不做**)。
    """
    by_url: dict[str, list] = {}
    for (anchor, url), box in zip(pairs, boxes):
        if item_published(box, index):
            by_url.setdefault(url, []).append(index.subtree(anchor)[0])
    for spots in by_url.values():
        spots.sort()
    marks = [(at, url) for url, spots in by_url.items() for at in spots]
    return index.subtree_tally(marks), by_url


def _holds_a_list(node, url: str, index: DateIndex, lists: tuple) -> bool:
    """这个容器里装着一个**列表**,所以它不是哪一条记录的容器。

    判据是 `MIN_DATED`:子树里有**不止一条**异址、而且**原生**就读得出日期
    的记录 —— 那是一组记录,不是一张卡片的几个字段。**按地址数,不按链接
    条数**:一张卡片里同一个分类角标被渲染两份(收起态/展开态)时,按条数
    记就把这张卡片判成了"装着一个列表",上移在读到本卡片的日期之前停手,
    整组 0 条带日期、整页报"找不到文章列表" —— 实测前两版都收 2 篇真文章,
    按条数记的那版 0 篇、给这条订阅记一次失败、进退避。

    **这一条和"只挡一条带日期的异址链接"差的就是那个 1。** 后者试过五遍
    (异址自带日期即停 / 自带日期者豁免被剔 / 兄弟容器即停 / 宿主只认原生
    容器 / 上移得来的日期不计入选组),再加上"压根没有独占祖先的链接不许
    上移",六种一律让用户报的那个故障当场复发:改版后那一页上的**精选卡片**
    正是"卡片里有一条自带日期的分类角标",一刀切会把 4 篇真文章的日期挡掉、
    把 `/news/safeguards` 等 8 个分类索引页重新收进库。一张卡片里只会有一
    两个这样的字段,一个列表里却有一整排记录,`MIN_DATED` 正好落在中间。
    **把这道闸的人群和组内那道合并、一起降到 1 也不行**:实测改版后那一页
    上多收 `/news/security`、`/news/product` 两个分类索引页,还丢掉一篇真
    文章的日期。两道闸人群不同、阈值不同,各自管一侧。

    **它挡住的是另一侧**:一个导航区里几百条**同址**的标题链接,按地址定界
    时组内看上去"这一层只属于我",于是一路上移到 `<body>`、把 `<main>` 里
    真文章的日期借过来,再靠"文字最长者当宿主"把真文章剔成自己的字段 ——
    基线收 2 篇真文章,没有这道闸只收 1 条导航链接,`errors` 为空、同步
    显示成功。`<body>` 下装着两条原生带日期的异址记录,这道闸认得出来。

    **代价**:真·嵌套列表(一张卡片里再嵌一个"相关文章",而且嵌的那几条
    原生带日期)会让外层卡片不再被当成记录容器,卡片本身那条记录读不到
    日期。那和已经披露的"嵌套相关文章会被当成字段"是同一处既有取舍。
    """
    tally, by_url = lists
    lo, hi = index.subtree(node)
    total = tally[lo]
    same = by_url.get(url, ())
    if same and lo <= same[0] and same[-1] < hi:
        total -= 1  # 自己那个地址不算,它的几份显示副本也只算这一次
    return total >= MIN_DATED


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
    # 整页的日期索引,建一次、全程共用(为什么不能"每问一次扫一遍子树",见
    # `page_date` 开头那段)。它只读 soup、不改 DOM,随这次调用一起丢。
    index = DateIndex(soup)
    found = _list_group(soup, base_url, index)
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
        published = item_published(box, index)
        entries.append({'url': url, 'title': title,
                        'body': shell_body(item_subject(box, title), published, url),
                        'author': '', 'published': published, 'content_kind': 'summary'})
    if not entries:
        raise FeedError('这一页上有文章列表，但一条标题都没解析出来 —— 站点结构'
                        '可能变了，需要适配')
    entries.sort(key=lambda e: e['published'], reverse=True)
    return entries[:MAX_ITEMS]
