# -*- coding: utf-8 -*-
"""管道运行状态:阶段进度、日志、最近一次结果的持久化。

单进程单管道,状态就是一个模块级 dict(与 WeChatOA 的 _crawl_state 同套路),
前端轮询 /api/pipeline/status 展示。各阶段实现与编排都改这里的状态,
单独成模块是为了让 stages / retry / 编排三方都能依赖它而不成环。
"""

from __future__ import annotations

import secrets
import threading
import time
from copy import deepcopy
from datetime import datetime

from kb import pipeline_open as _open
from kb.config import DATA_DIR
# 留档口径(留几天、超了削什么)在它自己那个模块里;`history_keep_days` 是面板那句
# 「保留最近 N 天」的公开入口,这里只是过一道(端点走 `kb.pipeline`)。
from kb.pipeline_retention import history_keep_days as history_keep_days
# 给人翻的那一份(历史记录的落盘、读取与「谁顶谁」)拆成了自己的模块,**只因为
# 行数**。这几个名字在本模块里**再导出一次**:几十处调用方和测试打桩点按
# `pipeline_state.xxx` 认它们,而 `_end_run` 的收尾循环读的也是本模块的全局 ——
# 打桩 `ps._save_history` 因此照旧拦得住(方向只有一个:本模块 → 那边)。
from kb.pipeline_history import (
    _history_file as _history_file,
    _history_rounds_strict as _history_rounds_strict,
    _run_record as _run_record,
    _save_history as _save_history,
    load_history as load_history,
    update_history_round as update_history_round,
)
from kb import pipeline_results as _results
from kb.pipeline_persist import (
    _SEQ_GEN,
    _dump_bytes,
    _history_runs,
    _history_shape_ok,
    _load_json,
    _state_shape_ok,
    _load_json_strict,
    _newest_round,
    _seq_of,
    run_record,
    _write_atomic,
    result_version as _version_of,
    retry_notes as retry_notes,  # 失败名单那本账的读法:判据层一份,这里只是过一道
    row_key as row_key,          # 面板那条历史清单的公开入口,这里只是过一道
    valid_round as valid_round,  # 同上:历史端点标「这一行有效吗」过的也是它
    same_round as same_round,    # 同上:起跑核对问「这两份是不是同一轮」也过它
)

_STATE_FILE = DATA_DIR / "pipeline_state.json"

# 给人翻的那几轮,**单独一个文件**。不和 `_STATE_FILE` 合并:那一份喂的是「重试
# 失败项」的范围派生(`pipeline_retry` 从它恢复 crawl_filter / export_filter /
# 失败名单),而那条路径读错一个字段的后果是**静默不抓** —— 用户点重试、面板报
# 「已跳过」,该补的那几条一个都没抓(见 docs/decisions/retry-scope.md)。拿一条
# 不可恢复的路径去换一个只读展示不值,所以那份文件的形状一个字节都没动,代价是
# 最新那一轮在两处各存一份(约 6 KB),有意的冗余。
def _open_file():
    """开工标记也在 `_STATE_FILE` 旁边,**派生,理由同上**。"""
    return _STATE_FILE.with_name(f"{_STATE_FILE.stem}_open{_STATE_FILE.suffix}")


def mark_open(run: dict) -> str:
    """占住这一轮的持久化边界。**空串 = 占住了;非空 = 这一轮不许起跑的理由。**

    **两个起跑入口都过这里**(「口径只有一份」),而且都在 `_lock` 里、`_state` 还没
    认下这一轮之前调 —— 占不住就什么都没改,不用回滚。放在 `_state.update` 之后的话
    「内存说在跑」和「磁盘说开工了」中间有个窗口,而拒绝起跑又得把那一堆字段挨个
    撤回来(撤漏一个就是下一轮带着上一轮的范围)。`run` 的内容契约在 `_open.mark`。
    """
    err = _open.mark(_open_file(), run)
    if err:
        _log(f'{err}没能落盘，这一轮没有起跑')
    return _open.NO_BOUNDARY.format(err=err) if err else ''

STAGES = ("crawl", "export", "index", "extract", "projects", "wiki")

