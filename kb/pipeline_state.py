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

from kb.config import DATA_DIR

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
            "log": _state["log"],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


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
