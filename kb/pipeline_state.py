# -*- coding: utf-8 -*-
"""管道运行状态:阶段进度、日志、最近一次结果的持久化。

单进程单管道,状态就是一个模块级 dict(与 WeChatOA 的 _crawl_state 同套路),
前端轮询 /api/pipeline/status 展示。各阶段实现与编排都改这里的状态,
单独成模块是为了让 stages / retry / 编排三方都能依赖它而不成环。
"""

from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime

from kb.config import DATA_DIR

_STATE_FILE = DATA_DIR / "pipeline_state.json"

STAGES = ("crawl", "export", "index", "extract", "projects", "wiki")

_state: dict = {
    "running": False,
    "stop_requested": False,
    "stopped": False,
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
    _state["stages"][name] = entry
    if status == "running":
        _state["current_stage"] = name
    if detail:
        _log(f"{name}: {detail}")


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


def request_stop() -> bool:
    """Finish active work before releasing the single-pipeline lock."""
    with _lock:
        if not _state["running"]:
            return False
        if not _state.get("stop_requested"):
            _state["stop_requested"] = True
            _log("已请求停止：当前执行中的阶段结束后，不再启动后续阶段")
        return True


def _end_run() -> None:
    with _lock:
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
        _state["running"] = False
