# -*- coding: utf-8 -*-
"""管道运行状态:阶段进度、日志、最近一次结果的持久化。

单进程单管道,状态就是一个模块级 dict(与 WeChatOA 的 _crawl_state 同套路),
前端轮询 /api/pipeline/status 展示。各阶段实现与编排都改这里的状态,
单独成模块是为了让 stages / retry / 编排三方都能依赖它而不成环。
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime

from kb.config import DATA_DIR

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
_run_seq = 0

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


def _save_last_run() -> None:
    """持久化最近一次结果,重启 kb-web 后仍能看到。"""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({
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
            "crawl_retry": _state.get("crawl_retry") or {},
            "log": _state["log"],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def _run_record() -> dict:
    """这一轮的终态,给人翻的那一份。

    字段取「面板渲染一轮运行要哪些」那一套(`pipe_view.js` 的 `renderPipeState`),
    不含 `export_filter` / `crawl_retry` —— 那两个是重试派生用的,只有最新那一轮
    的才有意义。
    """
    return {
        "run_id": _state.get("run_id", ""),
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
    runs = [r for r in load_history() if not rid or r.get("run_id") != rid]
    runs.insert(0, record)
    del runs[_HISTORY_KEEP:]
    path = _history_file()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps({"runs": runs}, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def load_history() -> list[dict]:
    """最近几轮的终态,**最新排在最前**。

    文件还没有、读不出来、或者内容不是预期的形状,一律空列表 —— 这一份是只读
    展示,报错不如少显示几行。
    """
    try:
        raw = json.loads(_history_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    runs = raw.get("runs") if isinstance(raw, dict) else None
    if not isinstance(runs, list):
        return []
    return [r for r in runs if isinstance(r, dict)]


def load_last_run() -> dict | None:
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
        _save_last_run()
        # 历史在状态文件**之后**写:那一份承重(重试范围从它派生),这一份只给人翻。
        _save_history()
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