_state: dict = {
    "running": False,
    "stop_requested": False,
    "stopped": False,
    # 这一轮运行的身份。管道是单例,但"正在跑"这个布尔认不出**跑的是哪一轮**:
    # 日报在两次两秒轮询之间,自己那一轮可能已经结束、另一项同步已经接替上来,
    # 它看到的仍是连续的 running=True(2026-09-30 第 2 轮审查实测)。有了 id,
    # "停我自己那一轮"和"等我自己那一轮"才表达得出来。永不复用(进程内自增)。
    "run_id": "",
    # 这一轮的**完成序号:跨进程单调**。两份落盘各自可能写失败,所以磁盘上它们
    # 记着的可能是不同轮次 —— 而恢复时必须分得出谁更新,墙上时钟做不到(`_now()`
    # 只到秒,两轮落在同一秒不罕见)。收尾时发(`_next_seq`),和终态一起落进两份。
    "run_seq": 0,
    # 发号世代。**内存这份终态也要带它** —— `event_id` 认的是出处,不带的话活进程
    # 里那份快照的事件身份是空的,起跑核对就只剩「内容逐字相同」那一级兜底(那是给
    # 旧格式留的退路,不该当主路使)。
    "seq_gen": _SEQ_GEN,
    "current_stage": "",
    "started_at": "",
    "finished_at": "",
    "crawl_filter": {},
    # 导出阶段**自己**那一份来源范围。常规同步里它和 crawl_filter 一样,重试时
    # 不一样:crawl 收窄到失败的那几个目标,而 export 要照旧走上一轮原来那几个
    # 来源(一个来源完全可能 crawl 成功、export 失败)。**所以它必须单独存一份**
    # —— 只存收窄后的 crawl_filter 的话,下一次重试会从那份收窄范围再派生一次
    # 导出范围,仍然导出失败的那个来源就此被摘掉:面板报「导出完成·已保存 0 条」,
    # 而它那几篇文章一个都没落盘,再点重试也不会回来(2026-10-09 第 2 轮审查实测)。
    "export_filter": {},
    # 「重试失败项」要重试谁:{"run": 这一轮的身份, "channels": {来源: 失败名单}}。
    # 见 `pipeline_segments.note_retry_targets` —— 带着 run id,所以不需要在每条启动路径上各清一次。
    "crawl_retry": {},
    "run_kind": "",
    # 这一轮属于哪个业务批次(`pipeline_batch.open_batch` 发的身份),空串 = 不属于任何批次。
    # **每条启动路径都要显式写一遍**:`_state` 是进程内常驻的,不写的话上一轮
    # 日报的批次身份会漏进下一轮独立运行,于是它被合进一个早就结束的批次。
    "batch": "",
    "planned_stages": None,
    # 每阶段: {status: pending/running/ok/skipped/error/stopped, detail: str}
    "stages": {s: {"status": "", "detail": ""} for s in STAGES},
    "log": [],          # 最近若干条进度行
}
_lock = threading.Lock()

# 这个进程自己那一截身份。**`run-{序号}` 只在进程内唯一,而历史落盘按身份去重** ——
# 重启之后序号从 1 重新开始,新的 `run-1` 和上个进程那个 `run-1` 并不是同一次运行,
# 却会把它从历史里**替换掉**(连它的失败原因和日志一起),而且后面每一轮接着覆盖对应
# 的那一轮:远在保留上限之前就开始丢,而丢没丢在面板上一个字都看不见。
# 取「启动秒 + 随机两字节」而不是 pid:pid 会被系统复用,而同一秒里重启两次也不算
# 罕见(口径同 `catgraph.md` 里发布序号那条 —— 自增计数 + 随机后缀)。
_RUN_EPOCH = f'{int(time.time()):x}{secrets.token_hex(2)}'
# 身份里那一截自增。**跟 `_state["run_seq"]`(完成序号)不是一回事** ——
# 这个只为了让 run id 在进程内不重复,那个是跨进程单调的落盘顺序凭据。
_id_seq = 0
# 本进程发出去的最大**完成序号**。磁盘上那两份都可能写失败、还会被截断,所以
# 「下一轮读磁盘拿到的最大值」不一定盖得住本进程发过的号 —— 理由在 `_next_seq`。
_seq_hwm = 0

# 最近一次留档失败的异常类型,空串 = 上一次留档是成功的。面板拿它如实说一句 ——
# **留档失败不许声张成同步失败,但也不许静默**:只看页面的话,「历史里没有这一轮」
# 和「这一轮还没跑过」长得一模一样。
_persist_error = ''

# 已经收尾的那几轮运行的终态,按 run id 存着。等一轮同步的人(日报)靠两秒一跳的
# 轮询取快照,而全局状态只有一份:我们那一轮收尾、另一轮马上接替时,轮询就再也
# 看不到自己那一轮的阶段结果了——只知道"不是我的了",不知道它是成功、失败还是
# 被停的。没有这份记录只能猜,而猜成"成功"就是拿一份没人证明过的结果去记账
# (2026-09-30 第 3 轮审查实测)。只留最近几轮:够覆盖"等的那一轮刚被接替"这个
# 窄窗口,又不让状态无限长。
_finished: dict[str, dict] = {}
# 跟落盘那份历史的 `_HISTORY_DAYS` **不是一回事,也不该统一**:这一份只为"等的那
# 一轮刚被接替"这个窄窗口,几轮就够;那一份是给人翻的,按天留。
_FINISHED_KEEP = 8


