# -*- coding: utf-8 -*-
"""数据管道:抓取 → 导出 → 索引 → 提取,一键串行跑。

各阶段实现方式:
- 抓取: HTTP 调 WeChatOA 后端(:8000),不在线则跳过 —— 凭证/反爬都归它管
- 导出: 子进程跑 WeChatOA 的 export_obsidian.py
- 索引: 进程内(复用已加载的 embedding 模型)
- 提取: 进程内逐篇调 claude CLI,tag 自动回写 obs frontmatter

状态放模块级 dict(单进程单管道,与 WeChatOA 的 _crawl_state 同套路),
前端轮询 /api/pipeline/status 展示进度。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from kb.config import DATA_DIR

# WeChatOA 项目位置与后端地址
WECHATOA_DIR = Path.home() / "code" / "gongzhonghao" / "WeChatOA_Aggregation"
WECHATOA_API = "http://127.0.0.1:8000"

_STATE_FILE = DATA_DIR / "pipeline_state.json"

STAGES = ("crawl", "export", "index", "extract")

_state: dict = {
    "running": False,
    "current_stage": "",
    "started_at": "",
    "finished_at": "",
    # 每阶段: {status: pending/running/ok/skipped/error, detail: str}
    "stages": {s: {"status": "", "detail": ""} for s in STAGES},
    "log": [],          # 最近若干条进度行
}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _log(msg: str) -> None:
    _state["log"].append(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")
    del _state["log"][:-80]


def _stage(name: str, status: str, detail: str = "") -> None:
    _state["stages"][name] = {"status": status, "detail": detail}
    if status == "running":
        _state["current_stage"] = name
    if detail:
        _log(f"{name}: {detail}")


def _save_last_run() -> None:
    """持久化最近一次结果,重启 kb-web 后仍能看到。"""
    try:
        _STATE_FILE.write_text(json.dumps({
            "finished_at": _state["finished_at"],
            "stages": _state["stages"],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass


def load_last_run() -> dict | None:
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def get_state() -> dict:
    return _state


# ── 各阶段 ──────────────────────────────────────────────────────────────────────

def _http_json(url: str, method: str = "GET", timeout: int = 10) -> dict | None:
    try:
        req = urllib.request.Request(url, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def wechatoa_status() -> dict:
    """WeChatOA 在线状态 + 凭证状态,给状态面板用。"""
    auth = _http_json(f"{WECHATOA_API}/api/auth/status")
    if auth is None:
        return {"online": False}
    return {"online": True, "auth": auth}


def _run_crawl() -> None:
    if _http_json(f"{WECHATOA_API}/api/crawl/status") is None:
        _stage("crawl", "skipped", "WeChatOA 后端不在线,跳过抓取")
        return
    _stage("crawl", "running", "触发全量爬取")
    started = _http_json(f"{WECHATOA_API}/api/crawl", method="POST")
    if started is None:
        # 409(已有任务在跑)也会走到这:继续轮询即可
        _log("crawl: 启动返回异常(可能已在跑),继续轮询状态")
    # WeChatOA 的爬取线程把 running 置 true 有个短暂窗口,先等一拍;
    # 且 running=false 时连续确认三次再收工,避免拿上一轮的旧状态提前结束
    time.sleep(2)
    deadline = time.time() + 30 * 60
    idle_polls = 0
    while time.time() < deadline:
        st = _http_json(f"{WECHATOA_API}/api/crawl/status")
        if st is None:
            _stage("crawl", "error", "轮询状态失败(后端掉线?)")
            return
        if not st.get("running"):
            idle_polls += 1
            if idle_polls < 3:
                time.sleep(1)
                continue
            errs = st.get("errors") or []
            if st.get("auth_error"):
                _stage("crawl", "error",
                       "凭证失效,请到 WeChatOA 配置页扫码续期后重跑")
            else:
                _stage("crawl", "ok",
                       f"新增 {st.get('new_articles', 0)} 篇"
                       + (f",{len(errs)} 个账号失败" if errs else ""))
            return
        idle_polls = 0
        _stage("crawl", "running",
               f"爬取中 {st.get('done', 0)}/{st.get('total', 0)} — {st.get('current', '')}")
        time.sleep(2)
    _stage("crawl", "error", "爬取超时(30分钟)")


def _run_export() -> None:
    _stage("export", "running", "导出到 obs")
    try:
        proc = subprocess.run(
            ["uv", "run", "python", "scripts/export_obsidian.py"],
            cwd=WECHATOA_DIR, capture_output=True, text=True, timeout=30 * 60,
        )
    except subprocess.TimeoutExpired:
        _stage("export", "error", "导出超时")
        return
    if proc.returncode != 0:
        _stage("export", "error", f"导出失败: {(proc.stderr or proc.stdout)[-300:]}")
        return
    # 取脚本的完成行作为 detail
    tail = [l for l in proc.stdout.splitlines() if "完成" in l]
    _stage("export", "ok", tail[-1].replace("[export] ", "") if tail else "完成")


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
    from kb import store
    from kb.extract import (_tag_slug, extract_with_claude, pending_docs,
                            refresh_content_hash, save_extraction,
                            writeback_tags)

    conn = store.connect()
    try:
        todo = pending_docs(conn)
        if not todo:
            _stage("extract", "ok", "没有待提取的文档")
            return
        ok = failed = 0
        for i, row in enumerate(todo, 1):
            _stage("extract", "running", f"提取 {i}/{len(todo)} — {row['title'][:24]}")
            body = conn.execute(
                "SELECT group_concat(text, char(10)) AS b FROM chunks WHERE path=?",
                (row["path"],),
            ).fetchone()["b"] or ""
            try:
                data = extract_with_claude(row["title"], body)
                save_extraction(conn, row["path"], data)
                conn.commit()
                # tag 回写 obs frontmatter(决策:自动回写);回写改了文件,
                # 同步刷新 hash 免得下次 index 白白重建这批文档
                tags = [f"topic/{_tag_slug(t)}" for t in data["topics"]] + \
                       [f"entity/{_tag_slug(e['name'])}" for e in data["entities"]]
                if writeback_tags(row["path"], tags):
                    refresh_content_hash(conn, row["path"])
                    conn.commit()
                ok += 1
            except Exception as e:
                failed += 1
                _log(f"extract: FAIL {row['title'][:24]}: {e}")
        _stage("extract", "ok" if failed == 0 else "error",
               f"成功 {ok},失败 {failed}")
    finally:
        conn.close()


# ── 编排 ────────────────────────────────────────────────────────────────────────

_RUNNERS = {"crawl": _run_crawl, "export": _run_export,
            "index": _run_index, "extract": _run_extract}


def _run_pipeline(stages: list[str]) -> None:
    try:
        for s in STAGES:
            if s in stages:
                _RUNNERS[s]()
            else:
                _state["stages"][s] = {"status": "skipped", "detail": "未选择"}
    finally:
        _state["running"] = False
        _state["current_stage"] = ""
        _state["finished_at"] = _now()
        _log("管道结束")
        _save_last_run()


def start_pipeline(stages: list[str] | None = None) -> bool:
    """启动管道线程。已在跑返回 False。"""
    wanted = [s for s in (stages or list(STAGES)) if s in STAGES]
    with _lock:
        if _state["running"]:
            return False
        _state.update({
            "running": True, "current_stage": "", "log": [],
            "started_at": _now(), "finished_at": "",
            "stages": {s: {"status": "pending" if s in wanted else "skipped",
                           "detail": ""} for s in STAGES},
        })
    threading.Thread(target=_run_pipeline, args=(wanted,), daemon=True).start()
    return True


# ── 待处理数(空闲时给面板显示)────────────────────────────────────────────────

def pending_counts() -> dict:
    """各阶段待处理数。文件扫描有成本,管道运行中不要调。"""
    out: dict = {}
    # 导出待处理:WeChatOA 库里的文章数 - obs 树里已有的 URL 数(近似)
    try:
        import sqlite3
        from kb.scanner import scan_all as _scan
        wdb = sqlite3.connect(WECHATOA_DIR / "data" / "wechatoa.db")
        total = wdb.execute(
            "SELECT COUNT(*) FROM articles WHERE is_user_deleted=0 AND is_wx_deleted=0"
        ).fetchone()[0]
        wdb.close()
        exported = sum(1 for d in _scan() if d.source in ("dr-gzh", "wechat", "gzh"))
        out["export"] = max(0, total - exported)
    except Exception:
        out["export"] = None
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
    return out
