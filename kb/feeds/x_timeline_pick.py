# -*- coding: utf-8 -*-
"""时间线上**这一条算不算该收、它折成什么形状** —— 一族纯判据,不碰浏览器。

从 `x_browser` 拆出来的(那边顶着 700 行硬上限)。拆的面是「**要不要开浏览器**」:
这边全是对一个个 dict 做判断,`x_browser` 那边才是开 Chrome、滚时间线、读页面状态。
口径一个字没动,`x_browser` 里**按旧位置再导出一次** —— 全仓注释和回归里那些
`x_browser._entry` / `_select` / `_utc` / `_old_enough` 的指向照旧成立。

**这一族一条桩都没有,所以再导出就够**(打桩要打在定义它的那个模块上:名字带的是
绑定不是引用,在 `x_browser` 上 patch 拦不住这边的内部调用)。真要给这里打桩,打
`x_timeline_pick` 这个模块。
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kb.feeds import x_article, x_body, x_post

SHANGHAI = ZoneInfo('Asia/Shanghai')
# 连续多少条老推文算"翻到底了"。1 不够:转推带的是原推时间,单条老的很常见。
_OLD_STREAK = 3


def _as_published(iso: str) -> str:
    """ISO UTC → 项目里 published 的统一写法(Asia/Shanghai,分钟精度)。

    和 `fetching.parse_feed` 一个口径:日报按 `published` 筛窗口,两处格式不一样
    的话同一天的内容会被算进不同的窗口(见 `kb/daily.py` 里字符串比较那条)。
    """
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return ''
    return dt.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M')


def _utc(iso: str) -> datetime | None:
    """任意 ISO 写法 → UTC。**不带时区的那种按项目时区(Asia/Shanghai)补齐。**

    `astimezone` 对 naive 的默认是拿**宿主机器**的时区补 —— 于是同一个
    `2026-10-05` 在 UTC 机器上是上海时间 08:00,那一天凌晨 0 点到 7 点 59 的
    推文一条也翻不到,还不报任何错(2026-10-08 审查实测 `TZ=UTC`:四条里只
    收进 08:01 那一条)。而这条通道里被比较的另一头(`_as_published`)固定按
    上海算,两头必须是同一个口径,不能由部署机器决定用户勾的日期是什么意思。

    区间补抓那个日期在 `x_channels.since_floor` 就已经转成带时区的下界了;
    这里是第二道 —— 以后谁再把一个裸日期交进来,含义也不会跟着机器漂。
    """
    try:
        dt = datetime.fromisoformat(iso.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=dt.tzinfo or SHANGHAI).astimezone(timezone.utc)


def _is_pinned(context: str) -> bool:
    return any(k in context for k in ('Pinned', '置顶'))


def _is_promoted(context: str) -> bool:
    return any(k in context for k in ('Promoted', '推广', '赞助'))


def _in_timeline(item: dict, handle: str) -> bool:
    """这一条是不是从**这个账号的时间线**里长出来的 —— 入库资格就是它。

    两种不算:
    ①**推广位** —— 它是广告,不是这个人的观点。
    ②**作者不是这个号、又没有转推那一行上下文** —— 推荐位、"你可能喜欢"。按这个
      订阅记进去就是把别人的推文当成这个大V的观点,和 feedgrab 那一侧按
      `/<handle>/status/` 过滤是同一条规矩。转推一定带着一行 socialContext,
      所以这么判不会把转推丢掉 —— 而转推正是"他此刻在想什么"的一部分,正文里
      保留那行上下文(见 `_entry`)。

    **置顶算**。它不参与"翻到底"的计数(见 `_own_tweet`),但那是另一个问题:
    刚发出来就被钉上去的推文在 X 上看得见,不入库就是永久丢掉一条真内容。
    "旧置顶每天都在、会反复入库"不成立 —— `put_items` 按 `sha256(kind:url)`
    去重,而且它的时间低于水位,下一轮 `_select` 自己就筛掉了。
    """
    context = (item.get('context') or '').strip()
    if _is_promoted(context):
        return False
    foreign = handle and (item.get('author') or '').lower() != handle.lower()
    return not (foreign and not context)


def _own_tweet(item: dict, handle: str) -> bool:
    """这一条身上那个时间能不能当成「读到时间线哪儿了」的证据。

    只有**这个号自己发的原创推**算得上。profile 时间线按"活动时间"倒序排,而
    `<time datetime>` 给的是**原推**发布时间:
    - 置顶:位置在最上面,时间却可能是几年前 —— 一条都不该算。
    - 转推:位置按转推时刻排,显示的是原推时间(必然 ≤ 转推时刻)。所以"它很老"
      证明不了我们已经翻过水位(可能是刚刚转的一条老推);反过来"它很新"倒是
      能证明还没翻过去(活动时间比它更新),那一档由 `_old_enough` 里的"重置"
      分支接住。
    只有原创推的 `datetime` 等于它的活动时间,才能当位置用。
    """
    if (item.get('context') or '').strip():          # 置顶、转推、推荐位都带它
        return False
    if not handle:
        return True
    return (item.get('author') or '').lower() == handle.lower()


def _quote_only(item: dict) -> bool:
    return bool(item.get('quoted') and not (item.get('text') or '').strip()
                and not item.get('article') and not item.get('truncated'))


def _entry(item: dict) -> dict | None:
    """一条推文 → feed_items 的条目形状。没正文也没链接的不要。"""
    published = _as_published(item.get('datetime', ''))
    text = (item.get('text') or '').strip()
    if not item.get('url') or not (text or published):
        return None
    if _quote_only(item):
        return None
    name = item.get('name') or item.get('author') or ''
    context = (item.get('context') or '').strip()
    # 转推/引用的上下文留在正文开头一行:它决定了这条是不是本人的观点,
    # 而 feed_items 里没有地方放这个标记。
    body = x_body.context_body(context if not _is_pinned(context) else '', text)
    out = {'url': item['url'], 'title': (text or x_body.placeholder(name))[:500],
           'body': body, 'author': name, 'published': published,
           'content_kind': 'post'}
    # 「这条带着长文卡片」的信号,只在采集期间有意义:`x_article.fill` 用完就把
    # 它摘掉(`_strip_hint`),不会进 `feed_items`。
    if item.get('article'):
        out[x_article.HINT] = str(item['article'])
    if item.get('truncated'):
        out[x_post.HINT] = body[:-len(text)] if text else body
    return x_body.record(out, text, prefix=body[:-len(text)] if text else body)


def _old_enough(items: list[dict], floor: datetime, handle: str = '') -> bool:
    """**按页面出现顺序**连续 `_OLD_STREAK` 条「自己发的推」都老于 floor 才算翻到底。

    两件事各有理由:

    **为什么按页面顺序,不按时间排序**:时间排序会把三条互不相邻的旧推(散落在
    新推之间的老转推、几年前的置顶)拼成一条"连续的老尾巴",第一屏就判翻到底、
    下面的新推全漏(`items` 的顺序由 `_COLLECT_JS` 那个 Map 的插入顺序保证,
    就是它们在 DOM 里第一次出现的顺序)。

    **为什么只有原创推算证据**(`_own_tweet`):profile 时间线按活动时间倒序,而
    `<time datetime>` 给的是原推时间 —— 置顶和转推身上那个时间说明不了"读到哪儿
    了"。但它们照旧入库(`_in_timeline`),只是不参与这个证明。

    **反过来,任何一条新于 floor 的时间线条目都重置计数**:转推显示的原推时间
    必然 ≤ 它的活动时间,所以"它很新"足以证明这个位置还没翻过水位。
    """
    streak = 0
    for item in items:                             # 页面出现顺序
        if not _in_timeline(item, handle):
            continue                               # 推广位、推荐位:不是这条时间线
        when = _utc(item.get('datetime', ''))
        if not when:
            continue
        if when > floor:
            streak = 0
        elif _own_tweet(item, handle):
            streak += 1
            if streak >= _OLD_STREAK:
                return True
    return False


class TimelineEntries(list):
    """Selected entries plus informational skips and their observed timestamps."""

    def __init__(self):
        super().__init__()
        self.skipped_quotes = 0
        self.skipped_newest = None


def _select(items: list[dict], floor: datetime | None, handle: str = '') -> TimelineEntries:
    """落到 feed_items 之前的最后一道:去掉不属于这条时间线的和过老的,转成条目形状。

    哪些不属于见 `_in_timeline`(推广位、推荐位)—— 入库口径只有那一份,两处各判
    各的话,被这里丢掉的条目还在"翻到底"那边顶着计数。**置顶照收**:它不参与
    翻到底的证明(`_own_tweet`),但刚发出来就被钉上去的推文在 X 上看得见,不收
    就是永久丢掉一条真内容。
    """
    out = TimelineEntries()
    for item in sorted(items, key=lambda i: i.get('datetime') or ''):
        if not _in_timeline(item, handle):
            continue
        when = _utc(item.get('datetime', ''))
        if floor and when and when <= floor:
            continue
        if entry := _entry(item):
            out.append(entry)
        elif _quote_only(item) and item.get('url') and when:
            out.skipped_quotes += 1
            out.skipped_newest = max(out.skipped_newest, when) if out.skipped_newest else when
    return out
