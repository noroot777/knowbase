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
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime

from kb.config import DATA_DIR
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
    _write_atomic,
    result_version as _version_of,
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
_HISTORY_KEEP = 20


def _history_file():
    """历史文件就在 `_STATE_FILE` 旁边。

    **从它派生,不另立一个模块变量**:测试隔离落盘状态时 patch 的是 `_STATE_FILE`
    (`tests/test_pipeline_stop.py` 那一套),另立一个的话每个走到 `_end_run()` 的
    测试都得记着再 patch 一次 —— 漏掉的那个会去写**生产**那份历史,而漏没漏在
    测试输出上一个字都看不见(同 `maintenance.DATA_DIR` 那条)。派生之后隔离点
    只有一个,不可能漏。
    """
    return _STATE_FILE.with_name(f"{_STATE_FILE.stem}_runs{_STATE_FILE.suffix}")

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
    # 见 note_retry_targets —— 带着 run id,所以不需要在每条启动路径上各清一次。
    "crawl_retry": {},
    "run_kind": "",
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
# 跟落盘那份历史的 `_HISTORY_KEEP` **不是一个数,也不该统一**:这一份只为"等的那
# 一轮刚被接替"这个窄窗口,几轮就够;那一份是给人翻的,留得住两三周才有用。
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
    # 带日期:日志保留 80 行且跨天不清,只打时分秒会看不出是哪天跑的
    _state["log"].append(f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}")
    del _state["log"][:-80]


def _stage(name: str, status: str, detail: str = "",
           failures: list[dict] | None = None) -> None:
    """failures: [{"title", "reason"}] 失败明细,面板卡片可展开查看。
    截断 50 条防状态体膨胀(历史积压场景可能一次几百篇)。"""
    entry: dict = {"status": status, "detail": detail}
    if failures:
        entry["failures"] = failures[:50]
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
        entry["failures"] = failures[:50]
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


def _history_rounds_strict() -> list[dict]:
    """历史记录的**严格版**:读不出来、或者形状不对,一律抛。

    「可解析但形状不对」和「读不出来」是同一档 —— 都不能当成「这份副本没有」。
    展示那条路照旧走 `load_history()`(宽口径,少显示几行、不出声)。
    """
    raw = _load_json_strict(_history_file())
    if not _history_shape_ok(raw):
        raise ValueError(f'历史文件形状不对：{type(raw).__name__}')
    return _history_runs(raw)


def _saved_round_strict() -> dict | None:
    """状态文件里那一轮的**严格版**,口径同上。发号和恢复共用它。"""
    raw = _load_json_strict(_STATE_FILE)
    if not _state_shape_ok(raw):
        raise ValueError(f'状态文件形状不对：{type(raw).__name__}')
    return raw


def _save_last_run() -> None:
    """持久化最近一次结果,重启 kb-web 后仍能看到。

    **异常不在这里吞**:两份落盘都由 `_end_run()` 那段统一收尾兜住,并如实记一笔
    诊断(`persist_error()`)。在这里各吞一次的话,「写没写上」就再也报不出去了。
    """
    _write_atomic(_STATE_FILE, _dump_bytes({
            "started_at": _state["started_at"],
            "finished_at": _state["finished_at"],
            "stopped": _state.get("stopped", False),
            "run_kind": _state.get("run_kind", ""),
            "planned_stages": _state.get("planned_stages"),
            "stages": _state["stages"],
            "crawl_filter": _state["crawl_filter"],
            # 导出范围单独落一份:下一次重试要从**它**恢复,不是从收窄后的抓取范围
            "export_filter": _state.get("export_filter") or {},
            # run_id 跟名单一起落盘:retry_notes 要拿它认"这份名单是不是这一轮的"
            "run_id": _state.get("run_id", ""),
            # 完成序号:恢复时靠它分出两份落盘各记着哪一轮
            "run_seq": _state.get("run_seq", 0),
            # 发号世代:**凭据自己带出处**,老记录那些号一个都不信(`_seq_of`)
            "seq_gen": _SEQ_GEN,
            "crawl_retry": _state.get("crawl_retry") or {},
            "log": _state["log"],
        }))


def _run_record() -> dict:
    """这一轮的终态,给人翻的那一份。

    字段取「面板渲染一轮运行要哪些」那一套(`pipe_view.js` 的 `renderPipeState`),
    不含 `export_filter` / `crawl_retry` —— 那两个是重试派生用的,只有最新那一轮
    的才有意义。
    """
    return {
        "run_id": _state.get("run_id", ""),
        "run_seq": _state.get("run_seq", 0),
        # 发号世代:**凭据自己带出处**,老记录那些号一个都不信(`_seq_of`)
        "seq_gen": _SEQ_GEN,
        "started_at": _state["started_at"],
        "finished_at": _state["finished_at"],
        "stopped": bool(_state.get("stopped", False)),
        "run_kind": _state.get("run_kind", ""),
        "planned_stages": _state.get("planned_stages"),
        "stages": deepcopy(_state["stages"]),
        "crawl_filter": deepcopy(_state["crawl_filter"]),
        "log": list(_state["log"]),
    }