def new_run_id() -> str:
    """给新一轮运行发一个身份。只在 `_lock` 里调(`start_pipeline` 那一段)。

    **跨进程唯一**,不只是进程内 —— 它同时是历史落盘的去重键,理由见 `_RUN_EPOCH`
    那段注释。老库里 `run-N` 那种旧身份不会和新格式撞(新格式多一截),所以它们不会
    被误替换,到点自然被保留上限挤出去。
    """
    global _id_seq
    _id_seq += 1
    return f'run-{_RUN_EPOCH}-{_id_seq}'


def run_result(run_id: str) -> dict | None:
    """那一轮运行的终态(还在跑、或者已经被挤出记录时回 None)。

    形状和 `get_state()` 的相关字段一致(`stages` / `stopped` / `finished_at`),
    调用方拿它当"我那一轮的最后一份快照"用。
    """
    with _lock:
        got = _finished.get(run_id)
        return deepcopy(got) if got else None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    _state["log"].append(_stamp(msg))
    del _state["log"][:-80]


def _stamp(msg: str) -> str:
    # 带日期:日志保留 80 行且跨天不清,只打时分秒会看不出是哪天跑的
    return f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}"


#: 失败明细带进状态体的条数上限(历史积压场景可能一次几百篇)。
_FAILURES_KEEP = 50


def _cap_failures(entry: dict, failures: list[dict]) -> dict:
    """把失败明细放进阶段结果,**截断了就把总数一起留下**。

    只截不记的后果:面板照着数组长度写「问题明细 · 50 项」,而真实失败数可能是
    三百 —— 用户看到的是一个看着完整的清单,余下那些连"还有"都不知道。重试名单
    另存(`crawl_retry`),不从这个展示用的数组反推,所以截断不影响重试范围。
    """
    entry["failures"] = failures[:_FAILURES_KEEP]
    if len(failures) > _FAILURES_KEEP:
        entry["failures_total"] = len(failures)
    return entry


def _stage(name: str, status: str, detail: str = "",
           failures: list[dict] | None = None) -> None:
    """failures: [{"title", "reason"}] 失败明细,面板卡片可展开查看。"""
    entry: dict = {"status": status, "detail": detail}
    if failures:
        _cap_failures(entry, failures)
    # 分段账本在 `pipeline_segments`(**依赖方向单向:那边 → 这边**,所以反过来
    # 用它只能在函数里 import;理由写在那个模块的头上)。这一阶段没在分段时
    # `fold` 返回 None,照原样写 —— 只更新当前那一段、再合成一条对外结论的判据
    # 全在那边,这里不许再判一次。
    from kb.pipeline_results import note
    from kb.pipeline_segments import fold
    # **两份账,各答一个问题。** 分段那一份答「面板上这张卡片怎么写」(各段扁平合并,
    # 不带来源归属);结果账答「哪个来源这一步什么结论、实际覆盖了什么」—— 当日欠账
    # 按它算。**记的是合并之前这一条**,合并之后就分不出是谁的了(第 3 轮审查 R2)。
    note(name, entry)
    folded = fold(name, entry, detail)
    if folded:
        entry, detail = folded
    _state["stages"][name] = entry
    if status == "running":
        _state["current_stage"] = name
    if detail:
        _log(f"{name}: {detail}")


def _next_seq() -> int:
    """给这一轮发一个完成序号,**比磁盘上记着的任何一轮都大;证明不了就发 0**。

    取两份现有记录里的最大值 +1:哪一份写失败都不影响下一轮继续递增,所以
    「状态记着第 1 轮、历史记着第 2 轮」这种失配也分得出先后(`_newest_round`)。

    **读不出来一律发 0,不许从 1 重新开始。** 0 在 `_seq_of` 那边表示「这一轮没有
    顺序凭据」,恢复时序号那一档直接走不通、退到位置和时间去证明。发一个看着合法的
    小整数是**最坏的那一档**:它和磁盘上那些号不相等,于是恢复层拿它当成「谁更晚」
    的证明用 —— 实测形态是磁盘上记着第 3 轮、这一轮读失证发了 1,恢复当场判出「第
    3 轮更新」,页头显示那一轮的失败、点「重试失败项」去抓了它那个来源,而这一轮
    明明已经成功。**失证必须是「没有凭据」,不是「一个小凭据」。**

    `_seq_hwm` 是**本进程发出去的最大号**,一道**冗余带**:读成功时磁盘上最新那一轮
    的号就是本进程上一轮发的(它写成功了),所以 +1 天然单调 —— 定点去掉它红 0 条,
    是真冗余不是用例不敏感。留着是因为它兜的那一档(磁盘上的最大值比本进程发过的
    还小)将来可能真出现:截断只留最近几轮、或者有谁写个清理脚本把老记录删掉。
    """
    global _seq_hwm
    try:
        seen = [_seq_of(_saved_round_strict() or {})]
        seen += [_seq_of(r) for r in _history_rounds_strict()]
    except Exception:  # noqa: BLE001 - 理由就在上面那段:失证发 0
        return 0
    nxt = max(seen + [_seq_hwm]) + 1
    _seq_hwm = nxt
    return nxt


