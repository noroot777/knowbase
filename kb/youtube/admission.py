# -*- coding: utf-8 -*-
"""入选视频拿到真实元数据之后的准入判断。

**硬约束在 `docs/decisions/youtube-intake.md` 第一节 —— 改这块之前先读那一份。**

**估算日期的条目在这里换成事实日期,资格由 `ranking.rejection` 重判一次。** 发现阶段的
后路没有发布时间、也没有简介,只有频道页的列表顺序(见 `discovery.approximate`),所以第一次
见到真实元数据的这一刻就是闭环的时刻:`calibrate()` 把日期、播放量和简介换成事实值,随后
调用方拿**同一个** `ranking.rejection` 重判 —— 窗口、指定日期范围、订阅历史下界、未来时间和
AI 判据全在那一份里。**这一档在排名那一头不许被判死**:`ranking.rejection` 会把拿估算值判出来
的「不合格」标成「还判不了」,留在榜上等这里核实(未知不等于不合格)。
**这里不另写一份窗口判断**:口径只有一份,否则改一处另一处不跟着走。
"""

import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TZ = ZoneInfo('Asia/Shanghai')

TEMPORARY = {'duration', 'upcoming', 'live', 'legacy', 'window'}


@dataclass(frozen=True)
class Decision:
    code: str
    state: str
    reason: str
    retry_seconds: int = 0


def moment(metadata):
    """从元数据里取真实发布时间。取不到返回 None —— **不拿「现在」冒充它**。"""
    for key in ('timestamp', 'release_timestamp'):
        value = metadata.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
            try:
                return datetime.fromtimestamp(value, timezone.utc)
            except (OverflowError, OSError, ValueError):
                continue
    text = str(metadata.get('upload_date') or '').strip()
    if re.fullmatch(r'\d{8}', text):
        try:
            return datetime.strptime(text, '%Y%m%d').replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


#: 读不出事实时间时的处置。**不放行也不永久排除** —— 留在原处的代价只是下一轮
#: 按 URL 去重地再探一次,而放行的代价是一个估出来的日子被当成发布日期永久落库。
UNDATED = Decision('window', 'deferred', '元数据里没有可靠的发布时间，稍后重新确认', 300)


def calibrate(row, metadata):
    """把 entry 里**未知的那几项**换成元数据里的事实值;读不出事实时间返回 `None`。

    **估算日期不许留到成品里。** 后路的条目是按频道页列表顺序估出来的,它只够决定
    「这条值不值得探一次元数据」;真实 `upload_date` 一到手它就必须让位 —— 否则成品、
    检索和日报上带的都是那个估出来的日子(审查实测:估 2026-10-27、实际 2026-10-25,
    指定 10-27 补抓时那条前天的视频被收进来,成品日期还是 10-27)。
    **播放量和简介同理**:后路给的是 `views=0` 和空简介,那不是「没人看」「没有简介」,
    是**不知道** —— 元数据里这一份才是事实。**简介尤其要补回来**:`ranking.rejection`
    的 AI 判据读的是标题加简介,空着的话标题没提 AI 的那条就判不了;补不回来的话
    「未知不等于不合格」只做了一半 —— 核实完了还是拿空简介去判,结论和没核实一样。
    **已经有的那几项照旧不动**:事实不许被另一份事实来回换(精确发现带回来的简介和
    播放量就该留着)。
    """
    original = row['entry']
    published = moment(metadata)
    if published is None:
        return None
    entry = {key: value for key, value in original.items() if key != 'approximate'}
    entry['published'] = published.astimezone(TZ).isoformat()
    count = metadata.get('view_count')
    if not original.get('views') and isinstance(count, (int, float)) and not isinstance(count, bool) and count >= 0:
        entry['views'] = int(count)
    described = str(metadata.get('description') or '').strip()
    if described and not str(original.get('description') or '').strip():
        entry['description'] = described
    return entry


def gate(metadata, settings, row=None):
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
    # 估算日期那一档的日期资格**不在这里判**:调用方先 `calibrate()` 换成事实日期,
    # 再过一次 `ranking.rejection`(窗口、指定范围、历史下界、未来时间都在那一份里)。
    return None
