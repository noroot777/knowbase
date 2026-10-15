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
import threading
import time
from copy import deepcopy

from kb.config import DATA_DIR, announce_config_issue
from kb.collectors import active_collector
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
    run_scopes,
    source_pending,
    source_status,
    source_targets,
)
from kb.pipeline_state import (
    _SEQ_GEN,
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
    row_key,
    same_round,
    valid_round,
    result_version,
    mark_open,
    new_run_id,
    request_stop,
    run_result,
)

# 批次层:同一个业务轮里的几轮管道合并着发布(`pipeline_batch`,从
# `pipeline_state` 拆出来只因为行数上限)。`kb.daily_run` 照旧只认本模块这三个名字。
from kb.pipeline_batch import PublishRefused, close_batch, open_batch

# 失败明细里「用户已经处理掉」的那几条(`pipeline_failures`):真正的删除在采集器
# 那边,这一份只管它从此不再出现在面板上。Web 层照旧只认本模块的名字。
from kb.pipeline_failures import (
    remember as drop_failure,
    strip_runs as strip_failure_runs,
    strip_state as strip_failure_state,
)

# 对外门面:Web 层只 import 本模块,不直接碰拆出去的几个子模块
__all__ = [
    "STAGES",
    "crawl_channels",
    "drop_failure",
    "get_state",
    "close_batch",
    "load_history",
    "last_result",
    "load_last_run",
    "open_batch",
    "persist_error",
    "restore_gap",
    "row_key",
    "same_round",
    "valid_round",
    "result_version",
    "PublishRefused",
    "pending_counts",
    "request_stop",
    "retry_failed_plan",
    "run_result",
    "run_scopes",
    "source_status",
    "source_targets",
    "start_pipeline",
    "start_retry_failed",
    "start_scheduler",
    "strip_failure_runs",
    "strip_failure_state",
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
                   crawl_filter: dict | None = None, batch: str = "",
                   refused: list[str] | None = None) -> str:
    """启动管道线程。返回这一轮的 run id;没起跑返回空串(照旧可以当布尔用)。

    crawl_filter 见 _run_crawl:限定来源目标 + 时间段的范围抓取;
    同时抓几个来源时来源列表在 channels 里(见 crawl_channels)。

    `batch` 是 `pipeline_batch.open_batch()` 发的身份:带着它的几轮运行算同一个
    业务批次,**每一轮收尾都把整批的汇总写进「最近一次运行」**(见
    `pipeline_batch._publish_in_batch`)。这里设进 `_state` 而不是事后再标,是因为
    一轮运行完全可能比调用方的下一跳还快 —— 事后标就赶不上它的收尾了。

    返回 id 而不是 True,是为了让调用方**原子地**拿到"我起的是哪一轮":
    事后再去读状态的话,我们那一轮可能已经跑完、另一项同步已经接替,读回来
    的就是别人的身份(见 `pipeline_state.request_stop` 的说明)。

    **没起跑有两种,要说给用户听的话传一个 `refused` 列表进来收原因**:管道已在跑,
    或者这一轮的持久化边界落不下来(数据目录写不进去,见 `mark_open`)。后者照着报
    「管道已在运行中」就是把一个要动手的故障说成「等一下再点」。
    原因**收在调用方自己的列表里,不放模块变量**:两个窗口同时点同步时,「刚才那次
    为什么没起」只有自己这一次的调用答得准,放全局会被另一次的结论盖掉。
    不关心原因的调用方(定时同步、日报、来源页那几条)照旧什么都不传。
    """
    wanted = [s for s in (stages or list(STAGES)) if s in STAGES]
    crawl_filter = deepcopy(crawl_filter or {})
    crawl_filter["channel"] = active_collector(crawl_filter.get("channel")).name
    # 每个来源都按注册表认一遍名字:认不出来在这里就抛,别等跑到一半才发现
    for scope in crawl_filter.get("channels") or []:
        scope["channel"] = active_collector(scope.get("channel")).name

    def no(why: str) -> str:
        if refused is not None:
            refused.append(why)
        return ""
    with _lock:
        if _state["running"]:
            return no("管道已在运行中")
        # **先占住这一轮的持久化边界,占不住就什么都不改。** 两份终态是收尾时才写的,
        # 都写失败那一档磁盘上一个字都不会留下这一轮:剩下的两份旧副本彼此一致,而
        # 标记那一笔也没落上 —— 恢复把上一轮当成最新、重试去抓上一轮的范围(理由在
        # `pipeline_open`)。顺序是「先落边界、再认这一轮」:反过来的话拒绝起跑得把
        # 下面那一堆字段挨个撤回去,撤漏一个就是下一轮带着上一轮的范围。
        begun = {"run_id": new_run_id(), "seq_gen": _SEQ_GEN,
                 "started_at": _now(), "run_kind": "sync"}
        err = mark_open(begun)
        if err:
            return no(err)
        run_id = begun["run_id"]
        _state.update({
            **begun,
            "running": True, "current_stage": "", "log": [],
            "stop_requested": False, "stopped": False,
            "finished_at": "",
            "crawl_filter": crawl_filter,
            # 常规同步的导出范围就是抓取范围。**显式写一份、不靠"没设过"兜底**:
            # 这是进程内的常驻状态,上一轮如果是重试,里面躺着的是那一轮的导出范围
            # —— 不覆盖的话这一轮的落盘会把它原样带走,下一次重试就按一份跟这一轮
            # 毫无关系的范围去导出。
            "export_filter": deepcopy(crawl_filter),
            # 这一轮属于哪个业务批次(`run_kind` 不在这里写:它在上面那个 `begun`
            # 里,`mark_open` 要带着它)。
            "batch": batch,
            "planned_stages": [s for s in STAGES if s in wanted],
            "stages": {s: {"status": "pending" if s in wanted else "skipped",
                           "detail": ""} for s in STAGES},
        })
    threading.Thread(target=_run_pipeline, args=(wanted, crawl_filter),
                     daemon=True).start()
    return run_id


