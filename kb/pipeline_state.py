# -*- coding: utf-8 -*-
"""管道运行状态:阶段进度、日志、最近一次结果的持久化。

单进程单管道,状态就是一个模块级 dict(与 WeChatOA 的 _crawl_state 同套路),
前端轮询 /api/pipeline/status 展示。各阶段实现与编排都改这里的状态,
单独成模块是为了让 stages / retry / 编排三方都能依赖它而不成环。
"""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime

from kb.config import DATA_DIR, write_config_file

_STATE_FILE = DATA_DIR / "pipeline_state.json"

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
    # 见 note_retry_targets —— 带着 run id,所以不需要在每条启动路径上各清一次。
    "crawl_retry": {},
    "run_kind": "",
    # 这一轮属于哪个业务批次(`open_batch` 发的身份),空串 = 不属于任何批次。
    # **每条启动路径都要显式写一遍**:`_state` 是进程内常驻的,不写的话上一轮
    # 日报的批次身份会漏进下一轮独立运行,于是它被合进一个早就结束的批次。
    "batch": "",
    "planned_stages": None,
    # 每阶段: {status: pending/running/ok/skipped/error/stopped, detail: str}
    "stages": {s: {"status": "", "detail": ""} for s in STAGES},
    "log": [],          # 最近若干条进度行
}
_lock = threading.Lock()
_run_seq = 0

# 已经收尾的那几轮运行的终态,按 run id 存着。等一轮同步的人(日报)靠两秒一跳的
# 轮询取快照,而全局状态只有一份:我们那一轮收尾、另一轮马上接替时,轮询就再也
# 看不到自己那一轮的阶段结果了——只知道"不是我的了",不知道它是成功、失败还是
# 被停的。没有这份记录只能猜,而猜成"成功"就是拿一份没人证明过的结果去记账
# (2026-09-30 第 3 轮审查实测)。只留最近几轮:够覆盖"等的那一轮刚被接替"这个
# 窄窗口,又不让状态无限长。
_finished: dict[str, dict] = {}
_FINISHED_KEEP = 8


def new_run_id() -> str:
    """给新一轮运行发一个身份。只在 `_lock` 里调(`start_pipeline` 那一段)。"""
    global _run_seq
    _run_seq += 1
    return f'run-{_run_seq}'


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
    segments = _segments.get(name)
    if segments:
        # 这一阶段正被多个来源各跑一遍:只更新当前这一段,再把各段合成一条对外的结论
        segments[-1].update(entry)
        entry = _merge_segments(segments)
        detail = f"{segments[-1]['label']}：{detail}" if detail else detail
    _state["stages"][name] = entry
    if status == "running":
        _state["current_stage"] = name
    if detail:
        _log(f"{name}: {detail}")


# ── 多来源分段汇报 ──────────────────────────────────────────────────────────────
# 一次同步可以同时抓几个来源,而 crawl / export 两个阶段是每个来源各跑一遍的。
# 各自直接写 _state["stages"][name] 的话,后一个来源的结论会把前一个整个盖掉——
# 面板上只剩最后那个来源的成败,前面几个是成功、失败还是跳过都看不见了。
_segments: dict[str, list[dict]] = {}

# 合并后的状态取"最要紧"的那一档:有一个红就是红,还有没跑完的就是执行中,
# 剩下全跳过才算跳过。
_MERGE_ORDER = ("error", "running", "pending", "stopped", "skipped", "ok")


def _merge_segments(segments: list[dict]) -> dict:
    status = next((s for s in _MERGE_ORDER
                   if any(seg["status"] == s for seg in segments)), "ok")
    entry: dict = {
        "status": status,
        "detail": "；".join(f"{seg['label']}：{seg['detail']}"
                            for seg in segments if seg.get("detail")),
    }
    failures = [item for seg in segments for item in seg.get("failures", [])]
    if failures:
        # 各段自己已经可能截过:总数取"各段记下的总数之和",而不是截完之后的长度
        _cap_failures(entry, failures)
        total = sum(seg.get("failures_total") or len(seg.get("failures") or [])
                    for seg in segments)
        if total > len(entry["failures"]):
            entry["failures_total"] = total
    return entry


@contextmanager
def stage_segment(name: str, label: str):
    """把这一段时间里对 name 阶段的汇报记成"label 这个来源的那一段"。"""
    segments = _segments.setdefault(name, [])
    segments.append({"label": label, "status": "running", "detail": ""})
    try:
        yield
    finally:
        current = segments[-1]
        if current["status"] == "running":
            # 采集器连一次最终状态都没报(异常穿出去了):不替它编一个成功
            current.update({"status": "error", "detail": current["detail"] or "没有返回结果"})
        _state["stages"][name] = _merge_segments(segments)