def _saved_round_strict() -> dict | None:
    """状态文件里那一轮的**严格版**,口径同上。发号和恢复共用它。"""
    raw = _load_json_strict(_STATE_FILE)
    if not _state_shape_ok(raw):
        raise ValueError(f'状态文件形状不对：{type(raw).__name__}')
    return raw


def run_payload() -> dict:
    """一轮运行的终态记录。**落盘的那份和按 id 留的那份是同一份**。

    两处曾经各写各的:`_finished` 只留了身份、结束时刻、stopped 和 stages。于是
    "没亲眼看到自己那一轮收尾、照 id 去取"的调用方(`daily_sync._sync_round` 被
    接替的那条路)拿到的是一份**缺计划、缺来源范围、缺失败名单**的记录 —— 它看着
    像一份完整结果,合并时却因为没有 `planned_stages` 被整个过滤掉,那一轮真实
    的抓取失败就此消失。契约只有一份,两边都用它。
    """
    return {
        "started_at": _state["started_at"],
        "finished_at": _state["finished_at"],
        "stopped": _state.get("stopped", False),
        "run_kind": _state.get("run_kind", ""),
        "planned_stages": _state.get("planned_stages"),
        "stages": deepcopy(_state["stages"]),
        "crawl_filter": deepcopy(_state["crawl_filter"]),
        # 导出范围单独落一份:下一次重试要从**它**恢复,不是从收窄后的抓取范围
        "export_filter": deepcopy(_state.get("export_filter") or {}),
        # run_id 跟名单一起落盘:retry_notes 要拿它认"这份名单是不是这一轮的"
        "run_id": _state.get("run_id", ""),
        # 完成序号 + 发号世代:恢复时靠它们分出两份落盘各记着哪一轮(见
        # `pipeline_persist._seq_of`)。**合并出来的那份也要带**,否则整批那一份一
        # 落盘就是「没有顺序凭据」,重启之后反而被判成比子轮更旧。
        "run_seq": _state.get("run_seq", 0),
        "seq_gen": _state.get("seq_gen"),
        "crawl_retry": deepcopy(_state.get("crawl_retry") or {}),
        # 结果账:逐来源的结论和它实际覆盖的范围(`kb/pipeline_results.py`)。
        # **跟着这份契约走**,当日账本折的就是它 —— 落不下来的话折账只能退回
        # 「把整步那张合并卡片摊到每个来源上」,一轮里成功的来源也被记成欠着。
        "results": _results.snapshot(),
        "log": list(_state["log"]),
    }



def _write_state(payload: dict) -> None:
    """把一份结果落到状态文件。**原子替换**,不是就地改写。

    就地 `write_text` 的话,写到一半退出(重启、OOM、断电)留下的是半截 JSON ——
    读的那一端只能当"读不出",而旧那份已经被截掉了,两份都没了。
    口径只有一份:普通一轮和批次汇总都过这里(`pipeline_persist._write_atomic`)。
    """
    _write_atomic(_STATE_FILE, _dump_bytes(payload))



def _save_last_run() -> None:
    """持久化最近一次结果,重启 kb-web 后仍能看到。

    **异常不在这里吞**:两份落盘都由 `_end_run()` 那段统一收尾兜住,并如实记一笔
    诊断(`persist_error()`)。在这里各吞一次的话,「写没写上」就再也报不出去了。

    写的是 `run_payload()` —— **落盘那份、按 id 留的那份、并进批次汇总的那份是同一个
    契约**(理由见那个函数)。
    """
    _write_state(run_payload())



# 三份留档各自的人话名字。**不按函数名切片**:那样切出来的是 `sh_in_batch`
# 这种东西,而这行字是给用户看的。取的时候带兜底(`.get`):这张表只喂一句日志,
# 将来多一份留档而忘了在这儿加一行的话,该报的那次失败照旧报出来、只是名字难看
# —— 让一句日志的 KeyError 从收尾里逃出去才是真麻烦。
_SAVE_LABEL = {'_publish_in_batch': '同步结果', '_save_daybook': '当日账本',
               '_save_history': '历史记录'}


def _save_daybook() -> None:
    """把这一轮折进**今天那份账本**(「重试失败项」的计划和范围从它派生)。

    折的是这一轮**自己**那份终态,不是整批合并出来的那一份 —— 理由在
    `pipeline_daybook` 的模块头(合并之后的来源清单摊开会把别人那一格也染红)。

    延迟 import,同 `_run_record`:账本层在模块级 import 本模块。
    """
    from kb.pipeline_daybook import fold
    fold(run_payload())


def persist_error() -> str:
    """最近一次留档失败的异常类型,空串 = 没失败。理由见 `_persist_error`。"""
    return _persist_error


def load_last_run() -> dict | None:
    """上一轮的终态,**磁盘那一份**。读不出来一律 `None`(口径和历史共用 `_load_json`)。

    只在**重启之后**该直接用它 —— 活着的进程里请用 `last_result()`,理由在那边。
    """
    raw = _load_json(_STATE_FILE)
    return raw if isinstance(raw, dict) else None


