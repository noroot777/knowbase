# -*- coding: utf-8 -*-
"""邮件简报:每期一个汇总页,页里并列着几十条带外链的简讯。

**硬约束在 `docs/decisions/digest.md` 第一节 —— 改这块之前先读那一份。**

TLDR AI 是典型形态,而它**三条路都堵死了**,所以这条通道不是偷懒:

- **官方 RSS 有,但没有正文。** `tldr.tech/api/rss/ai` 返回 200,20 条,可
  `<item>` 里**连 `description` 元素都没有** —— 每条代表一整期,标题是当期
  几个要点拼起来的。按它入库等于只收了一份期号清单。
- **没有"最新一期"的固定地址。** `tldr.tech/ai` 是营销页(Sign Up / Join
  1,100,000 readers),0 个并列块、0 个期号链接。
- **不能按日期猜地址。** `tldr.tech/ai/<还没发的日期>` 返回 **200 + 那张营销
  页**,不是 404 —— 没法靠状态码探测哪一期存在。

于是"有哪些期"只能从 RSS 拿、"正文"只能从每期落地页拿:**两跳绕不过去**
(社区那个 `Bullrich/tldr-rss` 也正是这么干的)。而 `fetch_source` 那套
`endpoints()` 是回落链(第一个成功就 return)、`parse()` 是纯函数,整条路只
发一次 HTTP —— 所以这条通道走**预取**,形状照抄 X 的登录态通道:在
`sync.collect` 开头整批抓完,`fetch_source` 开头按订阅 `take()` 自己那一份。

**拆块的判据不复用 `page_list`。** 那边按"链接的记录容器"算,而容器是"祖先
链上最后一个只属于它的节点" —— TLDR 的 19 个 `<article>` 里有 3 个带第二个
链接,于是 `seen>1` 让容器停在 `<a>` 上,**正文(在同级 `<div>` 里)整个被排除
在外**(实测)。这里要的是另一种形状:**一页上并列的、结构路径逐字相同的重复
块**,每块 = 一个外链 + 一段够长的文字。实测 TLDR 那一页上满足这个条件的组
**只有一个**(19 个 article),连歧义都没有。

正文直接取自落地页,**所以这条通道不需要补抓**:简讯是 TLDR 自己写的
128~615 字,而那个外链指向的是**别人家的原文** —— 真去抓它,收回来的是别人
的文章,不是这条简讯。
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import date
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from kb.feeds import readable
from kb.feeds.fetching import FeedError, canonical_url, parse_feed, plain, request
from kb.source_types import feed_types

# 一组至少这么多块才算"并列的简讯区"。两块太容易是巧合(页头/页脚常有成对的
# 卡片),而简报的一期动辄十几条。
MIN_ITEMS = 3
# 每块至少这么多字。导航、面包屑、页脚链接的块都够不着,而最短的一条真简讯
# 实测 128 字。
MIN_BODY = 80
MAX_BODY = 4000
MAX_TITLE = 300
# 一轮最多抓几期。简报一天一期,日常同步只会多出一两期;这个上限是给"停机很
# 久之后第一次跑"兜底的 —— 每期一次 HTTP,不限的话 20 期能把采集预算吃光。
# **截断只许截掉新的那一头,所以读取顺序必须是从旧到新**(见 `_harvest_one`)。
MAX_ISSUES = 6
# 一组里至少这么大比例的块合格,才算"并列的简讯区"。**不要求每一块都合格**:
# 实测 2026-10-08 那一期里夹着一条 TLDR 自家的广告(链接指向 `advertise.tldr.tech`,
# 是站内子域、过不了"外链"那一关),按"每块都要合格"判,**整整一期 18 条一条都
# 拆不出来** —— 而那条广告本来就不该收。合格的收下、不合格的跳过,比例这道闸
# 负责挡住"恰好有几个外链的导航区"。
MIN_SHARE = 2 / 3
# 广告位。认的是**站点自己的声明**,不是某个域名 —— TLDR 把赞助位的标题结尾
# 标成 `(Sponsor)`。维护一张域名名单会漂,而这几个措辞是通行写法。
# **招聘位不特判**:TLDR 每期固定有一条(`jobs.ashbyhq.com`),可它没有任何
# 稳定标记,靠招聘平台域名去认就又成了一张要维护的名单 —— 多收一条的代价,
# 比"哪天换个平台就静默漏判"小。
_AD = re.compile(r'[(\[【（]\s*(?:sponsor(?:ed)?|ad|advertisement|广告|赞助)\s*[)\]】）]\s*$',
                 re.I)

_WATER_KEY = 'digest_issue'
# 两头都认不出日期的期号,读过之后按**地址**记在这儿 —— 日期水位记不住它们
# (见 `_wanted`)。**它的上限不是一个常数,是当前那份清单自己**(见
# `taken_patch`):存回去的永远是这一轮列出来的期号的子集,而清单本身由
# `fetching.parse_feed` 截到最近 500 项。
_SEEN_KEY = 'digest_undated'
# 期号地址里的日期。TLDR 的期号地址逐字就是 `/ai/2026-10-09`。
_URL_DAY = re.compile(r'(\d{4})-(\d{2})-(\d{2})')
_NO_DAY = '未标日期的一期'
_lock = threading.Lock()
_harvest: dict[int, dict] = {}


def supports(kind: str) -> bool:
    return feed_types()[kind].adapter == 'digest' if kind in feed_types() else False


def targets(sources: list[dict]) -> list[dict]:
    return [s for s in sources if supports(s['kind'])]


def watermark(source: dict) -> str:
    try:
        return (json.loads(source.get('config') or '{}') or {}).get(_WATER_KEY, '')
    except (ValueError, TypeError):
        return ''


def watermark_patch(value: str) -> dict:
    return {_WATER_KEY: value} if value else {}


def taken(source: dict) -> list[str]:
    """已经读过的那些**没有日期**的期号地址。"""
    try:
        got = (json.loads(source.get('config') or '{}') or {}).get(_SEEN_KEY) or []
    except (ValueError, TypeError):
        return []
    return [u for u in got if isinstance(u, str)] if isinstance(got, list) else []


def taken_patch(source: dict, took: list[str], listed: list[str]) -> dict:
    """把这一轮新读的无日期期号并进那份名单(和条目同一个事务提交)。

    **裁剪按「还在不在当前清单里」,不按容量。** 上限原来是 60 条、挤出去的
    按最旧淘汰,理由写的是"真挤出去只多重读一次" —— **那句话是错的,它会把
    真正未读的那几期饿死**:被淘汰的地址仍然列在 RSS 里,于是下一轮又算"未
    读",而无日期期号的排序键都是空串、恒排在最前面,它们先把 `MAX_ISSUES`
    那点额度用光。实测一份 67 期全无日期的清单(2026-10-10 第 3 轮审查):
    前 11 轮正常收到 198 条,第 12 轮起请求的又是已经读过的 `opaque-000`~
    `005`,新增恒为 0,末期 `opaque-066` 那三条**永远**不入库,而 `errors=[]`、
    面板一直写着「还有 1 期没读,下一轮同步继续」——「再等一轮」是假的,第 22
    轮就回到了第 11 轮的状态,这个清单下它永远轮不到。
    **把 60 改大治不了**:67 期没越过任何已有输入上限(`parse_feed` 收最近
    500 项),换个数字只是把同一个循环推到另一个长度上。

    所以判据换成「当前清单的子集」:仍然列着的已读身份一个都不丢(未读集合
    于是**单调缩小**,每一轮都在往前走),滚出清单的自动掉出去 —— 存储照旧
    有界,界是清单自己而不是一个要维护的常数。清单这一轮没拿到(空的)时
    一条都不裁:那时我们对"谁还列着"没有任何证据,裁掉等于把整份已读状态
    按一次 feed 抖动清零(代价只是重读,但没必要)。
    """
    had = taken(source)
    keep = [u for u in had if u in set(listed)] if listed else had
    out = keep + [u for u in took if u not in set(keep)]
    return {} if out == had else {_SEEN_KEY: out}


def _tag_path(node) -> tuple:
    parts = []
    while node is not None and getattr(node, 'name', None):
        parts.append(node.name)
        node = node.parent
    return tuple(reversed(parts))


def _outbound(node, host: str) -> str:
    """这一块指向站外的那个链接。简报的每一条都是"荐读别人家的文章"。"""
    for a in node.find_all('a', href=True):
        href = (a.get('href') or '').strip()
        if not href.startswith(('http://', 'https://')):
            continue
        try:
            url = canonical_url(href)
        except ValueError:
            continue
        other = (urlsplit(url).hostname or '').lower()
        if other and host and (other == host or other.endswith('.' + host)):
            continue
        if other:
            return url
    return ''


def _title_of(node) -> str:
    for tag in node.find_all(['h1', 'h2', 'h3', 'h4', 'h5', 'h6']):
        if text := ' '.join(tag.get_text(' ').split()):
            return text[:MAX_TITLE]
    if link := node.find('a'):
        if text := ' '.join(link.get_text(' ').split()):
            return text[:MAX_TITLE]
    return ''


def _is_ad(title: str) -> bool:
    return bool(_AD.search(title))


def split_issue(body: bytes, url: str, published: str,
                headers: dict | None = None) -> list[dict]:
    """一期的落地页 → 条目。字段与 `fetching.parse_feed` 一致。

    **一条都拆不出来一律抛异常。** 口径同 `page_list`:静默返回空会让这条
    订阅从某天起永远是"今天没有新内容",而面板上一切正常 —— 站点改版和
    "这一期还没发"(落地页回落成营销页)都是这个形态,两种都要人看一眼。
    """
    if why := readable.html_problem(body, headers):
        raise FeedError(why)
    soup = BeautifulSoup(readable.decode(body, headers), 'html.parser')
    for tag in soup(['script', 'style', 'noscript', 'template']):
        tag.decompose()
    host = (urlsplit(url).hostname or '').lower()
    buckets: dict[tuple, list] = {}
    for node in soup.find_all(True):
        buckets.setdefault(_tag_path(node), []).append(node)
    best = None
    for path, nodes in buckets.items():
        if len(nodes) < MIN_ITEMS:
            continue
        picked = [n for n in nodes
                  if len(' '.join(n.get_text(' ').split())) >= MIN_BODY
                  and _outbound(n, host)]
        if len(picked) >= MIN_ITEMS and len(picked) >= len(nodes) * MIN_SHARE:
            # 路径最短 = 最外层。**如实记一笔:这一句今天是条确定性兜底,
            # 不是防住了某个已知事故。** 它本来是冲着"`<article>` 和它里面
            # 的 `<a>` 都合格、而只有外层那个含正文"去的,可 `_outbound` 用
            # `find_all` 找**后代**,`<a>` 自己不算自己的后代 —— 于是内层那个
            # 链接节点永远过不了"有外链"这一关(定点注入"取最内层"实测红 0
            # 条,照着这个形状造样本也触发不到)。留着是为了真有两层都合格时
            # 结果可复现,别让它取决于 `find_all` 的遍历顺序。同样长时取块数
            # 多的那个。
            key = (len(path), -len(picked))
            if best is None or key < best[0]:
                best = (key, picked)
    if best is None:
        raise FeedError('这一页上没有并列的简讯块 —— 可能这一期还没发布（落地页会回落'
                        '成订阅介绍页），也可能站点结构变了，需要适配')
    entries, seen = [], set()
    for node in best[1]:
        title = _title_of(node)
        link = _outbound(node, host)
        if not title or not link or link in seen or _is_ad(title):
            continue
        seen.add(link)
        text = plain(str(node))
        # 正文去掉开头那一行标题:面板上标题就显示在正文正上方,留着就是同一句
        # 话读两遍(口径同 `page_list.shell_body`)。
        if text.startswith(title):
            text = text[len(title):].lstrip(' ·—-\n')
        body_text = (text or title)[:MAX_BODY]
        entries.append({'url': link, 'title': title, 'body': body_text,
                        'author': '', 'published': published,
                        'content_kind': 'summary'})
    if not entries:
        raise FeedError('这一页上有并列的块，但一条简讯都没解析出来 —— 站点结构'
                        '可能变了，需要适配')
    return entries


def _url_day(url: str) -> str:
    """从期号地址里认一个日期,认不出来返回空串。

    **feed 漏了 `pubDate` 时这是第二个证据源**,而且对这条通道特别靠得住:
    期号地址本来就是按日期编的(TLDR 逐字是 `/ai/2026-10-09`)。

    **只看 path 的最后一段。** 认出来的日期是**会把这一期排除掉**的
    (`_wanted` 拿它比水位和下界),所以认错一个就是静默丢一期 —— 而"最后一段"
    是这条判据里唯一能当身份用的位置:`/archive/2026-10-09/latest` 那种把日期
    放在中间的路径,写死"扫整条 path 取最右边"会把归档目录名当成期号日期。认不
    出来**不编一个**,交给 `_wanted` 那条"照收" —— 一个认不出日期的期号会被收下
    (多付一次 HTTP),一个认错日期的期号会**永久消失**,两个方向差着一个量级。
    只认真日历日(`date()` 构造不出来的不算,比如 2 月 30 日);一段里有多个时
    取最右边那个(`tldr-2026-10-09` 这类带前缀的 slug 也认得出来)。
    """
    last = (urlsplit(url).path.rstrip('/').rsplit('/', 1) or [''])[-1]
    best = ''
    for m in _URL_DAY.finditer(last):
        try:
            date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        best = m.group(0)
    return best


def issues(body: bytes, url: str) -> list[tuple[str, str]]:
    """简报 RSS → `[(这一期的地址, 发布时间)]`。

    **feed 没给日期时从期号地址里认一个**(`_url_day`)。日期在这条通道上是
    承重的:选期、历史下界、增量水位全靠它,而缺一期日期的后果曾经是那一期
    **静默消失**(见 `_wanted`)。口径只有这一份 —— 两个分支各判一次的话,
    区间补抓和日常同步看到的期号清单会各漂一头。
    """
    out = []
    for entry in parse_feed(body, url):
        if entry['url']:
            out.append((entry['url'], entry['published'] or _url_day(entry['url'])))
    return out


def prefetch(sources: list[dict], settings: dict, *, budget: float = 0.0,
             since: str = '', until: str = '',
             on_progress=None, should_stop=None) -> dict:
    """整批抓完放进本轮缓存。失败落在它自己那一项上,不抛。

    **`since`/`until` 是区间补抓那段日期**(`FetchScope.since`/`until`,日常
    同步两个都是空)。两件事都在这一处定,交给下游各自解释必然各漂一头:
    ①**按那段日期选期号、不看日常水位** —— 水位很可能已经停在那段日期之后,
    按它筛的话用户点名的那几期一期也翻不到;②**日常水位一步不动**(交回去的
    `watermark` 一律是空串,于是 `watermark_patch` 不产生补丁)—— 区间读的是
    用户点名的那段,拿它当增量进度就是宣布中间那段已经收过。
    `sync._save_result` 在 ranged 下重建 validators 那一句顺带是第二道。
    """
    picked = targets(sources)
    result: dict[int, dict] = {}
    deadline = time.monotonic() + budget if budget else None
    for source in picked:
        if should_stop and should_stop():
            break
        got = {'entries': [], 'watermark': watermark(source)}
        try:
            got.update(_harvest_one(source, deadline, on_progress, should_stop,
                                    since, until))
        except Exception as e:                                   # noqa: BLE001
            got['error'] = f'{type(e).__name__}: {e}'[:400]
        result[source['id']] = got
    with _lock:
        _harvest.clear()
        _harvest.update(result)
    return result


def _harvest_one(source: dict, deadline, on_progress, should_stop,
                 since: str = '', until: str = '') -> dict:
    ranged = bool(since and until)
    # 区间补抓绕过日常水位(见 `prefetch`),也不推进它。
    water = '' if ranged else watermark(source)
    response = request(source['url'], deadline=deadline)
    found = issues(response.body, response.url)
    if ranged:
        # 闭区间,按日期前缀比 —— 期号的时刻是 `parse_feed` 按项目时区格式化
        # 出来的 `YYYY-MM-DD HH:MM`,而用户点的是日期。
        todo = [(url, day) for url, day in found if day and since <= day[:10] <= until]
    else:
        # **只抓比水位新的那几期。** 水位是"收到哪一期了",不是"读到哪条简讯了"
        # —— 一期是原子的:它要么整页拆下来,要么这一轮没拿到。
        #
        # **历史下界也在这儿筛一道。** 它本来由 `sync._within_history` 在存盘
        # 那一步把条目丢掉,可这条通道是"一期一次 HTTP" —— 下界之前的期号无论
        # 如何都入不了库,抓它们纯属白付。而下面那句额度是从旧往新截的:新订阅
        # 的下界是"今天往回 `HISTORY_DAYS` 天",RSS 里却躺着 20 期,不筛的话第
        # 一轮六期全落在下界之前、一条都入不了库,要连抓三轮(一天半)才够到
        # 今天的内容。区间补抓不筛 —— 口径同 `_within_history`,那是用户点名的
        # 那段日期、本来就是这道闸的逃生口。
        #
        # 两个条件合在 `_wanted` 里,因为**日期认不出来的那一档必须一起判**:
        # 分开写的话它会被两边各剔一次,而那正是上一轮的事故。
        floor = (source.get('history_floor') or '')[:10]
        seen = set(taken(source))
        todo = [(url, day) for url, day in found if _wanted(url, day, water, floor, seen)]
    # **从旧到新读,额度只截掉新的那一头。** 水位是一个标量("收到哪一期了"),
    # 它只能**连续**推进 —— 中间跳过一期,那一期就从此任何一轮增量都选不到。
    # 所以 `MAX_ISSUES` 必须在排序**之后**截:照 RSS 的新到旧顺序先截再排,
    # 取到的是最新那几期、水位跟着推到最新,被截掉的旧期永久漏掉(实测停机
    # 攒下九期时 10-01~10-03 那三期一条都没报错就消失了,反复同步也补不回来,
    # 2026-10-10 审查)。代价是积压时先补旧的那几期,靠下一轮同步跟上 —— 比
    # 静默丢内容轻得多,而且剩了几期会在面板上说出来。
    todo.sort(key=lambda x: x[1])
    left, todo = todo[MAX_ISSUES:], todo[:MAX_ISSUES]
    entries, newest, failed, took = [], water, [], []
    for i, (url, day) in enumerate(todo):
        if should_stop and should_stop():
            left = todo[i:] + left
            break
        if on_progress:
            on_progress(url)
        try:
            page = request(url, deadline=deadline)
            entries.extend(split_issue(page.body, page.url, day, dict(page.headers)))
        except Exception as e:                                   # noqa: BLE001
            # 这一期没拿到就停在这儿:后面几期即使拿得到也不推水位,否则中间
            # 这一期从此任何一轮增量都选不到。
            failed.append(f'{day or _NO_DAY}: {type(e).__name__}')
            left = todo[i:] + left
            break
        if day:
            newest = max(newest, day) if newest else day
        else:
            # 日期水位记不住它(它没有日期可比),所以读成了就按地址记一笔。
            took.append(url)
    out = {'entries': entries, 'watermark': '' if ranged else newest}
    if not ranged:
        # 那份已读名单要按**当前清单**裁剪,所以把这一轮列出来的期号一起交
        # 回去(见 `taken_patch`)—— 只交"新读的那几个"的话,名单只增不减,
        # 一有容量上限就会把仍在清单里的已读身份挤掉,而那正是饿死的来源。
        out['listed'] = [u for u, _ in found]
        if took:
            out['taken'] = took
    note = _unread_note(left, ranged, failed)
    if not note and ranged and not todo and found:
        # **点名的那段日期一期都没选中,得说出为什么。** 期号清单只能从 RSS 拿
        # (见模块开头那三条堵死的路),而它只留最近几十期 —— 更早的日期拿不到
        # 期号地址,压根无从抓起。不说的话面板上就是一句"新增 0 条、同步已完成",
        # 和"筛选写错了把它排空了"长得一模一样(2026-10-10 审查报的正是后者)。
        oldest = min((day for _, day in found if day), default='')[:10]
        note = (f'这段日期不在简报还列着的 {len(found)} 期里（最早 {oldest}）——'
                f'期号清单只能从 RSS 拿，更早的已经滚出去了')
    if note:
        out['incomplete'] = note
    return out


def _wanted(url: str, day: str, water: str, floor: str, seen: set[str]) -> bool:
    """日常同步要不要读这一期。

    **两头都认不出日期的照收**(feed 没给、期号地址里也认不出来),口径同
    `sync._within_history`:它身上没有任何"很旧"或"已经收过"的证据,而丢掉是
    不可恢复的静默损失。原来空日期被下界和水位两个条件**一起**剔掉,而水位又
    照常推进到更新的那一期 —— 于是**那一期简报静默消失,源站之后把日期补上也
    补不回来**(水位已经越过它),面板上 `errors`/`partial`/`last_note` 全是空
    (2026-10-10 第 2 轮审查实测:三期里抽掉中间那期的 `pubDate`,新增 6 条、
    报"同步已完成",补回日期再同步新增 0 条、一期页面都不请求)。两个条件只修
    一边不够:新订阅带着非空下界、老订阅带着非空水位,各自都能单独把它剔掉。

    **收下之后按地址记一笔**(`seen`,由调用方从 `taken()` 取)。日期水位记不住
    一个没有日期的期号,不记的话它每一轮都被重读一次;更糟的是它不受水位管、
    排序时又恒排在最前面(空串最小),**所以一旦没有日期的期号多过一轮额度,
    排在额度之外的那些就永远读不到** —— 和这条规则本来要挡的事故同一个形状。
    """
    if not day:
        return url not in seen
    return (not water or day > water) and (not floor or day[:10] >= floor)


def _unread_note(left: list[tuple[str, str]], ranged: bool, failed: list[str]) -> str:
    """这一轮没读完的那句话(经 `take` → `summary['partial']` 上面板)。

    **剩了几期必须说出来**:日常那一侧水位停在读完的地方、下一轮自己接着读,
    可"攒了九期只读了六期"和"全读完了"在面板上不该长得一模一样;区间那一侧
    更要说 —— 它不推水位,下一轮不会自动接上,用户得按提示把起始日期改一下
    再补一次,否则重复点同一个区间永远只读最旧那几期。
    """
    notes = list(failed)
    if left:
        # 最早那个**有日期**的 —— 没有日期的期号排序时恒在最前面(空串最小),
        # 拿它去填"把起始日期改到 X"会给出一句空话。
        oldest = next((day[:10] for _, day in left if day), '')
        where = f'（最早 {oldest}）' if oldest else ''
        notes.append(f'指定区间里还有 {len(left)} 期没读{where}——'
                     f'把起始日期改到 {oldest} 再补一次' if ranged and oldest else
                     f'还有 {len(left)} 期没读{where}，下一轮同步继续')
    return '；'.join(notes)[:200]


def take(source: dict, settings: dict) -> tuple[list[dict], dict]:
    """取这条订阅在本轮预取里的那一份,形状同 `fetch_source` 的返回值。"""
    with _lock:
        got = _harvest.get(source['id'])
    if got is None:
        raise LookupError('简报这一轮没有抓到这条订阅')
    if got.get('error'):
        raise ValueError(got['error'])
    meta = {k: source.get(k, '') for k in ('etag', 'modified', 'resolved_url')}
    # 水位和那份无日期期号名单都走同一份 config 补丁,所以它们和条目在同一个
    # 事务里提交(`sync._save_result`)—— 分开提交的话,存盘失败那一轮"已经读过"
    # 已经落下了,而这一期的简讯一条都没入库。
    if patch := {**watermark_patch(got.get('watermark', '')),
                 **taken_patch(source, got.get('taken') or [],
                               got.get('listed') or [])}:
        meta['config_patch'] = patch
    if note := got.get('incomplete'):
        meta['note'] = note
    return got['entries'], meta


def reset() -> None:
    with _lock:
        _harvest.clear()
