# -*- coding: utf-8 -*-
"""日报这一轮的运行状态:进程里那份 `_state`、它的日志、以及「上次那一轮」的落盘。

从 `kb.daily_run` 拆出来(单文件 700 行上限)。**这里只放状态本身和读它的那几个纯
函数** —— 起跑、停止、同步编排、定时都还在 `daily_run` 里:那些要动管道、模型和配置,
不是状态。

`_lock` 也在这里:设置回写、定时账本、运行状态三处共用**同一把**可重入锁。一把锁就
没有锁序可反(元规则「并发与数据安全」);这几处也没有任何地方需要同时持有两把。

硬约束见 `docs/decisions/sync-schedule.md`。
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime

from kb.config import DATA_DIR, write_config_file

STATE_FILE = DATA_DIR / 'daily_last_run.json'

_lock = threading.RLock()


_state: dict = {
    'running': False, 'phase': '', 'detail': '', 'date': '',
    'since': '', 'until': '', 'sync': False, 'run_id': '',
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
    # 同一条口径:**写失败不许把上一次那份截成空文件**(就地 write_text 是先截断
    # 再写)。这一份只供面板显示「上次那一轮」,所以写不进去照旧只是吞掉 ——
    # 但盘上原来那份要留着,不然面板上那一块会从"上次跑过"变成"从没跑过"。
    try:
        write_config_file(STATE_FILE, {
            k: _state[k] for k in ('date', 'since', 'until', 'sync', 'trigger', 'started_at',
                                   'finished_at', 'error', 'channels', 'result', 'log')})
    except OSError:
        pass


def _load_last_run() -> dict | None:
    try:
        return json.loads(STATE_FILE.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return None


def _halting() -> bool:
    """还要不要继续同步下一个通道。两个原因分开记:用户按的停止会连生成一起
    取消,时间预算耗尽只是不再同步、照样拿已有内容出日报。"""
    return bool(_state['stop_requested'] or _state['over_budget'])


def _sync_issues() -> list[dict]:
    """这一轮没跑成功的那几项(error / unknown / 被跳过的都算)。

    **不止是来源**:抓取那一段按通道一项,入库那一段(索引与提取)自己占一项。
    它不属于任何通道,所以 `channel` 是空串、文案一律读 `label` —— 把全库那一段
    的红记到某个来源头上是错的归因(改两段式之前就是这么记的)。

    降级拿已有内容出报是对的("今天的日报没出来"比"少了两个来源"严重),但这份
    名单必须一路传下去:面板上的横幅、完成提示、写进日报文件的那几行都读它。
    """
    return [{'channel': c['channel'], 'label': c['label'],
             'status': c['status'], 'detail': c['detail']}
            for c in _state['channels'] if c['status'] != 'ok']