# 恢复的结果:'' = 还没恢复成功(**也包括这次读不通,下一次还要再试**),
# 'ok' = 已经恢复完,或者确认这个库从没跑过。**懒做,不在模块级做** ——
# `_STATE_FILE` 的位置测试要 patch,模块 import 那一刻读到的是生产那一份。
_restored = ''

# 恢复得不完整时记一笔:历史能证明有更新的一轮,而那一轮的**重试派生范围**
# (`export_filter` / `crawl_retry`)只存在于那份没写上的状态文件里。空串 = 完整。
_restore_gap = ''


def restore_gap() -> str:
    """手上这一份终态**可信到什么程度**。空串 = 可信的最新,且完整。

    五档非空值,**都意味着「重试失败项」不能起跑**(闸在
    `pipeline_retry.start_retry_failed`,面板上那颗按钮同时按不下去并说明原因):

    - `'scope'`   挑中的是历史那一份:它的阶段、日志、抓取范围都齐(所以展示是
                  对的),但**没有导出范围和失败名单**
    - `'unknown'` 恢复时有一份读不出来,**核对不了哪一轮更新** —— 展示的是能读到
                  的那一份,它可能是旧的
    - `'order'`   两份各记着不同的一轮,而**先后证明不了**(都没有完成序号,收尾
                  时间也相同)
    - `'lost'`    **有一轮开工过,而磁盘上最新那一份不是它** —— 它的终态一个字都
                  没落下来(两份写入都失败、或者进程在收尾前没了)。屏幕上那一轮
                  **已经过期**
    - `'broken'`  **最新那一轮确实发生过,可它的留档渲染不了**(日志落成字符串、
                  失败名单形状不对……)。展示退回到读得出来的更早那一份 —— 所以
                  屏幕上那一轮**不是最新的**,它的范围更不能拿来重试

    **跑完一轮就补回来了**(`_end_run` 收尾时清空):那一轮是在本进程里跑的,内存
    里这份终态带着完整字段,也不存在「谁更新」的疑问。
    """
    return _restore_gap


