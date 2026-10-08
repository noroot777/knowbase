# -*- coding: utf-8 -*-
"""浏览器通道:用用户自己的 X 登录态,从时间线上按推文时间取增量。

**边界是推文时间,不是页数**。X 的 profile 页是虚拟列表(滚过去的节点会被回收),
所以"抓到第几条"没有稳定含义;而每条推文身上都带着 `<time datetime>`,那是
这条路上唯一可靠的游标。每个订阅记一个水位(上次抓到的最新推文时间),这一轮
滚到比水位更老就停。

**为什么不是「看到一条老的就停」**:profile 页顶上可能钉着一条几年前的置顶推文,
转推显示的又是**原推**的时间 —— 两者都会在新推文中间插进一条很老的时间。所以
判据是「连续 `_OLD_STREAK` 条都老于水位」,并且置顶那条**不参与**判定。

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

from kb.feeds.x_cdp import Chrome, CdpError

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

# 页面里的收集器:挂在 window 上跨多次 eval 累积,因为虚拟列表会把滚过去的
# 节点回收掉 —— 滚完再统一取就只剩最后一屏。按推文 id 去重。
_COLLECT_JS = r'''
(() => {
  const S = window.__kbX = window.__kbX || {seen: new Map()};
  const norm = s => (s || '').replace(/ /g, ' ').trim();
  let parsed = 0, articles = 0;
  for (const art of document.querySelectorAll('article[data-testid="tweet"]')) {
    articles++;
    const time = art.querySelector('time[datetime]');
    const anchor = art.querySelector('a[href*="/status/"]');
    if (!time || !anchor) continue;
    const m = anchor.getAttribute('href').match(/^\/([^\/]+)\/status\/(\d+)/);
    if (!m) continue;
    parsed++;
    const ctx = norm(art.querySelector('[data-testid="socialContext"]')?.innerText);
    const text = norm(art.querySelector('[data-testid="tweetText"]')?.innerText);
    // 作者块的第一行是显示名,后面跟着 @handle 和时间,只取第一行。
    const who = norm(art.querySelector('[data-testid="User-Name"]')?.innerText).split('\n')[0];
    const id = m[2];
    const prev = S.seen.get(id);
    // 同一条推文会在多次滚动里重复出现;文本偶尔首次渲染不全(折叠/懒加载),
    // 所以后出现的更长文本覆盖前一次。
    if (!prev || (text && text.length > (prev.text || '').length)) {
      S.seen.set(id, {id, author: m[1], name: who, datetime: time.getAttribute('datetime'),
                      text, context: ctx, url: `https://x.com/${m[1]}/status/${id}`});
    }
  }
  return JSON.stringify({articles, parsed, items: [...S.seen.values()]});
})()
'''

_RESET_JS = 'window.__kbX = {seen: new Map()}; window.scrollTo(0, 0); true'
_SCROLL_JS = 'window.scrollBy(0, Math.round(window.innerHeight * 0.85)); true'
# 登录态失效的几种落地页。X 会跳 /i/flow/login,也会在原地渲染登录按钮。
_STATE_JS = r'''
(() => {
  const txt = (document.body?.innerText || '').slice(0, 4000);
  return JSON.stringify({
    url: location.href,
    ready: document.readyState,
    articles: document.querySelectorAll('article[data-testid="tweet"]').length,
    login: !!document.querySelector('[data-testid="loginButton"], [data-testid="login"]')
           || /^\/i\/flow\/login/.test(location.pathname),
    empty: /Hmm\.\.\.this page doesn|这个页面不存在|Account suspended|账号已被冻结|doesn’t exist|does not exist/i.test(txt),
    limited: /Rate limit exceeded|Something went wrong\. Try reloading|请稍后再试/i.test(txt),
  });
})()
'''


class XBrowserError(RuntimeError):
    """浏览器通道的失败。message 是成品文案。"""


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
    try:
        return datetime.fromisoformat(iso.replace('Z', '+00:00')).astimezone(timezone.utc)
    except (ValueError, AttributeError):
        return None


def _is_pinned(context: str) -> bool:
    return any(k in context for k in ('Pinned', '置顶'))


def _is_promoted(context: str) -> bool:
    return any(k in context for k in ('Promoted', '推广', '赞助'))


def _countable(item: dict, handle: str) -> bool:
    """这一条算不算「这个账号时间线上的推文」。翻到底的计数和入库共用这一个判据。

    三种不算:
    ①**置顶** —— 每天都在那儿、发布时间又很老。算进"翻到底"的连续计数会让每一轮
      都在第一屏就收手;收进库里就是每天多一条"新动态",日报那边还会把它当"补录"
      反复提。
    ②**推广位** —— 它是广告,不是这个人的观点。
    ③**作者不是这个号、又没有转推那一行上下文** —— 那它不是从这个账号的时间线里
      长出来的(推荐位、"你可能喜欢")。按这个订阅记进去就是把别人的推文当成这个
      大V的观点,和 feedgrab 那一侧按 `/<handle>/status/` 过滤是同一条规矩。
      转推一定带着一行 socialContext,所以这么判不会把转推丢掉 —— 而转推正是
      "他此刻在想什么"的一部分,正文里保留那行上下文(见 `_entry`)。
    """
    context = (item.get('context') or '').strip()
    if _is_pinned(context) or _is_promoted(context):
        return False
    foreign = handle and (item.get('author') or '').lower() != handle.lower()
    return not (foreign and not context)


def _entry(item: dict) -> dict | None:
    """一条推文 → feed_items 的条目形状。没正文也没链接的不要。"""
    published = _as_published(item.get('datetime', ''))
    text = (item.get('text') or '').strip()
    if not item.get('url') or not (text or published):
        return None
    name = item.get('name') or item.get('author') or ''
    context = (item.get('context') or '').strip()
    # 转推/引用的上下文留在正文开头一行:它决定了这条是不是本人的观点,
    # 而 feed_items 里没有地方放这个标记。
    body = f'> {context}\n\n{text}' if context and not _is_pinned(context) else text
    return {'url': item['url'], 'title': (text or f"{name} 的动态")[:500],
            'body': body, 'author': name, 'published': published,
            'content_kind': 'post'}


def _wait_state(page: Chrome, handle: str, deadline: float) -> dict:
    """等页面落定,顺带认出登录失效/不存在/限流这三种不该当成"没有推文"的情况。"""
    last = {}
    while time.monotonic() < deadline:
        last = json.loads(page.eval(_STATE_JS))
        if last['login']:
            raise XBrowserError('X 登录态已失效,请在浏览器通道设置里重新登录一次')
        if last['limited']:
            raise XBrowserError('X 暂时限制了访问频率,请稍后重试或降低同步频率')
        if last['empty']:
            raise XBrowserError(f'@{handle} 这个账号在 X 上打不开(可能已改名、注销或被冻结)')
        if last['articles'] and last['ready'] == 'complete':
            return last
        time.sleep(0.3)
    if last.get('ready') != 'complete':
        raise XBrowserError(f'@{handle} 的页面没有加载完')
    raise XBrowserError(f'@{handle} 的时间线上没有读到推文')


def _harvest_one(page: Chrome, handle: str, floor: datetime | None) -> list[dict]:
    """抓一个账号,返回时间上不早于 floor 的条目(置顶那条例外,见下)。"""
    page.goto(f'https://x.com/{handle}')
    deadline = time.monotonic() + ACCOUNT_SECONDS
    _wait_state(page, handle, deadline)
    page.eval(_RESET_JS)
    seen_articles = parsed = 0
    items: list[dict] = []
    for _ in range(MAX_SCROLLS):
        snap = json.loads(page.eval(_COLLECT_JS))
        seen_articles, parsed, items = snap['articles'], snap['parsed'], snap['items']
        if floor and _old_enough(items, floor, handle):
            break
        if time.monotonic() > deadline:
            break
        before = len(items)
        page.eval(_SCROLL_JS)
        if not _settle(page, before, deadline):
            break                                  # 滚不出新东西了 = 到底了
    if seen_articles and not parsed:
        # 有推文卡片却一个字段都解析不出来 —— 这是 X 改了 DOM,不是"没有推文"。
        # 两者必须分开:后者安静跳过是对的,前者安静跳过会让这条通道从某天起
        # 永远返回空,而面板上一切正常。
        raise XBrowserError(f'@{handle} 的页面结构看不懂了(读到 {seen_articles} 条卡片,'
                            '一条都解析不出来),X 可能改版了')
    return _select(items, floor, handle)


def _old_enough(items: list[dict], floor: datetime, handle: str = '') -> bool:
    """按时间倒序看最后几条:连续 _OLD_STREAK 条老于 floor 才算翻到底。

    只数"这个账号时间线上的推文"(见 `_countable`):置顶、推广和推荐位的时间
    跟这条时间线的进度没有关系。
    """
    ordered = sorted((i for i in items if _countable(i, handle)),
                     key=lambda i: i.get('datetime') or '', reverse=True)
    tail = ordered[-_OLD_STREAK:]
    if len(tail) < _OLD_STREAK:
        return False
    return all((dt := _utc(i.get('datetime', ''))) and dt <= floor for i in tail)


def _settle(page: Chrome, before: int, deadline: float) -> bool:
    """等懒加载吐出新推文。返回 False = 等满了也没有新的。"""
    end = min(time.monotonic() + _SETTLE_SECONDS, deadline)
    while time.monotonic() < end:
        time.sleep(_SETTLE_STEP)
        if len(json.loads(page.eval(_COLLECT_JS))['items']) > before:
            return True
    return False


def _select(items: list[dict], floor: datetime | None, handle: str = '') -> list[dict]:
    """落到 feed_items 之前的最后一道:去掉不属于这条时间线的和过老的,转成条目形状。

    哪些不属于见 `_countable`(置顶、推广、推荐位)—— 判据和"翻到底"那一步共用
    一份,两处各判各的话,被这里丢掉的条目还在那边顶着连续计数。
    """
    out = []
    for item in sorted(items, key=lambda i: i.get('datetime') or ''):
        if not _countable(item, handle):
            continue
        when = _utc(item.get('datetime', ''))
        if floor and when and when <= floor:
            continue
        if entry := _entry(item):
            out.append(entry)
    return out


def harvest(jobs: list[tuple[str, str]], *, profile: str, chrome_path: str = '',
            budget: float = 0.0, on_progress=None, should_stop=None) -> dict[str, dict]:
    """一次会话抓完所有账号。

    jobs:  [(handle, 水位 ISO 或空串)]
    返回:  {handle: {'entries': [...], 'error': '', 'watermark': ISO}}

    整批共用一个 Chrome;任何一个账号的失败只落在它自己那一项上,不影响其余
    ——这条通道一轮跑几十个账号,一个改名的号不该把整轮带掉。
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
    try:
        with Chrome(profile, headless=True, chrome_path=chrome_path) as page:
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
                try:
                    entries = _harvest_one(page, handle, floor)
                    results[handle] = {'entries': entries, 'error': '',
                                       'watermark': _newest(entries, water)}
                except (XBrowserError, CdpError) as e:
                    results[handle] = {'entries': [], 'error': str(e)}
    except (CdpError, EOFError, OSError) as e:
        # 浏览器整个起不来/中途死了:剩下的账号统一记成同一个原因,
        # 而不是逐个报一条看不出共同根因的错。
        reason = str(e) if isinstance(e, CdpError) else f'浏览器会话中断:{e}'
        for handle, _ in jobs:
            results.setdefault(handle, {'entries': [], 'error': reason})
    return results


