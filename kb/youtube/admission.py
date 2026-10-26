import math
from dataclasses import dataclass


TEMPORARY = {'duration', 'upcoming', 'live', 'legacy'}


@dataclass(frozen=True)
class Decision:
    code: str
    state: str
    reason: str
    retry_seconds: int = 0


def gate(metadata, settings):
    if metadata.get('live_status') == 'is_upcoming':
        return Decision('upcoming', 'deferred', '视频尚未发布或首映未结束，稍后重新确认', 120)
    if metadata.get('live_status') == 'is_live' or metadata.get('is_live'):
        return Decision('live', 'deferred', '直播尚未结束，稍后重新确认发布状态', 900)
    duration = metadata.get('duration')
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        return Decision('duration', 'deferred', '没有可靠时长，不开始下载或转录；稍后重新确认', 120)
    if metadata.get('live_status') in ('was_live', 'post_live') or metadata.get('was_live'):
        return Decision('replay', 'excluded', '直播回放不进入自动处理')
    media_type = str(metadata.get('media_type', '')).strip().lower()
    if '/shorts/' in str(metadata.get('webpage_url', '')) or media_type in ('short', 'shorts'):
        return Decision('shorts', 'excluded', 'Shorts 不进入自动处理')
    if duration > settings['max_video_minutes'] * 60:
        return Decision('duration_budget', 'deferred', '超过单条视频时长预算')
    return None
