import os
import shutil
from pathlib import Path


DEFAULTS = {'max_videos': 3, 'max_minutes': 60, 'max_video_minutes': 45,
            'window_days': 7, 'heat_limit': 10, 'value_limit': 10,
            'run_seconds': 900, 'asr_enabled': True, 'asr_threads': 2,
            'whisper_model': '', 'ai_only': True}
RANGES = {'max_videos': (1, 20), 'max_minutes': (1, 240), 'max_video_minutes': (1, 180),
          'window_days': (1, 30), 'heat_limit': (1, 50), 'value_limit': (1, 50),
          'run_seconds': (60, 3600), 'asr_threads': (1, 8)}


def validate(values):
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError('YouTube 设置字段不正确')
    merged = {**DEFAULTS, **values}
    for key, (low, high) in RANGES.items():
        if type(merged[key]) is not int or not low <= merged[key] <= high:
            raise ValueError(f'{key} 必须是 {low}–{high} 之间的整数')
    for key in ('asr_enabled', 'ai_only'):
        if type(merged[key]) is not bool:
            raise ValueError(f'{key} 必须是开关值')
    if not isinstance(merged['whisper_model'], str) or len(merged['whisper_model']) > 2048:
        raise ValueError('Whisper 模型路径不正确')
    merged['whisper_model'] = merged['whisper_model'].strip()
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
    model = model_path(settings)
    return {'yt_dlp': bool(shutil.which('yt-dlp')), 'ffmpeg': bool(shutil.which('ffmpeg')),
            'whisper': bool(shutil.which('whisper-cli')), 'model': model.is_file(),
            'model_path': str(model), 'asr_enabled': of(settings)['asr_enabled']}
