# -*- coding: utf-8 -*-
"""管道各阶段的实现:抓取 → 导出 → 索引 → 提取 → 条目化 → wiki。

- 抓取/导出: 委托给来源采集器(kb.collectors),本模块不知道来源是什么
- 索引: 进程内(复用已加载的 embedding 模型)
- 提取: 进程内并发调 LLM,tag 自动回写 obs frontmatter
- 条目化: 子进程跑 scripts/*_projects_export.py,产物回灌 pre_tagged 来源
- wiki: 子进程跑 scripts/wiki/wiki_daily.py

编排(谁先谁后、并发存量提取)在 kb.pipeline。
"""

from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path

from kb.collectors import FetchScope, active_collector
from kb.config import VIDEOS_ROOT
from kb.pipeline_state import _log, _now, _stage, _state, stage_segment


# ── 采集:抓取 + 导出,两阶段都只跟采集器契约打交道 ────────────────────────────

def crawl_channels(crawl_filter: dict | str | None = None) -> list[dict]:
    """把一次同步的来源摊平成 [{"channel", "accounts"}],一个或多个都是这个读法。

    一次同步可以同时抓几个来源(面板上勾几个就是几个),所以范围是
    `channels: [{"channel": ..., "accounts": [...]}]`;只抓一个来源时仍写成
    老样子的 `channel` + `accounts`,存量的状态文件、定时配置和重试记录
    因此一律不用迁移。
    """
    if isinstance(crawl_filter, str) or crawl_filter is None:
        crawl_filter = {"channel": crawl_filter or ""}
    scopes = crawl_filter.get("channels") or [
        {"channel": crawl_filter.get("channel") or "",
         "accounts": crawl_filter.get("accounts") or []}]
    return [{"channel": s.get("channel") or "",
             "accounts": [n for n in (s.get("accounts") or [])]} for s in scopes]


def _channel_label(collector) -> str:
    return getattr(collector, "channel_label", collector.label)


def _per_channel(stage: str, crawl_filter: dict | str | None, run) -> None:
    """让每个选中的来源各跑一遍这个阶段。

    多来源时每个来源的汇报各记一段(stage_segment),否则后一个来源的结论会
    把前一个整个盖掉,面板上只剩最后那个来源的成败。
    """
    scopes = crawl_channels(crawl_filter)
    for scope in scopes:
        collector = active_collector(scope["channel"])
        if len(scopes) == 1:
            run(collector, scope)
            continue
        if _state.get("stop_requested"):
            break            # 停止是对整次同步说的,别再起下一个来源
        with stage_segment(stage, _channel_label(collector)):
            try:
                run(collector, scope)
            except Exception as e:
                # 勾了好几个来源:这一个炸了不该把剩下的一起带走。记成它自己那一段的
                # 失败,面板上照样看得见是谁挂的,后面的来源继续跑。
                _stage(stage, "error", f"{type(e).__name__}: {e}")


def source_status(channel: str | None = None) -> dict:
    """当前来源的健康状况,给状态面板用。"""
    collector = active_collector(channel)
    payload = collector.status().as_dict()
    payload["label"] = collector.label
    payload["range_hint"] = getattr(collector, "range_hint", "")
    payload["channel"] = collector.name
    payload["channel_label"] = getattr(collector, "channel_label", collector.label)
    return payload


def source_targets(channel: str | None = None) -> list[str]:
    """来源里可筛选的子集(公众号名等),给面板的筛选下拉用。"""
    from kb.collectors import target_manager

    collector = active_collector(channel)
    manager = target_manager(collector)
    if manager:
        return [t.name for t in manager.list_targets() if t.enabled]
    return list(collector.status().targets)


def source_pending(channel: str | None = None) -> int | None:
    """采集器那边已有、但还没落进 vault 的条数。算不出返回 None。"""
    return active_collector(channel).pending()


def _run_crawl(crawl_filter: dict | None = None) -> None:
    """让采集器把远端内容拉到它本地。

    crawl_filter 为 None 走增量;否则形如
    {"accounts": [...], "since_date": "YYYY-MM-DD", "until_date": "YYYY-MM-DD"}
    的区间补抓,accounts 为空 = 该来源的全部订阅目标。多来源同时抓时
    accounts 记在各自的 channels 项里(见 crawl_channels)。
    """
    filters = crawl_filter or {}

    def _fetch(collector, scope: dict) -> None:
        _log(f"采集通道: {_channel_label(collector)}")
        collector.fetch(FetchScope(
            targets=tuple(scope["accounts"]),
            since=filters.get("since_date", ""),
            until=filters.get("until_date", ""),
            force=bool(filters.get("force")),
        ))

    _per_channel("crawl", crawl_filter, _fetch)


