# -*- coding: utf-8 -*-
"""重试失败项:按上次运行的失败状态编排 回填空正文 → 重建索引 → 重跑提取。

面板上那个全局「重试失败项」按钮的后端。计划由 retry_failed_plan 推导,
执行复用 kb.pipeline_stages 里的阶段函数。
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
from copy import deepcopy
from pathlib import Path

from kb.pipeline_state import (
    STAGES,
    _end_run,
    _lock,
    _log,
    _now,
    _stage,
    _state,
    load_last_run,
    new_run_id,
)
from kb.pipeline_stages import (
    _run_crawl,
    _run_export,
    _run_extract,
    _run_index,
    _run_projects,
    _run_wiki,
)

# 正文回填脚本是**可选外挂**:路径由 KB_REFILL_SCRIPT 指定,仓库里不带它。
# 它要干的事(找 dr-gzh 树里正文为空的 md、重抓原文回填,转载 stub 还要走平台列表
# API 定位原文短链)取决于各人存量是怎么坏的,没法内置成一个通用实现——所以做成
# "配了就用、没配就整段不做",而不是在代码里写死某台机器上的一个路径:后者让每个
# 外部用户每次点「重试失败项」都看到一行指向别人家目录的提示,而这个功能对他们
# 永远不可用。
_REFILL_ENV = "KB_REFILL_SCRIPT"


def refill_script() -> Path | None:
    """配置好的回填脚本路径;没配返回 None。

    现读环境变量而不是在导入时定下来:测试要能逐个用例换路径。
    """
    raw = os.environ.get(_REFILL_ENV, "").strip()
    return Path(raw).expanduser() if raw else None

# 阶段 detail 里写了失败条数就算失败(状态本身是 ok 也算),与面板按钮同一套判定
_FAIL_DETAIL_RE = re.compile(r"失败 [1-9]|\d+ 个账号失败")


def _run_refill() -> None:
    """子进程跑配置好的回填脚本:找 dr-gzh 树里正文为空的 md,重抓原文回填
    (转载 stub 会走平台列表 API 定位原文短链)。占用 export 卡片展示进度。

    脚本按约定逐行输出 `OK <相对路径>` / `FAIL <说明>`,没有空正文时打印
    "没有找到空正文文件"。回填成功的文档重置 extracted_at:防御历史上空正文却
    仅凭标题"提取成功"的假成功,让它们跟着这次的 extract 重提。"""
    script = refill_script()
    if script is None:   # 正常形态:没配这个外挂,这一步不存在
        _stage("export", "skipped", f"未配置正文回填脚本({_REFILL_ENV}),跳过回填")
        return
    if not script.exists():   # 配了却指不到——这是配置错了,得说出来
        _stage("export", "skipped", f"{_REFILL_ENV} 指向的脚本不存在:{script}")
        return
    _stage("export", "running", "回填空正文(重抓原文)")
    try:
        proc = subprocess.run(
            ["python3", str(script)],
            cwd=script.parent, capture_output=True, text=True,
            timeout=30 * 60,
        )
    except subprocess.TimeoutExpired:
        _stage("export", "error", "回填超时")
        return
    out = proc.stdout or ""
    fixed_rel, fail_items = [], []
    for line in out.splitlines():
        s = line.strip()
        if s.startswith("OK"):
            fixed_rel.append(s.split(None, 1)[1])
        elif s.startswith("FAIL"):
            fail_items.append({"title": s[4:].strip()[:80], "reason": "回填失败"})
            _log(f"export: 回填 {s}")
    failed = len(fail_items)
    if not fixed_rel and failed == 0:
        if "没有找到空正文文件" in out:
            _stage("export", "ok", "没有空正文文件,直接重跑提取")
        else:
            _stage("export", "error",
                   f"回填脚本异常: {(proc.stderr or out)[-200:]}")
        return
    if fixed_rel:
        from kb import store
        conn = store.connect()
        try:
            for rel in fixed_rel:
                conn.execute(
                    "UPDATE documents SET extracted_at=NULL, summary='' "
                    "WHERE path=?", (f"source/dr-gzh/{rel}",))
            conn.commit()
        finally:
            conn.close()
    _stage("export", "ok" if failed == 0 else "error",
           f"回填 {len(fixed_rel)} 篇"
           + (f",{failed} 篇回填失败" if failed else ""),
           failures=fail_items)


def retry_failed_plan() -> list[str]:
    """按上次运行的失败状态编排重试链(面板全局"重试失败项"按钮的后端)。

    - crawl 有账号失败/出错 → 重抓(增量爬取,便宜)
    - export 有失败/出错、或链里重抓过 → 重导出
    - extract 有"正文空"跳过、且配了回填脚本 → 先回填(refill)
    - export/refill 可能补了数据、或 index 自己出错 → 重建索引
    - extract 出过错、或前面补了新文章 → 重提取
    - projects/wiki 出过错 → 重跑(它们在链尾,不牵连别人)

    「算失败」的判定必须和面板按钮(kb/static/js/pipe.js)保持同一套规则:
    status=error,或 detail 里写了失败条数。两边不一致的话按钮会亮着但这里
    返回空计划,用户点下去只收到 409。
    """
    prev = (load_last_run() or {}).get("stages", {})

    def _detail(name: str) -> str:
        return (prev.get(name) or {}).get("detail") or ""

    def _failed(name: str) -> bool:
        return ((prev.get(name) or {}).get("status") == "error"
                or bool(_FAIL_DETAIL_RE.search(_detail(name))))

    # projects 的"入库失败"卡在最后的 chunk 入库/编码那步 —— gate/transcribe 的
    # 大模型调用其实已经成功了。而 index 扫的是整个 vault,dr-*-projects 只是它的
    # 子集,交给 index 重建就够,不必再烧一遍额度重跑条目化。
    projects_ingest_only = (_failed("projects")
                            and _detail("projects").startswith("入库失败"))

    plan: list[str] = []
    if _failed("crawl"):
        plan.append("crawl")
    if _failed("export") or "crawl" in plan:
        plan.append("export")
    # 没配回填脚本时这一步压根不进计划:让外部用户看一张"本次跳过"的卡片,
    # 比不显示更糟——那个功能对他们不存在。
    if "正文空" in _detail("extract") and refill_script() is not None:
        plan.append("refill")
    if plan or _failed("index") or projects_ingest_only:
        plan.append("index")
    if _failed("extract") or "index" in plan:
        plan.append("extract")
    if _failed("projects") and not projects_ingest_only:
        plan.append("projects")
    if _failed("wiki"):
        plan.append("wiki")
    if not plan:
        # 上次没记录失败,但可能还有提取积压(如手动往 vault 放过文档)
        from kb import store
        from kb.extract import pending_docs
        conn = store.connect()
        try:
            if pending_docs(conn, limit=1):
                plan = ["extract"]
        finally:
            conn.close()
    return plan


def _run_retry_failed(plan: list[str], crawl_filter: dict | None = None) -> None:
    try:
        runners = {"crawl": lambda: _run_crawl(crawl_filter),
                   "export": lambda: _run_export(crawl_filter),
                   "refill": _run_refill, "index": _run_index,
                   "extract": _run_extract, "projects": _run_projects, "wiki": _run_wiki}
        for stage in plan:
            if _state.get("stop_requested"):
                break
            runners[stage]()
    finally:
        _end_run()


def start_retry_failed() -> tuple[bool, str]:
    """一键重试所有失败项。返回 (started, msg);无失败项或已在跑时 False。"""
    plan = retry_failed_plan()
    crawl_filter = deepcopy((load_last_run() or {}).get("crawl_filter") or {})
    if not plan:
        return False, "上次运行没有失败项,也没有提取积压"
    with _lock:
        if _state["running"]:
            return False, "管道已在运行中"
        # card 标记这一轮会碰哪些阶段:按 STAGES 生成,STAGES 加新阶段也不会漏 key。
        # refill 不是独立卡片,占用 export 那张展示进度。
        card = {s: s in plan for s in STAGES}
        card["export"] = card["export"] or "refill" in plan
        # 这条也是一次"新的运行":发个新身份,别让它继承上一轮的 id
        # (日报只停自己那一轮,靠的就是这个 id 不复用)。
        _state.update({
            "running": True, "current_stage": "", "log": [],
            "stop_requested": False, "stopped": False,
            "run_id": new_run_id(),
            "started_at": _now(), "finished_at": "",
            "crawl_filter": crawl_filter,
            "run_kind": "retry",
            "planned_stages": [s for s in STAGES if card[s]],
            "stages": {s: {"status": "pending" if card[s] else "skipped",
                           "detail": "" if card[s] else "本次无需重试"}
                       for s in STAGES},
        })
    _log("重试失败项: " + " → ".join(plan))
    threading.Thread(target=_run_retry_failed, args=(plan, crawl_filter), daemon=True).start()
    return True, " → ".join(plan)