# ── 定时同步 ────────────────────────────────────────────────────────────────────
# 只剩一条:日报那一份(`kb.daily_run.tick`,设置存 `data/daily_report.json`)。
# 调度线程跟随 kb-web 进程存活;kb-web 不在跑时到点不会触发(无守护进程)。
#
# **另外两条都拆掉了,别再往回加**(来由与代价写在
# `docs/decisions/sync-schedule.md`):管道是单例,几份各自到点的定时撞在一起,
# 先到的那个把后到的挤成「跳过」,而面板上两边都显示正常。
# - 我的订阅页头那颗「定时同步」(原来读 `data/pipeline_schedule.json`):它独有的
#   只有 projects + wiki,手动路径本来就带;那份配置文件现在是**纯存量**,
#   没有任何地方读它。
# - 每类订阅按自己的间隔自动同步(原 `kb.feeds.schedule`):订阅内容现在跟着日报
#   那一轮进库(`daily_run.tick` 的同步范围),手动同步照旧随时可用。

_sched_started = False


def _scheduler_tick() -> None:
    from kb import daily_run
    # 日报自己会依次占用管道跑每个通道,一轮要几十分钟。**这道守卫留着** ——
    # 它现在挡的是"上一轮还在跑,30 秒后这一跳又把 `tick` 叫进去";`tick` 自己也
    # 判,所以这里是冗余带,而冗余带的用处正是不把这件事押在它那一处上。
    if daily_run.is_running():
        return
    _isolated('日报定时', daily_run.tick)


def _isolated(what: str, task) -> bool:
    """跑一条定时任务,**它的异常留在这一跳里**,并留下可诊断的原因。

    读不出配置那一族(`ScheduleUnreadable`)在任务里面已经各自处理过(跳过 + 留
    原因);到这里的是**没预料到的**那些 —— 一律按"这一跳失败"处理,说一句、
    返回 False(没起跑)。

    **剩一条定时了也不许把它摘掉。** 外面那层 `_scheduler_loop` 只有一个
    `except Exception: pass`:异常从这里抛出去就是**一句话都不留**,而定时从此不再
    触发、面板上却一切正常(实测形态:把那份配置写成非 UTF-8 字节,
    `UnicodeDecodeError` 直接穿过去)。「线程没死」不等于「这条任务还在跑」。
    """
    try:
        return bool(task())
    except Exception as e:                            # noqa: BLE001 - 见上
        announce_config_issue(what, f'{what}这一跳出错({type(e).__name__}: {e});'
                                    '这一跳跳过,下一跳照常')
        return False


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