def _restore_once() -> None:
    """把磁盘终态恢复进 `_state`。**一律在 `_lock` 外面调。**

    **「最新那一轮的终态在内存里」这个契约,重启之后也得成立** —— `last_result()`
    和起跑核对都建在它上面,而新进程的 `_state` 里身份、起止时间全是空的,于是
    重启之后那一档就有个洞:面板拿空身份当「这份附属数据不是这一轮的」,一条
    待处理数查询刚回来就作废、紧接着再查一次,实测 1.3 秒里发出 2000 多次(每次
    都去扫一遍库),而页面上那个数始终是空的;起跑核对那边反过来 —— 空身份对不上
    磁盘上那份快照,就得靠「拿不准一律放行」绕过去,而那正好把并发不变量也一起
    绕过了。

    **两份落盘都要核对,不能只信状态文件。** 那两份是分别写、分别诊断的(见
    `_end_run`),所以状态文件写失败而历史写成功时,磁盘上最新的那一轮**只在历史
    里**。只读状态文件的话恢复到的是**上一轮**,而且它会被当成「最新」固化下来:
    实测里上一轮成功、这一轮翻译 503,重启之后页头写着「同步已完成」、而历史第一
    条就是那个失败;反过来摆(上一轮失败、这一轮成功)点「重试失败项」,导出阶段
    实际收到的是**上一轮那个来源**(`accounts=['A']`)。
    历史那份记录**没有重试派生字段**(有意的,见 `_run_record`),所以取它时记一笔
    `_restore_gap`,由重试那边据此拒绝起跑 —— **「只能证明有新结果、恢复不了它的
    范围」要如实说出来并拦住,不许静默退回上一轮的范围**(那一档的后果是导出摘掉
    失败那个来源、面板报「已保存 0 条」,见 `retry-scope.md`)。
    """
    global _restored, _restore_gap, _seq_hwm
    if _restored:
        return
    with _lock:
        # 已经跑过(或正在跑)就不是「刚启动」,磁盘那份不该盖掉内存里的。
        if _state["running"] or _state.get("finished_at"):
            _restored = 'ok'
            return
        try:
            # **形状不对和读不出来同一档**(判据在 `_state_shape_ok` /
            # `_history_shape_ok`,发号那边过的是同两个):「可解析但形状不是预期
            # 的」当成「这份副本没有」的话,恢复就静默认可了另一份 —— 而它可能正是
            # 旧的那一轮。
            saved = _saved_round_strict()
            runs = _history_rounds_strict()
        except Exception:  # noqa: BLE001 - 理由在 `_load_json_strict` 的 docstring
            # **读不通不许固化成「从没跑过」**:下一次读状态时再试一遍。
            # 但**这一刻手上那一份不可信**:`_latest_locked()` 会回退到状态文件
            # 那一份,而它可能是旧的(另一份这会儿读不出来、核对不了)。实测形态:
            # 磁盘状态记着 A、历史记着更新的 B,只对历史文件注入读失败 —— 展示 A、
            # 按钮可用,点一次重试导出阶段真去抓了 A 那个来源。**同源不等于最新**,
            # 所以这一档要如实报出来并拦住重试。
            _restore_gap = 'unknown'
            return
        pick, gap = _newest_round(runs, saved)
        # **现存的候选不等于「发生过的全部运行」。** 两份终态都没写上那一档,磁盘上
        # 一个字都没留下这一轮 —— 剩下的两份旧副本彼此一致,`same_event` 于是正确地
        # 证明「它们是同一次事件」,可那证明不了「没有更新的一轮」。开工标记就是为了
        # 补上这一句,理由和实测形态都在 `pipeline_open`。
        og = _open.gap(_open_file(), [saved, *runs])
        if og == 'unknown':
            # **读不出来不是「不存在」,而且不许固化**(同上面那一档):下一次读状态
            # 时再试一遍。固化的后果是一次瞬时读失败就永久认定「没有未交付的运行」
            # —— 实测解除注入之后同一份文件正确报出 `lost`,能力一直在。
            _restore_gap = 'unknown'
            return
        if og:
            gap = og
        if not isinstance(pick, dict) or not pick.get("finished_at"):
            # **「一份都挑不出来」不等于「确定从没跑过」。** 留档坏到渲染不了时那一轮
            # 确实发生过(欠账是 `broken`),这里把欠账一起吞掉的话,面板报「尚无运行
            # 记录」而「重试失败项」照旧可点 —— 它会按磁盘上那份读得出来的旧范围抓。
            # 所以欠账原样记下来,再说「恢复这件事做完了」(坏正文不会自己变好,
            # 不像 `unknown` 那一档要下次再试)。
            _restore_gap = gap
            _restored = 'ok'
            return
        for key in ("started_at", "finished_at", "run_kind", "planned_stages",
                    "run_id", "run_seq", "log"):
            if key in pick:
                _state[key] = deepcopy(pick[key])
        # **出处要跟着那一轮走,不许沿用内存里的默认值。** `_state` 初始带的是当前
        # 世代,而恢复回来的可能是旧格式记录(压根没有这个字段)—— 不覆盖的话它在
        # 内存里就**冒充**成了当前世代,于是它那个只有进程内自增的 `run_id` 和可能
        # 回退过的 `run_seq` 都被当成可信凭据用。`seq_gen` 的全部意义就是「凭据自己
        # 带出处」,这里沿用默认值等于把出处改掉了。
        _state["seq_gen"] = pick.get("seq_gen")
        # **恢复来的那一轮同时抬高水位**,和 `run_seq` 一起:内存里这份终态该和磁盘
        # 那份一致。这两笔也是**冗余带**(定点去掉红 0 条,理由同 `_next_seq`)——
        # 发号读的是磁盘不是内存,所以它们眼下改变不了任何可观测行为。
        _seq_hwm = max(_seq_hwm, _seq_of(pick))
        _state["stopped"] = bool(pick.get("stopped", False))
        # **结果账也要恢复**(`results`):重启之后那颗按钮照旧要知道「这一轮里公众号
        # 已经成功了」。丢掉它的后果是降级规划退回「整步那张合并卡片」,把成功的
        # 来源也算成欠着 —— 状态文件里明明写着 `results.crawl.wechat_local=ok`,
        # 恢复后自己把这份事实扔了,点一次重试又把公众号抓一遍(第 4 轮审查 R2
        # 实测 `restored.results=<字段缺失>`、`retry_wechat=['wechat_local']`)。
        for key in ("stages", "crawl_filter", "export_filter", "crawl_retry",
                    "results"):
            got = pick.get(key)
            # 取历史那份时 `export_filter` / `crawl_retry` 本来就没有:**留空而不是
            # 沿用上一轮的**,那两个字段错一个就是静默不抓。存量记录没有 `results`
            # 也走这一档 —— 空账等于「说不出来」,折账自然退回保守档。
            _state[key] = deepcopy(got) if isinstance(got, dict) else {}
        _restore_gap = gap
        _restored = 'ok'


def result_version(run: dict | None = None) -> str:
    """「现在这一屏是哪个版本」。判据本身在 `pipeline_persist` 里,这一层只负责
    **缺省拿当前运行状态**:调用方问的多半就是「此刻」,而那份状态只有这边有。
    """
    return _version_of(_state if run is None else run)