def _save_history() -> None:
    """把这一轮的终态记进历史。

    **按 run id 幂等**(身份非空时):同一轮重复落盘时替换那一条、不追加 —— 否则一次
    重复调用就在历史里留下两条一模一样的记录,而它们的区别只有写入顺序。

    **原子换文件**:面板那条读历史的请求随时在跑,而这份文件比状态文件大得多
    (带着每轮 80 行日志),截断重写的窗口里读到的是半截 JSON(同
    `docs/decisions/llm-stages.md` 那条)。写失败只是少一条历史记录,不出声 ——
    这一份是只读展示,它写不写得上不该影响这一轮同步的结论。
    """
    record = _run_record()
    rid = record["run_id"]
    # **空身份一律追加,不去重。** 空串不是身份 —— 拿它当键会把两轮各自跑过的运行
    # 合成一条(没经过 `start_pipeline` 的那条路身份就是空的)。历史里少一轮是静默
    # 丢数据,多留一条只是多一行。
    # **存量必须是「确实读出来了」或者「文件确实不存在」,别的一律抛。** 这一步之后
    # 是整份原子替换,拿一个因为读失败而空着的集合去替换,就是把存量**删掉** ——
    # 原子性只保证新文件完整,不保证旧记录还在。实测形态:库里已经三轮(其中一轮
    # 带着失败原因和日志),第四轮成功、只在这一次读上注入一个 `PermissionError`,
    # 历史当场从 3 条变 1 条、面板上一个字不说(两份都写成功了,诊断是空的)。
    # 抛出去反而正好:`_end_run` 那两份是分别尝试、分别诊断的,这一份写不上只是
    # 少一条历史记录,面板会如实说一句,而**原文件一个字节都没动**。
    runs = [r for r in _history_rounds_strict() if not rid or r.get("run_id") != rid]
    runs.insert(0, record)
    del runs[_HISTORY_KEEP:]
    _write_atomic(_history_file(), _dump_bytes({"runs": runs}))


def load_history() -> list[dict]:
    """最近几轮的终态,**最新排在最前**。

    文件还没有、读不出来、或者内容不是预期的形状,一律空列表 —— 这一份是只读
    展示,报错不如少显示几行。
    """
    return _history_runs(_load_json(_history_file()))


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

    三档非空值,**都意味着「重试失败项」不能起跑**(闸在
    `pipeline_retry.start_retry_failed`,面板上那颗按钮同时按不下去并说明原因):

    - `'scope'`   挑中的是历史那一份:它的阶段、日志、抓取范围都齐(所以展示是
                  对的),但**没有导出范围和失败名单**
    - `'unknown'` 恢复时有一份读不出来,**核对不了哪一轮更新** —— 展示的是能读到
                  的那一份,它可能是旧的
    - `'order'`   两份各记着不同的一轮,而**先后证明不了**(都没有完成序号,收尾
                  时间也相同)

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
        if not isinstance(pick, dict) or not pick.get("finished_at"):
            _restored = 'ok'                      # 确定没跑过任何一轮
            return
        for key in ("started_at", "finished_at", "run_kind", "planned_stages",
                    "run_id", "run_seq", "log"):
            if key in pick:
                _state[key] = deepcopy(pick[key])
        # **恢复来的那一轮同时抬高水位**,和 `run_seq` 一起:内存里这份终态该和磁盘
        # 那份一致。这两笔也是**冗余带**(定点去掉红 0 条,理由同 `_next_seq`)——
        # 发号读的是磁盘不是内存,所以它们眼下改变不了任何可观测行为。
        _seq_hwm = max(_seq_hwm, _seq_of(pick))
        _state["stopped"] = bool(pick.get("stopped", False))
        for key in ("stages", "crawl_filter", "export_filter", "crawl_retry"):
            got = pick.get(key)
            # 取历史那份时 `export_filter` / `crawl_retry` 本来就没有:**留空而不是
            # 沿用上一轮的**,那两个字段错一个就是静默不抓。
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
        return deepcopy(_state)
    return load_last_run() or {}


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
        # **落盘之前发完成序号**,两份都带上它 —— 恢复时靠它分出谁更新
        _state["run_seq"] = _next_seq()
        _log("同步已停止，已完成的结果保留" if stopped else "管道结束")
        # **留档失败不许把一轮已经做完的正文工作变成「永远进行中」。** 这两份落盘
        # 都是旁路:状态文件喂「重试失败项」的范围派生(重启之后才用得上),历史文件
        # 只给人翻 —— 而下面 finally 里那两件事是**这一轮能不能收尾**。实测形态:
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
            # **两份分别尝试、分别诊断,不许串在一个 try 里。** 串着的话状态文件
            # 写失败就把本来写得成的历史一起跳过 —— 那一轮在历史里直接不存在,而
            # 「历史里没有这一轮」和「这一轮还没跑过」在面板上长得一模一样。
            # 顺序仍是状态在前:那一份承重(重试范围从它派生),这一份只给人翻。
            errs = []
            for save in (_save_last_run, _save_history):
                try:
                    save()
                except Exception as exc:  # noqa: BLE001 - 理由就在上面那段
                    errs.append(type(exc).__name__)
                    _log(f'结果没能留档（{type(exc).__name__}）：'
                         f'{save.__name__[6:]} 这一份没写上，同步结论不受影响')
            _persist_error = ' / '.join(errs)
        finally:
            # 收尾状态按 run id 留一份,再把 running 放下:等这一轮的人晚一步来取
            # 也还拿得到自己那一轮的结果(阶段修正已经在上面做完了,存的就是终态)。
            run_id = _state.get("run_id") or ""
            if run_id:
                _finished[run_id] = {
                    "run_id": run_id, "stopped": stopped,
                    "finished_at": _state["finished_at"],
                    "stages": deepcopy(_state["stages"]),
                }
                for old_id in list(_finished)[:-_FINISHED_KEEP]:
                    del _finished[old_id]
            _state["running"] = False
