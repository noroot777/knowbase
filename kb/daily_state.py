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


#: 日报这一轮的**结论**:出报要么成了、要么没成、要么被停了。
#: 这三个 phase 一落下就是**终态**,此后 `_set` 不再改写任何东西(见那边)。
SETTLED = ('done', 'failed', 'stopped')

_state: dict = {
    'running': False, 'phase': '', 'detail': '', 'date': '',
    'since': '', 'until': '', 'sync': False, 'run_id': '',
    'trigger': '', 'started_at': '', 'finished_at': '', 'error': '',
    #: 结论落下的时刻。**和 `finished_at` 是两件事**:后者是整轮(含出报之后那段
    #: 库整理)收尾的时刻,而出报一完成页面就该刷新阅读区 —— 中间那几十分钟里
    #: `running` 还是 True、`finished_at` 还是空的,前端认的就是这一格。
    'settled_at': '',
    #: 出报之后那一段(整理图鉴与 Wiki)自己的进度。**不和日报共用 phase/detail**:
    #: 它跑在 `finally` 里、复用同一个 `_sync_round`,共用的话会把已经确定的
    #: done/failed 改写成 sync,而前端只在 phase==='failed' 时留失败横幅 —— 一次
    #: 没出来的日报于是被报成「日报已生成」(`docs/decisions/sync-schedule.md`)。
    'library': {'phase': '', 'detail': ''},
    'stop_requested': False, 'over_budget': False, 'sched_day': '',
    # 我们自己起的那一轮管道的 run id(没起就是空串)。日报允许在别人的管道
    # 跑着时排队等,这时候用户按"停止"只该结束我们的等待,不能顺手把别人的
    # 同步也停掉。**记 id 而不是布尔**:布尔只回答"是不是我起的",答不了
    # "现在跑的还是不是我那一轮"——我们那轮在两次轮询之间跑完、另一项普通
    # 同步紧接着拿到全局管道时,布尔仍是 True,停止就落到了别人头上
    # (2026-09-30 审查实测)。
    'pipeline_run': '',
    #: 这一轮开的那个业务批次(同步中心那份计划的 owner,停止按它核对是不是这一批)。
    'batch': '',
    'channels': [], 'result': None, 'log': [],
}


# ── 运行状态 ────────────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    _state['log'].append(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")
    del _state['log'][:-60]


def _set(phase: str, detail: str = '', *, quiet: bool = False) -> None:
    """写这一轮的 phase / detail。**结论落下之后这个函数一个字都不再改。**

    守卫放在这里而不是各调用点:出报之后还有一段库整理要跑,它**复用同一个
    `_sync_round`**,而那个函数一路 `_set('sync', …)`。拦不住的话已经确定的
    done/failed 被改写成 sync —— 前端只在 `phase === 'failed'` 时留失败横幅,
    于是一次**根本没出来**的日报被报成「日报已生成」(实测终态
    `phase='sync', error='Claude 503', result=None`)。把守卫写在调用点就是
    「下一个复用 `_sync_round` 的人再踩一遍」。

    新的一轮由 `start()` 直接重设 `_state`(不过这个函数),所以不会被冻住。
    库整理自己的进度进 `_state['library']`,见 `library_progress`。

    `quiet=True` 给**两秒一跳的轮询进度**:只换 detail,不进日志 —— 不然同一句话
    每两秒刷一行,60 行的日志当场只剩这一个阶段。
    """
    if _state['phase'] in SETTLED:
        return
    _state['phase'] = phase
    if phase in SETTLED and not _state['settled_at']:
        _state['settled_at'] = datetime.now().isoformat(timespec='seconds')
    if detail:
        _state['detail'] = detail
        if not quiet:
            _log(detail)


def library_progress(phase: str, detail: str = '', *, quiet: bool = False) -> None:
    """出报之后那一段自己的进度。**只写 `_state['library']`,碰不到日报的结论。**

    签名和 `_set` 对齐(`_sync_round` 的 `progress` 认的就是这个形状)。`quiet` 同上。
    """
    _state['library'] = {'phase': phase, 'detail': detail}
    if detail and not quiet:
        _log(detail)


def fresh_round() -> dict:
    """一轮开跑时要归零的那几格。**「新的一轮长什么样」口径只有这一份。**

    新一轮**不走 `_set`**(它认结论冻结,见上),所以归零只能发生在这里;而回归里
    直接驱动 `_run` 的摆盘也必须过它 —— 不过的话上一条用例留下的那个 `done` 会把
    这一轮**整轮冻住**(实测:同跑时 7 条红、单跑全绿,正是最难归因的那一档)。
    各自列一份字段的话,以后新增一格就只有一边记得加。

    `running` / `run_id` / 窗口那几格不在这里:它们是**这一轮自己的身份**,由
    `start()` 按参数填。
    """
    return {'phase': 'starting', 'detail': '', 'error': '', 'log': [],
            'settled_at': '', 'library': {'phase': '', 'detail': ''},
            'stop_requested': False, 'over_budget': False, 'batch': '',
            'channels': [], 'result': None, 'finished_at': ''}


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
                                   'finished_at', 'settled_at', 'error', 'channels',
                                   'result', 'log')})
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