def last_result() -> dict:
    """「最新那一轮的终态」**只有这一份口径**,展示和重试都从这里取。

    优先内存:`_state` 在 `_end_run()` 收尾后存的就是终态(阶段修正已经做完)。
    **磁盘那份是给重启做恢复的,它写失败了不该改变活进程里「最新是哪一轮」这个
    事实** —— 原来展示和重试各自直读磁盘,于是最近状态写失败(权限、满盘)那一轮
    就静默落后一整轮:实测里新一轮翻译报了 503、内存终态是 error,而面板显示上一轮
    的「已保存 3 条·同步已完成」、连重试按钮都没有;方向反过来就是显示着上一轮的
    失败、而「重试失败项」去抓**上一轮那条订阅**。
    **`running` 时回退磁盘**:那时内存里躺的是正在跑的这一轮(阶段还是 pending),
    拿它派生重试计划会错 —— 这一档要的恰恰是「上一轮」,而那只有磁盘有。
    """
    _restore_once()
    with _lock:
        return _latest_locked()


def _latest_locked() -> dict:
    """同一件事,**调用方已经持有 `_lock`** 的那个版本。

    抽出来是为了让起跑那道并发核对跟展示**走同一份口径**:核对必须在锁内比,而
    `last_result()` 自己要拿锁。各写一遍的话两边会在「恢复还没成功」那一档上岔开
    —— 展示从磁盘读到了真实那一轮,核对却拿空内存去比,于是每一次重试都被拒
    (实测:连点两次都是 409「这期间又跑完了一轮」,而其实什么都没跑过)。
    """
    # 判据是**收尾时间**,不是身份 —— 恢复进来的旧格式记录没有 `run_id`,
    # 而它一样是一份已经收尾的终态。
    if not _state["running"] and _state.get("finished_at"):
        # 一轮日报的几次管道运行合并出来的那份汇总**不是任何一个 `_state`**,
        # 而它才是这一刻的最新(`_state` 只剩最后那个子轮:入库那一轮的 crawl 是
        # 「未选择」,前面几轮的抓取失败和重试名单都不在里面)。独立的一轮收尾时
        # 它已经被清掉(见 `pipeline_batch._remember_aggregate`),所以这里不会读到更旧的东西。
        from kb.pipeline_batch import _batch_aggregate    # 延迟 import,同上
        batch = _batch_aggregate()
        return deepcopy(batch if batch is not None else _state)
    # 回落到磁盘那份时**要过同一道形状判据**。这一档走的是「恢复还没成功 / 恢复
    # 拒了磁盘那份」,而拒的理由多半正是形状不对(`valid_round`)——
    # `load_last_run()` 是宽口径的「磁盘那份原样」(还有只取失败名单的消费者在用
    # 它),不加这一道的话被恢复挡在门外的那份记录从这里原路进来,照旧交给面板整屏
    # 渲染和重试派生。**宁可报「尚无运行记录」**:那是实话,而且下一轮就补回来了。
    disk = load_last_run()
    return disk if valid_round(disk) else {}


def get_state() -> dict:
    _restore_once()
    return deepcopy(_state)


def request_stop(run_id: str = "") -> bool:
    """Finish active work before releasing the single-pipeline lock.

    `run_id` 非空时只停这一轮:调用方手里那个 id 跟当前运行不一致,说明自己
    那一轮已经结束、现在跑的是别人起的,停它就是越权(日报的"停止"原来就会
    误停紧接着启动的普通同步)。空串 = 停当前这一轮,不问是谁起的(面板上那个
    停止按钮就是这个语义)。
    """
    with _lock:
        if not _state["running"]:
            return False
        if run_id and _state.get("run_id") != run_id:
            return False
        if not _state.get("stop_requested"):
            _state["stop_requested"] = True
            _log("已请求停止：当前执行中的阶段结束后，不再启动后续阶段")
        return True


