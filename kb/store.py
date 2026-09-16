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
  summary       TEXT NOT NULL DEFAULT '',
  -- 软广判定(提取阶段顺带产出,见 kb/extract.py)
  promo_level   TEXT NOT NULL DEFAULT '',  -- '' = 未判定;none/soft/hard
  promo_reason  TEXT NOT NULL DEFAULT '',
  promo_at      TEXT
);
-- idx_documents_promo 在 _migrate_promo 里建(老库先补列才能建索引)

CREATE TABLE IF NOT EXISTS doc_tags (
  path  TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  tag   TEXT NOT NULL,
  PRIMARY KEY (path, tag)
);
CREATE INDEX IF NOT EXISTS idx_doc_tags_tag ON doc_tags(tag);

-- LLM 提取出的实体(比 tag 结构化:带类型)
CREATE TABLE IF NOT EXISTS entities (
  path    TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  entity  TEXT NOT NULL,      -- 原始名,如 Claude Code
  kind    TEXT NOT NULL DEFAULT '',  -- project/product/org/model/research/other
  norm    TEXT NOT NULL DEFAULT '',  -- entity_norm 归一键,变体聚合/过滤用
  PRIMARY KEY (path, entity)
);
CREATE INDEX IF NOT EXISTS idx_entities_entity ON entities(entity);
-- idx_entities_norm 在 _migrate_entities 里建(老库先补列才能建索引)

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

