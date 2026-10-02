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
    """配置好的回填脚本路径(绝对路径);没配返回 None。**这个函数永远不抛异常。**

    现读环境变量而不是在导入时定下来:测试要能逐个用例换路径。

    **必须在这里就变成绝对路径**:跑它的时候 `cwd` 是脚本自己所在的目录(脚本按相对
    路径找 vault 和存量 md),相对路径配置下这两件事会打架 ——
    `KB_REFILL_SCRIPT=tools/refill.py` 的存在性检查在当前目录下通过,子进程却带着
    `cwd=tools/` 再去找 `tools/refill.py`,于是打开的是 `tools/tools/refill.py`,
    报一句「回填脚本异常: ... No such file」,而检查明明说文件在
    (2026-10-02 审查实测)。检查和执行用同一个绝对路径,这条缝就没有了。

    **绝对化只做字符串层面的 `abspath`,不做 `resolve()`。** `Path.resolve()` 要顺着
    符号链接走,**链接成环时 Python 3.11 抛 `RuntimeError: Symlink loop`** —— 而这个
    函数在 `retry_failed_plan()` 里被调用,异常从计划生成阶段一路冒到 HTTP 层:面板上
    点「重试失败项」只看到一句「重试未能启动 / Internal Server Error」,**连同一轮里
    本该重试的 index / extract 一起没了**,而这只是一个可选外挂配错了
    (2026-10-02 第 2 轮审查实测,真走 `POST /api/pipeline/retry-failed` 复现 500)。
    路径对不对本来就有 `_run_refill` 里那道 `is_file()` 负责报(坏链接上它返回 False,
    不抛),这里只负责交出一个「检查和执行共用」的绝对路径。`abspath` 是纯字符串运算、
    碰不到文件系统,唯一的例外是相对路径要问一次 cwd。

    **`~` 展开也必须在同一条异常边界里面。** `Path.expanduser()` 对 `~不存在的用户名/…`
    抛 `RuntimeError: Could not determine home directory.`(Python 3.11)—— 写错一个
    用户名、或者从别人的配置里抄一条 `~someone/...` 过来,后果和上面那个坏链接**一模
    一样**:`POST /api/pipeline/retry-failed` 返回 500,面板上只有一句「重试未能启动」,
    index / extract 一起没了(2026-10-02 第 3 轮审查实测)。

    所以这个函数的契约是一句话:**不管配进来的是什么字符串,它只返回路径、永不抛。**
    任何一种解析失败都把原字符串原样交回去(这也是最好的诊断文案 —— 提示里出现的就是
    用户自己配的那一行),照样让 `is_file()` 那道检查去报「指不到脚本文件」。
    """
    raw = os.environ.get(_REFILL_ENV, "").strip()
    if not raw:
        return None
    try:
        return Path(os.path.abspath(Path(raw).expanduser()))
    except (OSError, RuntimeError, ValueError):
        return Path(raw)

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
    # 配了却指不到一个文件——这是配置错了,得说出来。`is_file()` 只吞「不存在 / 不是
    # 目录 / 链接成环」那几个 errno,**名字太长(ENAMETOOLONG)它是照抛的** —— 而这里
    # 抛出去就把 `_run_retry_failed` 的循环打断,后面的 index / extract 一个都不跑
    # (POST 已经返回 200 了,面板上只看到回填这张卡片停在 running)。坏路径的处置
    # 只有一种:说清楚、跳过。
    try:
        usable = script.is_file()
    except (OSError, ValueError):
        usable = False
    if not usable:
        _stage("export", "skipped", f"{_REFILL_ENV} 指不到脚本文件:{script}")
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
