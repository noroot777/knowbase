# -*- coding: utf-8 -*-
"""不登录通道:不碰任何 X 账号、cookie 或浏览器,每一步挑最合适的那件工具。

**硬约束在 `docs/decisions/x-channels.md` 第一节 —— 改这块之前先读那一份。**

两件工具,各有射程(2026-11-03 实测):
- **FxTwitter 公开接口**(`/2/profile/<handle>/statuses`):按页倒序给完整时间线,带
  `cursor.bottom` 往前翻;长推(note tweet)的 `text` 是全文、t.co 已展开。拿近五天五个
  账号和登录通道入库的对过一遍:登录通道收到的它几乎都有,另外还多出一些。代价:它是
  第三方公共实例,账号名和推文地址会发给它;**同一个号偶发 404**(一分钟里 404 / 200
  交替出现),所以 404 按临时故障重试,不许当成「账号没有推文」。
- **X 自己的未登录页面**(`x_guest_page`,一次 HTTP,不起浏览器):第一方,但个人主页只露
  最新四五条、正文是截断预览;单条详情页是全文。

按步骤分配:
- **列时间线**:FxTwitter 为主(只有它翻得到五天下界);它问不到时退到 X 主页,收下露出来
  的那几条(低频的号光凭这几条就能证明读到了下界,多产的号如实记「没读完」)。
- **补截断正文**(只出现在退到 X 主页的那一轮):X 详情页为主(第一方全文,此时 FxTwitter
  多半正问不到),FxTwitter 单条接口兜底;两头都拿不到就扣留这一条,不拿预览冒充全文。
- **长文**:只有 FxTwitter 拿得到(X 的文章页不登录是登录墙),交给 `x_article.fill`。

**两件工具都问不到才算这个号这一轮失败**,原话两段都留着。

**入库口径跟浏览器通道对齐**:只收这个号自己发的(转推不收,作者不是这个号的不收);
只有引用、自己一个字没写的跳过(不拿被引用者的话冒充他的正文)。

**「读到下界」的证明口径同 `x_browser._old_enough`**:按给出的顺序,连续 `_OLD_STREAK`
条他自己的推都不新于下界才算;任何一条新于下界的条目都重置计数(转推带的是原推时间,
它很新足以证明还没翻过去,很老却证明不了什么),置顶既不计数也不重置。翻到时间线尽头
(没有下一页)也算读完。别的情况 —— 翻页上限、时限、停止、中途请求失败 —— 都是没读完,
如实写进 `incomplete`,水位不动。
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from kb.feeds import x_article, x_body, x_guest_page, x_post_meta
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


def _build(url: str, when: datetime, text: str, name: str, *, quoted: bool, article: str) -> dict | None:
    """一条自己发的推文 → feed_items 的条目形状(口径同 `x_browser._entry`)。两件工具共用。"""
    text = text.strip()
    if not text and quoted and not article:
        return None                                  # 只有引用:不拿别人的话当他的正文
    out = {'url': url, 'title': (text or x_body.placeholder(name))[:500], 'body': text,
           'author': name, 'published': when.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M'),
           'content_kind': 'post'}
    if article:
        # 时间线上那份 article 没有正文(FxTwitter 给空块,X 主页只有一张卡片),只当信号用。
        out[x_article.HINT] = article
    out = x_post_meta.stamp(out, 'post', '', author_text=x_post_meta.AuthorText(text, 'tweet.text'))
    return x_body.record(out, text)


def _entry(status: dict) -> dict | None:
    url = _url(status)
    when = _when(status)
    if not url or not when:
        return None
    article = status.get('article')
    return _build(url, when, status.get('text') or '',
                  str(status['author'].get('name') or status['author']['screen_name']),
                  quoted=bool(status.get('quote')),
                  article=str(article.get('id') if isinstance(article, dict) else article) if article else '')


def _scan_api(handle: str, floor: datetime, *, deadline: float, should_stop=None) -> dict:
    """经 FxTwitter 把 `handle` 的时间线从最新一路读到 `floor`(排他)。

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


def _api_status(handle: str, ident: str, deadline: float) -> str:
    """FxTwitter 单条接口给的全文。问不到或读不懂抛 `GuestError`。"""
    try:
        resp = request(f'{API}/{quote(handle)}/status/{ident}', {'Accept': 'application/json'},
                       deadline=min(deadline, time.monotonic() + TIMEOUT))
        tweet = json.loads(resp.body.decode('utf-8', 'replace')).get('tweet')
    except (FeedError, OSError, ValueError, UnicodeError, RecursionError, AttributeError) as e:
        raise GuestError(f'公开接口没答复({re.split(r"[;；]", str(e))[0].strip()})') from None
    if not isinstance(tweet, dict) or str(tweet.get('id')) != ident or not isinstance(tweet.get('text'), str):
        raise GuestError('公开接口的回包里没有这条推文')
    return tweet['text']


