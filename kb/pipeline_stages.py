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
from kb.pipeline_state import _log, _now, _stage


# ── 采集:抓取 + 导出,两阶段都只跟采集器契约打交道 ────────────────────────────

def source_status() -> dict:
    """当前来源的健康状况,给状态面板用。"""
    collector = active_collector()
    payload = collector.status().as_dict()
    payload["label"] = collector.label
    payload["range_hint"] = getattr(collector, "range_hint", "")
    return payload


def source_targets() -> list[str]:
    """来源里可筛选的子集(公众号名等),给面板的筛选下拉用。"""
    return list(active_collector().status().targets)


def source_pending() -> int | None:
    """采集器那边已有、但还没落进 vault 的条数。算不出返回 None。"""
    return active_collector().pending()


def _run_crawl(crawl_filter: dict | None = None) -> None:
    """让采集器把远端内容拉到它本地。

    crawl_filter 为 None 走增量;否则形如
    {"accounts": [...], "since_date": "YYYY-MM-DD", "until_date": "YYYY-MM-DD"}
    的区间补抓,accounts 为空 = 该来源的全部订阅目标。
    """
    scope = FetchScope()
    if crawl_filter:
        scope = FetchScope(
            targets=tuple(crawl_filter.get("accounts") or ()),
            since=crawl_filter.get("since_date", ""),
            until=crawl_filter.get("until_date", ""),
        )
    active_collector().fetch(scope)


def _run_export() -> None:
    """让采集器把它本地的内容落成 obs md。"""
    active_collector().materialize()


def _run_index() -> None:
    from kb import store
    from kb.chunker import split_chunks
    from kb.extract import parse_pretagged, save_extraction
    from kb.scanner import scan_all

    _stage("index", "running", "扫描 vault")
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

        rows = store.chunks_without_vectors(conn)
        if rows:
            _stage("index", "running", f"编码 {len(rows)} 个新 chunk")
            from kb.embedder import embed_texts
            B = 128
            for i in range(0, len(rows), B):
                batch = rows[i:i + B]
                store.insert_vectors(conn, [r["id"] for r in batch],
                                     embed_texts([r["text"] for r in batch]))
                conn.commit()
        _stage("index", "ok", f"新增/更新 {new},移除 {removed},新向量 {len(rows)}")
    except Exception as e:
        _stage("index", "error", f"索引失败: {e}")
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
    return {"ok": 0, "failed_paths": set(), "empty_paths": set(), "failures": {}}


def _finish_extract_stage(stats: dict) -> None:
    ok = stats["ok"]
    failed = len(stats["failed_paths"])
    empty = len(stats["empty_paths"])
    if ok + failed + empty == 0:
        _stage("extract", "ok", "没有待提取的文档")
        return
    detail = f"成功 {ok},失败 {failed}"
    if empty:
        detail += f",跳过 {empty}(正文空,需回填)"
    _stage("extract", "ok" if failed == 0 and empty == 0 else "error", detail,
           failures=list(stats["failures"].values()))


# 提取并发数。瓶颈是逐篇几十秒的 LLM 调用,与微信反爬无关,可以放心并发。
# 2026-08-26 起提取走 kb.llm 的 API 通道(gpt-5.4-mini),每个 worker 只是一个
# HTTP 请求,不再是 claude CLI(Node)子进程,本机 RAM/CPU 不再是上限;真正的
# 约束变成中转站的并发/速率限制与按 token 计费,故沿用实测稳定的 6 不上调。
EXTRACT_WORKERS = 6


def _extract_one(path: str, title: str) -> dict | None:
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
    return extract_with_llm(title, body)


def _extract_pass(label: str, stats: dict) -> None:
    """跑一轮提取,结果累加进 stats。label 用于进度展示("存量"/"增量")。

    LLM 调用在线程池里并发,DB 写全部留在本线程串行,避免多写者竞争。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from kb import store
    from kb.extract import (_tag_slug, pending_docs, refresh_content_hash,
                            save_extraction, writeback_tags)

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
            futs = {pool.submit(_extract_one, row["path"], row["title"]): row
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
                        save_extraction(conn, row["path"], data)
                        conn.commit()
                        # tag 回写 obs frontmatter(决策:自动回写);回写改了文件,
                        # 同步刷新 hash 免得下次 index 白白重建这批文档
                        tags = [f"topic/{_tag_slug(t)}" for t in data["topics"]] + \
                               [f"entity/{_tag_slug(e['name'])}" for e in data["entities"]]
                        if writeback_tags(row["path"], tags):
                            refresh_content_hash(conn, row["path"])
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
            if l.startswith(("linkify:", "digest:", "synthesis:"))]
    detail = ";".join(t.strip() for t in tail) or proc.stdout.strip()[-160:]
    # 面板卡片空间有限,压成一行短语;完整输出看日志
    m1 = re.search(r"新双链化 (\d+)", proc.stdout)
    m2 = re.search(r"digest: 生成 (\d+) 天", proc.stdout)
    m3 = re.search(r"synthesis: 更新 (\d+) 个实体", proc.stdout)
    if m1 or m2 or m3:
        detail = (f"双链化 {m1.group(1) if m1 else 0} 篇 · "
                  f"日摘要 {m2.group(1) if m2 else 0} 天 · "
                  f"综合 {m3.group(1) if m3 else 0} 实体")
    if proc.returncode != 0:
        _stage("wiki", "error", f"失败: {(proc.stderr or detail)[-200:]}")
    else:
        _stage("wiki", "ok", detail[:200] or "完成")


# ── 条目化阶段:工具/项目拆条目(dr-gzh-projects / dr-video-projects)─────────────

KB_DIR = Path(__file__).resolve().parent.parent
VIDEOS_DIR = Path.home() / "code" / "daily-report" / "videos"


def _run_export_script(script: str, *argv: str, timeout_min: int):
    return subprocess.run(
        ["uv", "run", "python", f"scripts/{script}", *argv],
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
        rows = store.chunks_without_vectors(conn)
        if rows:
            from kb.embedder import embed_texts
            B = 128
            for i in range(0, len(rows), B):
                batch = rows[i:i + B]
                store.insert_vectors(conn, [r["id"] for r in batch],
                                     embed_texts([r["text"] for r in batch]))
                conn.commit()
        return new, len(rows)
    finally:
        conn.close()


def _run_projects() -> None:
    """条目化:gate(gpt-5.4-mini)判「工具/项目介绍型」→ transcribe(gpt-5.6-luna)按工具拆条目。

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

