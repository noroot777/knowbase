# -*- coding: utf-8 -*-
"""数据管道编排:抓取 → 导出 → 索引 → 提取 → 条目化 → wiki,一键串行跑。

本模块只管「谁先谁后」与对外门面;拆分如下:
    kb.pipeline_state   运行状态、阶段进度、日志
    kb.pipeline_stages  各阶段的实际实现
    kb.pipeline_retry   失败项重试链

Web 层统一 `from kb import pipeline as pl` 调用,所以这里把三个模块里
对外要用的名字重新导出,拆分对调用方透明。
"""

from __future__ import annotations

import json
import re
import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta

from kb.config import DATA_DIR
from kb.collectors import active_collector, known_channels, resolve_channel
from kb.pipeline_retry import retry_failed_plan, start_retry_failed
from kb.pipeline_stages import (
    _extract_pass,
    _finish_extract_stage,
    _new_extract_stats,
    _run_crawl,
    _run_export,
    _run_extract,
    _run_index,
    _run_projects,
    _run_wiki,
    crawl_channels,
    source_pending,
    source_status,
    source_targets,
)
from kb.pipeline_state import (
    STAGES,
    _end_run,
    _lock,
    _log,
    _now,
    _stage,
    _state,
    get_state,
    load_history,
    last_result,
    load_last_run,
    persist_error,
    restore_gap,
    result_version,
    new_run_id,
    request_stop,
    run_result,
)

# 对外门面:Web 层只 import 本模块,不直接碰拆出去的三个子模块
__all__ = [
    "STAGES",
    "channel_filter",
    "crawl_channels",
    "get_state",
    "load_history",
    "last_result",
    "load_last_run",
    "persist_error",
    "restore_gap",
    "result_version",
    "load_schedule",
    "pending_counts",
    "request_stop",
    "retry_failed_plan",
    "run_result",
    "save_schedule",
    "source_status",
    "source_targets",
    "start_pipeline",
    "start_retry_failed",
    "start_scheduler",
]


# ── 编排 ────────────────────────────────────────────────────────────────────────

_RUNNERS = {"crawl": _run_crawl, "export": _run_export,
            "index": _run_index, "extract": _run_extract,
            "projects": _run_projects, "wiki": _run_wiki}


def _run_pipeline(stages: list[str], crawl_filter: dict | None = None) -> None:
    pre_thread = None
    try:
        # 抓取被反爬间隔限速(页间/号间随机 sleep),期间 CPU 和网络全闲着:
        # 先并行开跑一轮"存量"提取,消化上一轮已索引但没提取的积压。
        # 安全边界:提取只写 kb.db(WAL)和 obs frontmatter,与抓取(采集器进程外)、
        # 导出(幂等,已存在的文件跳过、不覆盖)互不冲突;索引前 join,保持库的单写者。
        ex_stats = _new_extract_stats()
        if "extract" in stages and "crawl" in stages and not _state.get("stop_requested"):
            def _pre_extract():
                try:
                    _extract_pass("存量", ex_stats)
                    # 管道还没走到 extract 阶段:别让卡片停在"running"误导人
                    if ex_stats["ok"] + len(ex_stats["failed_paths"]) \
                            + len(ex_stats["empty_paths"]) \
                            + len(ex_stats["no_text_paths"]) > 0:
                        _state["stages"]["extract"] = {
                            "status": "pending",
                            "detail": f"存量已提取 {ex_stats['ok']} 篇,等索引后做增量"}
                except Exception as e:
                    _log(f"extract(存量): {e}")
            pre_thread = threading.Thread(target=_pre_extract, daemon=True)
            pre_thread.start()

        for s in STAGES:
            if _state.get("stop_requested"):
                break
            if s not in stages:
                _state["stages"][s] = {"status": "skipped", "detail": "未选择"}
            elif s == "crawl":
                _run_crawl(crawl_filter)
            elif s == "export":
                _run_export(crawl_filter)
            elif s == "index":
                if pre_thread and pre_thread.is_alive():
                    _stage("index", "running", "等存量提取收尾(避免并发写库)")
                    pre_thread.join()
                    if _state.get("stop_requested"):
                        break
                _run_index()
            elif s == "extract":
                if pre_thread:
                    pre_thread.join()
                    if _state.get("stop_requested"):
                        break
                    _extract_pass("增量", ex_stats)
                    _finish_extract_stage(ex_stats)
                else:
                    _run_extract()
            else:
                _RUNNERS[s]()
    finally:
        # Parallel extraction owns database writes too. Do not advertise idle
        # until it finishes, including stop requests and unexpected exceptions.
        if pre_thread:
            pre_thread.join()
        _end_run()


