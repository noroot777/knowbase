# -*- coding: utf-8 -*-
"""枚举类查询:全量 SQL 聚合,不走 top-K,保证不漏。

适用于"最近一周都提过哪些开源项目"这类问题——答案要求穷举,
top-K 检索天然会漏,必须按结构化条件扫全表。
"""

from __future__ import annotations

import sqlite3


def enumerate_entities(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """按条件枚举实体及其出处文章。返回按提及文档数降序。

    kind: project/product/org/model/other;None = 全部。
    """
    clauses, params = ["1=1"], []
    if since:
        clauses.append("d.published >= ?")
        params.append(since)
    if until:
        clauses.append("d.published <= ?")
        params.append(until + " 23:59")
    if source:
        clauses.append("d.source = ?")
        params.append(source)
    if kind:
        clauses.append("e.kind = ?")
        params.append(kind)
    where = " AND ".join(clauses)

    rows = conn.execute(
        f"""
        SELECT e.entity, e.kind, d.title, d.path, d.published, d.account
        FROM entities e JOIN documents d ON d.path = e.path
        WHERE {where}
        ORDER BY e.entity, d.published DESC
        """,
        params,
    ).fetchall()

    by_entity: dict[str, dict] = {}
    for r in rows:
        item = by_entity.setdefault(r["entity"], {
            "entity": r["entity"], "kind": r["kind"], "docs": [],
        })
        item["docs"].append({
            "title": r["title"], "path": r["path"],
            "published": r["published"], "account": r["account"],
        })
    ranked = sorted(by_entity.values(), key=lambda x: len(x["docs"]), reverse=True)
    return ranked[:limit]


def enumerate_documents(
    conn: sqlite3.Connection,
    since: str | None = None,
    until: str | None = None,
    source: str | None = None,
    tag: str | None = None,
    limit: int = 300,
) -> list[dict]:
    """按条件枚举文档(标题+摘要),给"这段时间都有什么文章"类问题。"""
    clauses, params = ["1=1"], []
    if since:
        clauses.append("d.published >= ?")
        params.append(since)
    if until:
        clauses.append("d.published <= ?")
        params.append(until + " 23:59")
    if source:
        clauses.append("d.source = ?")
        params.append(source)
    if tag:
        clauses.append("d.path IN (SELECT path FROM doc_tags WHERE tag = ? OR tag LIKE ?)")
        params.extend([tag, f"{tag}/%"])
    where = " AND ".join(clauses)
    rows = conn.execute(
        f"""
        SELECT d.path, d.title, d.account, d.published, d.summary
        FROM documents d WHERE {where}
        ORDER BY d.published DESC LIMIT ?
        """,
        [*params, limit],
    ).fetchall()
    return [dict(r) for r in rows]
