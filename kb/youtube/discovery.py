# -*- coding: utf-8 -*-
"""带登录态轻量列频道，精确元数据由 candidate_metadata 增量补齐。

硬约束在 docs/decisions/youtube-intake.md 第一节，变更策略需同步。

普通同步不展开频道内每条视频的详情，不以匿名 RSS 或慢速展开掩盖列表失败。
显式日期补抓仍保留详细发现，避免把估算列表当成指定日期内的事实。
估算列表不覆盖已知精确数据；未核实的预排候选在处理前校准。
"""

import calendar
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser

from kb.feeds.fetching import FeedError, request
from kb.youtube import login, throttle
from kb.youtube.channels import CHANNEL_ID, VIDEO_ID, config
from kb.youtube.settings import of


def local_identity(source):
    """**一次请求都不发**就能说出的频道归属:地址里直接带的,或者上一轮缓存下来的。

    带登录态那两条路要的就是这一份 —— 它们的归属以 yt-dlp 自己报的 `channel_id` 为准
    (`media._detailed` / `_listed` 会带回来),这里只是 handle 订阅第一轮时的兜底。
    **不许在这里回落到 `channel_id()`**:那是一次公开主页请求,而全程登录态这一轮
    的全部意义就是不发那种请求(报出来会是「发现失败」,而工具一个进程都没起)。
    """
    direct = source['url'].rsplit('/', 1)[-1]
    if CHANNEL_ID.fullmatch(direct):
        return direct
    cached = str(config(source).get('channel_id', '') or '')
    return cached if CHANNEL_ID.fullmatch(cached) else ''


def channel_id(source, deadline):
    """解析频道 ID,**解析不出来时会发一次公开主页请求** —— 所以只有公开那条路能用它。"""
    if identity := local_identity(source):
        return identity
    response = request(source['url'], deadline=deadline)
    text = response.body.decode('utf-8', errors='replace')
    patterns = (r'"(?:externalId|channelId)"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
                r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})')
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match[1]
    raise FeedError('无法解析频道 ID，请改填 /channel/UC… 频道主页')


#: 公开那条路的重试(照 daily-report 的 `--retry 3 --retry-delay 1 --retry-all-errors`)。
#: **只有「没有登录态可用」那一档会走到它** —— 那时它是唯一的办法,失败多试两次近乎免费。
RSS_ATTEMPTS = 3
RSS_BACKOFF = 1.0


def fetch(source, settings=None, timeout=90, estimates=True, notes=None):
    """普通同步只列 flat；指定日期只取精确列表；未配置登录态的显式调用才走 RSS。"""
    budget = max(6.0, float(timeout))
    spec = '' if settings is None else of(settings)['cookies_browser']
    if not spec:
        return rss(source, time.monotonic() + budget)
    label = '带登录态轻量列表' if estimates else '带登录态逐条解析'
    try:
        fetcher = approximate if estimates else detailed
        return fetcher(source, settings, time.monotonic() + budget, notes)
    except throttle.NotAttempted:
        raise
    except Exception as error:
        raise _combined([f'{label}：{error}'], [error]) from None


def _combined(reasons, failures):
    """包装发现错误时保留原始上游、分档和完整诊断。"""
    return login.merge(FeedError('；'.join(reasons)), failures)


#: 主路一轮往回看几条。**不是把频道历史翻完**:逐条解析实测每条约 4.3 秒,10 条就是
#: 40 多秒,正好落在它那一段预算里;日更的频道隔一天同步也够。预算在第 7 条上到点时
#: 前面那几条照旧收下(`media._collect_listing`),所以这个数字给大一点也不会颗粒无收。
DETAIL_LIMIT = 10


def detailed(source, settings, deadline, notes=None):
    """第 1 条路:频道页**带登录态逐条解析**,日期和播放量都是真的。

    和第 2 条同一个地址,差别只在不带 `--flat-playlist`(见 `media.detailed_videos`)。
    条目上不打 `approximate`,所以它们走的是正常的窗口与范围判断,不必等
    `admission.calibrate` 事后校准。
    """
    from kb.youtube import media
    listed = media.detailed_videos(source, settings, deadline, lambda: False,
                                   limit=DETAIL_LIMIT, notes=notes)
    return [{'video_id': item['video_id'], 'channel_id': _owner(item, source), 'url': item['url'],
             'title': item['title'], 'published': item['published'],
             'description': item['description'], 'views': item['views'],
             'duration': item['duration']} for item in listed]


