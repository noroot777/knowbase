# -*- coding: utf-8 -*-
"""检索与浏览 API:混合检索、过滤器选项、统计、单篇原文、工具图鉴。

都是对索引的只读查询;写入走 kb index / kb extract 或管道。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from kb import projects as projlib
from kb import store
from kb.config import VAULT_ROOT
from kb.search import Filters, search
from kb.web.common import VAULT_NAME

router = APIRouter()


@router.get("/api/search")
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


@router.get("/api/facets")
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
        # 实体按 norm 聚合(写法变体归一),展示名取出现最多的写法
        totals = {r["norm"]: r["n"] for r in conn.execute(
            "SELECT norm, COUNT(DISTINCT path) AS n FROM entities GROUP BY norm")}
        best_name: dict[str, tuple[int, str]] = {}
        for r in conn.execute(
            "SELECT norm, entity, COUNT(*) AS n FROM entities GROUP BY norm, entity"
        ):
            if r["norm"] not in best_name or r["n"] > best_name[r["norm"]][0]:
                best_name[r["norm"]] = (r["n"], r["entity"])
        entities = sorted(
            ({"value": best_name[n][1], "n": c} for n, c in totals.items()),
            key=lambda x: x["n"], reverse=True,
        )[:50]
        return {"sources": sources, "accounts": accounts,
                "topics": topics, "entities": entities}
    finally:
        conn.close()


@router.get("/api/stats")
def api_stats():
    conn = store.connect()
    try:
        one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "documents": one("SELECT COUNT(*) FROM documents"),
            "chunks": one("SELECT COUNT(*) FROM chunks"),
            "vectors": one("SELECT COUNT(*) FROM chunk_vec"),
            "entities": one("SELECT COUNT(DISTINCT norm) FROM entities"),
            "extracted": one("SELECT COUNT(*) FROM documents WHERE extracted_at IS NOT NULL"),
        }
    finally:
        conn.close()


# ── 工具图鉴(dr-*-projects 条目按实体聚合) ────────────────────────────────────


@router.get("/api/projects")
def api_projects():
    """图鉴列表:工具聚合卡片 + 类别/主题计数(文件缓存,首次或有新条目时约 1s)。"""
    return projlib.list_tools()


@router.get("/api/projects/{norm:path}")
def api_project_detail(norm: str):
    """单个工具的全部记录(含正文 md),给详情抽屉。norm 可能带斜杠(如 pirl-/-pipo),必须 :path。"""
    d = projlib.get_tool(norm)
    if d is None:
        raise HTTPException(404, "没有这个工具")
    d["vault"] = VAULT_NAME
    return d


@router.get("/api/doc")
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
