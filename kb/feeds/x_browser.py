# -*- coding: utf-8 -*-
"""浏览器通道:用用户自己的 X 登录态,从时间线上按推文时间取增量。

**边界是推文时间,不是页数**。X 的 profile 页是虚拟列表(滚过去的节点会被回收),
所以"抓到第几条"没有稳定含义;而每条推文身上都带着 `<time datetime>`,那是
这条路上唯一可靠的游标。每个订阅记一个水位(上次抓到的最新推文时间),这一轮
滚到比水位更老就停。

**为什么不是「看到一条老的就停」**:profile 页顶上可能钉着一条几年前的置顶推文,
转推显示的又是**原推**的时间 —— 两者都会在新推文中间插进一条很老的时间。所以
判据是「**按页面出现顺序**连续 `_OLD_STREAK` 条**自己发的**推都老于水位」:置顶和
转推身上那个时间跟"读到哪儿了"没有关系,不参与这个证明(但它们照旧入库,
见 `_in_timeline` / `_own_tweet`)。

**没证明到就不推进水位**(`harvest` 里那句 `water if incomplete else ...`)。滚动
次数用完、单个账号的时间预算用完、页面不再长出新内容而又不在底部 —— 这三种都是
「没读完」:这一轮停在时间线半中间,下面那一段既没读也没入库,而把水位推到"这一轮
见过的最新"就等于宣布那一段已经收过 —— 它们比旧水位新、又比新水位老,从此**任何
一轮增量都选不到它们**,再同步也补不回来。没读完就留着旧水位:下一轮重叠重抓,
`put_items` 按 URL 去重,一条都不会重复入库。

**为什么一次会话抓多个账号**:起一个 Chrome 要两三秒,51 个账号各起一次就是
两分多钟纯开销,还会撞上采集阶段 90 秒的预算;而同一个 profile 目录同时只能
被一个 Chrome 实例打开,并发也不成立。所以这条通道是「一次会话、串行遍历」,
由 `harvest()` 整批做完,采集阶段再按订阅各取自己那一份(见 `x_channels`)。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from kb.feeds import x_account, x_article, x_body, x_page_state, x_post, x_post_meta
from kb.feeds.x_cdp import Chrome, CdpError, has_login_footprint, profile_busy
# 登录窗口的生死搬去了 `x_login_window`(x_cdp 顶着 700 行上限)
from kb.feeds.x_login_window import login_window_left, release_login_window
from kb.feeds.x_page_state import (
    ProblemConfirmation, XAccessBlocked, XBrowserError, XRateLimited, navigate, read)
from kb.feeds.x_timeline_state import TimelineState, wait_state

SHANGHAI = ZoneInfo('Asia/Shanghai')
# 连续多少条老推文算"翻到底了"。1 不够:转推带的是原推时间,单条老的很常见。
_OLD_STREAK = 3
# 没有水位时(新订阅)最多往回翻多少天。真正的历史下界由 `sync._within_history`
# 按订阅创建时间钉死,这里只是"别无限滚"的兜底。
FIRST_DAYS = 14
# 单个账号的上限:滚动次数与墙上时间。两道都要,滚动次数防"一直有新内容"
# (盘点号一天几十条),时间防"每次滚都等满超时"。
MAX_SCROLLS = 40
ACCOUNT_SECONDS = 50.0
# 每次滚动后等新节点出现的上限。X 的懒加载实测 0.3~1.5 秒。
_SETTLE_SECONDS = 2.5
_SETTLE_STEP = 0.25
# 「滚不出新东西」要连着几次、而且每次都停在页面底部,才算真的到底了。
# 一次不算:慢网络下的一次懒加载空窗和"时间线到底了"长得一模一样,而把前者
# 当成后者就是在给一次没读完的抓取盖上"完整"的章。
_BOTTOM_TRIES = 3

# 注进页面的那几段 JS(收集器/复位/滚动/页面状态)搬去了 `x_timeline_js`
# (本文件顶着 700 行硬上限)。**按旧位置再导出一次**:本模块按这几个名字取,
# 全仓回归里那些 `x_browser._COLLECT_JS` / `_STATE_JS` 的指向也照旧成立。
# 选这个面的理由(以及为什么**不**搬那一族判据)写在那边的模块抬头里。
from kb.feeds.x_timeline_js import (  # noqa: E402 - 再导出,见上
    _COLLECT_JS as _COLLECT_JS,
    _RESET_JS as _RESET_JS,
    _SCROLL_JS as _SCROLL_JS,
    _STATE_JS as _STATE_JS,
)


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
    context = f'> {context}' if context and not _is_pinned(context) else ''
    body = x_body.context_body(context[2:] if context else '', text)
    out = {'url': item['url'], 'title': (text or x_body.placeholder(name))[:500],
           'body': body, 'author': name, 'published': published,
           'content_kind': 'post'}
    # 「这条带着长文卡片」的信号,只在采集期间有意义:`x_article.fill` 用完就把
    # 它摘掉(`_strip_hint`),不会进 `feed_items`。
    if item.get('article'):
        out[x_article.HINT] = str(item['article'])
    prefix = x_post_meta.context_prefix(body, context)
    if item.get('truncated'):
        out[x_post.HINT] = prefix
    out = x_post_meta.stamp(out, 'post', prefix, author_text=x_post_meta.AuthorText(text, 'tweetText'))
    return x_body.record(out, text, prefix=prefix)


def _wait_state(page: Chrome, handle: str, deadline: float) -> dict:
    return wait_state(page, handle, deadline, _STATE_JS)


def _harvest_one(page: Chrome, handle: str,
                 floor: datetime | None) -> tuple[list[dict], str]:
    """抓一个账号。返回 (条目, 没读完的原因)。

    **原因非空 = 这一轮没有读到水位的证据**,调用方据此不推进水位(见 `harvest`)。
    三种"没读完":滚动次数用完、这个账号的时间预算用完、页面不再长出新内容而又
    不在底部(懒加载卡住/被限流)。没有下界(floor 为空)时无从谈起"读到水位",
    那是新订阅的首轮,按读到的为准。逐条来源证据不足也算没读完:保留其余条目,
    留着原下界重试,不让无法确认的内容拖垮整个账号。
    """
    navigate(page, f'https://x.com/{handle}')
    deadline = time.monotonic() + ACCOUNT_SECONDS
    state = _wait_state(page, handle, deadline)
    page = TimelineState(page, handle, state, _STATE_JS)
    page.eval(_RESET_JS)
    seen_articles = parsed = 0
    items: list[dict] = []
    incomplete = '滚动次数用完,没有读到上次的位置' if floor else ''
    dry = 0                                        # 连着几次"滚完没长出新东西"
    for _ in range(MAX_SCROLLS):
        snap = json.loads(page.eval(_COLLECT_JS))
        seen_articles, parsed, items = snap['articles'], snap['parsed'], snap['items']
        if page.incomplete:
            incomplete = page.incomplete
            break
        if floor and _old_enough(items, floor, handle):
            incomplete = ''
            break
        if time.monotonic() > deadline:
            if floor:
                incomplete = f'单个账号的 {int(ACCOUNT_SECONDS)} 秒用完,没有读到上次的位置'
            break
        before = len(items)
        bottom = json.loads(page.eval(_SCROLL_JS))['bottom']
        grew = False if page.incomplete else _settle(page, before, deadline)
        if page.incomplete:
            incomplete = page.incomplete
            break
        if grew:
            dry = 0
            continue
        # 滚完没长出新东西。**一次不算**:慢网络下的一次懒加载空窗和"到底了"
        # 长得一模一样,而把前者当成后者就是给一次没读完的抓取盖"完整"的章。
        dry += 1
        if dry < _BOTTOM_TRIES:
            continue                               # 回到循环顶上:再 collect、再滚一次
        if bottom:
            incomplete = ''                        # 停在底部、连着几次没新内容 = 真到底
        elif floor:
            incomplete = '页面不再长出新内容,而且不在时间线底部'
        break
    if seen_articles and not parsed:
        # 有推文卡片却一个字段都解析不出来 —— 这是 X 改了 DOM,不是"没有推文"。
        # 两者必须分开:后者安静跳过是对的,前者安静跳过会让这条通道从某天起
        # 永远返回空,而面板上一切正常。
        raise XBrowserError(f'@{handle} 的页面结构看不懂了(读到 {seen_articles} 条卡片,'
                            '一条都解析不出来),X 可能改版了')
    rejected: list[str] = []
    entries = _select(items, floor, handle, rejected=rejected)
    if rejected:
        note = (f'@{handle} 有 {len(rejected)} 条推文的来源或上下文无法确认,'
                f'本轮已跳过(如 {rejected[0]});其余推文照常收录,'
                '同步位置保持不变,下次同步重试')
        incomplete = f'{incomplete};{note}' if incomplete else note
    page.check()
    return entries, ';'.join(dict.fromkeys(filter(None, [page.incomplete, incomplete])))


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


def _settle(page: Chrome, before: int, deadline: float) -> bool:
    """等懒加载吐出新推文。返回 False = 等满了也没有新的。"""
    end = min(time.monotonic() + _SETTLE_SECONDS, deadline)
    while time.monotonic() < end:
        time.sleep(_SETTLE_STEP)
        count = len(json.loads(page.eval(_COLLECT_JS))['items'])
        if getattr(page, 'incomplete', ''):
            return False
        if count > before:
            return True
    return False


class TimelineEntries(list):
    """Selected entries plus informational skips and their observed timestamps."""

    def __init__(self):
        super().__init__()
        self.skipped_quotes = 0
        self.skipped_newest = None


def _select(items: list[dict], floor: datetime | None, handle: str = '', *,
            rejected: list[str] | None = None) -> TimelineEntries:
    """落到 feed_items 之前的最后一道:去掉不属于这条时间线的和过老的,转成条目形状。

    哪些不属于见 `_in_timeline`(推广位、推荐位)—— 入库口径只有那一份,两处各判
    各的话,被这里丢掉的条目还在"翻到底"那边顶着计数。**置顶照收**:它不参与
    翻到底的证明(`_own_tweet`),但刚发出来就被钉上去的推文在 X 上看得见,不收
    就是永久丢掉一条真内容。来源证据不足只跳过该条,URL 交给 rejected 供调用方
    提示没读完并保留水位。
    """
    out = TimelineEntries()
    for item in sorted(items, key=lambda i: i.get('datetime') or ''):
        if not _in_timeline(item, handle):
            continue
        when = _utc(item.get('datetime', ''))
        if floor and when and when <= floor:
            continue
        try:
            entry = _entry(item)
        except x_post.PostUnavailable:
            if rejected is not None:
                rejected.append(item['url'])
            continue
        if entry:
            out.append(entry)
        elif _quote_only(item) and item.get('url') and when:
            out.skipped_quotes += 1
            out.skipped_newest = max(out.skipped_newest, when) if out.skipped_newest else when
    return out


def browser_session(profile: str, chrome_path: str = '', *, attach: bool = False):
    """这一轮用哪个浏览器。返回一个还没进入的上下文管理器。

    两条路暴露的是同一套 `open_page/goto/eval`(都继承 `x_cdp.Session`),所以
    底下那些页面脚本一行都不用知道自己跑在哪条上 —— 差别全在这一个函数里:

    - `attach=False`:我们自己起一个独立 profile 的无头 Chrome(`x_cdp.Chrome`);
    - `attach=True`:连用户日常那个浏览器(`cdp_attach.Attached`),`profile` 和
      `chrome_path` 这时都没有意义 —— 浏览器不是我们起的。
    """
    if attach:
        from kb.feeds.cdp_attach import Attached
        return Attached()
    return Chrome(profile, headless=True, chrome_path=chrome_path)


def harvest(jobs: list[tuple[str, str]], *, profile: str, chrome_path: str = '',
            budget: float = 0.0, on_progress=None, should_stop=None,
            attach: bool = False) -> dict[str, dict]:
    """一次会话抓完所有账号。

    jobs:  [(handle, 水位 ISO 或空串)]
    返回:  {handle: {'entries': [...], 'error': '', 'watermark': ISO}}

    整批共用一个浏览器会话;普通账号失败只落在它自己那一项上,不影响其余。
    明确限流则停止余批,保留已完成账号的结果,未完成账号报同一原因且不推进水位。

    `attach` 决定浏览器从哪儿来(见 `browser_session`)。整批共用一个会话这件事
    两条路都成立,只是理由不同:独立 profile 那条是因为起一个 Chrome 要两三秒、
    而同一个 profile 目录同时只能被一个实例打开;直连那条是因为每次连接都可能
    让浏览器弹一次授权。
    """
    results: dict[str, dict] = {}
    if not jobs:
        return results
    if should_stop and should_stop():
        # 早就按下的停止不去排队:起一个 Chrome 要两三秒,而起完只会把每个
        # 账号逐个跳过(口径同 `maintenance._lock_waiter` 那条"不去排队")。
        return {h: {'entries': [], 'error': '', 'skipped': '已按停止请求中断'}
                for h, _ in jobs}
    deadline = time.monotonic() + budget if budget else None
    if not attach:
        # **开工前先把到点的登录窗口关掉。** 它攥着 profile 的 ProcessSingleton
        # 锁,不关的话下面 `browser_session` 起不来,而那个异常会被每个账号各记
        # 一条失败(实测 51 个号 50 条同一句话),整条通道从此每轮全红。
        # 没到点的不动 —— 那一档起不来照旧落到各账号的 error 上,文案里有
        # 「关掉之前打开的登录窗口」这句话。
        if note := release_login_window(profile):
            if on_progress:
                on_progress(note)
    try:
        with browser_session(profile, chrome_path, attach=attach) as page:
            page.open_page('about:blank')
            for index, (handle, water) in enumerate(jobs, 1):
                stopping = should_stop() if should_stop else False
                if stopping or (deadline and time.monotonic() > deadline):
                    results[handle] = {
                        'entries': [], 'error': '',
                        'skipped': '已按停止请求中断' if stopping
                                   else '本轮时间预算用完,留待下次同步'}
                    continue
                if on_progress:
                    on_progress(index, len(jobs), handle)
                floor = _utc(water) if water else (
                    datetime.now(timezone.utc) - timedelta(days=FIRST_DAYS))
                # 这一轮真正用过的下界。没读完时存回**它**而不是存 `water`:新订阅
                # 的 `water` 是空的,存空值等于什么都没存(`watermark_patch` 对空
                # 值不产生补丁),下一轮又现算一个「当天往回 FIRST_DAYS 天」——
                # 那段没读完的日期就随时间往前滑走了,而面板显示「已同步」。
                # 实测与理由记在 `x_feedgrab.harvest` 那条注释里(两条通道同一个
                # 缺口);`_select` 两轮都按同一个**排他**下界筛,窗口逐字相同。
                started = water or floor.isoformat()
                unavailable = []
                try:
                    entries, incomplete = _harvest_one(page, handle, floor)
                    skipped_quotes = getattr(entries, 'skipped_quotes', 0)
                    observed_watermark = _newest(entries, water)
                    note = (f'跳过 {skipped_quotes} 条本人无正文的引用推文,未冒充本人观点入库'
                            if skipped_quotes else '')
                    # 长文(Article)推文在时间线上一个字都没有,正文要单独取一次。
                    # 取不到的那几条本轮不入库、并按"没读完"办 —— 空正文一旦落库
                    # 就再也更新不了(`put_items` 是 DO NOTHING),见 `x_article`。
                    #
                    # **`session=page` 就是那条后路**:长文接口(FxTwitter)是个
                    # 第三方服务,它挂了的时候这条通道手里正好有一个**已经登录**
                    # 的浏览器,直接把文章页读出来即可。导航安全 —— 这个账号的
                    # 时间线已经读完了,下一个账号的 `_harvest_one` 自己会 goto
                    # 回它的 profile。
                    #
                    # **整轮预算和停止条件要传下去。** 这一步跑在时间线已经读完
                    # 之后,外面那两道闸(`_harvest_one` 的账号级上限、进下一个
                    # 账号之前的检查)都够不着它:一个发了几十篇长文的账号会在
                    # 两道闸之间串行启动几十次新请求,而 `_GIVE_UP_AFTER` 只数
                    # 连续失败(每次成功都清零),限不住「大量慢但成功」那一种
                    # (2026-10-09 第 1 轮审查实测:预算 0.020 秒的一轮,停止在
                    # 第一条之后就置位了,后面照样新发 7 次请求、耗时 0.323 秒,
                    # 8 条全留下、`incomplete` 空、水位照常前进)。
                    entries, missing = x_post.complete(
                        entries, session=page,
                        deadline=deadline, should_stop=should_stop, unavailable=unavailable)
                    if missing:
                        incomplete = f'{incomplete};{missing}' if incomplete else missing
                    # **没读完就不推进水位**:这一轮停在时间线半中间,下面那一段
                    # 既没读也没入库,而把水位推到"这一轮见过的最新"就等于宣布
                    # 那一段已经收过 —— 它们比旧水位新、又比新水位老,从此任何
                    # 一轮增量都选不到它们。留着这一轮的下界:下一轮重叠重抓,
                    # `put_items` 按 URL 去重,一条都不会重复入库。
                    results[handle] = {
                        'entries': entries, 'error': '', 'incomplete': incomplete,
                        **x_post.unavailable_report(unavailable),
                        'skipped_quotes': skipped_quotes,
                        'note': ';'.join(filter(None, [note, x_post.unavailable_note(unavailable)])),
                        'watermark': started if incomplete else observed_watermark}
                    if timeline := getattr(page, '_x_timeline', None):
                        timeline.finalize(results[handle], started)
                except (XRateLimited, XAccessBlocked, x_post.PostSessionUnavailable) as e:
                    if unavailable:
                        results[handle] = {'entries': [], 'error': str(e),
                                           **x_post.unavailable_report(unavailable)}
                    raise
                except x_account.XAccountUnavailable as e:
                    results[handle] = {'entries': [], 'error': str(e),
                                       'account_state': e.state, 'account_evidence': e.evidence}
                except (XBrowserError, CdpError, x_post.PostUnavailable) as e:
                    results[handle] = {'entries': [], 'error': str(e),
                                       **x_post.unavailable_report(unavailable)}
    except (XRateLimited, XAccessBlocked, x_post.PostSessionUnavailable, CdpError, EOFError, OSError) as e:
        # 浏览器整个起不来/中途死了:剩下的账号统一记成同一个原因,
        # 而不是逐个报一条看不出共同根因的错。
        reason = str(e) if isinstance(e, (XBrowserError, x_post.PostSessionUnavailable, CdpError)) else f'浏览器会话中断:{e}'
        for handle, _ in jobs:
            results.setdefault(handle, {'entries': [], 'error': reason})
    return results


def _newest(entries: list[dict], fallback: str) -> str:
    """这一轮抓到的最新推文时间,作为下一轮的水位。

    **一条都没抓到时沿用旧水位**,不要改成"现在":那等于把这段时间宣布为已抓过,
    而这一轮可能只是没滚到(预算用完、X 限流)。水位只前进到确实见过的那条推文。
    """
    stamps = [_utc(entry['published']) for entry in entries if entry.get('published')]
    stamps.append(getattr(entries, 'skipped_newest', None))
    newest = max((stamp for stamp in stamps if stamp is not None), default=None)
    return newest.isoformat() if newest else fallback


def open_login_window(profile: str, chrome_path: str = '') -> str:
    """开一个**有头**窗口让用户自己登录 X,起完**记下它、并安排到点关掉**。

    这一步必须有头、也必须是用户点出来的:输密码、过两步验证、认人机校验都
    得他本人做。起完立刻返回 —— 等他登录完是几分钟的事,一个 HTTP 请求不该
    挂在那里。登录态落在 profile 目录里,之后的定时抓取无头复用它。

    **「起完就不管了」已经改掉了。** 这个窗口攥着 profile 的 ProcessSingleton 锁,
    忘了关就是这条通道每一轮同步全红(2026-10-19 实测形态写在
    `x_login_window` 的模块注释里)。所以起完要记下它的身份、并起一个
    守望线程到点关掉;抓取和「检查连接」开工前还各兜一道。文案照旧请用户自己关
    —— 自己关是最快的那条路,自动关只是**忘了关时的下限**。

    **这一档用的是一个独立 profile,不是用户日常那个 Chrome** —— 所以要在这里
    单独登录一次。好处是它不依赖用户:日常窗口关了、浏览器重启了,定时抓取照旧
    无头跑。想免掉这次登录就换成「我日常用的浏览器」那一档(`x_channels.ATTACH`,
    直连已经登录好的浏览器),它的边界和代价写在 `cdp_attach` 的模块注释里。
    """
    from kb.feeds.x_cdp import find_chrome
    from kb.feeds.x_login_window import (LOGIN_WINDOW_TTL,
                                         remember_login_window,
                                         watch_login_window)
    binary = find_chrome(chrome_path)
    os.makedirs(profile, exist_ok=True)
    if profile_busy(profile):
        raise XBrowserError('登录窗口已经开着了,请在那个窗口里完成登录')
    try:
        proc = subprocess.Popen(
            [binary, f'--user-data-dir={profile}', '--no-first-run',
             '--no-default-browser-check', 'https://x.com/login'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise XBrowserError(f'打不开登录窗口:{e}') from None
    # **先记录、再守望**:守望线程第一跳就要读这份记录。反过来的话它会读到
    # 「没有我们的窗口」当场收工,而窗口已经起来了 —— 那就退回"起完就不管了"。
    # **句柄要一起交给守望线程**:`Popen` 返回不代表 Chrome 已经建好
    # ProcessSingleton 锁,那几百毫秒里光看 profile 里那两个文件认不出我们的窗口
    # (审查 R5,理由全文在 `watch_login_window`)。
    remember_login_window(profile, proc.pid)
    watch_login_window(profile, proc=proc)
    return ('已打开登录窗口。在里面登录你要用的 X 账号,登录成功后**关掉这个窗口**'
            f'(抓取时要独占这个 profile);忘了关也不要紧,{int(LOGIN_WINDOW_TTL // 60)} '
            '分钟后它会自动关闭。登录完回来点一次「检查连接」。')


def verify_timeline(page, timeout: float = 0.0) -> tuple[str, dict]:
    """在一个已经导航到 `x.com/home` 的会话上,把落地状态判成一个结论码。

    返回 `(结论码, 最后那份状态)`,码有七个:`ok`(读到推文了)、`login`(掉回
    登录页)、`limited`(限流)、`failed`(请求出错)、`challenge`(安全验证)、
    `not-loaded`(首页压根没加载完)、`no-tweets`
    (加载完了却一条推文都没有)。

    **只判、不写文案。** 同一个状态在不同入口下该说的下一步完全不同:「检查连接」
    要指向**这一档自己**的登录办法(独立 profile 点按钮 / 日常浏览器里自己登),
    而「从日常浏览器导入登录态」那条要说的是「cookie 搬过来了但 X 不认」——
    指错地方等于让人白跑一趟(`probe` 那条注释里记着这件事)。所以判据收在这一份,
    文案留给调用方。

    **缺省值在调用时才取,不写成形参默认值。** 写成 `timeout=ACCOUNT_SECONDS` 的话
    那个数在 `def` 那一刻就绑死了,之后 `patch.object(x_browser,
    'ACCOUNT_SECONDS', …)` 改到的是另一个名字 —— 口径同 `daily.collect` 那条
    `limit`/`MAX_ITEMS`。实测代价是回归里那两条走超时的用例各等满 50 秒(整份测试
    从 2 秒涨到 100 秒),而**真实代价是预算收紧改不动它**:谁把
    `ACCOUNT_SECONDS` 调小,这一处照旧按老值等。
    """
    deadline = time.monotonic() + (timeout or ACCOUNT_SECONDS)
    last: dict = {}
    errors = ProblemConfirmation()
    confirmed = ''
    while time.monotonic() < deadline:
        last = read(page, _STATE_JS)
        if last['login']:
            return 'login', last
        confirmed = errors.check(last)
        if verdict := x_page_state.terminal(last, confirmed):
            return verdict, last
        time.sleep(_SETTLE_STEP)
    if verdict := x_page_state.terminal(last, confirmed, expired=True):
        return verdict, last
    return ('no-tweets' if last.get('ready') == 'complete' else 'not-loaded'), last


def probe(profile: str, chrome_path: str = '', *,
          attach: bool = False) -> tuple[bool, str]:
    """真连一次浏览器,看这份登录态还读不读得到时间线。

    用户点「检查连接」才走这条,所以允许慢(几秒到几十秒)。它和抓取走的是
    **同一条路**(同一个 `browser_session`、同一套页面脚本),所以结论是真的;
    `x_channels.readiness` 那道便宜判断只看文件在不在、端口活不活,而
    **cookie 过期在文件上看不出来**。

    前置检查两条路不同,但判的是同一件事「现在这个浏览器用得上吗」:独立
    profile 那条怕被登录窗口占着(同一个目录只能开一个实例),直连那条怕压根
    没开调试开关。
    """
    if attach:
        from kb.feeds import cdp_attach
        if cdp_attach.discover() is None:
            if problem := cdp_attach.explicit_problem():
                return False, f'{problem}。{cdp_attach.ENDPOINT_HINT}'
            return False, '没有发现开着远程调试的浏览器。' + cdp_attach.ENABLE_HINT
    else:
        if profile_busy(profile):
            # 到点的先关掉再判:用户点「检查连接」正是想知道"这条通道现在行不行",
            # 而我们自己起的、早该关的那个窗口不该算成"行不通"。
            release_login_window(profile)
            if profile_busy(profile):
                left = login_window_left(profile)
                if left:
                    return False, (f'X 登录窗口还开着,它占着这个 profile。'
                                   f'在那个窗口里登录完就关掉它,或者等 {int(left // 60) + 1} '
                                   '分钟它自动关闭')
                return False, '这个 profile 正被另一个窗口占着(登录窗口还开着?)。关掉它再试'
        # **关掉那个窗口之后,「登录过没有」这一档还要再过。** 这两档原来是同一条
        # if/elif 链上的兄弟,于是「窗口到点被我们关掉」那条路会把它整条跳过 ——
        # 而**最容易留下这种窗口的恰恰是从没登录成功过的 profile**(人点开窗口、
        # 没登完就走了)。跳过去之后用户收到的是后面真连一次得出的那句含糊失败,
        # 而本来该念的是「还没登录过 X,去点那两颗按钮」。attach 那条路不过这一档
        # (它用的是用户自己的浏览器,这个独立 profile 里有没有痕迹跟它无关)。
        if not has_login_footprint(profile):
            return False, ('浏览器通道还没有登录过 X:先点「打开登录窗口」登录一次,'
                           '或者点「从日常浏览器导入登录态」')
    try:
        with browser_session(profile, chrome_path, attach=attach) as page:
            page.open_page('about:blank')
            navigate(page, 'https://x.com/home')
            verdict, last = verify_timeline(page)
            if verdict == 'login':
                # 两条路的下一步不一样:独立 profile 要点那颗按钮再登一次,
                # 直连那条得在他自己的浏览器里登 —— 我们没有也不该有那个
                # 窗口的控制权。指错地方等于让人白跑一趟。
                #
                # **独立 profile 这档有两颗按钮,都要说。** 登录态是「从日常浏览器
                # 导入」来的那些人,点「打开登录窗口」等于被推去做他本来想免掉的
                # 那次登录 —— 而他要的只是再导一次(cookie 过期是常态,见
                # `x_login_import` 那条「这件事要能反复做」)。
                return False, ('这个浏览器里的 X 登录态已失效,'
                               '请在它里面打开 x.com 登录一次,再回来检查'
                               if attach else
                               'X 登录态已失效:点「打开登录窗口」重新登录一次,'
                               '或者再点一次「从日常浏览器导入登录态」')
            if verdict == 'challenge':
                # **不许说成登录失效,也不许说成限流。** 这张页要先有登录态才会
                # 出现,把人推去重新登录是白跑一趟 —— 该做的是去他自己那个浏览器
                # 里把验证过完(文案和采集那一侧同一个口径,见 `x_page_state`)。
                return False, ('X 要求先过一次安全验证（账号访问受限）:请在对应通道的'
                               '浏览器里打开 x.com/account/access，完成验证后重试，'
                               '不必因此重新登录')
            if verdict == 'limited':
                return False, 'X 暂时限制了访问频率,稍后再试'
            if verdict == 'failed':
                return False, 'X 页面请求出错,暂时无法验证登录态;稍后点「检查连接」重试,不必因此重新登录'
            if verdict == 'ok':
                return True, f'登录态有效,首页读到 {last["articles"]} 条推文'
            observed = '没有加载完' if verdict == 'not-loaded' else '一条推文都没读到'
            local = ('请在对应浏览器中确认 X 首页能否打开' if attach else
                     '本地已有登录痕迹,不代表当前登录态有效;可点「打开登录窗口」查看 X 首页')
            return False, f'X 首页{observed};未能判出原因。{local},再点「检查连接」重试'
    except (XBrowserError, CdpError) as e:
        return False, str(e)
    except (EOFError, OSError) as e:
        return False, f'浏览器会话中断:{e}'