def _newest(entries: list[dict], fallback: str) -> str:
    """这一轮抓到的最新推文时间,作为下一轮的水位。

    **一条都没抓到时沿用旧水位**,不要改成"现在":那等于把这段时间宣布为已抓过,
    而这一轮可能只是没滚到(预算用完、X 限流)。水位只前进到确实见过的那条推文。
    """
    stamps = [e['published'] for e in entries if e.get('published')]
    if not stamps:
        return fallback
    newest = max(stamps)
    try:
        local = datetime.strptime(newest, '%Y-%m-%d %H:%M').replace(tzinfo=SHANGHAI)
    except ValueError:
        return fallback
    return local.astimezone(timezone.utc).isoformat()


def open_login_window(profile: str, chrome_path: str = '') -> str:
    """开一个**有头**窗口让用户自己登录 X,起完就不管了。

    这一步必须有头、也必须是用户点出来的:输密码、过两步验证、认人机校验都
    得他本人做。起完立刻返回 —— 等他登录完是几分钟的事,一个 HTTP 请求不该
    挂在那里。登录态落在 profile 目录里,之后的定时抓取无头复用它。

    **用的是一个独立 profile,不是用户日常那个 Chrome**。连日常浏览器要开调试
    端口(本机任何进程都能接管它),而且日常窗口一关、或者 Chrome 一重启,定时
    抓取就断了;独立 profile 里登录一次,之后每天无头跑,互不干扰。代价是要在
    这个窗口里再登录一次。
    """
    from kb.feeds.x_cdp import find_chrome
    binary = find_chrome(chrome_path)
    os.makedirs(profile, exist_ok=True)
    if os.path.lexists(os.path.join(profile, 'SingletonLock')):
        raise XBrowserError('登录窗口已经开着了,请在那个窗口里完成登录')
    try:
        subprocess.Popen(
            [binary, f'--user-data-dir={profile}', '--no-first-run',
             '--no-default-browser-check', 'https://x.com/login'],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        raise XBrowserError(f'打不开登录窗口:{e}') from None
    return ('已打开登录窗口。在里面登录你要用的 X 账号,登录成功后**关掉这个窗口**'
            '(抓取时要独占这个 profile),再回来点一次「检查连接」。')


def probe(profile: str, chrome_path: str = '') -> tuple[bool, str]:
    """真开一次无头浏览器,看这份登录态还读不读得到时间线。

    用户点「检查连接」才走这条,所以允许慢(几秒到几十秒)。它和抓取用的是
    同一个 profile、同一条 CDP 路径,所以结论是真的;`x_channels.readiness`
    那道便宜判断只看 Cookies 文件在不在,而**cookie 过期在文件上看不出来**。
    """
    if os.path.lexists(os.path.join(profile, 'SingletonLock')):
        return False, '这个 profile 正被另一个窗口占着(登录窗口还开着?)。关掉它再试'
    try:
        with Chrome(profile, headless=True, chrome_path=chrome_path) as page:
            page.open_page('about:blank')
            page.goto('https://x.com/home')
            deadline, last = time.monotonic() + ACCOUNT_SECONDS, {}
            while time.monotonic() < deadline:
                last = json.loads(page.eval(_STATE_JS))
                if last['login']:
                    return False, 'X 登录态已失效,点「打开登录窗口」重新登录一次'
                if last['limited']:
                    return False, 'X 暂时限制了访问频率,稍后再试'
                if last['articles'] and last['ready'] == 'complete':
                    return True, f'登录态有效,首页读到 {last["articles"]} 条推文'
                time.sleep(_SETTLE_STEP)
            if last.get('ready') != 'complete':
                return False, 'X 首页没有加载完 —— 网络不通,或者这台机器访问 X 要走代理'
            return False, 'X 首页打开了,但一条推文都没读到。登录态可能刚刚失效'
    except (XBrowserError, CdpError) as e:
        return False, str(e)
    except (EOFError, OSError) as e:
        return False, f'浏览器会话中断:{e}'
