# -*- coding: utf-8 -*-
"""重试失败项:按上次运行的失败状态编排 回填空正文 → 重建索引 → 重跑提取。

面板上那个全局「重试失败项」按钮的后端。计划由 retry_failed_plan 推导,
执行复用 kb.pipeline_stages 里的阶段函数。
"""

from __future__ import annotations

import re
import subprocess
import threading
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
)
from kb.pipeline_stages import _run_crawl, _run_export, _run_extract, _run_index

REFILL_SCRIPT = Path.home() / "code" / "gongzhonghao" / "gzh_refill_body.py"


def _run_refill() -> None:
    """子进程跑 gzh_refill_body.py:找 dr-gzh 树里正文为空的 md,重抓原文回填
    (转载 stub 会走平台列表 API 定位原文短链)。占用 export 卡片展示进度。

    回填成功的文档重置 extracted_at:防御历史上空正文却仅凭标题"提取成功"
    的假成功,让它们跟着这次的 extract 重提。"""
    if not REFILL_SCRIPT.exists():
        _stage("export", "skipped", f"未找到 {REFILL_SCRIPT},跳过回填")
        return
    _stage("export", "running", "回填空正文(重抓原文)")
    try:
        proc = subprocess.run(
            ["python3", str(REFILL_SCRIPT)],
            cwd=REFILL_SCRIPT.parent, capture_output=True, text=True,
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
    - extract 有"正文空"跳过 → 先回填(refill)
    - export/refill 可能补了数据 → 重建索引
    - extract 出过错、或前面补了新文章 → 重提取
    """
    prev = (load_last_run() or {}).get("stages", {})

    def _detail(name: str) -> str:
        return (prev.get(name) or {}).get("detail") or ""

    def _err(name: str) -> bool:
        return (prev.get(name) or {}).get("status") == "error"

    plan: list[str] = []
    if _err("crawl") or re.search(r"\d+ 个账号失败", _detail("crawl")):
        plan.append("crawl")
    if _err("export") or re.search(r"失败 [1-9]", _detail("export")) \
            or "crawl" in plan:
        plan.append("export")
    if "正文空" in _detail("extract"):
        plan.append("refill")
    if plan:
        plan.append("index")
    if _err("extract") or "index" in plan:
        plan.append("extract")
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


def _run_retry_failed(plan: list[str]) -> None:
    try:
        if "crawl" in plan:
            _run_crawl(None)
        if "export" in plan:
            _run_export()
        if "refill" in plan:
            _run_refill()
        if "index" in plan:
            _run_index()
        if "extract" in plan:
            _run_extract()
    finally:
        _end_run()


def start_retry_failed() -> tuple[bool, str]:
    """一键重试所有失败项。返回 (started, msg);无失败项或已在跑时 False。"""
    plan = retry_failed_plan()
    if not plan:
        return False, "上次运行没有失败项,也没有提取积压"
    with _lock:
        if _state["running"]:
            return False, "管道已在运行中"
        # card 只列重试链会碰的阶段;STAGES 后来新增的(projects/wiki)按"无需重试"处理。
        # 这里必须 .get 兜底 —— 写死 key 的话,STAGES 一加新阶段就 KeyError 500。
        card = {"crawl": "crawl" in plan,
                "export": "export" in plan or "refill" in plan,
                "index": "index" in plan,
                "extract": "extract" in plan}
        _state.update({
            "running": True, "current_stage": "", "log": [],
            "started_at": _now(), "finished_at": "",
            "stages": {s: {"status": "pending" if card.get(s) else "skipped",
                           "detail": "" if card.get(s) else "本次无需重试"}
                       for s in STAGES},
        })
    _log("重试失败项: " + " → ".join(plan))
    threading.Thread(target=_run_retry_failed, args=(plan,), daemon=True).start()
    return True, " → ".join(plan)