def clear_segments() -> None:
    _segments.clear()


# ── 「重试失败项」要重试谁 ─────────────────────────────────────────────────────
# 报得出"这一轮哪几个目标失败了"的采集器在这里留一份**稳定身份**的名单(订阅是
# 它的 id),「重试失败项」据此把下一轮的抓取范围收到失败项上。
#
# **名单必须在失败那一刻记下来,不能事后反推。** 事后能查的只有订阅行上的
# `last_error`,而用户修正地址时 `save_subscription` 的 reset 会把它连同退避一起
# 清空 —— 那是正当的改址行为、不是"抓成功了"的证据,照它反推就把「已改正、还没
# 验证过」的那条当成「已经恢复」筛掉:用户修完地址点重试,那个账号一条都不抓,
# 界面还显示「已跳过」(2026-10-09 第 1 轮审查实测,而那正是这颗按钮的来由)。
#
# **而且必须按来源各记一份。** 一轮可以同时抓几个来源,而 `_merge_segments` 把各
# 段的失败明细扁平合并、项里不带来源归属;拿"合并之后有没有失败项"当一个全局开关
# 的话,RSS 的一条逐项失败会让**所有**来源都按失败项收窄,而整批失败的那个来源
# (一条订阅都没碰到、一份名单都没留下)就被收窄成空集 —— 恢复之后点重试,RSS 补抓
# 了,大V一条也没抓,面板上还不再显示有失败要处理(同轮审查实测)。所以判据是
# 「**这个来源**留下名单了吗」:没留下 = 整批失败、或者它报不出名单(公众号的失败
# 是文章级的,`--targets` 是账号级的,压根对不上),一律按原范围重跑。
#
# 名单带着这一轮的 run id。下一轮一份名单都没留下时(来源级整轮故障),陈旧名单会
# 连同这一轮的状态一起落盘,拿它收窄就是按上一轮的失败去抓 —— 带上身份之后这件事
# 不必在每条启动路径上各清一次,少一处就是少一个会漏的地方。

def note_retry_targets(channel: str, targets, at: str) -> None:
    """记下「这个来源这一轮有哪几个目标失败了」。

    `targets` 是稳定身份(订阅 id 这类),不是显示名 —— 显示名会被改。

    `at` 是这份名单的时刻,用来回答"这个目标后来有没有成功同步过"。**必填,而且
    要用采集器自己那套时间格式**(订阅那边是 `reg.now()` 的 UTC ISO,不是这里
    `_now()` 的本地空格格式):名单只有写它的那个采集器解释得了,而两种格式直接
    比字符串是错的 —— ISO 的 `T`(0x54)排在空格(0x20)后面,同一天里 ISO 那个
    **恒大于**空格那个,于是"它后来成功过"这一问会一律答成"是",整份名单当场
    被摘空。所以这里不给默认值、也不替它回落到 `_now()`:格式由写名单的人负责,
    少传一个参数会当场报错,而回落只会把"填错了"变成"一个很久以后才表现成『重试
    什么都不抓』的坏时钟"。空串原样存着 —— 那等于不设时间判定,保守多抓一次。
    """
    run = _state.get("run_id") or ""
    note = _state.get("crawl_retry") or {}
    if note.get("run") != run:
        note = {"run": run, "channels": {}}
        _state["crawl_retry"] = note
    note["channels"][channel] = {"targets": [str(t) for t in targets], "at": at}


def retry_notes(run: dict) -> dict:
    """那一轮各来源留下的失败名单:{channel: {"targets": [...], "at": ts}}。

    `run` 是 `load_last_run()` 或 `get_state()` 给的那份快照。身份对不上一律
    当成"这一轮没有名单"(宁可按原范围重跑,也不拿上一轮的失败去收窄)。
    """
    note = run.get("crawl_retry") or {}
    if note.get("run") != (run.get("run_id") or ""):
        return {}
    return note.get("channels") or {}


