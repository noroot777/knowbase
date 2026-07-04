# -*- coding: utf-8 -*-
"""知识库 Web 服务:检索 API + 静态前端。

    uv run kb-web            # http://127.0.0.1:8787

只读服务(除 /api/ask 会调 claude CLI),索引更新仍走 kb index / kb extract。
每个请求独立开 SQLite 连接(WAL 模式),与后台索引/提取进程并存无碍。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel

from kb import store
from kb.askflow import run_ask
from kb.config import VAULT_ROOT
from kb.search import Filters, search

app = FastAPI(title="knowledge-base")

_STATIC = Path(__file__).resolve().parent / "static"

# obsidian://open 深链需要 vault 名(obs 目录名)
VAULT_NAME = VAULT_ROOT.name


@app.get("/")
def index_page():
    return FileResponse(_STATIC / "index.html")


@app.get("/api/search")
def api_search(
    q: str = Query(..., min_length=1),
    source: str | None = None,
    account: str | None = None,
    tag: list[str] = Query(default=[]),
    entity: list[str] = Query(default=[]),
    top: int = 20,
    vector: bool = True,
):
    conn = store.connect()
    try:
        hits = search(
            conn, q,
            Filters(source=source, account=account, tags=tag, entities=entity),
            top_k=min(top, 50), use_vector=vector,
        )
        out = []
        for h in hits:
            d = conn.execute(
                "SELECT summary FROM documents WHERE path=?", (h.path,)
            ).fetchone()
            ents = [r["entity"] for r in conn.execute(
                "SELECT entity FROM entities WHERE path=? LIMIT 8", (h.path,)
            )]
            out.append({
                **h.__dict__,
                "summary": d["summary"] if d else "",
                "entities": ents,
                "vault": VAULT_NAME,
            })
        return {"hits": out}
    finally:
        conn.close()


@app.get("/api/facets")
def api_facets():
    """给前端过滤器下拉用:来源/账号/topic/热门实体。"""
    conn = store.connect()
    try:
        sources = [dict(r) for r in conn.execute(
            "SELECT source AS value, COUNT(*) AS n FROM documents GROUP BY source ORDER BY n DESC"
        )]
        accounts = [dict(r) for r in conn.execute(
            "SELECT account AS value, COUNT(*) AS n FROM documents "
            "WHERE account != '' GROUP BY account ORDER BY n DESC LIMIT 40"
        )]
        topics = [dict(r) for r in conn.execute(
            "SELECT tag AS value, COUNT(*) AS n FROM doc_tags "
            "WHERE tag LIKE 'topic/%' GROUP BY tag ORDER BY n DESC LIMIT 30"
        )]
        entities = [dict(r) for r in conn.execute(
            "SELECT entity AS value, COUNT(*) AS n FROM entities "
            "GROUP BY entity ORDER BY n DESC LIMIT 50"
        )]
        return {"sources": sources, "accounts": accounts,
                "topics": topics, "entities": entities}
    finally:
        conn.close()


@app.get("/api/stats")
def api_stats():
    conn = store.connect()
    try:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "vectors": one("SELECT COUNT(*) FROM chunk_vec"),
            "entities": one("SELECT COUNT(DISTINCT entity) FROM entities"),
            "extracted": one("SELECT COUNT(*) FROM documents WHERE extracted_at IS NOT NULL"),
        }
    finally:
        conn.close()


@app.get("/api/doc")
def api_doc(path: str):
    """读一篇文档全文(md 原文),给前端预览面板。"""
    conn = store.connect()
    try:
        row = conn.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404, "文档不在索引里")
    full = VAULT_ROOT / path
    try:
        text = full.read_text(encoding="utf-8")
    except OSError:
        raise HTTPException(404, "文件读取失败(可能已移动)")
    return {"path": path, "title": row["title"], "content": text, "vault": VAULT_NAME}


class AskRequest(BaseModel):
    query: str
    source: str | None = None
    tags: list[str] = []
    entities: list[str] = []
    model: str = "sonnet"


# ── MCP 注册管理 ────────────────────────────────────────────────────────────────
# MCP server 是 stdio 服务,由 Claude Code 按需拉起,没有"启动/停止"概念;
# 这里管理的是它在 claude CLI 里的注册(claude mcp add/remove)。

MCP_NAME = "kb"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_MCP_ADD_CMD = [
    "claude", "mcp", "add", MCP_NAME, "--scope", "user", "--",
    "uv", "run", "--directory", str(_PROJECT_ROOT), "kb-mcp",
]


@app.get("/api/mcp/status")
def mcp_status():
    try:
        proc = subprocess.run(
            ["claude", "mcp", "get", MCP_NAME],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return {"registered": False, "error": f"claude CLI 不可用: {e}",
                "command": " ".join(_MCP_ADD_CMD)}
    return {
        "registered": proc.returncode == 0,
        "detail": proc.stdout.strip() if proc.returncode == 0 else "",
        "command": " ".join(_MCP_ADD_CMD),
        "tools": ["kb_search", "kb_read", "kb_entities"],
    }


@app.post("/api/mcp/register")
def mcp_register():
    proc = subprocess.run(_MCP_ADD_CMD, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise HTTPException(500, f"注册失败: {(proc.stderr or proc.stdout)[:300]}")
    return {"ok": True, "message": proc.stdout.strip()}


@app.post("/api/mcp/unregister")
def mcp_unregister():
    proc = subprocess.run(
        ["claude", "mcp", "remove", MCP_NAME, "--scope", "user"],
        capture_output=True, text=True, timeout=30,
    )
    if proc.returncode != 0:
        raise HTTPException(500, f"取消注册失败: {(proc.stderr or proc.stdout)[:300]}")
    return {"ok": True, "message": proc.stdout.strip()}


# ── 数据管道 ────────────────────────────────────────────────────────────────────

from kb import pipeline as pl  # noqa: E402


@app.get("/api/pipeline/status")
def pipeline_status(pending: bool = False):
    state = dict(pl.get_state())
    state["wechatoa"] = pl.wechatoa_status()
    state["last_run"] = pl.load_last_run()
    # 待处理数要扫文件,只有显式要求且空闲时才算
    if pending and not state["running"]:
        state["pending"] = pl.pending_counts()
    return state


class PipelineRunRequest(BaseModel):
    stages: list[str] | None = None  # None = 全部


@app.post("/api/pipeline/run")
def pipeline_run(body: PipelineRunRequest | None = None):
    stages = body.stages if body else None
    if not pl.start_pipeline(stages):
        raise HTTPException(409, "管道已在运行中")
    return {"ok": True}


@app.post("/api/ask")
def api_ask(body: AskRequest):
    """意图规划(枚举 vs 检索)→ 执行 → Claude 综合。同步阻塞 20-90s。"""
    conn = store.connect()
    try:
        result = run_ask(conn, body.query, model=body.model)
    except RuntimeError as e:
        raise HTTPException(502, str(e))
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "claude 调用超时")
    finally:
        conn.close()
    for r in result["refs"]:
        r["vault"] = VAULT_NAME
    return result


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8787)


if __name__ == "__main__":
    main()
