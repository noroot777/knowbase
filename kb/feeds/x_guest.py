# -*- coding: utf-8 -*-
"""不登录通道:经 FxTwitter 的公开接口读账号时间线,不碰任何 X 账号、cookie 或浏览器。

**硬约束在 `docs/decisions/x-channels.md` 第一节 —— 改这块之前先读那一份。**

**为什么是这条路,不是「不登录的浏览器」**(2026-11-03 实测):
- 无头 Chrome 的默认 UA 带 `HeadlessChrome`,x.com 直接回 HTTP 403;换成普通 UA 之后
  个人页只露出最近五条左右(多产的号只到几个小时前),再往下滚就是登录墙。
- `cdn.syndication` 那条嵌入接口给的是几年前的旧推、乱序,没有最新内容。
- FxTwitter 的 `/2/profile/<handle>/statuses` 不要登录,按页倒序给完整时间线,带
  `cursor.bottom` 往前翻;长推(note tweet)的 `text` 是全文、t.co 已展开。拿近五天
  五个账号和登录通道入库的对过一遍:登录通道收到的它几乎都有,另外还多出一些。

**它是第三方公共服务**(开源 FixTweet 跑的公共实例),代价如实记着:推文地址和账号名
会发给它;它挂了这条通道就停,不会偷偷换到要登录的那几条。**同一个号偶发 404**
(同一分钟里 404 / 200 交替出现,实测),所以 404 按临时故障重试,**不许当成「账号
没有推文」**—— 那会让这条订阅安静地永远停在「今天没有新内容」。

**入库口径跟浏览器通道对齐**:只收这个号自己发的(转推不收,作者不是这个号的不收);
只有引用、自己一个字没写的跳过(不拿被引用者的话冒充他的正文);长文带上
`x_article.HINT`,正文交给 `x_article.fill`(同一个公开接口,同样不登录)。

**「读到下界」的证明口径同 `x_browser._old_enough`**:按接口给出的顺序,连续
`_OLD_STREAK` 条他自己的推都不新于下界才算;任何一条新于下界的条目都重置计数
(转推带的是原推时间,它很新足以证明还没翻过去,很老却证明不了什么)。翻到时间线
尽头(没有下一页)也算读完。别的情况 —— 翻页上限、时限、停止、中途请求失败 —— 都是
没读完,如实写进 `incomplete`,水位不动。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from kb.feeds import x_article, x_body, x_post_meta
from kb.feeds.fetching import FeedError, request
from kb.feeds.x_errors import PostUnavailable

# 接口地址。和长文那一步(`KB_X_ARTICLE_API`)分开设:那一步三条登录通道也在用。
API = os.environ.get('KB_X_GUEST_API', 'https://api.fxtwitter.com').rstrip('/')
# 单次请求的墙钟上限。
TIMEOUT = float(os.environ.get('KB_X_GUEST_TIMEOUT', '20'))
# 一个账号最多翻几页(一页 20 条左右)。五天窗口里最多产的号实测五六页。
MAX_PAGES = int(os.environ.get('KB_X_GUEST_PAGES', '15'))
# 同一页请求失败后再试几次、每次隔多久。偶发 404 / 断流隔两秒再问一次多半就好。
RETRIES = 3
RETRY_PAUSE = 2.0
# 翻页之间歇一下:这是别人的公共实例。
PAGE_PAUSE = 0.5
# 剩下不到这么多秒就不再发新请求(一次请求本身就要一两秒)。
MIN_USEFUL = 2.0
# 当成没有水位(新订阅、区间补抓之外)时只读这么多天。真正的下界由调用方给。
FIRST_DAYS = 5
SHANGHAI = ZoneInfo('Asia/Shanghai')
_OLD_STREAK = 3
_STATUS_URL = re.compile(r'^https://(?:x|twitter)\.com/([A-Za-z0-9_]{1,20})/status/(\d+)$')


class GuestError(RuntimeError):
    """这一页没问到。文案直接给用户。"""


def _page(handle: str, cursor: str, deadline: float) -> dict:
    """问一页。拿不到或读不懂一律抛 `GuestError`;重试在这里做。"""
    endpoint = f'{API}/2/profile/{quote(handle)}/statuses'
    if cursor:
        endpoint += f'?cursor={quote(cursor, safe="")}'
    why = ''
    for attempt in range(RETRIES):
        if attempt:
            if time.monotonic() + RETRY_PAUSE + MIN_USEFUL >= deadline:
                break
            time.sleep(RETRY_PAUSE)
        try:
            resp = request(endpoint, {'Accept': 'application/json'},
                           deadline=min(deadline, time.monotonic() + TIMEOUT))
            payload = json.loads(resp.body.decode('utf-8', 'replace'))
        except FeedError as e:
            # `fetching` 的后半句「请检查来源或填写专用订阅链接」在这里是胡话。
            why = re.split(r'[;；]', str(e))[0].strip()
            continue
        except (OSError, ValueError, UnicodeError, RecursionError) as e:
            why = f'回包读不懂:{e}'
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get('results'), list):
            why = '回包里没有推文列表'
            continue
        return payload
    raise GuestError(f'公开接口没答复({why or "时限用完"})')


def _when(status: dict) -> datetime | None:
    stamp = status.get('created_timestamp')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or stamp <= 0:
        return None
    return datetime.fromtimestamp(stamp, timezone.utc)


def _well_formed(status) -> bool:
    return (isinstance(status, dict) and isinstance(status.get('author'), dict)
            and isinstance(status['author'].get('screen_name'), str)
            and isinstance(status.get('text', ''), str) and _when(status) is not None)


def _own(status: dict, handle: str) -> bool:
    """这条是不是**这个号自己发的**(不是转推、不是别人的)。"""
    return (not status.get('reposted_by')
            and status['author']['screen_name'].lower() == handle.lower())


def _url(status: dict) -> str:
    url = str(status.get('url') or '')
    m = _STATUS_URL.match(url)
    if m and str(status.get('id') or m.group(2)) == m.group(2):
        return f'https://x.com/{m.group(1)}/status/{m.group(2)}'
    ident = str(status.get('id') or '')
    if not ident.isdigit():
        return ''
    return f"https://x.com/{status['author']['screen_name']}/status/{ident}"


def _entry(status: dict) -> dict | None:
    """一条自己发的推文 → feed_items 的条目形状(口径同 `x_browser._entry`)。"""
    url = _url(status)
    when = _when(status)
    if not url or not when:
        return None
    text = (status.get('text') or '').strip()
    article = status.get('article')
    if not text and status.get('quote') and not article:
        return None                                  # 只有引用:不拿别人的话当他的正文
    name = str(status['author'].get('name') or status['author']['screen_name'])
    out = {'url': url, 'title': (text or x_body.placeholder(name))[:500], 'body': text,
           'author': name, 'published': when.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M'),
           'content_kind': 'post'}
    if article:
        # 时间线里那份 article 的正文是空的(实测 `content.blocks == []`),只当信号用。
        out[x_article.HINT] = str(article.get('id') if isinstance(article, dict) else article)
    out = x_post_meta.stamp(out, 'post', '', author_text=x_post_meta.AuthorText(text, 'tweet.text'))
    return x_body.record(out, text)


def scan(handle: str, floor: datetime, *, deadline: float, should_stop=None) -> dict:
    """把 `handle` 的时间线从最新一路读到 `floor`(排他)。

    返回 `{'entries', 'incomplete', 'error', 'skipped_quotes'}`:
    - `error` 非空 = 第一页都没问到,这个号这一轮什么也没读成;
    - `incomplete` 非空 = 读了一部分,条目照收,但没读到下界(调用方不许推进水位)。
    """
    entries, seen, cursor, pages, streak, skipped = [], set(), '', 0, 0, 0
    incomplete = ''
    while True:
        if should_stop and should_stop():
            incomplete = '已按停止请求中断,时间线未读完'
            break
        if pages >= MAX_PAGES:
            incomplete = f'翻了 {MAX_PAGES} 页还没读到这一轮的下界'
            break
        if time.monotonic() + MIN_USEFUL >= deadline:
            incomplete = '本账号的时限用完,时间线未读完'
            break
        if pages:
            time.sleep(PAGE_PAUSE)
        try:
            payload = _page(handle, cursor, deadline)
        except GuestError as e:
            if not pages:
                return {'entries': [], 'incomplete': '', 'error': f'@{handle}:{e}', 'skipped_quotes': 0}
            incomplete = f'翻到第 {pages + 1} 页时{e},时间线未读完'
            break
        pages += 1
        results = payload['results']
        statuses = [s for s in results if _well_formed(s)]
        if results and not statuses:
            # 拿到东西了却一条都认不出来:接口改了形状。不许当成「没有新推文」。
            error = f'@{handle}:公开接口的回包换了形状,{len(results)} 条一条都认不出来'
            if not entries:
                return {'entries': [], 'incomplete': '', 'error': error, 'skipped_quotes': 0}
            incomplete = error
            break
        done = False
        for status in statuses:
            when = _when(status)
            if when > floor:
                streak = 0
            elif _own(status, handle):
                streak += 1
                if streak >= _OLD_STREAK:
                    done = True
            if not _own(status, handle) or when <= floor:
                continue
            try:
                entry = _entry(status)
            except PostUnavailable:
                entry = None
            if entry is None:
                skipped += bool(status.get('quote'))
                continue
            if entry['url'] not in seen:
                seen.add(entry['url'])
                entries.append(entry)
        cursor = str((payload.get('cursor') or {}).get('bottom') or '')
        if done or not results or not cursor:
            break                                     # 翻到下界或时间线尽头:读完了
    entries.sort(key=lambda e: e['published'])
    return {'entries': entries, 'incomplete': incomplete, 'error': '', 'skipped_quotes': skipped}


def _floor(water: str) -> datetime:
    """水位 / 区间下界 → UTC。不带时区的按项目时区补齐;没有就按 `FIRST_DAYS` 现算。"""
    try:
        stamp = datetime.fromisoformat(str(water).replace('Z', '+00:00'))
        return stamp.replace(tzinfo=stamp.tzinfo or SHANGHAI).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return datetime.fromtimestamp(time.time() - FIRST_DAYS * 86400, timezone.utc)


def harvest(jobs: list[tuple[str, str]], *, budget: float = 0.0, on_progress=None,
            should_stop=None, complete_articles=True, account_seconds=None) -> dict[str, dict]:
    """逐个账号读,返回和另外几条通道同形状的结果(区间补抓那条路用它)。

    jobs: [(handle, 下界 ISO 或空串)]
    返回: {handle: {'entries', 'error', 'incomplete', 'watermark'}}
    """
    results: dict[str, dict] = {}
    deadline = time.monotonic() + budget if budget else float('inf')
    for index, (handle, water) in enumerate(jobs, 1):
        stopping = bool(should_stop and should_stop())
        if stopping or time.monotonic() + MIN_USEFUL >= deadline:
            results[handle] = {'entries': [], 'error': '',
                               'skipped': '已按停止请求中断' if stopping else '本轮时间预算用完,留待下次同步'}
            continue
        if on_progress:
            on_progress(index, len(jobs), handle)
        floor = _floor(water)
        limit = deadline if account_seconds is None else min(deadline, time.monotonic() + account_seconds)
        got = scan(handle, floor, deadline=limit, should_stop=should_stop)
        if got['error']:
            results[handle] = {'entries': [], 'error': got['error']}
            continue
        entries, note = got['entries'], got['incomplete']
        if complete_articles:
            entries, missing = x_article.fill(entries, deadline=None if deadline == float('inf') else deadline,
                                              should_stop=should_stop)
            note = f'{note};{missing}' if note and missing else note or missing
        newest = max((e['published'] for e in entries), default='')
        results[handle] = {'entries': entries, 'error': '', 'incomplete': note,
                           'watermark': floor.isoformat() if note or not newest else newest}
        if got['skipped_quotes']:
            results[handle].update(note=f"{got['skipped_quotes']} 条只有引用、本人没写字的推文没有收",
                                   skipped_quotes=got['skipped_quotes'])
    return results


def probe(handle: str) -> tuple[bool, str]:
    """「检查连接」:真问一页。返回 (就绪, 成品文案)。"""
    if not API:
        return False, '不登录通道的接口地址被设成了空(KB_X_GUEST_API)'
    try:
        payload = _page(handle, '', time.monotonic() + TIMEOUT)
    except GuestError as e:
        return False, f'@{handle}:{e}'
    count = sum(1 for s in payload['results'] if _well_formed(s))
    if payload['results'] and not count:
        return False, f'@{handle}:公开接口的回包换了形状'
    return True, f'公开接口可用,@{handle} 读到 {count} 条'