def start_pipeline(stages: list[str] | None = None,
                   crawl_filter: dict | None = None) -> str:
    """启动管道线程。返回这一轮的 run id;已在跑返回空串(照旧可以当布尔用)。

    crawl_filter 见 _run_crawl:限定来源目标 + 时间段的范围抓取;
    同时抓几个来源时来源列表在 channels 里(见 crawl_channels)。

    返回 id 而不是 True,是为了让调用方**原子地**拿到"我起的是哪一轮":
    事后再去读状态的话,我们那一轮可能已经跑完、另一项同步已经接替,读回来
    的就是别人的身份(见 `pipeline_state.request_stop` 的说明)。
    """
    wanted = [s for s in (stages or list(STAGES)) if s in STAGES]
    crawl_filter = deepcopy(crawl_filter or {})
    crawl_filter["channel"] = active_collector(crawl_filter.get("channel")).name
    # 每个来源都按注册表认一遍名字:认不出来在这里就抛,别等跑到一半才发现
    for scope in crawl_filter.get("channels") or []:
        scope["channel"] = active_collector(scope.get("channel")).name
    with _lock:
        if _state["running"]:
            return ""
        run_id = new_run_id()
        _state.update({
            "running": True, "current_stage": "", "log": [],
            "stop_requested": False, "stopped": False, "run_id": run_id,
            "started_at": _now(), "finished_at": "",
            "crawl_filter": crawl_filter,
            # 常规同步的导出范围就是抓取范围。**显式写一份、不靠"没设过"兜底**:
            # 这是进程内的常驻状态,上一轮如果是重试,里面躺着的是那一轮的导出范围
            # —— 不覆盖的话这一轮的落盘会把它原样带走,下一次重试就按一份跟这一轮
            # 毫无关系的范围去导出。
            "export_filter": deepcopy(crawl_filter),
            "run_kind": "sync",
            "planned_stages": [s for s in STAGES if s in wanted],
            "stages": {s: {"status": "pending" if s in wanted else "skipped",
                           "detail": ""} for s in STAGES},
        })
    threading.Thread(target=_run_pipeline, args=(wanted, crawl_filter),
                     daemon=True).start()
    return run_id


# ── 定时同步 ────────────────────────────────────────────────────────────────────
# 每天固定时刻自动触发全流程同步(全部五个阶段)。配置持久化在 data/,
# 调度线程跟随 kb-web 进程存活;kb-web 不在跑时到点不会触发(无守护进程)。

_SCHED_FILE = DATA_DIR / "pipeline_schedule.json"
_sched_started = False
_schedule_lock = threading.RLock()


def _schedule_channels(value) -> list[str]:
    """要存进定时配置的来源列表。认不出的名字在这里就抛,别等到点了才发现。

    先过 resolve_channel:公众号早先那条凭证通道已经退役(见 kb.collectors),
    老配置和老页面里写着的旧名字要换成接替它的那条落盘,别当成"不认识"抛掉
    ——抛掉等于公众号从此不再定时同步,而面板上完全看不出少了什么。
    """
    names, seen = [], set()
    for name in (value or []):
        resolved = active_collector(resolve_channel(name)).name
        if resolved not in seen:
            seen.add(resolved)
            names.append(resolved)
    return names


def channel_filter(channels: list[str]) -> dict:
    """把来源名列表做成 crawl_filter。只有一个来源时写成老样子的 channel。"""
    scopes = [{"channel": name, "accounts": []} for name in channels]
    crawl_filter: dict = {"channel": scopes[0]["channel"] if scopes else ""}
    if len(scopes) > 1:
        crawl_filter["channels"] = scopes
    return crawl_filter