def _run_export(crawl_filter: dict | str | None = None) -> None:
    """让采集器把它本地的内容落成 obs md。"""
    _per_channel("export", crawl_filter, lambda collector, scope: collector.materialize())


class _StopWhileWaiting(Exception):
    """等维护锁的时候用户按了停止。"""


def _index_lock_wait(waited: float) -> None:
    """等维护锁时出声,顺带认一次「停止」。

    不出声的话面板上那张卡片会一直停在「扫描 vault」,看着像卡死;而停止是对
    整次同步说的,等锁期间按下的那一下不该等到锁拿到手才生效。
    """
    if _state.get("stop_requested"):
        raise _StopWhileWaiting()
    _stage("index", "running",
           f"等维护锁:另一个 kb.db 维护任务正在跑(已等 {int(waited)} 秒)")


def _run_index() -> None:
    """扫 vault → 增量 upsert → 收掉已删的 → 补向量。**整趟在维护锁里。**

    锁的原委和取舍都在 `kb.maintenance`:scan 读到的是那一刻磁盘上的内容,而
    删文件和 documents 行是另一个进程的事(存量清理
    `scripts/feed_trim_history.py`)。不互斥的话,一轮在清理之前开扫的索引会拿
    删除**之前**读到的那个 Doc 完成 upsert —— 库里留下一行指向不存在文件的
    documents:搜得到、点开没有原文,而清理那一侧已经收尾、重跑也找不到它
    (2026-10-01 审查实测 `documents=1`、全文检索命中 1)。

    等不到锁就**不做**这一轮:索引漏一轮下一轮自己就补上了(扫描是全量比对),
    而按旧快照写回去的那行假结果要等到下一次索引才会被收掉。
    """
    from kb.maintenance import LockBusy, maintenance_lock

    _stage("index", "running", "扫描 vault")
    try:
        with maintenance_lock(on_wait=_index_lock_wait):
            _index_pass()
    except _StopWhileWaiting:
        _stage("index", "stopped", "已请求停止:等维护锁时放弃了这一轮索引")
    except LockBusy as e:
        _stage("index", "error", f"索引没做: {e}")
    except Exception as e:
        _stage("index", "error", f"索引失败: {e}")


def _index_pass() -> None:
    from kb import store
    from kb.chunker import split_chunks
    from kb.extract import parse_pretagged, save_extraction
    from kb.scanner import scan_all

    conn = store.connect()
    try:
        now = _now()
        seen: set[str] = set()
        new = 0
        for doc in scan_all():
            seen.add(doc.path)
            if not store.doc_needs_reindex(conn, doc.path, doc.content_hash):
                continue
            store.upsert_document(conn, doc, split_chunks(doc.body), now)
            pre = parse_pretagged(doc)
            if pre is not None:
                save_extraction(conn, doc.path, pre)
            new += 1
            if new % 25 == 0:
                conn.commit()
                _stage("index", "running", f"已索引 {new} 篇")
        removed = store.remove_missing_documents(conn, seen)
        conn.commit()

        def _report(done: int, total: int) -> None:
            _stage("index", "running",
                   f"编码 {total} 个新 chunk" if done == 0 else f"编码 {done}/{total}")

        # 向量编码也在锁里:`chunk_vec` 是没有外键级联的虚表,清理在编码中途
        # 删掉那些 chunk 的话,后面几批会按已经不存在的 chunk id 插回孤儿向量
        # 行,谁也不会再来收(见 `kb.maintenance`)。
        from kb.embedder import embed_pending_chunks
        n = embed_pending_chunks(conn, _report)
        _stage("index", "ok", f"新增/更新 {new},移除 {removed},新向量 {n}")
    finally:
        conn.close()


def _run_extract() -> None:
    """单独跑提取阶段(管道编排里 crawl+extract 同选时走 _extract_pass 的并行路径)。"""
    stats = _new_extract_stats()
    _extract_pass("", stats)
    _finish_extract_stage(stats)


def _new_extract_stats() -> dict:
    # failed/empty 按路径记 set:存量+增量两轮会重试同一篇,计数去重,
    # 面板上的"失败 N"才等于真正有问题的文档数
    # failures: path -> {"title", "reason"},重试成功会移除,只留最终仍失败的
    return {"ok": 0, "promo": 0, "failed_paths": set(), "empty_paths": set(),
            "failures": {}}


