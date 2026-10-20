# -*- coding: utf-8 -*-
"""子进程跑的那两段**全库慢活**:条目化(工具/项目拆条目)与 wiki 层同步。

从 `kb/pipeline_stages.py` 拆出来**只因为行数**(那边贴着单文件 700 行上限,
CLAUDE.md 那条),整段逐符号搬过来、一个判据都没改。两段的共同点是:都起子进程、
都是全库范围(不认来源)、都一跑几十分钟 —— 面板上那两枚「几十分钟」标记说的就是
它们。

**依赖方向:`pipeline_stages` → 本模块**(那边在模块级 import 本模块、把这几个名字
再导出一次,因为 `pipeline.py` / `pipeline_retry.py` 和 `tests/test_index_lock.py`
都按旧位置取 `_run_wiki` / `_run_projects`)。所以本模块反过来只许**在函数体里**
取它。`_lock_waiter` / `_StopWhileWaiting` 还住在那边(它们也服务 `_run_index`),
**要它们的两个函数各自取一次** —— `except _StopWhileWaiting:` 认的是函数作用域里
那个名字,只在其中一个函数里 import 的话另一个当场 NameError(而那一条路正是「等
不到锁 / 等锁期间被停止」,平时走不到)。

**打桩要打在本模块上。** `_run_projects` 查的是**本模块**的全局名字,
`patch.object(pipeline_stages, '_ingest_pretagged_sources', ...)` 拦不住它
—— 那个名字带的是绑定不是引用(同 `tests/pipeline_case.py` 里那条)。
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

from kb.config import VIDEOS_ROOT
from kb.pipeline_state import _log, _now, _stage, _state


# ── wiki 层同步 ─────────────────────────────────────────────────────────────────

WIKI_DAILY = Path(__file__).resolve().parent.parent / "scripts" / "wiki" / "wiki_daily.py"
_ERR_KEEP = 1500          # 失败时保留的 stderr 尾部长度(日志只留 80 条,别让一条撑爆)


def _run_wiki() -> None:
    """extract 之后跑 wiki 层:双链化 → 实体/主题页 → index → digest/synthesis → graph。

    子进程用当前 venv 的 python(wiki_daily 里 linkify 要 import kb.store)。
    digest/synthesis 有日期门(只看最近 2 天且 >= EPOCH),历史批量导入只会触发
    机械的双链化和页面重渲染,不烧 LLM。"""
    import sys as _sys

    _stage("wiki", "running", "同步 wiki 层(双链/页面/摘要)")
    try:
        proc = subprocess.run(
            [_sys.executable, str(WIKI_DAILY), "--recent-days", "2"],
            cwd=WIKI_DAILY.parent, capture_output=True, text=True, timeout=45 * 60,
        )
    except subprocess.TimeoutExpired:
        _stage("wiki", "error", "wiki 同步超时(45 分钟)")
        return
    tail = [l for l in proc.stdout.splitlines()
            if l.startswith(("linkify:", "digest:", "synthesis:",
                             "graph:", "catgraph:"))]
    detail = ";".join(t.strip() for t in tail) or proc.stdout.strip()[-160:]
    # 面板卡片空间有限,压成一行短语;完整输出看日志
    m1 = re.search(r"新双链化 (\d+)", proc.stdout)
    m2 = re.search(r"digest: 生成 (\d+) 天", proc.stdout)
    m3 = re.search(r"synthesis: 更新 (\d+) 个实体", proc.stdout)
    if m1 or m2 or m3:
        detail = (f"双链化 {m1.group(1) if m1 else 0} 篇 · "
                  f"日摘要 {m2.group(1) if m2 else 0} 天 · "
                  f"综合 {m3.group(1) if m3 else 0} 实体")
    # 图谱子步骤自己会让 wiki_daily 非零退出,但那三项摘要一重建就把它盖没了,
    # 面板上只剩「失败: 」一句空话。图谱那几行单拎出来,附在详情后面。
    graph = [l.strip() for l in proc.stdout.splitlines()
             if l.startswith(("graph:", "catgraph:"))]
    bad = [l for l in graph if ": FAIL" in l]
    if m1 or m2 or m3:
        note = ";".join(bad) if bad else ("图谱 ok" if graph else "")
        if note:
            detail = f"{detail} · {note}"
    if proc.returncode != 0:
        _fail_wiki(proc, bad, detail)
    else:
        _stage("wiki", "ok", detail[:200] or "完成")


def _fail_wiki(proc, bad: list[str], detail: str) -> None:
    """wiki 阶段标红:标签(哪张图)、根因(一句话)、完整 stderr(可展开+进日志)。

    三样都得留住,少一样面板上就不够诊断(2026-09-22 第 9 轮审查发现):

    - **标签单独留出来,只截错误正文**。整句一起 `[-200:]` 的话,稍长一点的
      报错就把开头的 `graph:`/`catgraph:` 截没了,红态里连是哪张图挂的都看不出来。
    - **摘要优先取 stderr 里最后一条真正的错误行**。多行 traceback 在 stdout
      那边只留一句「Traceback (most recent call last):」,根因在 stderr 末行;
      `wiki_daily:` 开头那句是它自己的汇总(只说哪张图挂了),跳过去继续往上找。
    - **完整 stderr 进日志、也进失败明细**。子进程是 `capture_output=True` 抓的,
      不主动写出来的话它哪儿都不会出现,用户只能手工重跑一次才能看到根因。
    """
    err = (proc.stderr or "").strip()
    lines = [x.strip() for x in err.splitlines() if x.strip()]
    root = next((x for x in reversed(lines) if not x.startswith("wiki_daily:")), "")
    tags = "/".join(dict.fromkeys(l.split(":", 1)[0] for l in bad))
    body = root or ";".join(l.partition(": FAIL")[2].strip() or l
                            for l in bad) or detail.strip()
    fails = None
    if err:
        _log(f"wiki 子进程 stderr:\n{err[-_ERR_KEEP:]}")
        fails = [{"title": f"{tags or 'wiki_daily'} 子进程报错",
                  "reason": err[-_ERR_KEEP:]}]
    _stage("wiki", "error", f"{tags or 'wiki'} 失败: {body[-160:]}", failures=fails)


# ── 条目化阶段:工具/项目拆条目(dr-gzh-projects / dr-video-projects)─────────────

KB_DIR = Path(__file__).resolve().parent.parent
VIDEOS_DIR = VIDEOS_ROOT


def _run_export_script(script: str, *argv: str, timeout_min: int):
    import sys
    return subprocess.run(
        [sys.executable, f"scripts/{script}", *argv],
        cwd=KB_DIR, capture_output=True, text=True, timeout=timeout_min * 60,
    )


def _ingest_pretagged_sources() -> tuple[int, int]:
    """把条目化脚本新产出的 md 就地入库(只增不删;删除留给下轮 index 阶段)。

    **整趟在维护锁里**,理由和 index 阶段逐字相同(见 `kb.maintenance`):这条
    路也是「scan 读一份磁盘快照 → upsert → 补向量」,而 `embed_pending_chunks`
    补的是**全库**待编码的 chunk、不限于条目来源。不取锁的话,一轮并发的存量
    清理删掉的那些 chunk 会被这里按旧快照插回 `chunk_vec` —— 那张虚表没有外键
    级联,孤儿行谁也不会再来收,还照样占着语义检索的候选名额:清理之后仍然留着
    的文章从此召不回来,重跑索引也不恢复(2026-10-01 审查实测 151 行孤儿向量、
    语义检索返回空)。「只增不删」防不住这件事 —— 要删的是另一个进程。
    """
    from kb.maintenance import maintenance_lock

    # 延迟 import:**方向单向**,理由在模块头(那两个名字还服务 `_run_index`)。
    from kb.pipeline_stages import _StopWhileWaiting, _lock_waiter

    waited, on_poll, on_wait = _lock_waiter("projects")
    with maintenance_lock(on_wait=on_wait, on_poll=on_poll):
        if waited["on"] and _state.get("stop_requested"):
            raise _StopWhileWaiting()
        return _ingest_pass()


def _ingest_pass() -> tuple[int, int]:
    from kb import store
    from kb.chunker import split_chunks
    from kb.extract import parse_pretagged, save_extraction
    from kb.scanner import scan_all

    targets = {"dr-video-projects", "dr-gzh-projects"}
    conn = store.connect()
    try:
        now = _now()
        new = 0
        for doc in scan_all():
            if doc.source not in targets:
                continue
            if store.observed_document_is_current(conn, doc):
                continue
            store.upsert_document(conn, doc, split_chunks(doc.body), now)
            pre = parse_pretagged(doc)
            if pre is not None:
                save_extraction(conn, doc.path, pre)
            new += 1
        conn.commit()
        # 同 `_index_pass`:条目化入库也不替抓取记账(这两棵还都是衍生树,
        # 日报取材和抓取账本都把它们排除在外)。
        from kb.embedder import embed_pending_chunks
        return new, embed_pending_chunks(conn)
    finally:
        conn.close()


def _run_projects() -> None:
    """条目化:gate 判「工具/项目介绍型」→ transcribe 按工具拆条目。
    两步各用哪个模型看设置面板的「模型分配」(kb.llm_stages 的 gate / transcribe)。

    公众号侧 transcribe 只处理最近 3 天发布的文章——存量积压(数千篇)由用户
    手动分批消化(scripts/gzh_projects_export.py transcribe --limit N),
    避免定时同步悄悄烧大额度。脚本自带文件锁:手动实例在跑时本阶段跳过。"""
    # 延迟 import,同 `_ingest_pretagged_sources`(方向单向,见模块头)。
    from kb.pipeline_stages import _StopWhileWaiting

    from datetime import timedelta

    from kb.maintenance import LockBusy
    since = (datetime.now() - timedelta(days=3)).strftime("%Y-%m-%d")
    steps = [
        ("gzh gate", "gzh_projects_export.py", ["gate"], 30),
        ("gzh 条目化", "gzh_projects_export.py",
         ["transcribe", "--since", since], 60),
    ]
    if VIDEOS_DIR.is_dir():
        steps += [
            ("视频 gate", "video_projects_export.py", ["gate"], 20),
            ("视频转写", "video_projects_export.py", ["transcribe"], 90),
        ]
    summaries = []
    for label, script, argv, tmo in steps:
        _stage("projects", "running", f"{label}…")
        try:
            proc = _run_export_script(script, *argv, timeout_min=tmo)
        except subprocess.TimeoutExpired:
            _stage("projects", "error", f"{label} 超时({tmo}分钟)")
            return
        if proc.returncode == 3:
            _stage("projects", "skipped", f"{label}: 有手动实例在跑,本轮跳过")
            return
        if proc.returncode != 0:
            _stage("projects", "error",
                   f"{label} 失败: {(proc.stderr or proc.stdout)[-200:]}")
            return
        tail = [l for l in proc.stdout.splitlines() if "完成:" in l]
        if tail:
            summaries.append(f"{label} {tail[-1].split('完成:')[-1].strip()}")
    _stage("projects", "running", "新条目入库")
    try:
        n_new, n_vec = _ingest_pretagged_sources()
    except _StopWhileWaiting:
        _stage("projects", "stopped", "已请求停止:等维护锁时放弃了新条目入库")
        return
    except LockBusy as e:
        _stage("projects", "error", f"新条目没入库: {e}")
        return
    except Exception as e:
        _stage("projects", "error", f"入库失败: {e}")
        return
    summaries.append(f"入库 {n_new} 条目")
    _stage("projects", "ok", " · ".join(summaries)[:200] or "无新增")