def _full_text(post: x_guest_page.Post, deadline: float) -> str:
    """截断预览的全文:X 详情页为主,FxTwitter 单条兜底。都拿不到抛 `GuestError`。"""
    try:
        return x_guest_page.detail(post.author, post.ident, deadline).text
    except x_guest_page.PageError as page:
        try:
            return _api_status(post.author, post.ident, deadline)
        except GuestError as api:
            raise GuestError(f'{page};{api}') from None


def _scan_page(handle: str, floor: datetime, *, deadline: float, api_error: str, should_stop=None) -> dict:
    """FxTwitter 问不到时的后路:读 X 未登录主页露出来的那几条。"""
    try:
        shown = x_guest_page.timeline(handle, deadline)
    except x_guest_page.PageError as e:
        return {'entries': [], 'incomplete': '', 'skipped_quotes': 0, 'notice': '',
                'error': f'{api_error};改读 X 未登录主页也没读成:{e}'}
    entries, streak, done, skipped, held = [], 0, False, 0, []
    for post in shown:
        if post.author.lower() != handle.lower():
            continue
        if post.when <= floor:
            if not post.pinned:
                streak += 1
                done = done or streak >= _OLD_STREAK
            continue
        if not post.pinned:
            streak = 0
        text = post.text
        if post.truncated:
            if should_stop and should_stop() or time.monotonic() + MIN_USEFUL >= deadline:
                held.append('时限用完')
                continue
            try:
                text = _full_text(post, deadline)
            except GuestError as e:
                held.append(str(e))
                continue
        try:
            entry = _build(post.url, post.when, text, post.name, quoted=post.quoted, article=post.article)
        except PostUnavailable:
            entry = None
        if entry is None:
            skipped += post.quoted
            continue
        entries.append(entry)
    notes = []
    if not done:
        notes.append(f'公开接口没答复,改读 X 未登录主页只露出最新 {len(shown)} 条,没读到这一轮的下界')
    if held:
        notes.append(f'{len(held)} 条推文的全文没取到({held[0]}),留到下次同步再试')
    entries.sort(key=lambda e: e['published'])
    return {'entries': entries, 'incomplete': ';'.join(notes), 'error': '', 'skipped_quotes': skipped,
            'notice': f'{api_error},这一轮改读了 X 未登录主页'}


def scan(handle: str, floor: datetime, *, deadline: float, should_stop=None) -> dict:
    """把 `handle` 的时间线从最新一路读到 `floor`(排他)。FxTwitter 为主,X 主页为后路。

    返回 `{'entries', 'incomplete', 'error', 'skipped_quotes', 'notice'}`:
    - `error` 非空 = 两件工具都没读成,这个号这一轮什么也没读到;
    - `incomplete` 非空 = 读了一部分,条目照收,但没读到下界(调用方不许推进水位);
    - `notice` = 纯提示(这一轮走了后路),不影响控制逻辑。
    """
    got = _scan_api(handle, floor, deadline=deadline, should_stop=should_stop)
    if not got['error']:
        return got | {'notice': ''}
    if should_stop and should_stop():
        return got | {'notice': ''}
    return _scan_page(handle, floor, deadline=deadline, api_error=got['error'].removeprefix(f'@{handle}:'),
                      should_stop=should_stop)


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
            results[handle] = {'entries': [], 'error': f"@{handle}:{got['error'].removeprefix(f'@{handle}:')}"}
            continue
        entries, note = got['entries'], got['incomplete']
        if complete_articles:
            entries, missing = x_article.fill(entries, deadline=None if deadline == float('inf') else deadline,
                                              should_stop=should_stop)
            note = f'{note};{missing}' if note and missing else note or missing
        newest = max((e['published'] for e in entries), default='')
        results[handle] = {'entries': entries, 'error': '', 'incomplete': note,
                           'watermark': floor.isoformat() if note or not newest else newest}
        notice = [got['notice']] if got['notice'] else []
        if got['skipped_quotes']:
            notice.append(f"{got['skipped_quotes']} 条只有引用、本人没写字的推文没有收")
            results[handle]['skipped_quotes'] = got['skipped_quotes']
        if notice:
            results[handle]['note'] = ';'.join(notice)
    return results


def probe(handle: str) -> tuple[bool, str]:
    """「检查连接」:两件工具各真问一次。任何一件能用就算就绪,两件的结论都报。"""
    deadline = time.monotonic() + TIMEOUT
    if not API:
        api = '公开接口的地址被设成了空(KB_X_GUEST_API)'
    else:
        try:
            payload = _page(handle, '', deadline)
            count = sum(1 for s in payload['results'] if _well_formed(s))
            api = '' if count or not payload['results'] else '公开接口的回包换了形状'
            api_ok = f'公开接口可用(@{handle} 读到 {count} 条)'
        except GuestError as e:
            api = str(e)
    try:
        shown = x_guest_page.timeline(handle, time.monotonic() + TIMEOUT)
        page, page_ok = '', f'X 未登录主页可用(露出 {len(shown)} 条)'
    except x_guest_page.PageError as e:
        page = str(e)
    parts = [api or api_ok, page or page_ok]
    return (not api or not page), ';'.join(parts)
