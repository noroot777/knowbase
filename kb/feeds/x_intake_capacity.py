"""One window/budget model; a scan opportunity is not proof of completed coverage."""
from datetime import timedelta
from math import ceil, floor

from kb.feeds.x_intake_metrics import seconds

WINDOW = timedelta(days=5)
RESCAN_NOTICE = '每轮从顶部尽力重扫，不是断点续页；不保证五天内补齐，超窗后需指定历史区间补抓。'
PHASE_SECONDS = {'discovery': 240, 'history': 120, 'body': 120}


def phase_seconds(phase, reader=None):
    return (reader or seconds)(f'KB_X_{phase.upper()}_SECONDS', PHASE_SECONDS[phase])


def account_seconds(channel, phase, reader=None):
    read = reader or seconds
    if channel == 'feedgrab':
        return read('KB_X_FEEDGRAB_ACCOUNT_SECONDS', 25)
    return read('KB_X_ACCOUNT_SECONDS', 8 if phase == 'discovery' else 20)


def plan(count, channel, *, budget=480, window=WINDOW):
    available = max(0, budget - min(30, max(0.1, budget) * 0.2) - 5)
    stages = {}
    for phase in (['combined'] if channel == 'feedgrab' else ['discovery', 'history']):
        limit = min(available, phase_seconds('discovery' if phase == 'combined' else phase))
        quota = account_seconds(channel, phase)
        slots = floor(limit / quota)
        stages[phase] = dict(seconds=limit, account_seconds=quota, slots=slots,
                             rounds=ceil(count / slots) if slots else None,
                             fits_daily_window=slots * window.total_seconds() >= count * 86400)
        available = max(0, available - limit)
    return dict(sources=count, window_days=window.total_seconds() / 86400, stages=stages,
                assumption='按每日一次、账号用满切片估算；未计启动、冷却及反复从顶部重扫，不是完成保证')


def schedule_notice():
    from kb.config import ScheduleUnreadable
    from kb.daily_run import load_settings
    try:
        settings = load_settings()
    except ScheduleUnreadable:
        return '日报定时状态无法读取，不能确认后续自动追补机会。'
    if not (settings['enabled'] and settings['sync'] and 'social' in settings['channels']):
        return '日报未安排自动同步大V，手动同步次数未知，不能承诺后续自动追补。'
    return ''


def warning(value, schedule=''):
    labels = {'discovery': '最新发现', 'history': '历史追补', 'combined': '时间线扫描'}
    reasons = [schedule] if schedule else []
    for phase, stage in value['stages'].items():
        if not stage['fits_daily_window']:
            rounds = f"至少{stage['rounds']}轮" if stage['rounds'] else '当前预算容不下一个完整切片'
            reasons.append(f"{labels[phase]}预算容量不足：{value['sources']}个来源，每轮按配置{stage['slots']}个完整账号切片，"
                           f"{rounds}；每日一次无法在{value['window_days']:g}天窗口内轮遍。")
    return ''.join(reasons) + RESCAN_NOTICE
