# -*- coding: utf-8 -*-
"""日报的编排与定时:到点先把所有来源同步一遍,再生成当天的日报。

同步这一步不自己造轮子,而是**依次**借用已有的管道(`kb.pipeline`)——每个通道
跑一趟 抓取→导出→索引→提取。依次而不是并发:管道是单例(全局一把运行锁,
索引/提取都要单写者写 kb.db),两个通道同时开等于一个必然被拒。

进度和失败留在各自的管道运行状态里,这里只记"哪个通道跑完了、结果如何",
面板要看细节仍旧点进同步中心——同一件事不做两套展示。
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
# 定时那一轮临时失败后,隔多久重试、同一天最多补几次。
RETRY_AFTER = timedelta(minutes=30)
MAX_RETRIES = 2

_lock = threading.RLock()
_state: dict = {
    'running': False, 'phase': '', 'detail': '', 'date': '',
    'since': '', 'until': '', 'sync': False,
    'trigger': '', 'started_at': '', 'finished_at': '', 'error': '',
    'stop_requested': False, 'over_budget': False, 'sched_day': '',
    # 我们自己起的那一轮管道的 run id(没起就是空串)。日报允许在别人的管道
    # 跑着时排队等,这时候用户按"停止"只该结束我们的等待,不能顺手把别人的
    # 同步也停掉。**记 id 而不是布尔**:布尔只回答"是不是我起的",答不了
    # "现在跑的还是不是我那一轮"——我们那轮在两次轮询之间跑完、另一项普通
    # 同步紧接着拿到全局管道时,布尔仍是 True,停止就落到了别人头上
    # (2026-09-30 审查实测)。
    'pipeline_run': '',
    'channels': [], 'result': None, 'log': [],
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
            # 一份日报都没有、接不上任何截止时刻时回溯几天。老配置里这个键
            # 叫 days/max_days(当时是"覆盖最近几天"和"最长回溯几天"),窗口
            # 改成一律接上一份日报的截止时刻之后,它只剩"第一份回溯几天"。
            'first_days': max(1, min(7, int(d.get('first_days') or d.get('max_days')
                                            or d.get('days') or 0) or daily.FIRST_DAYS)),
            'channels': [str(c) for c in (d.get('channels') or [])],
            'last_fired': d.get('last_fired') or '',
            'fail_day': d.get('fail_day') or '',
            'fail_count': int(d.get('fail_count') or 0),
            'next_run': d.get('next_run') or ''}


def load_settings() -> dict:
    d = _raw_settings()
    known = _channel_labels()
    channels = [c for c in d['channels'] if c in known]
    # 通道被删掉/改名之后,存下来的那份可能一个都不认识了。回落到默认范围,
    # 而不是留一份空列表——那会让定时日报每天都跑出"一个来源都没同步"。
    return {**d, 'channels': channels or [c for c in default_channels() if c in known]}


def save_settings(enabled: bool, time_str: str, channels: list[str] | None = None,
                  sync: bool = True, first_days: int = daily.FIRST_DAYS) -> dict:
    if not re.fullmatch(r'([01]\d|2[0-3]):[0-5]\d', time_str or ''):
        raise ValueError(f'时间格式应为 HH:MM,收到 {time_str!r}')
    if not 1 <= int(first_days) <= 7:
        raise ValueError('第一份日报的回溯天数须在 1 到 7 天之间')
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
                    'first_days': int(first_days), 'channels': picked or cur['channels'],
                    'next_run': nxt.isoformat(timespec='minutes') if enabled else ''})
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                                 encoding='utf-8')
        return cur


def _write_settings(**changes) -> None:
    with _lock:
        cur = _raw_settings()
        cur.update(changes)
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                                 encoding='utf-8')


def _at(day: datetime, time_str: str) -> str:
    return day.strftime('%Y-%m-%d') + 'T' + time_str


def _mark_attempt(now: datetime) -> None:
    """定时起了一轮:先把下一跳推到重试时刻,**不记"今天已出报"**。

    记"已出报"要等真的出来了(`_finish_schedule`)。在这里就记的话,这一轮
    因为模型 502 之类的临时故障失败,当天再也不会自己试第二次,用户一整天
    等不到日报、面板上也只有一句失败原因。
    """
    _write_settings(next_run=(now + RETRY_AFTER).isoformat(timespec='minutes'))


def _finish_schedule(day: str, retry: bool) -> None:
    """定时这一轮收尾。retry=True 表示临时故障,同一天内补几次;False 表示
    今天到此为止(出报成功、没有素材、或者用户自己按了停止)。"""
    with _lock:
        cur = _raw_settings()
        now = datetime.now()
        count = (cur['fail_count'] if cur['fail_day'] == day else 0) + (1 if retry else 0)
        done = not retry or count > MAX_RETRIES
        _write_settings(
            last_fired=day if done else cur['last_fired'],
            fail_day=day if retry else '', fail_count=count if retry else 0,
            next_run=(_at(datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1), cur['time'])
                      if done else (now + RETRY_AFTER).isoformat(timespec='minutes')))


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


STOP_IDLE = '当前没有正在运行的日报任务'
STOP_GENERATING = ('日报正在生成,这一阶段不能中途停止:模型已经在出稿,'
                   '这一轮会照常保存(需要的话生成完可以删掉这一份)')
STOP_OK = '当前阶段结束后停止,这一轮不生成日报;已同步的内容会保留'


def request_stop() -> tuple[bool, str]:
    """停下这一轮日报:剩下的通道不同步了,**日报也不生成**。

    返回 `(受理了没有, 说明)`。两种不受理:没有在跑的日报;已经进了生成阶段
    ——那时候模型钱已经花出去了,`daily.generate()` 自己把文件写完才返回,
    受理下来只会承诺一件做不到的事(界面上写着"这一轮不生成日报",实际照样
    多出一份日报,2026-09-30 审查实测)。所以那一档如实拒掉、按钮也置灰。

    只停自己起的那一轮管道:在别人的管道后面排队等的时候按停止,只结束我们
    自己的等待。
    """
    from kb import pipeline as pl
    with _lock:
        if not _state['running']:
            return False, STOP_IDLE
        # 这个判断和 `_run` 里"进入生成阶段"那一步共用 `_lock`,所以两者严格
        # 二选一:要么停止先落下、生成整段跳过,要么生成已经开跑、停止被拒。
        if _state['phase'] == 'generate':
            return False, STOP_GENERATING
        mine = _state['pipeline_run']
        if not _state['stop_requested']:
            _state['stop_requested'] = True
            _log('已请求停止:当前阶段结束后不再继续,这一轮不生成日报')
    # 带上 id:我们那一轮可能已经收尾、现在跑的是别人起的同步,那就不该停它。
    # 这个判断交给 pipeline 在它自己的锁里做,免得"读状态"和"去停"之间又开一道缝。
    if mine:
        pl.request_stop(mine)
    return True, STOP_OK


def _halting() -> bool:
    """还要不要继续同步下一个通道。两个原因分开记:用户按的停止会连生成一起
    取消,时间预算耗尽只是不再同步、照样拿已有内容出日报。"""
    return bool(_state['stop_requested'] or _state['over_budget'])


def _sync_issues() -> list[dict]:
    """这一轮没同步成功的通道(error / unknown / 被跳过的都算)。

    降级拿已有内容出报是对的("今天的日报没出来"比"少了两个来源"严重),但这份
    名单必须一路传下去:面板上的横幅、完成提示、写进日报文件的那几行都读它。
    """
    return [{'channel': c['channel'], 'label': c['label'],
             'status': c['status'], 'detail': c['detail']}
            for c in _state['channels'] if c['status'] != 'ok']


# ── 执行 ────────────────────────────────────────────────────────────────────────

def _wait_idle(deadline: float) -> bool:
    """等管道空下来。True = 空了;False = 等超时或被要求停止。"""
    from kb import pipeline as pl
    while pl.get_state()['running']:
        if _halting() or time.monotonic() > deadline:
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
    run = pl.start_pipeline(SYNC_STAGES, crawl_filter={'channel': channel})
    if not run:
        entry.update(status='skipped', detail='同步管道已被其它任务占用,本轮跳过')
        return entry
    with _lock:
        _state['pipeline_run'] = run
    # 停止请求正好落在"等到空闲"和"起跑"之间时,request_stop 那一刻还不归我们,
    # 它不会去停这一轮;补一次,免得用户按了停止还要眼看着它跑完整个通道。
    if _halting():
        pl.request_stop(run)
    _set('sync', f'{label}:同步中')
    # 只认 run id 相同的快照。两秒一跳的轮询里,我们那一轮跑完、另一项同步
    # 马上接手全局管道是完全可能的:只看 running 的话看到的是连绵不断的 True,
    # 既不知道已经换人,还会把别人的阶段结果当成自己这个通道的成绩单。
    #
    # `mine` 只装**终态**(自己那一轮 running 已经放下的那份快照)。中途快照不算:
    # 那时阶段还是 running/pending,拿它数 error 一个都数不出来,于是"被接替"和
    # "跑完了还全是绿的"长得一模一样——报成功却没有任何证据。被接替的那条路统一
    # 去问管道按 id 留的收尾记录。
    mine: dict | None = None
    while True:
        snapshot = pl.get_state()
        if snapshot.get('run_id') != run:
            break                    # 我们那一轮已经收尾,现在跑的是别人的
        if not snapshot['running']:
            mine = snapshot          # 正常收尾:这份快照就是最终结果
            break
        stage = snapshot.get('current_stage') or ''
        detail = (snapshot['stages'].get(stage) or {}).get('detail') or ''
        _state['detail'] = f'{label}:{detail}' if detail else f'{label}:同步中'
        if time.monotonic() > budget_end and not _halting():
            _log(f'{label}:同步超出时间预算,请求停止后直接生成日报')
            _state['over_budget'] = True
            pl.request_stop(run)
        time.sleep(2)
    with _lock:
        if _state['pipeline_run'] == run:
            _state['pipeline_run'] = ''
    # 没能亲眼看到自己那一轮收尾(它跑得比下一跳还快,接替者已经顶上来):
    # 管道按 run id 留了收尾快照,照 id 去取自己那一份。
    if mine is None:
        mine = pl.run_result(run)
    if mine is None:
        # 连收尾记录都没有(被更多轮挤出去了)。**这一档不许算成功**:我们既没
        # 看到 run-1 的任何阶段状态,也不知道它是跑完了、失败了还是被停的,写
        # 一句"同步完成"等于拿一份没人证明过的结果去记账(2026-09-30 第 3 轮
        # 审查实测)。如实记未知,日报照样生成——素材是库里已有的那些,不因为
        # 一个通道的结果查不到就整轮不出。
        entry.update(status='unknown',
                     detail='没拿到这一轮的同步结果(管道已被下一轮运行接替),'
                            '成败未知')
        _log(f'{label}:{entry["detail"]}')
        return entry
    # 只看这一轮请求了的那几个阶段:没选中的阶段一开跑就落 skipped(见
    # `pipeline.start_pipeline`),全收进来的话每个通道都要多报两句"条目化/
    # wiki 未执行"。
    stages = [(name, mine['stages'].get(name) or {}) for name in SYNC_STAGES]
    bad = [f'{name} {s.get("detail") or "失败"}'
           for name, s in stages if s.get('status') == 'error']
    # skipped / stopped = 这个阶段该做的事一件没做,**不能跟着算成功**。
    # 2026-09-30 实测:51 个大V账号全都缺 X 桥接配置,crawl 因此整个落 skipped
    # (「待配置 51 个」),而这里只数 error —— 那一轮 social 通道报的是绿色的
    # 「同步完成」,日报里那行只剩下 extract 的结果,"一条都没抓到"这件事连同
    # 原因一起看不见。超预算/被停止时管道落 stopped,同理。
    idle = [f'{name} {s.get("detail") or "本轮未执行"}'
            for name, s in stages if s.get('status') in ('skipped', 'stopped')]
    entry.update(status='error' if bad else 'skipped' if idle else 'ok',
                 detail='；'.join(bad + idle)[:400] if bad or idle else '同步完成')
    _log(f'{label}:{entry["detail"]}')
    return entry


def _run(since: datetime | None, until: datetime | None, sync: bool,
         channels: list[str], first_days: int) -> None:
    sched_day, retry = _state['sched_day'], False
    try:
        if sync:
            labels = _channel_labels()
            budget_end = time.monotonic() + SYNC_BUDGET
            for channel in channels:
                if _halting():
                    _state['channels'].append(
                        {'channel': channel, 'label': labels.get(channel, channel),
                         'status': 'skipped',
                         'detail': '已停止,未同步' if _state['stop_requested']
                                   else '超出时间预算,未同步'})
                    continue
                _state['channels'].append(
                    _sync_channel(channel, labels.get(channel, channel), budget_end))
        # 用户按的停止到此为止:接着生成等于把一份"同步到一半"的日报当成功交出去,
        # 还要再花一次模型钱。超预算是另一回事——那本来就是"别再同步了,拿现有的
        # 内容出报",继续生成正是它的目的。
        # 判断和"进入生成阶段"必须在同一把锁里:分两步的话,停止请求正好挤在中间
        # 时它既看不到 phase=generate(于是受理、承诺不出报)、又赶不上这道检查
        # (于是照样生成),承诺和行为当场对不上。
        with _lock:
            if _state['stop_requested']:
                _set('stopped', '已按请求停止,这一轮没有生成日报')
                return
            _set('generate', '正在生成日报')
        # 窗口的截止时刻要在**同步跑完之后**才定下来(没显式指定时 generate 自己
        # 取 now):日报覆盖的是"到生成这一刻为止",同步花掉的那几小时里产出的
        # 内容也算数。开跑时就把 until 钉死的话,那几小时会掉进两份日报的缝里。
        issues = _sync_issues()
        res = daily.generate(since=since, until=until, first_days=first_days,
                             progress=lambda text: _set('generate', text),
                             sync_issues=issues)
        _state.update(result=res, date=res['date'], since=res['since'], until=res['until'])
        # 完成这句话要把同步缺口说出来。"日报已生成"配一个绿勾、外加一份看起来
        # 完整的日报,用户没法区分"全都同步到了"和"一个来源都没同步到、直接拿
        # 旧素材出报"——后者实测就长这样(2026-09-30 审查:三个来源全部连接失败)。
        note = f"日报已生成:{res['docs']} 条素材"
        if res['late']:
            note += f"(另有 {res['late']} 条补录)"
        if issues:
            note += (f";{len(issues)} 个来源未同步成功("
                     + '、'.join(i['label'] for i in issues) + ')')
        _set('done', note)
    except LookupError as e:
        # "这段时间没有新内容"不是故障,重试多少次都是同一个结果。
        _state['error'] = str(e)
        _set('failed', str(e))
    except Exception as e:                       # noqa: BLE001 - 面板要看到原因
        retry = True
        _state['error'] = str(e)
        _set('failed', f'日报生成失败: {e}')
    finally:
        with _lock:
            _state['pipeline_run'] = ''
            _state['finished_at'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            _save_last_run()
            _state['running'] = False
        if sched_day:
            _finish_schedule(sched_day, retry)


def start(since: datetime | None = None, until: datetime | None = None,
          sync: bool = True, channels: list[str] | None = None,
          first_days: int | None = None, trigger: str = 'manual',
          sched_day: str = '') -> bool:
    """起一轮日报。已经在跑返回 False。

    `since`/`until` 都不给时由 `daily.next_window()` 接着上一份日报算,那是
    日常路径;显式给窗口是为了补一份旧的、或者重算一段。`sched_day` 是定时
    那一跳的日期(定时专用):这一轮的成败要记回那一天,而不是记在收尾时刻
    所在的那一天——同步跨过零点的话两者差一天,记错会把明天那一跳也吃掉。
    """
    settings = load_settings()
    known = _channel_labels()
    picked = [c for c in (channels if channels is not None else settings['channels'])
              if c in known]
    if since and until and since >= until:
        raise ValueError('日报的起始时刻必须早于截止时刻')
    # 勾了"先同步"却一个来源都没选,同步阶段会静静地什么都不做,面板上仍旧
    # 一路"成功"——"同步全部并生成"被悄悄降级成"直接生成",用户不知道素材
    # 根本没刷新(2026-09-30 审查实测)。要么给个来源,要么明确走"直接生成"。
    if sync and not picked:
        raise ValueError('同步范围里一个可用来源都没有:先勾选至少一个,'
                         '或者改用"直接生成"(只用已入库的内容出报)')
    with _lock:
        if _state['running']:
            return False
        _state.update({
            'running': True, 'phase': 'starting', 'detail': '',
            'date': until.date().isoformat() if until else '',
            'since': since.strftime(daily.STAMP) if since else '',
            'until': until.strftime(daily.STAMP) if until else '',
            'sync': bool(sync), 'trigger': trigger, 'error': '', 'log': [],
            'stop_requested': False, 'over_budget': False, 'sched_day': sched_day,
            'channels': [], 'result': None,
            'started_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'), 'finished_at': '',
        })
        window = f'{since:%m-%d %H:%M} 起' if since else '接上一份日报'
        _log(f'开始生成日报({window})'
             + (f',先同步 {len(picked)} 个来源' if sync else ''))
    threading.Thread(target=_run, args=(since, until, bool(sync), picked,
                                        first_days or settings['first_days']),
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
    # 先把下一跳推到重试时刻,再起跑:反过来的话,一轮跑得极快(比如不同步、
    # 当场报错)时它已经写完收尾状态了,这一行会把它盖回去。
    _mark_attempt(now)
    # 定时这一轮不指定窗口:起点接上一份日报的截止时刻,终点是同步跑完的那一刻。
    return start(sync=settings['sync'], channels=settings['channels'],
                 first_days=settings['first_days'], trigger='schedule', sched_day=today)
