import calendar
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import feedparser

from kb.feeds.fetching import FeedError, request
from kb.youtube.channels import CHANNEL_ID, VIDEO_ID, config


def channel_id(source, deadline):
    direct = source['url'].rsplit('/', 1)[-1]
    if CHANNEL_ID.fullmatch(direct):
        return direct
    cached = config(source).get('channel_id', '')
    if CHANNEL_ID.fullmatch(cached):
        return cached
    response = request(source['url'], deadline=deadline)
    text = response.body.decode('utf-8', errors='replace')
    patterns = (r'"(?:externalId|channelId)"\s*:\s*"(UC[A-Za-z0-9_-]{22})"',
                r'feeds/videos\.xml\?channel_id=(UC[A-Za-z0-9_-]{22})')
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match[1]
    raise FeedError('无法解析频道 ID，请改填 /channel/UC… 频道主页')


def fetch(source, timeout=20):
    deadline = time.monotonic() + timeout
    identity = channel_id(source, deadline)
    response = request('https://www.youtube.com/feeds/videos.xml?channel_id=' + identity,
                       deadline=deadline)
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