-- 问答会话(web UI 的 chatbox,与索引数据同库但互不相干)
CREATE TABLE IF NOT EXISTS conversations (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  title       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  conversation_id  INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  role             TEXT NOT NULL,               -- user / assistant
  content          TEXT NOT NULL,
  refs_json        TEXT NOT NULL DEFAULT '[]',  -- assistant 消息的引用来源
  plan_json        TEXT NOT NULL DEFAULT '',    -- assistant 消息的查询计划
  model            TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
"""

# 收藏(web UI;整篇或片段,语义是"待试备忘"而非典藏,所以带状态流转。
# 文档从索引移除时级联清掉,重索引 upsert 不影响)。
# 单独抽出来:favorites v1→v2 迁移时要独立重建这张表。
_FAVORITES_DDL = """
CREATE TABLE IF NOT EXISTS favorites (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  path        TEXT NOT NULL REFERENCES documents(path) ON DELETE CASCADE,
  quote       TEXT NOT NULL DEFAULT '',      -- 片段原文;'' = 整篇收藏
  prefix      TEXT NOT NULL DEFAULT '',      -- 片段定位锚:前文
  suffix      TEXT NOT NULL DEFAULT '',      -- 片段定位锚:后文
  note        TEXT NOT NULL DEFAULT '',      -- 一句话:当时为什么收
  status      TEXT NOT NULL DEFAULT 'inbox', -- inbox 待试 / done 已处理 / archived 沉底
  action_json TEXT NOT NULL DEFAULT '',      -- LLM 行动卡 {name,what,cmd,eta,kind},收藏时异步生成
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL                  -- 状态/备注最后一次变动,陈旧判断基于它
);
CREATE INDEX IF NOT EXISTS idx_favorites_path ON favorites(path);
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


def _migrate_favorites(conn: sqlite3.Connection) -> None:
    """favorites 结构升级,幂等。
    v1(path 主键、仅整篇星标)→ v2(id 主键 + 片段/状态)→ v3(+action_json)。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(favorites)").fetchall()}
    if not cols:
        return
    if "id" not in cols:  # v1 → 重建
        conn.execute("ALTER TABLE favorites RENAME TO favorites_v1")
        conn.executescript(_FAVORITES_DDL)
        conn.execute(
            "INSERT INTO favorites (path, created_at, updated_at)"
            " SELECT path, created_at, created_at FROM favorites_v1"
        )
        conn.execute("DROP TABLE favorites_v1")
        conn.commit()
    elif "action_json" not in cols:  # v2 → 加列
        conn.execute("ALTER TABLE favorites ADD COLUMN action_json TEXT NOT NULL DEFAULT ''")
        conn.commit()


def _migrate_entities(conn: sqlite3.Connection) -> None:
    """entities 加 norm 归一列并回填,顺手把历史野生 kind 归一。幂等。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(entities)").fetchall()}
    if "norm" not in cols:
        from kb.norm import entity_norm, kind_norm
        conn.execute("ALTER TABLE entities ADD COLUMN norm TEXT NOT NULL DEFAULT ''")
        rows = conn.execute("SELECT path, entity, kind FROM entities").fetchall()
        for r in rows:
            conn.execute(
                "UPDATE entities SET norm=?, kind=? WHERE path=? AND entity=?",
                (entity_norm(r["entity"]), kind_norm(r["kind"]), r["path"], r["entity"]),
            )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_entities_norm ON entities(norm)")
    conn.commit()


def _migrate_promo(conn: sqlite3.Connection) -> None:
    """documents 加软广判定三列并建索引。幂等。

    老库 documents 已存在,_SCHEMA 的 CREATE TABLE IF NOT EXISTS 不会补列,
    只能 ALTER;索引要等列补完才能建,所以不写进 _SCHEMA(同 _migrate_entities)。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)").fetchall()}
    for name, ddl in (("promo_level", "TEXT NOT NULL DEFAULT ''"),
                      ("promo_reason", "TEXT NOT NULL DEFAULT ''"),
                      ("promo_at", "TEXT")):
        if name not in cols:
            conn.execute(f"ALTER TABLE documents ADD COLUMN {name} {ddl}")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_promo ON documents(promo_level)")
    # 软广排除要按 url 从条目回溯原文(见 promo_exclude_sql),没索引就是每行一次全表扫
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url)")
    conn.commit()


def promo_exclude_sql(alias: str = "d") -> str:
    """软广排除子句,检索(kb.search)和枚举(kb.aggregate)共用同一份。

    挡两样东西:被判 hard 的原文,**以及它拆出来的条目**。条目化产物是另一条
    document(dr-gzh-projects),自己的 promo_level 永远是空的——只挡原文的话,
    存量补判之后会出现「原文被挡住了,从这篇广告拆出来的工具条目还留在搜索
    结果和实体榜里」,等于没抛弃。两者的关联键是 documents.url:write_entry
    把原文链接原样写进条目的 frontmatter,所以按 url 回溯得到。

    `pm.url <> ''` 不能省:库里有几十篇没有 url 的文档(dr 来源居多),少了这个
    条件,只要有一篇空 url 的文章被判 hard,所有空 url 文档会被一并挡掉。
    alias 只由本仓库的调用方传固定字面量,不接受外部输入。返回的是裸条件,
    不带前导 AND —— 拼进 WHERE 列表还是接在 f-string 后面由调用方决定。
    """
    return (f"{alias}.promo_level <> 'hard'"
            f" AND NOT EXISTS (SELECT 1 FROM documents pm"
            f" WHERE pm.url = {alias}.url AND pm.url <> ''"
            f" AND pm.promo_level = 'hard')")


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    _migrate_favorites(conn)
    conn.executescript(_SCHEMA)
    conn.executescript(_FAVORITES_DDL)
    conn.executescript(_VEC_SCHEMA)
    _migrate_entities(conn)
    _migrate_promo(conn)
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
    """vecs 每项可以是 float 列表,也可以是已经打好包的 float32 裸字节。

    走子进程编码时向量本来就是裸字节回来的(见 kb/embedder.py),再拆成 Python
    float 列表、转头又 struct.pack 回去纯属白烧 CPU——一批 128×1024 个 float。
    """
    import struct
    for cid, v in zip(ids, vecs):
        blob = (bytes(v) if isinstance(v, (bytes, bytearray, memoryview))
                else struct.pack(f"{len(v)}f", *v))
        conn.execute(
            "INSERT OR REPLACE INTO chunk_vec (chunk_id, embedding) VALUES (?, ?)",
            (cid, blob),
        )


# ── 读取 ──────────────────────────────────────────────────────────────────────

def doc_body(conn: sqlite3.Connection, path: str) -> str:
    """把一篇文章的 chunk 按正文顺序拼回全文。

    ORDER BY 必须写在子查询里:group_concat 的拼接顺序取决于聚合看到行的顺序,
    外层 ORDER BY 只排最终那一行结果,约束不到聚合输入。之前重建索引恰好按
    seq 插入所以看不出问题,一旦经历重写/VACUUM/查询计划变化就会错序,
    喂给 LLM 的正文段落顺序被打乱。
    """
    row = conn.execute(
        "SELECT group_concat(text, char(10)) AS body FROM "
        "(SELECT text FROM chunks WHERE path=? ORDER BY seq)",
        (path,),
    ).fetchone()
    return (row["body"] if row and row["body"] else "") or ""