def load_schedule() -> dict:
    try:
        d = json.loads(_SCHED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        d = {}
    # 读盘这一侧宽容:认不出的名字跳过(通道被删/改过名),绿泡泡那条跟着设置走。
    # 一个坏名字不该让整份定时配置读不出来。
    channels = known_channels([c for c in (d.get("channels") or [d.get("channel")]) if c]) \
        or [active_collector().name]
    return {"enabled": bool(d.get("enabled")),
            "time": d.get("time") or "07:30",
            "channel": channels[0],
            "channels": channels,
            "last_fired": d.get("last_fired") or "",
            "next_run": d.get("next_run") or ""}


def save_schedule(enabled: bool, time_str: str,
                  channels: list[str] | str | None = None) -> dict:
    if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time_str or ""):
        raise ValueError(f"时间格式应为 HH:MM,收到 {time_str!r}")
    if isinstance(channels, str):
        channels = [channels]
    with _schedule_lock:
        cur = load_schedule()
        names = _schedule_channels(channels) or _schedule_channels(cur["channels"])
        now = datetime.now()
        hour, minute = map(int, time_str.split(':'))
        next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        # Saving always schedules a future occurrence, never an implicit catch-up.
        # Keep last_fired truthful, and allow at most one daily run per calendar day.
        if next_run <= now or cur['last_fired'] == now.strftime('%Y-%m-%d'):
            next_run += timedelta(days=1)
        # channel 与 channels 一起写:旧版本的读取方(以及还没升级的状态文件消费者)
        # 认前者,多来源时它就是第一个。
        cur.update({"enabled": bool(enabled), "time": time_str,
                    "channel": names[0], "channels": names,
                    "next_run": next_run.isoformat(timespec='minutes') if enabled else ''})
        _SCHED_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SCHED_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                               encoding="utf-8")
        return cur


def _mark_fired(day: str) -> None:
    cur = load_schedule()
    cur["last_fired"] = day
    tomorrow = datetime.strptime(day, '%Y-%m-%d') + timedelta(days=1)
    cur['next_run'] = tomorrow.strftime('%Y-%m-%d') + 'T' + cur['time']
    _SCHED_FILE.write_text(json.dumps(cur, ensure_ascii=False, indent=1),
                           encoding="utf-8")


def _scheduler_tick() -> None:
    from kb import daily_run
    # 日报自己会依次占用管道跑每个通道。这时候再让别的定时任务插进来抢锁,
    # 日报那边就会一直等不到空闲、把通道一个个记成"跳过"。
    if daily_run.is_running():
        return
    with _schedule_lock:
        s = load_schedule()
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        # Legacy schedules retain restart catch-up; newly saved schedules persist
        # their next occurrence, including busy deferrals across midnight/restarts.
        due = (now.isoformat(timespec='minutes') >= s['next_run'] if s.get('next_run')
               else now.strftime('%H:%M') >= s['time'])
        if s["enabled"] and due and s["last_fired"] != today:
            # channels 是新写法;读到的要是旧配置(或还没升级的调用方给的 dict)就退回单个 channel
            channels = [c for c in (s.get("channels") or [s.get("channel")]) if c]
            if start_pipeline(None, crawl_filter=channel_filter(channels)):
                _mark_fired(today)
                _log(f"定时任务触发全流程同步(每日 {s['time']})")
                return
    if daily_run.tick():
        return
    from kb.feeds.schedule import tick
    tick(start_pipeline)


def _scheduler_loop() -> None:
    while True:
        time.sleep(30)
        try:
            _scheduler_tick()
        except Exception:
            pass          # 调度线程绝不能死


def start_scheduler() -> None:
    global _sched_started
    if _sched_started:
        return
    _sched_started = True
    threading.Thread(target=_scheduler_loop, daemon=True).start()


# ── 待处理数(空闲时给面板显示)────────────────────────────────────────────────

def pending_counts(channel: str | None = None) -> dict:
    """各阶段待处理数。文件扫描有成本,管道运行中不要调。"""
    out: dict = {}
    # 导出待处理:采集器那边已有、但还没落进 vault 的条数(采集器自己算)
    out["export"] = source_pending(channel)
    # 索引/提取待处理
    try:
        from kb import store
        from kb.scanner import scan_all
        conn = store.connect()
        out["index"] = sum(
            1 for d in scan_all()
            if store.doc_needs_reindex(conn, d.path, d.content_hash)
        )
        from kb.extract import pending_docs
        out["extract"] = len(pending_docs(conn))
        conn.close()
    except Exception:
        out["index"] = out["extract"] = None
    # 条目化待处理:gate 通过但还没拆条目的(含存量积压,手动分批消化)
    try:
        st = json.loads((DATA_DIR / "gzh_projects_state.json")
                        .read_text(encoding="utf-8"))
        out["projects"] = sum(
            1 for r in st.get("docs", {}).values()
            if r.get("gate", {}).get("project_article")
            and not r.get("transcribed_at"))
    except Exception:
        out["projects"] = None
    return out