def approximate(source, settings, deadline, notes=None):
    """轻量列频道；估算日期只记录列表顺序，排名前必须补齐真实元数据。"""
    from kb.youtube import media
    listed = media.channel_videos(source, settings, deadline, lambda: False, limit=20,
                                  notes=notes)
    now = datetime.now(timezone.utc)
    entries = []
    for position, item in enumerate(listed):
        # 频道页是时间倒序,所以**用列表位置拉开一个递减的估算时间**:顺序信息留住了(同分时
        # 还知道谁更新),又都落在默认窗口里,由 `gate` 按真实 upload_date 收口。
        # 不给所有条目写同一个 now —— 那会把「谁更新」这一位信息丢掉。
        stamp = (now - timedelta(hours=position)).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
        entries.append({'video_id': item['video_id'], 'channel_id': _owner(item, source),
                        'url': item['url'], 'title': item['title'], 'published': stamp,
                        'description': '', 'views': 0, 'approximate': True,
                        'duration': item['duration'], 'listing_position': position})
    return entries


def _owner(item, source):
    """这条视频记在哪个频道名下。**先认 yt-dlp 自己报的那一份**,再退回本地那一份。

    归属留空的话 `storage.discover` 写进库的条目就不知道自己属于谁;而为了补上它去发
    一次公开请求,恰恰是全程登录态这一轮要去掉的那种请求 —— 所以两边都不发请求:
    工具已经在输出里报了 `channel_id`,地址里带 `/channel/UC…` 或缓存过的也认。
    """
    reported = str(item.get('channel_id') or '')
    return reported if CHANNEL_ID.fullmatch(reported) else local_identity(source)


def rss(source, deadline):
    """公开那份 Atom。**只有「没有登录态可用」那一档走到这里**(见 `fetch`)。

    那批 47/48 的 404 是间歇性的(事后同样 4 路并发复测 52 个频道全部 200),所以这一条
    路上第一道防线是**同一条路多试两次**,代价接近零。重试待在给它的那段预算里:
    时间连一次退避加一次请求都放不下就不试了。
    """
    identity = channel_id(source, deadline)
    url = 'https://www.youtube.com/feeds/videos.xml?channel_id=' + identity
    for attempt in range(1, max(1, RSS_ATTEMPTS) + 1):
        try:
            return _parse_feed(request(url, deadline=deadline), identity)
        except Exception:
            # 最后一次、或者剩下的时间连一次退避加一次请求都放不下,就把原样的失败抛出去。
            if attempt >= max(1, RSS_ATTEMPTS) or time.monotonic() + RSS_BACKOFF * 2 >= deadline:
                raise
        time.sleep(RSS_BACKOFF)


def _parse_feed(response, identity):
    """把那份 Atom 解成条目。**归属要对**:`identity` 省掉的话 `yt_channelid` 一比就全被
    滤掉,整个频道静默变成「今天没有新内容」。"""
    parsed = feedparser.parse(response.body)
    if not parsed.version or parsed.bozo and not parsed.entries:
        raise FeedError('YouTube 没有返回可解析的频道订阅，可能被限流或访问受限')
    entries = []
    for item in parsed.entries:
        video_id = item.get('yt_videoid') or item.get('id', '').rsplit(':', 1)[-1]
        if not VIDEO_ID.fullmatch(video_id):
            continue
        owner = item.get('yt_channelid', identity)
        if owner != identity:
            continue
        published = item.get('published_parsed')
        stamp = (datetime.fromtimestamp(calendar.timegm(published), timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).isoformat()
                 if published else '')
        statistics = item.get('media_statistics') or {}
        try:
            views = min(2 ** 63 - 1, max(0, int(statistics.get('views', 0))))
        except (ValueError, TypeError):
            views = 0
        entries.append({'video_id': video_id, 'channel_id': identity,
                        'url': 'https://www.youtube.com/watch?v=' + video_id,
                        'title': item.get('title', video_id), 'published': stamp,
                        'description': item.get('summary') or item.get('media_description', ''), 'views': views})
    if parsed.entries and not entries:
        raise FeedError('频道返回了条目，但无法识别视频 ID 或频道归属')
    return entries
