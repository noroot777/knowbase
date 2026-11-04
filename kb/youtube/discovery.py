# -*- coding: utf-8 -*-
"""带登录态轻量列频道，精确元数据由 candidate_metadata 增量补齐。

硬约束在 docs/decisions/youtube-intake.md 第一节，变更策略需同步。

普通同步不展开频道内每条视频的详情，不以匿名 RSS 或慢速展开掩盖列表失败。
显式日期补抓仍保留详细发现，避免把估算列表当成指定日期内的事实。
估算列表不覆盖已知精确数据；未核实的预排候选在处理前校准。
"""

import time

from kb.feeds.fetching import FeedError
from kb.youtube import login, throttle
from kb.youtube.channels import CHANNEL_ID, config


def local_identity(source):
    """**一次请求都不发**就能说出的频道归属:地址里直接带的,或者上一轮缓存下来的。

    带登录态那两条路要的就是这一份 —— 它们的归属以 yt-dlp 自己报的 `channel_id` 为准
    (`media._detailed` / `_listed` 会带回来),这里只是 handle 订阅第一轮时的兜底。
    **不许在这里补一次公开主页请求去解析归属**:全程登录态这一轮的全部意义就是不发那种
    请求(报出来会是「发现失败」,而工具一个进程都没起)。
    """
    direct = source['url'].rsplit('/', 1)[-1]
    if CHANNEL_ID.fullmatch(direct):
        return direct
    cached = str(config(source).get('channel_id', '') or '')
    return cached if CHANNEL_ID.fullmatch(cached) else ''


def fetch(source, settings, timeout=90, estimates=True, notes=None):
    """普通同步走增量轻量列表(`windowed_discovery`),指定日期那一轮逐条解析拿真实日期。

    普通一轮要的 `discovery_after` 由 `sync_window.prepare` 给;订阅在那之前已经被改走的
    那一档由 `collector` 直接按改指向收口,不进这里。
    """
    budget = max(6.0, float(timeout))
    label = '带登录态增量轻量列表' if estimates else '带登录态逐条解析'
    try:
        if estimates:
            from kb.youtube.windowed_discovery import fetch as windowed
            return windowed(source, settings, time.monotonic() + budget, notes)
        return detailed(source, settings, time.monotonic() + budget, notes)
    except throttle.NotAttempted:
        raise
    except Exception as error:
        from kb.youtube.faults import local_fault
        if local_fault(error):
            raise
        raise _combined([f'{label}：{error}'], [error]) from None


def _combined(reasons, failures):
    """包装发现错误时保留原始上游、分档和完整诊断。"""
    return login.merge(FeedError('；'.join(reasons)), failures)


#: 主路一轮往回看几条。**不是把频道历史翻完**:逐条解析实测每条约 4.3 秒,10 条就是
#: 40 多秒,正好落在它那一段预算里;日更的频道隔一天同步也够。预算在第 7 条上到点时
#: 前面那几条照旧收下(`media._collect_listing`),所以这个数字给大一点也不会颗粒无收。
DETAIL_LIMIT = 10


def detailed(source, settings, deadline, notes=None):
    """指定日期那一轮:频道页**带登录态逐条解析**,日期和播放量都是真的。

    和普通一轮的轻量列表同一个地址,差别只在不带 `--flat-playlist`(见 `media.detailed_videos`)。
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


def _owner(item, source):
    """这条视频记在哪个频道名下。**先认 yt-dlp 自己报的那一份**,再退回本地那一份。

    归属留空的话 `storage.discover` 写进库的条目就不知道自己属于谁;而为了补上它去发
    一次公开请求,恰恰是全程登录态这一轮要去掉的那种请求 —— 所以两边都不发请求:
    工具已经在输出里报了 `channel_id`,地址里带 `/channel/UC…` 或缓存过的也认。
    """
    reported = str(item.get('channel_id') or '')
    return reported if CHANNEL_ID.fullmatch(reported) else local_identity(source)