def run_payload() -> dict:
    """一轮运行的终态记录。**落盘的那份和按 id 留的那份是同一份**。

    两处曾经各写各的:`_finished` 只留了身份、结束时刻、stopped 和 stages。于是
    "没亲眼看到自己那一轮收尾、照 id 去取"的调用方(`daily_run._sync_round` 被
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
        "crawl_retry": deepcopy(_state.get("crawl_retry") or {}),
        "log": list(_state["log"]),
    }


# **「最近一次运行」的权威在内存里,磁盘只是重启之后的后路。**
# 落盘会失败(满盘、权限、目录没了),而那一下**是吞掉的** —— 吞掉本身没错,一轮同步
# 不该因为写不进状态文件就判失败。错的是拿磁盘当"有没有更新的一轮"的判据:那一轮
# 明明跑完了、结论还在内存里,磁盘上却还是上一份,于是一批更早的结果被判定"有资格
# 当最新",把它刚刚的失败改写成成功、「重试失败项」跟着消失。
# 进程内的每一次收尾都更新这一份,它不会失败,所以它才是判据。
_last_result: dict | None = None
# 内存那份是**某一个**状态文件的回写缓存。`_STATE_FILE` 被改指向别处(切数据目录、
# 测试打桩)时它说的不是同一个库,一律作废,改问新的那份磁盘。
_last_result_file = None


def _remember_result(payload: dict) -> None:
    """记下「本进程见过的最近一次终态」。落盘成不成功都要记。"""
    global _last_result, _last_result_file
    _last_result, _last_result_file = deepcopy(payload), _STATE_FILE


def _authoritative() -> dict | None:
    return _last_result if _last_result_file == _STATE_FILE else None


def latest_run_id() -> str:
    """本进程见过的最近一轮终态的身份。空串 = 这个进程还没见过任何一轮收尾。"""
    return (_authoritative() or {}).get("run_id") or ""


def _write_state(payload: dict) -> None:
    """把一份结果落到状态文件。**原子替换**,不是就地改写。

    就地 `write_text` 的话,写到一半退出(重启、OOM、断电)留下的是半截 JSON ——
    读的那一端只能当"读不出",而旧那份已经被截掉了,两份都没了。先写同目录的
    临时文件再 `os.replace`,中断只会留下一个临时文件,原来那份一字未动。
    """
    # 原子替换那几行收在 `kb.config.write_config_file`(配置那几个写口共用它):
    # 同一件事别在两处各写一遍 —— 那边还负责失败时把临时文件带走。
    write_config_file(_STATE_FILE, payload)


def _with_persist_issue(payload: dict, issue: str) -> dict:
    """把"这份结果没写进磁盘"记进它自己:一格结构化字段 + 一行日志。

    只写日志的话前端只能让人展开日志去找,面板上那句收尾文案仍是一句干净的
    「同步完成」—— 看不出这一轮是降级完成的(重启就退回旧那份)。`persist_issue`
    这一格供面板直接显示(`pipe_view.js`),日志那行留着时序。
    """
    payload = {**payload, "persist_issue": issue,
               "log": [*(payload.get("log") or []), _stamp(issue)][-80:]}
    _log(issue)
    return payload


def _save_last_run() -> None:
    """持久化最近一次结果,重启 kb-web 后仍能看到。"""
    payload = run_payload()
    try:
        _write_state(payload)
    except OSError as e:
        # 不判这一轮失败,但**也不许一声不吭**:面板上那份结论从这一刻起只活在
        # 内存里,重启就没了。
        note = (f"这一轮的结果没能写进磁盘({e});本进程内照常可查,"
                "重启后会退回上一次写成功的那一份")
        _remember_batch_note(note)
        payload = _with_persist_issue(payload, note)
    _remember_result(payload)


# ── 业务批次:一轮日报 = N+1 次管道运行,而「最近一次运行」只有一个位子 ────────
# **子轮一收尾就把整批的汇总写进去**,不是先写它自己、再等日报那边稍后补回。
# 先写自己的后果:这一刻盘上那份只有刚收尾的这一轮(入库段那一轮 crawl 是
# 「本次不执行」),前几轮的抓取失败只活在进程内 —— 进程在这中间退出(重启、
# OOM、被杀),重启后同步中心只看到「同步已完成」,既没有那条 RSS 抓取失败,
# 也没有「重试失败项」。那个窗口有多窄都不行:它正是"写了一份会删掉前序失败的
# 局部结果,然后指望还活着的调用方稍后修好"。
# 账本待在**结果存储这一层**,由 `_end_run` 喂:日报那边看不到自己那一轮收尾
# (被下一轮挤出记录)的那一档也照样进账。
_batch_id = ""              # 开着的批次身份,空串 = 没有批次
_batch_runs: list[dict] = []        # 这一批里已经收尾的子轮终态,按时间顺序
_batch_issue = ""           # 这一批**当前**没能发布出去的原因,空串 = 一切正常
# 这一批自己的事件(落盘失败/恢复),跟子轮的日志分开存。**必须单独留一份**:
# 下一轮的合并是从子轮账本重新拼日志的,而子轮快照是在落盘之前拍的 —— 只把那行
# 字追加到当轮合并结果里的话,后面一轮发布成功之后这条历史就没了,而面板上写着
# "原因留在运行日志里"(第 6 轮实测:最终日志里找不到那次失败)。
_batch_notes: list[str] = []


def _remember_batch_note(note: str) -> None:
    """把一条保存事件记进**批次账本**(当前有批次才记)。

    **两个写出口都要记,不按"走的是普通保存还是汇总保存"分裂。** 这一批的头一轮
    走的是 `_save_last_run`:它那行字只进了当轮的 payload,而后面几轮的合并是从
    子轮账本重新拼日志的(子轮快照在落盘**之前**拍),于是首轮那次保存失败在后
    一轮写成功之后就没了 —— 第 1 轮实测注入首轮写失败,最终日志里找不到它,而
    决策文档和回归都写着"原因留在运行日志里"。
    """
    if _batch_id and _state.get("batch") == _batch_id:
        _batch_notes.append(_stamp(note))
_batch_seq = 0


def open_batch() -> str:
    """开一个业务批次,返回它的身份 —— 交给 `start_pipeline(batch=...)`。

    **和 `close_batch()` 成对**,调用方放在 try/finally 里:不关的话下一轮管道
    运行会被算进一个早就结束的批次。
    """
    global _batch_id, _batch_runs, _batch_issue, _batch_notes, _batch_seq
    with _lock:
        _batch_seq += 1
        _batch_id, _batch_runs = f"batch-{_batch_seq}", []
        _batch_issue, _batch_notes = "", []
        return _batch_id


def close_batch() -> str:
    """关掉批次,返回它没能发布出去的原因(空串 = 发布正常)。

    盘上那份已经在最后一个子轮收尾时写全了,这里不再写 —— 所以**提早返回、
    甚至中途崩掉都不会少东西**。
    """
    global _batch_id, _batch_runs, _batch_issue, _batch_notes
    with _lock:
        issue, _batch_id, _batch_runs = _batch_issue, "", []
        _batch_issue, _batch_notes = "", []
        return issue


def _publish_in_batch() -> None:
    """子轮收尾时的落盘。只在 `_end_run` 里调(已持 `_lock`)。

    不属于任何批次、或者这一批的头一轮:写自己那份就是全的。往后每一轮都把
    **整批**合并着写,盘上那份于是始终等于"已经收尾的这几轮合起来"。
    """
    global _batch_issue
    if not _batch_id or _state.get("batch") != _batch_id:
        _save_last_run()
        return
    _batch_runs.append(run_payload())
    if len(_batch_runs) < 2:
        _save_last_run()
        return
    # **资格冲突和 I/O 故障是两件完全不同的事,分开判。** 混成一种异常的后果是
    # I/O 那一档被按"别人更新了"处理:退回去写当前子轮那一份局部成功,把整批的
    # 失败连同重试名单一起抹掉 —— 入库那一轮的 crawl 是「未选择」,前面那条抓取
    # 失败于是内存里和磁盘上一起没了(第 5 轮实测:一次临时写失败就够)。
    try:
        _publish_right({r.get("run_id") for r in _batch_runs if r.get("run_id")})
    except PublishRefused as e:
        # 没资格:期间有更新的一轮独立运行收尾(用户自己点了同步)。退回只写自己
        # 那一份 —— **我们这一轮确实是此刻最新的**,而把前几轮一起合进去等于拿一批
        # 比那一轮更早的结果去盖掉它。原因留给 `close_batch` 的调用方说出来。
        _batch_issue = e.reason
        _save_last_run()
        return
    from kb.pipeline_merge import _merge_runs      # 延迟:它反过来 import 本模块
    merged = _merge_runs(_batch_runs, _batch_notes)
    try:
        _write_state(merged)
    except OSError as e:
        # **不退回去写局部结果。** 盘上那份是这一批上一轮的汇总(同样带着前面
        # 那些失败),原样留着就是最好的后路;整批那份照旧当权威(见下一句)。
        _batch_issue = f'这一批的同步结果没能写进磁盘({e})'
        note = (f'{_batch_issue};本进程内照常可查,'
                '重启后会退回上一次写成功的那一份')
        # 记在批次账本上,后面那几轮的合并才带得上它(见 `_remember_batch_note`)。
        _remember_batch_note(note)
        merged = _with_persist_issue(merged, note)
    else:
        # 发布成功就解除之前那句警告:原因留在运行日志里,但"当前仍未保存"
        # 这句话不能挂着不放 —— 后来真写进去了,日报却还在说没写进。
        _batch_issue = ""
    # **整批那份是权威,与写成不成功无关。** 写成功只回答"重启之后读不读得到",
    # 答不了"本进程这一批有没有出过那条失败"。
    _remember_result(merged)
    _finished[merged["run_id"]] = deepcopy(merged)
    for old_id in list(_finished)[:-_FINISHED_KEEP]:
        del _finished[old_id]


def load_last_run() -> dict | None:
    """最近一次运行的终态。**内存那份优先** —— 它是权威,磁盘是重启后的后路。

    面板(`/api/pipeline/status` 的 `last_run`)和「重试失败项」
    (`pipeline_retry.retry_failed_plan`)都走这里,所以两者看到的始终是同一份;
    分别去读磁盘的话,落盘失败的那一轮会让两边一起退回上一轮的结论。
    """
    mine = _authoritative()
    if mine is not None:
        return deepcopy(mine)
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_state() -> dict:
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
        _log("同步已停止，已完成的结果保留" if stopped else "管道结束")
        _publish_in_batch()
        # 收尾状态按 run id 留一份,再把 running 放下:等这一轮的人晚一步来取
        # 也还拿得到自己那一轮的结果(阶段修正已经在上面做完了,存的就是终态)。
        run_id = _state.get("run_id") or ""
        if run_id:
            # 跟落盘那份同一个契约(见 `run_payload`):少一个字段,照 id 来取的
            # 调用方就会拿到一份"看着完整、其实合并不进去"的结果。
            _finished[run_id] = run_payload()
            for old_id in list(_finished)[:-_FINISHED_KEEP]:
                del _finished[old_id]
        _state["running"] = False



class PublishRefused(Exception):
    """这一批没能发布出去。`reason` 是人话原因,调用方要把它说出来。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _publish_right(ours: set[str]) -> None:
    """这一批有没有资格替换「最近一次运行」。没资格就抛 `PublishRefused`。

    **只管资格:不合并、不写磁盘;必要时读磁盘判断资格**(手里没有权威身份那
    一档,见函数末尾)。只有当那份「最近一次运行」仍然是这一批里的某一轮时才
    有资格。期间要是另一轮独立运行已经收尾(用户自己点了同步),
    它是**比我们新**的结果,替换掉它等于把一次刚失败的手动同步改写成成功、顺带
    让它的「重试失败项」消失。一把锁只防同时写,答不了"我有没有资格当最新"。
    """
    # **判据是内存里那份权威身份,不是磁盘。** 磁盘写会失败而且是吞掉的:
    # 那一轮手动同步明明已经收尾、结论也在内存里,磁盘上却还是我们这一批的
    # 某一轮 —— 照磁盘判,我们就"有资格"把它刚刚的失败改写成成功。
    latest = latest_run_id()
    if latest:
        if latest not in ours:
            raise PublishRefused(_taken_over(latest))
        return
    # 这个进程还没见过任何一轮收尾(刚重启、这一批是从别处恢复的)。只能问磁盘,
    # 而**读不出来时失败关闭**:一份读不出的状态既可能是「没有更新的运行」也可能
    # 是「有而且更重要」,拿不准就不许替换。
    try:
        current = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = {}
    except (OSError, json.JSONDecodeError) as e:
        raise PublishRefused(
            f'读不出当前的「最近一次运行」({e}),不确定期间有没有更新的'
            '一轮收尾,这一批没有替换它') from e
    if current.get("run_id") and current["run_id"] not in ours:
        raise PublishRefused(_taken_over(current["run_id"]))


def _taken_over(other: str) -> str:
    """"被更新的一轮接替了"那句话。**只说已经发生的事。**

    原来这句写的是"同步中心显示的是那一轮" —— 而紧接着的退路会把当前子轮写
    进去,于是同步中心显示的其实是刚结束的这一轮,提示指着另一轮(第 5 轮实测
    文案不实)。所以只说"没有合并发布",显示的是谁由"最后收尾的那一轮"决定。
    """
    return (f'期间已经有更新的一轮同步收尾({other}),这一轮日报的几段同步结果'
            '没有合并成一份发布;「最近一次运行」显示的是最后收尾的那一轮')
