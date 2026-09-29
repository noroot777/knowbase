# -*- coding: utf-8 -*-
"""日报的编排与定时:到点先把所有来源同步一遍,再生成当天的日报。

同步这一步不自己造轮子,而是**依次**借用已有的管道(`kb.pipeline`)——每个通道
跑一趟 抓取→导出→索引→提取。依次而不是并发:管道是单例(全局一把运行锁,
索引/提取都要单写者写 kb.db),两个通道同时开等于一个必然被拒。

进度和失败留在各自的管道运行状态里,这里只记"哪个通道跑完了、结果如何",
面板要看细节仍旧点进同步管理——同一件事不做两套展示。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta

from kb import daily
from kb.config import DATA_DIR

SETTINGS_FILE = DATA_DIR / 'daily_report.json'
STATE_FILE = DATA_DIR / 'daily_last_run.json'
# 同步阶段:抓取→导出→索引→提取。提取给每篇一句话摘要,日报的素材质量全靠它;
# 条目化/wiki 两个重活不跑——日报不读它们的产物,跑一遍要几十分钟。
SYNC_STAGES = ['crawl', 'export', 'index', 'extract']
# 整个同步阶段的时间预算。超了就请求管道停下、跳过剩余通道,拿已有内容出日报
# ——"今天的日报没出来"比"日报少了两个来源"严重得多。
SYNC_BUDGET = int(os.environ.get('KB_DAILY_SYNC_BUDGET', str(3 * 3600)))
# 管道被别人占着时,等它空出来的上限(每个通道各算各的)。
WAIT_BUSY = 900

_lock = threading.RLock()
_state: dict = {
    'running': False, 'phase': '', 'detail': '', 'date': '',
    'since': '', 'until': '', 'sync': False,
    'trigger': '', 'started_at': '', 'finished_at': '', 'error': '',
    'stop_requested': False, 'channels': [], 'result': None, 'log': [],
}


# ── 设置 ────────────────────────────────────────────────────────────────────────

def _channel_labels() -> dict[str, str]:
    from kb.collectors import collection_channels
    return {c['id']: c['label'] for c in collection_channels()}


def default_channels() -> list[str]:
    """默认同步范围:微信默认通道 + 全部订阅类来源。

    微信两条通道(凭证/本地库)是同一批内容的两条路,默认只走当前生效的那条;
    两条都勾上只会把同一批公众号抓两遍。
    """
    from kb.collectors import active_collector
    from kb.source_types import feed_types
    return [active_collector().name, *feed_types()]


def _raw_settings() -> dict:
    """盘上那份原样内容(补齐缺省值),不去问采集器注册表。

    定时线程每 30 秒读一次,只为判断"到点没有";把通道校验也放进来的话,
    每一跳都要把所有采集器模块导入一遍,而绝大多数跳什么都不做。
    """
    try:
        d = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        d = {}
    return {'enabled': bool(d.get('enabled')),
            'time': d.get('time') or '08:00',
            'sync': d.get('sync', True) is not False,
            # 老配置里这个键叫 days、意思是"覆盖最近几天";窗口改成按上一份
            # 日报的截止时刻接力之后,它只剩"最长回溯几天"这一个用途。
            'max_days': max(1, min(7, int(d.get('max_days') or d.get('days') or 0)
                                   or daily.MAX_WINDOW_DAYS)),
            'channels': [str(c) for c in (d.get('channels') or [])],
            'last_fired': d.get('last_fired') or '',
            'next_run': d.get('next_run') or ''}


def load_settings() -> dict:
    d = _raw_settings()
    known = _channel_labels()
    channels = [c for c in d['channels'] if c in known]
    # 通道被删掉/改名之后,存下来的那份可能一个都不认识了。回落到默认范围,
    # 而不是留一份空列表——那会让定时日报每天都跑出"一个来源都没同步"。
    return {**d, 'channels': channels or [c for c in default_channels() if c in known]}


def save_settings(enabled: bool, time_str: str, channels: list[str] | None = None,
                  sync: bool = True, max_days: int = daily.MAX_WINDOW_DAYS) -> dict:
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', time_str or ''):
        raise ValueError(f'时间格式应为 HH:MM,收到 {time_str!r}')
    if not 1 <= int(max_days) <= 7:
        raise ValueError('最长回溯天数须在 1 到 7 天之间')
    known = _channel_labels()
    picked = [c for c in (channels or []) if c in known]
    if channels and not picked:
        raise ValueError('同步范围里没有一个可用的来源通道')
    with _lock:
        cur = load_settings()
        now = datetime.now()
        hour, minute = map(int, time_str.split(':'))
        nxt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # 保存只安排下一次,绝不"补跑"今天已经过去的时刻;同一天也只跑一次。
        if nxt <= now or cur['last_fired'] == now.strftime('%Y-%m-%d'):
            nxt += timedelta(days=1)
        cur.update({'enabled': bool(enabled), 'time': time_str, 'sync': bool(sync),
                    'max_days': int(max_days), 'channels': picked or cur['channels'],
                    'next_run': nxt.isoformat(timespec='minutes') if enabled else ''})
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                                 encoding='utf-8')
        return cur


def _mark_fired(day: str) -> None:
    with _lock:
        cur = _raw_settings()
        cur['last_fired'] = day
        tomorrow = datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1)
        cur['next_run'] = tomorrow.strftime('%Y-%m-%d') + 'T' + cur['time']
        SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                                 encoding='utf-8')


# ── 运行状态 ────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    _state['log'].append(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")
    del _state['log'][:-60]


def _set(phase: str, detail: str = '') -> None:
    _state['phase'] = phase
    if detail:
        _state['detail'] = detail
        _log(detail)


def is_running() -> bool:
    return bool(_state['running'])


def get_state() -> dict:
    out = deepcopy(_state)
    if not out['running'] and not out['started_at']:
        out['last_run'] = _load_last_run()
    return out


def _save_last_run() -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps({
            k: _state[k] for k in ('date', 'since', 'until', 'sync', 'trigger', 'started_at',
                                   'finished_at', 'error', 'channels', 'result', 'log')
        }, ensure_ascii=False, indent=1), encoding='utf-8')
    except OSError:
        pass


def _load_last_run() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def request_stop() -> bool:
    """停止这一轮日报。正在跑的管道阶段按它自己的规矩收尾,已完成的结果保留。"""
    from kb import pipeline as pl
    with _lock:
        if not _state['running']:
            return False
        if not _state['stop_requested']:
            _state['stop_requested'] = True
            _log('已请求停止:当前阶段结束后不再继续')
    if pl.get_state()['running']:
        pl.request_stop()
    return True


# ── 执行 ────────────────────────────────────────────────────────────────────────

def _wait_idle(deadline: float) -> bool:
    """等管道空下来。True = 空了;False = 等超时或被要求停止。"""
    from kb import pipeline as pl
    while pl.get_state()['running']:
        if _state['stop_requested'] or time.monotonic() > deadline:
            return False
        time.sleep(2)
    return True


def _sync_channel(channel: str, label: str, budget_end: float) -> dict:
    """跑一个通道的完整同步,等它结束。返回这个通道的结果记录。"""
    from kb import pipeline as pl

    entry = {'channel': channel, 'label': label, 'status': 'pending', 'detail': ''}
    _set('sync', f'{label}:等待同步管道空闲')
    if not _wait_idle(min(budget_end, time.monotonic() + WAIT_BUSY)):
        entry.update(status='skipped', detail='同步管道一直被占用,本轮跳过')
        return entry
    if not pl.start_pipeline(SYNC_STAGES, crawl_filter={'channel': channel}):
        entry.update(status='skipped', detail='同步管道已被其它任务占用,本轮跳过')
        return entry
    _set('sync', f'{label}:同步中')
    while True:
        snapshot = pl.get_state()
        if not snapshot['running']:
            break
        stage = snapshot.get('current_stage') or ''
        detail = (snapshot['stages'].get(stage) or {}).get('detail') or ''
        _state['detail'] = f'{label}:{detail}' if detail else f'{label}:同步中'
        if time.monotonic() > budget_end and not _state['stop_requested']:
            _log(f'{label}:同步超出时间预算,请求停止后直接生成日报')
            _state['stop_requested'] = True
            pl.request_stop()
        time.sleep(2)
    stages = pl.get_state()['stages']
    bad = [f'{name} {s["detail"]}' for name, s in stages.items() if s['status'] == 'error']
    entry.update(status='error' if bad else 'ok',
                 detail='；'.join(bad)[:400] if bad else '同步完成')
    _log(f'{label}:{entry["detail"]}')
    return entry


def _run(since: datetime | None, until: datetime | None, sync: bool,
         channels: list[str], max_days: int, trigger: str) -> None:
    try:
        if sync:
            labels = _channel_labels()
            budget_end = time.monotonic() + SYNC_BUDGET
            for channel in channels:
                if _state['stop_requested']:
                    _state['channels'].append(
                        {'channel': channel, 'label': labels.get(channel, channel),
                         'status': 'skipped', 'detail': '已停止,未同步'})
                    continue
                _state['channels'].append(
                    _sync_channel(channel, labels.get(channel, channel), budget_end))
        _set('generate', '正在生成日报')
        # 窗口的截止时刻要在**同步跑完之后**才定下来(没显式指定时 generate 自己
        # 取 now):日报覆盖的是"到生成这一刻为止",同步花掉的那几小时里产出的
        # 内容也算数。开跑时就把 until 钉死的话,那几小时会掉进两份日报的缝里。
        res = daily.generate(since=since, until=until, max_days=max_days,
                             progress=lambda text: _set('generate', text))
        _state.update(result=res, date=res['date'], since=res['since'], until=res['until'])
        _set('done', f"日报已生成:{res['docs']} 条素材"
                     + (f"(另有 {res['late']} 条补录)" if res['late'] else ''))
    except LookupError as e:
        _state['error'] = str(e)
        _set('failed', str(e))
    except Exception as e:                       # noqa: BLE001 - 面板要看到原因
        _state['error'] = str(e)
        _set('failed', f'日报生成失败: {e}')
    finally:
        with _lock:
            _state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save_last_run()
            _state['running'] = False


def start(since: datetime | None = None, until: datetime | None = None,
          sync: bool = True, channels: list[str] | None = None,
          max_days: int | None = None, trigger: str = 'manual') -> bool:
    """起一轮日报。已经在跑返回 False。

    `since`/`until` 都不给时由 `daily.next_window()` 接着上一份日报算,那是
    日常路径;显式给窗口是为了补一份旧的、或者重算一段。
    """
    settings = load_settings()
    known = _channel_labels()
    picked = [c for c in (channels if channels is not None else settings['channels'])
              if c in known]
    if since and until and since >= until:
        raise ValueError('日报的起始时刻必须早于截止时刻')
    with _lock:
        if _state['running']:
            return False
        _state.update({
            'running': True, 'phase': 'starting', 'detail': '',
            'date': until.date().isoformat() if until else '',
            'since': since.strftime(daily.STAMP) if since else '',
            'until': until.strftime(daily.STAMP) if until else '',
            'sync': bool(sync), 'trigger': trigger, 'error': '', 'log': [],
            'stop_requested': False, 'channels': [], 'result': None,
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'finished_at': '',
        })
        window = f'{since:%m-%d %H:%M} 起' if since else '接上一份日报'
        _log(f'开始生成日报({window})'
             + (f',先同步 {len(picked)} 个来源' if sync else ''))
    threading.Thread(target=_run, args=(since, until, bool(sync), picked,
                                        max_days or settings['max_days'], trigger),
                     daemon=True).start()
    return True


# ── 定时 ────────────────────────────────────────────────────────────────────────

def tick() -> bool:
    """由 kb.pipeline 的调度线程每 30 秒调一次。到点了就起一轮,返回是否起了。"""
    with _lock:
        s = _raw_settings()
        if not s['enabled'] or _state['running']:
            return False
        now = datetime.now()
        today = now.strftime('%Y-%m-%d')
        due = (now.isoformat(timespec='minutes') >= s['next_run'] if s['next_run']
               else now.strftime('%H:%M') >= s['time'])
        if not due or s['last_fired'] == today:
            return False
    settings = load_settings()
    # 定时这一轮不指定窗口:起点接上一份日报的截止时刻,终点是同步跑完的那一刻。
    if start(sync=settings['sync'], channels=settings['channels'],
             max_days=settings['max_days'], trigger='schedule'):
        _mark_fired(today)
        return True
    return False
