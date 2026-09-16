# -*- coding: utf-8 -*-
"""混合检索:FTS(关键词) + 向量(语义) → RRF 融合 → doc 级聚合。

RRF(Reciprocal Rank Fusion):score = Σ 1/(k + rank),k=60。
不依赖两路打分的量纲,免调参,是混合检索的稳妥默认。

过滤条件(source/account/tag/entity)编译成 SQL 子查询,
作用在两路召回上,保证过滤后仍有完整的 top-N。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from kb.config import expand_sources
from kb.norm import entity_norm
from kb.store import promo_exclude_sql, tokenize

RRF_K = 60
CANDIDATES_PER_LEG = 50   # 每路召回数,融合后再截断
# 向量 KNN 带过滤时的候选数:vec0 只能先全局 KNN 再 join 过滤,小来源(如视频系
# 只占全库 chunk ~2%)在全局 top-150 里几乎不出现,过滤后向量腿必然为空。
# vec0 的 KNN 本来就是全表暴力扫,k 放大只多付堆维护成本,几乎不影响耗时。
KNN_K_FILTERED = 4000


@dataclass
class Hit:
    path: str
    title: str
    source: str
    account: str
    published: str
    url: str
    score: float
    best_chunk: str        # 得分最高 chunk 的文本(用于展示/引用)
    heading: str
    matched_by: list[str] = field(default_factory=list)  # ["fts", "vec"]


@dataclass
class Filters:
    # 来源:单个来源名 / 组名或别名("视频"→dig+video-projects) / 列表,None=全部
    source: str | list[str] | None = None
    account: str | None = None
    tags: list[str] = field(default_factory=list)      # AND 关系
    entities: list[str] = field(default_factory=list)  # AND 关系
    since: str | None = None   # published >= "YYYY-MM-DD"
    until: str | None = None   # published <= "YYYY-MM-DD"(含当天)
    # 默认把 hard 软广挡在检索之外(判定见 kb/extract.py)。想专门查广告本身
    # (比如核对误杀)才置 False。soft 不挡:那一档是厂商通稿/带返利的推荐,
    # 里面的技术信息通常仍然有用。
    exclude_promo: bool = True


def _filter_sql(f: Filters) -> tuple[str, list]:
    """把过滤条件编译成作用于 documents 的 WHERE 片段。"""
    clauses, params = [], []
    srcs = expand_sources(f.source)
    if srcs:
        clauses.append(f"d.source IN ({','.join('?' * len(srcs))})")
        params.extend(srcs)
    if f.account:
        clauses.append("d.account = ?")
        params.append(f.account)
    if f.since:
        clauses.append("d.published >= ?")
        params.append(f.since)
    if f.until:
        # published 是 "YYYY-MM-DD HH:MM" 字符串,补一天的尾巴使其含当天
        clauses.append("d.published <= ?")
        params.append(f.until + " 23:59")
    for t in f.tags:
        clauses.append(
            "d.path IN (SELECT path FROM doc_tags WHERE tag = ? OR tag LIKE ?)"
        )
        params.extend([t, f"{t}/%"])
    for e in f.entities:
        # 按归一键匹配:输入 "Vibe Kanban" 也能命中库里的 "vibe-kanban"
        clauses.append(
            "d.path IN (SELECT path FROM entities WHERE norm = ?)"
        )
        params.append(entity_norm(e))
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    return where, params


def _promo_sql(f: Filters) -> str:
    """软广排除子句。单独拼、不进 _filter_sql:后者的返回值还被 _vec_leg 用来
    判断「有没有把召回缩到小子集」(有就放大 KNN 候选)。软广只占全库很小一块,
    混进去会让每一次检索都误判成带过滤、白白跑 4000 的 KNN。

    口径(含「连它拆出来的条目一起挡」)在 store.promo_exclude_sql,与枚举共用。"""
    return f" AND {promo_exclude_sql('d')}" if f.exclude_promo else ""


def _fts_leg(conn: sqlite3.Connection, query: str, f: Filters) -> list[tuple[int, str]]:
    """FTS 召回:返回 [(chunk_id, path)] 按相关性排序。"""
    tok = tokenize(query)
    if not tok.strip():
        return []
    # OR 连接:意图式长 query 不要求全部词命中
    match_expr = " OR ".join(f'"{t}"' for t in tok.split())
    where, params = _filter_sql(f)
    try:
        rows = conn.execute(
            f"""
            SELECT c.id AS chunk_id, c.path
            FROM chunks_fts ft
            JOIN chunks c ON c.id = ft.chunk_id
            JOIN documents d ON d.path = c.path
            WHERE chunks_fts MATCH ? {where}{_promo_sql(f)}
            ORDER BY bm25(chunks_fts, 0, 3.0, 1.0)
            LIMIT ?
            """,
            [match_expr, *params, CANDIDATES_PER_LEG],
        ).fetchall()
    except sqlite3.OperationalError:
        return []  # 特殊字符导致 MATCH 语法错误时放弃这一路
    return [(r["chunk_id"], r["path"]) for r in rows]


def _vec_leg(conn: sqlite3.Connection, query: str, f: Filters) -> list[tuple[int, str]]:
    """向量召回。向量表为空或依赖缺失时返回 []。"""
    n = conn.execute("SELECT COUNT(*) AS c FROM chunk_vec").fetchone()["c"]
    if n == 0:
        return []
    try:
        from kb.embedder import embed_query
        qv = embed_query(query)
    except RuntimeError:
        return []
    import struct
    where, params = _filter_sql(f)
    # vec0 的 KNN 查询先取 top-N,再 join 过滤。有过滤条件时把 KNN 候选放大
    # (KNN_K_FILTERED),否则被过滤到小子集(如单一小来源)后向量腿会空
    knn_k = KNN_K_FILTERED if where else CANDIDATES_PER_LEG * 3
    rows = conn.execute(
        f"""
        SELECT v.chunk_id, c.path
        FROM (
          SELECT chunk_id, distance FROM chunk_vec
          WHERE embedding MATCH ? AND k = ?
        ) v
        JOIN chunks c ON c.id = v.chunk_id
        JOIN documents d ON d.path = c.path
        WHERE 1=1 {where}{_promo_sql(f)}
        ORDER BY v.distance
        LIMIT ?
        """,
        [struct.pack(f"{len(qv)}f", *qv), knn_k,
         *params, CANDIDATES_PER_LEG],
    ).fetchall()
    return [(r["chunk_id"], r["path"]) for r in rows]


def search(
    conn: sqlite3.Connection,
    query: str,
    filters: Filters | None = None,
    top_k: int = 10,
    use_vector: bool = True,
) -> list[Hit]:
    f = filters or Filters()
    legs: dict[str, list[tuple[int, str]]] = {"fts": _fts_leg(conn, query, f)}
    if use_vector:
        legs["vec"] = _vec_leg(conn, query, f)

    # RRF:chunk 级融合
    chunk_score: dict[int, float] = {}
    chunk_legs: dict[int, set[str]] = {}
    chunk_path: dict[int, str] = {}
    for leg_name, results in legs.items():
        for rank, (cid, path) in enumerate(results):
            chunk_score[cid] = chunk_score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
            chunk_legs.setdefault(cid, set()).add(leg_name)
            chunk_path[cid] = path

    if not chunk_score:
        return []

    # doc 级聚合:文档分 = 其最高 chunk 分(避免长文靠 chunk 数量刷分)
    doc_best: dict[str, tuple[float, int]] = {}
    for cid, s in chunk_score.items():
        p = chunk_path[cid]
        if p not in doc_best or s > doc_best[p][0]:
            doc_best[p] = (s, cid)

    ranked = sorted(doc_best.items(), key=lambda kv: kv[1][0], reverse=True)[:top_k]

    hits: list[Hit] = []
    for path, (score, cid) in ranked:
        d = conn.execute("SELECT * FROM documents WHERE path=?", (path,)).fetchone()
        c = conn.execute("SELECT heading, text FROM chunks WHERE id=?", (cid,)).fetchone()
        if d is None:
            continue
        hits.append(Hit(
            path=path, title=d["title"], source=d["source"], account=d["account"],
            published=d["published"], url=d["url"], score=round(score, 5),
            best_chunk=(c["text"][:300] if c else ""),
            heading=(c["heading"] if c else ""),
            matched_by=sorted(chunk_legs.get(cid, set())),
        ))
    return hits
