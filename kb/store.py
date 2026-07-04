# -*- coding: utf-8 -*-
"""SQLite 索引存储:documents / chunks / FTS5 / 向量表。

中文 FTS:FTS5 的 unicode61 不分中文词,所以入库前用 jieba 分词、
空格连接存到 *_tok 列;查询同样先分词。这与 WeChatOA_Aggregation
的做法一致。

向量表用 sqlite-vec 的 vec0 虚拟表;没装 sentence-transformers 或
没跑过 embed 时向量表为空,检索层自动退化为纯 FTS。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import jieba
import sqlite_vec

from kb.config import DATA_DIR, DB_PATH, EMBEDDING_DIM

_SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  path          TEXT PRIMARY KEY,
  source        TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  account       TEXT NOT NULL DEFAULT '',
  published     TEXT NOT NULL DEFAULT '',
  url           TEXT NOT NULL DEFAULT '',
  content_hash  TEXT NOT NULL DEFAULT '',
  indexed_at    TEXT,
  extracted_at  TEXT,          -- NULL = 还没做 entity/topic 提取
  summary       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS doc_tags (
  path  TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  tag   TEXT NOT NULL,
  PRIMARY KEY (path, tag)
);
CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON doc_tags(tag);

-- LLM 提取出的实体(比 tag 结构化:带类型)
CREATE TABLE IF NOT EXISTS entities (
  path    TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  entity  TEXT NOT NULL,      -- 规范名,如 book-to-skill
  kind    TEXT NOT NULL DEFAULT '',  -- project/product/org/model/other
  PRIMARY KEY (path, entity)
);
CREATE INDEX IF NOT EXISTS idx_entities_entity ON entities(entity);

CREATE TABLE IF NOT EXISTS chunks (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  path     TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  seq      INTEGER NOT NULL,
  heading  TEXT NOT NULL DEFAULT '',
  text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  title_tok,
  text_tok,
  tokenize='unicode61'
);
"""

_VEC_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_vec USING vec0(
  chunk_id INTEGER PRIMARY KEY,
  embedding float[{EMBEDDING_DIM}]
);
"""


def tokenize(text: str) -> str:
    """jieba 分词,空格连接 —— 入库和查询用同一个函数。"""
    return " ".join(t for t in jieba.cut_for_search(text) if t.strip())


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(_SCHEMA)
    conn.executescript(_VEC_SCHEMA)
    return conn


# ── 写入 ──────────────────────────────────────────────────────────────────────

def doc_needs_reindex(conn: sqlite3.Connection, path: str, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT content_hash FROM documents WHERE path=?", (path,)
    ).fetchone()
    return row is None or row["content_hash"] != content_hash


def upsert_document(conn: sqlite3.Connection, doc, chunks, now: str) -> None:
    """写入/更新一篇文档及其 chunk 与 FTS。旧 chunk(含向量)全部删除重建。"""
    # 老 chunk 的向量一并清掉(chunk_id 会变)
    old_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM chunks WHERE path=?", (doc.path,)
    ).fetchall()]
    if old_ids:
        ph = ",".join("?" * len(old_ids))
        conn.execute(f"DELETE FROM chunk_vec WHERE chunk_id IN ({ph})", old_ids)
        conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({ph})", old_ids)
    conn.execute("DELETE FROM chunks WHERE path=?", (doc.path,))

    conn.execute(
        """
        INSERT INTO documents (path, source, title, account, published, url,
                               content_hash, indexed_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET
          source=excluded.source, title=excluded.title, account=excluded.account,
          published=excluded.published, url=excluded.url,
          content_hash=excluded.content_hash, indexed_at=excluded.indexed_at
        """,
        (doc.path, doc.source, doc.title, doc.account, doc.published,
         doc.url, doc.content_hash, now),
    )

    conn.execute("DELETE FROM doc_tags WHERE path=?", (doc.path,))
    conn.executemany(
        "INSERT OR IGNORE INTO doc_tags (path, tag) VALUES (?, ?)",
        [(doc.path, t) for t in doc.tags],
    )

    title_tok = tokenize(doc.title)
    for ch in chunks:
        cur = conn.execute(
            "INSERT INTO chunks (path, seq, heading, text) VALUES (?, ?, ?, ?)",
            (doc.path, ch.seq, ch.heading, ch.text),
        )
        conn.execute(
            "INSERT INTO chunks_fts (chunk_id, title_tok, text_tok) VALUES (?, ?, ?)",
            (cur.lastrowid, title_tok, tokenize(ch.heading + " " + ch.text)),
        )


def remove_missing_documents(conn: sqlite3.Connection, alive_paths: set[str]) -> int:
    """删掉 vault 里已不存在的文档(含级联的 chunk/tag/entity 和向量)。"""
    rows = conn.execute("SELECT path FROM documents").fetchall()
    dead = [r["path"] for r in rows if r["path"] not in alive_paths]
    for p in dead:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM chunks WHERE path=?", (p,)
        ).fetchall()]
        if ids:
            ph = ",".join("?" * len(ids))
            conn.execute(f"DELETE FROM chunk_vec WHERE chunk_id IN ({ph})", ids)
            conn.execute(f"DELETE FROM chunks_fts WHERE chunk_id IN ({ph})", ids)
        conn.execute("DELETE FROM documents WHERE path=?", (p,))
    return len(dead)


def chunks_without_vectors(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT c.id, c.text, d.title
        FROM chunks c JOIN documents d ON d.path = c.path
        WHERE c.id NOT IN (SELECT chunk_id FROM chunk_vec)
        ORDER BY c.id
        """
    ).fetchall()


def insert_vectors(conn: sqlite3.Connection, ids: list[int], vecs) -> None:
    import struct
    for cid, v in zip(ids, vecs):
        conn.execute(
            "INSERT OR REPLACE INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
            (cid, struct.pack(f"{len(v)}f", *v)),
        )