def _finish_extract_stage(stats: dict) -> None:
    ok = stats["ok"]
    failed = len(stats["failed_paths"])
    empty = len(stats["empty_paths"])
    if ok + failed + empty == 0:
        _stage("extract", "ok", "没有待提取的文档")
        return
    detail = f"成功 {ok},失败 {failed}"
    if stats.get("promo"):
        detail += f",软广 {stats['promo']}(已排除出条目化)"
    if empty:
        detail += f",跳过 {empty}(正文空,需回填)"
    _stage("extract", "ok" if failed == 0 and empty == 0 else "error", detail,
           failures=list(stats["failures"].values()))


# 提取并发数。瓶颈是逐篇几十秒的 LLM 调用,与微信反爬无关,可以放心并发。
# 2026-08-26 起提取走 kb.llm 的 API 通道(模型见设置面板的「模型分配」),每个 worker 只是一个
# HTTP 请求,不再是 claude CLI(Node)子进程,本机 RAM/CPU 不再是上限;真正的
# 约束变成中转站的并发/速率限制与按 token 计费,故沿用实测稳定的 6 不上调。
EXTRACT_WORKERS = 6


def _extract_one(path: str, title: str, account: str = "") -> dict | None:
    """工作线程:读正文(独立连接,WAL 下并发读安全)→ 调 LLM。不碰共享连接。

    正文为空(转载 stub 等导致 md 没有正文)时返回 None——调 LLM 必然失败或
    产出只凭标题的劣质结果,直接跳过留给"重试失败项"回填后再提。"""
    from kb import store
    from kb.extract import extract_with_llm

    conn = store.connect()
    try:
        body = store.doc_body(conn, path)
    finally:
        conn.close()
    if not body.strip():
        return None
    return extract_with_llm(title, body, account=account)


def _extract_pass(label: str, stats: dict) -> None:
    """跑一轮提取,结果累加进 stats。label 用于进度展示("存量"/"增量")。

    LLM 调用在线程池里并发,DB 写全部留在本线程串行,避免多写者竞争。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from kb import store
    from kb.extract import (pending_docs, save_extraction,
                            writeback_extraction)

    conn = store.connect()
    try:
        todo = pending_docs(conn)
        if not todo:
            if label:
                _log(f"extract: {label}无待提取文档")
            return
        _stage("extract", "running", f"{label}提取 0/{len(todo)}")
        done = 0
        with ThreadPoolExecutor(max_workers=EXTRACT_WORKERS) as pool:
            futs = {pool.submit(_extract_one, row["path"], row["title"],
                                row["account"]): row
                    for row in todo}
            for fut in as_completed(futs):
                row = futs[fut]
                done += 1
                try:
                    data = fut.result()
                    if data is None:
                        stats["empty_paths"].add(row["path"])
                        stats["failures"][row["path"]] = {
                            "title": row["title"], "reason": "正文为空,需回填"}
                        _log(f"extract: SKIP 正文为空 {row['title'][:24]}")
                    else:
                        stale_flag = save_extraction(conn, row["path"], data)
                        conn.commit()
                        promo = data.get("promo") or {}
                        if promo.get("level") == "hard":
                            stats["promo"] += 1
                            _log(f"extract: 软广 {row['title'][:24]} — "
                                 f"{promo.get('reason', '')[:40]}")
                        # tag 回写 obs frontmatter(决策:自动回写);回写改了文件,
                        # 同步刷新 hash 免得下次 index 白白重建这批文档
                        if writeback_extraction(conn, row["path"], data, stale_flag):
                            conn.commit()
                        stats["ok"] += 1
                        # 存量轮失败、增量轮成功:清掉旧失败记录,计数才是最终状态
                        stats["failed_paths"].discard(row["path"])
                        stats["empty_paths"].discard(row["path"])
                        stats["failures"].pop(row["path"], None)
                except Exception as e:
                    stats["failed_paths"].add(row["path"])
                    stats["failures"][row["path"]] = {
                        "title": row["title"], "reason": str(e)[:160]}
                    _log(f"extract: FAIL {row['title'][:24]}: {e}")
                _stage("extract", "running",
                       f"{label}提取 {done}/{len(todo)} — {row['title'][:24]}")
    finally:
        conn.close()


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
    """把条目化脚本新产出的 md 就地入库(只增不删;删除留给下轮 index 阶段)。"""
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
            if not store.doc_needs_reindex(conn, doc.path, doc.content_hash):
                continue
            store.upsert_document(conn, doc, split_chunks(doc.body), now)
            pre = parse_pretagged(doc)
            if pre is not None:
                save_extraction(conn, doc.path, pre)
            new += 1
        conn.commit()
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
    from datetime import timedelta
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
    except Exception as e:
        _stage("projects", "error", f"入库失败: {e}")
        return
    summaries.append(f"入库 {n_new} 条目")
    _stage("projects", "ok", " · ".join(summaries)[:200] or "无新增")
