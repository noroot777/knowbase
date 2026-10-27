import os
import shutil
from pathlib import Path


DEFAULTS = {'max_videos': 3, 'max_minutes': 60, 'max_video_minutes': 45,
            'window_days': 7, 'rank_limit': 10,
            'run_seconds': 900, 'asr_enabled': True, 'asr_threads': 2,
            'whisper_model': '', 'ai_only': True, 'cookies_browser': 'chrome'}
RANGES = {'max_videos': (1, 20), 'max_minutes': (1, 240), 'max_video_minutes': (1, 180),
          'window_days': (1, 30), 'rank_limit': (1, 50),
          'run_seconds': (60, 3600), 'asr_threads': (1, 8)}
#: 热度榜和价值榜合成一张榜之前的那两个上限。**仍然要认,一个都不许删** ——
#: `validate` 对不认识的键是整个抛,而它同时管**读**存量设置(`of`)和保存;只认新键
#: 的话,库里存着 `heat_limit` 的人一打开「我的订阅 → YouTube」整块面板就读不出来,
#: 连预算和登录态配置一起没了。换算取两者里**宽**的那一个:老口径是「名次 A 以内
#: **或** 名次 B 以内」,取大的才不缩小他原来的准入面(同 `SOURCE_ALIASES` 那条教训)。
LEGACY_LIMITS = ('heat_limit', 'value_limit')


def fold_legacy(values):
    """把旧的两个榜上限折成一个 `rank_limit`。显式给了新键就以新键为准。"""
    # 越界的旧值**夹进范围、不丢掉**:丢掉会悄悄换成默认的 10(比用户原来设的宽),
    # 而抛错会让存量设置连读都读不出来 —— 两头都比夹一下坏。
    low, high = RANGES['rank_limit']
    legacy = [min(high, max(low, values[key])) for key in LEGACY_LIMITS
              if type(values.get(key)) is int]
    values = {key: item for key, item in values.items() if key not in LEGACY_LIMITS}
    if legacy and 'rank_limit' not in values:
        values['rank_limit'] = max(legacy)
    return values


def validate(values):
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys() - set(LEGACY_LIMITS):
        raise ValueError('YouTube 设置字段不正确')
    merged = {**DEFAULTS, **fold_legacy(values)}
    for key, (low, high) in RANGES.items():
        if type(merged[key]) is not int or not low <= merged[key] <= high:
            raise ValueError(f'{key} 必须是 {low}–{high} 之间的整数')
    for key in ('asr_enabled', 'ai_only'):
        if type(merged[key]) is not bool:
            raise ValueError(f'{key} 必须是开关值')
    if not isinstance(merged['whisper_model'], str) or len(merged['whisper_model']) > 2048:
        raise ValueError('Whisper 模型路径不正确')
    merged['whisper_model'] = merged['whisper_model'].strip()
    # 浏览器规格串自己有一份校验口径(`login.validate`),这里不另判一遍 —— 它同时管
    # 面板保存和拼命令两处,多一份就会有一天只改了其中一处。
    from kb.youtube.login import validate as validate_browser
    merged['cookies_browser'] = validate_browser(merged['cookies_browser'])
    return merged


def of(settings):
    return validate(settings.get('youtube') or {})


def model_path(settings):
    configured = of(settings)['whisper_model'] or os.environ.get('KB_WHISPER_MODEL', '')
    if configured:
        return Path(configured).expanduser()
    from kb.config import VIDEOS_ROOT
    return VIDEOS_ROOT.parent / 'models' / 'whisper-cpp' / 'ggml-base.en.bin'


def tools(settings):
    """面板上那行工具状态。**yt-dlp 按 `toolchain` 的解析顺序问**(自备优先),
    不能只看 PATH —— 用户点过更新之后 PATH 里那份可能还是旧的、甚至压根没有。"""
    from kb.youtube import toolchain
    model = model_path(settings)
    return {'yt_dlp': bool(toolchain.command()), 'ffmpeg': bool(shutil.which('ffmpeg')),
            'whisper': bool(shutil.which('whisper-cli')), 'model': model.is_file(),
            'model_path': str(model), 'asr_enabled': of(settings)['asr_enabled']}