def _end_run() -> None:
    # 延迟 import,同 `_run_record`:批次层和分段账本都在模块级 import 本模块。
    from kb.pipeline_batch import _publish_in_batch, _remember_settled
    from kb.pipeline_segments import clear_segments
    with _lock:
        # 分段是"这一轮某个阶段被几个来源各跑一遍"的临时账本,收尾时一定要清掉:
        # 留着的话下一轮的第一条汇报会被并进上一轮的段里。
        clear_segments()
        stopped = bool(_state.get("stop_requested"))
        _state["stopped"] = stopped
        for name, stage in _state["stages"].items():
            if stage["status"] in ("pending", "running"):
                _stage(name, "stopped" if stopped else "error",
                       ("已停止，未继续执行" if stopped else "流程中断，未完成")
                       + (" · " + stage["detail"] if stage.get("detail") else ""),
                       failures=stage.get("failures"))
        _state["current_stage"] = ""
        _state["finished_at"] = _now()
        # **落盘之前发完成序号**,两份都带上它 —— 恢复时靠它分出谁更新。
        # **出处和号是一对,号出自哪一代发号逻辑就由那一代在定稿时自己署名。** 恢复
        # 会把 `seq_gen` 改成被恢复那一轮自己的出处(可能是 None),不在这里盖回当前
        # 世代的话,恢复过一次之后内存那份终态就**永久失证**:`event_id` 一直是空,
        # 起跑核对只剩内容兜底那一级。两个起跑入口另外也在开新一轮时署一次名(开工
        # 标记要带着它,见 `mark_open`),所以这一笔眼下是**冗余带** —— 别删:那边署
        # 的是「开工时的出处」,这边署的是「定稿时的出处」,两件事。
        _state["seq_gen"] = _SEQ_GEN
        _state["run_seq"] = _next_seq()
        _log("同步已停止，已完成的结果保留" if stopped else "管道结束")
        # **留档失败不许把一轮已经做完的正文工作变成「永远进行中」。** 这三份落盘
        # 都是旁路:状态文件喂「重试失败项」的范围派生(重启之后才用得上),当日账本
        # 喂同一颗按钮的当日继承,历史文件只给人翻 —— 而下面 finally 里那两件事是
        # **这一轮能不能收尾**。实测形态:
        # 历史文件里一个非 UTF-8 字节,解码异常就从 `_save_history()` 逃出来,阶段
        # 卡片和日志都已经写着「管道结束」,而标题是「同步正在启动」、按钮禁用,
        # 新同步和重试一概被拒,点停止也救不回来(线程早退了);普通同步和失败重试
        # 共用这段收尾,两条入口一起死。**判据只能钉在 `finally` 上** —— 光把某一种
        # 坏字节的异常类型补进捕获里,下一种坏法照旧重现。
        global _persist_error, _restore_gap
        # 这一轮是在本进程里跑完的,内存里这份终态带着完整的重试派生字段 ——
        # 启动时那笔「只恢复到历史那份」的欠账到此为止。
        _restore_gap = ''
        try:
            # **三份分别尝试、分别诊断,不许串在一个 try 里。** 串着的话状态文件
            # 写失败就把本来写得成的历史一起跳过 —— 那一轮在历史里直接不存在,而
            # 「历史里没有这一轮」和「这一轮还没跑过」在面板上长得一模一样。
            # 顺序按承重排:状态那一份和当日账本都喂「重试失败项」的派生,历史
            # 只给人翻。**当日账本写不上也只是退回存量行为**(那颗按钮按最近一次
            # 运行来补),所以它跟另两份一样不许挡住收尾。
            errs = []
            # 状态那一份改走批次层:普通一轮就是 `_save_last_run`,
            # 批次的第 2 轮起写的是**整批合并**那一份(见 `pipeline_batch._publish_in_batch`)。
            for save in (_publish_in_batch, _save_daybook, _save_history):
                try:
                    save()
                except Exception as exc:  # noqa: BLE001 - 理由就在上面那段
                    errs.append(type(exc).__name__)
                    _log(f'结果没能留档（{type(exc).__name__}）：'
                         f'{_SAVE_LABEL.get(save.__name__, save.__name__)}'
                         '这一份没写上，同步结论不受影响')
            _persist_error = ' / '.join(errs)
            # **两份都写上了才清开工标记。** 任一份失败就留着 —— 下一次启动据它
            # 报失证(误报顶多多跑一轮完整同步;反过来是漏报,那一轮查不出来)。
            if not errs:
                _open.clear(_open_file())
        finally:
            # 收尾状态按 run id 留一份,再把 running 放下:等这一轮的人晚一步来取
            # 也还拿得到自己那一轮的结果(阶段修正已经在上面做完了,存的就是终态)。
            run_id = _state.get("run_id") or ""
            if run_id:
                # 跟落盘那份同一个契约(见 `run_payload`):少一个字段,照 id 来取的
                # 调用方就会拿到一份"看着完整、其实合并不进去"的结果 —— 日报那条
                # "没亲眼看到自己那一轮收尾"的路就是这么把一轮的抓取失败丢掉的。
                _finished[run_id] = run_payload()
                for old_id in list(_finished)[:-_FINISHED_KEEP]:
                    del _finished[old_id]
            # **内存里这份终态也要带上结果账。** `last_result()` 在活进程里返回的
            # 是 `_state`(不是落盘那份),而降级规划就从它派生 —— 不往这里存一份的
            # 话,这份账只在落盘和按 id 那两份里有,日账一写不上就退回「整步那张
            # 合并卡片」,这一轮成功的来源当场又被算成欠着。落盘那份走的是
            # `run_payload()`,同一刻、同一个源,所以两边说的是同一件事。
            _state["results"] = _results.snapshot()
            # 结果账清在**三份留档和按 id 那一份都取完之后**:它们都靠
            # `run_payload()` 把它带出去。早清一步,当日账本折到的就是空账 ——
            # 退回「整步那张合并卡片」,R2 当场复发。下一轮要是接着用上一轮这份账,
            # 就等于拿昨天的结论清今天的欠账,所以也不许不清。
            _results.clear()
            # **「上一轮收尾是谁」在这里记**(批次资格判据读的就是它):记在收尾处,
            # 所以上面 `pipeline_batch._publish_in_batch` 读到的是我们这一轮**之前**那一轮。
            _remember_settled(run_id)
            _state["running"] = False
