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

from kb.config import DATA_DIR, DB_PATH, EMBEDDING_DIM, SOURCES
# 老库升级那一块搬去了 kb/store_migrate.py(收藏表 DDL 也在那儿 ——
# 重建 v4 时要用它,留在这儿两头就得互相 import)
from kb import store_migrate as mig

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
  promo_at      TEXT,
  -- 「这一版内容里没有可提取的文字」的终态(见 kb/extract.py 的 body_gap)。
  -- 存的是当时那份 content_hash,不是一个布尔:正文哪天被补回来、或者 md 以
  -- 任何方式变了,hash 就对不上,这一篇自动回到提取队列。
  no_body_hash  TEXT NOT NULL DEFAULT ''
);
-- idx_documents_promo 在 store_migrate.migrate_promo 里建(老库先补列才能建索引)

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
-- idx_entities_norm 在 store_migrate.migrate_entities 里建(老库先补列才能建索引)

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

-- 日报的「这一条上报过没有」账本。日报原来只按时间窗 + top-N 取材,于是没挤进
-- 名额的条目在下一个窗口里两个桶都选不到,**静默永久丢失**(实测一份日报
-- late_total=679 而只带得走 30 条)。判据换成「这一条有没有被哪一份日报带走过」,
-- 没带走的就留在队列里等下一份 —— 口径在 kb/daily.py。
--
-- 故意**不**对 documents 建外键:清理脚本删掉一篇文章不该让它重新变成未上报
-- (它确实报过),而残留的孤儿行是惰性的。
CREATE TABLE IF NOT EXISTS daily_carried (
  path       TEXT NOT NULL,
  day        TEXT NOT NULL,   -- 带走它的那一份日报的日期;'#seed' = 启用账本时一次性盖的章,
                             -- '#drop' = 人工从队列里丢弃(见 kb/daily_carry.py 的 DROP_DAY)
  carried_at TEXT NOT NULL,
  PRIMARY KEY (path, day)
);

-- 日报自己的几个标量状态(目前只有 carry_epoch:账本是什么时候启用的)。
CREATE TABLE IF NOT EXISTS daily_state (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
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


def promo_level_sql(alias: str = "d") -> str:
    """一篇文档的**有效**软广档位,作为 SELECT 表达式用。

    自己判过就用自己的;没判过(条目化衍生条目 dr-gzh-projects 永远是空的)
    就按 url 从被拆的那篇原文继承。这和 promo_exclude_sql 挡 hard 时的回溯
    是同一条关系,两处必须一致——只标原文的话,一篇 soft 广告拆出来的工具
    条目在搜索结果和引用里仍然是干干净净的,标记等于没加。

    同一个 url 挂多篇时按 hard > soft > none 取最重的一档:宁可多提示一次,
    也不要把该提示的漏掉。`pm.url <> ''` 的理由同 promo_exclude_sql——库里
    几十篇没有 url 的文档否则会互相继承。
    """
    return (
        f"CASE WHEN {alias}.promo_level <> '' THEN {alias}.promo_level ELSE"
        f" COALESCE((SELECT pm.promo_level FROM documents pm"
        f" WHERE pm.url = {alias}.url AND pm.url <> '' AND pm.path <> {alias}.path"
        f" AND pm.promo_level <> ''"
        f" ORDER BY CASE pm.promo_level WHEN 'hard' THEN 0 WHEN 'soft' THEN 1"
        f" ELSE 2 END LIMIT 1), '') END"
    )


def effective_promo_levels(conn, paths: list[str]) -> dict[str, str]:
    """批量取一批文档的有效软广档位,口径与 promo_level_sql 同一份。

    给检索用:排序要在截断 top-k **之前**知道谁是 soft,而那时还没查文档元数据。
    只有 soft/hard 才进返回值,none 和未判定的不占位。
    """
    out: dict[str, str] = {}
    for i in range(0, len(paths), 400):     # SQLITE_MAX_VARIABLE_NUMBER 留余量
        chunk = paths[i:i + 400]
        ph = ",".join("?" * len(chunk))
        for r in conn.execute(
            f"SELECT d.path, {promo_level_sql('d')} AS lvl FROM documents d"
            f" WHERE d.path IN ({ph})", chunk
        ):
            if r["lvl"] in ("soft", "hard"):
                out[r["path"]] = r["lvl"]
    return out


def db_uri(db_path: Path) -> str:
    """一个库文件的规范 `file:` URI。口径只有这一份。

    `Path.as_uri()` 会把路径里的 `#` `?` `\'` 空格一并 percent-encode —— 手写
    `f"file:{path}"` 的话,`#` 和 `?` 会把地址在那儿截断(SQLite 把后半截当成
    fragment / query),于是打开的是一个**名字更短的别的库**,顺带在那个位置
    凭空建一个文件(2026-10-11 审查实测:数据目录叫 `reader#data` 时建出一个
    叫 `reader` 的文件,而 `mode=ro` 连带失效)。
    """
    return Path(db_path).absolute().as_uri()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # **`uri=True` 不是为了主库,是为了这条连接上后来的 ATTACH**
    # (`extract_optin.attached_feeds` 要以只读挂 feeds.db)。ATTACH 认不认
    # `file:…?mode=ro` 取决于**连接**有没有带 SQLITE_OPEN_URI —— 而 Python 的
    # `uri=` 参数正是它。不带时只能靠 SQLite 编译默认值 `SQLITE_USE_URI`,
    # 本机 Homebrew 的 3.53.2 开着、项目实际用的 uv 那份 3.50.4 **没开**
    # (2026-10-11 审查实测:后者上 ATTACH 一律 `unable to open database`,
    # 连普通目录都失败,整个待提取查询取不到结果 —— 混在里面的绿泡泡积压
    # 也跟着不处理)。所以显式开,不押在构建选项上。
    # 开了之后主库路径自己也必须是规范 URI,否则含 `#`/`?` 的数据目录会被
    # 当成 query/fragment 截断 —— 那是同一枚硬币的另一面,见 `db_uri`。
    conn = sqlite3.connect(db_uri(db_path), uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    mig.migrate_favorites(conn)
    conn.executescript(_SCHEMA)
    conn.executescript(mig.FAVORITES_DDL)
    conn.executescript(_VEC_SCHEMA)
    mig.migrate_entities(conn)
    mig.migrate_promo(conn)
    mig.migrate_no_body(conn)
    mig.migrate_fav_anchor(conn)
    return conn


# ── 写入 ──────────────────────────────────────────────────────────────────────

def doc_needs_reindex(conn: sqlite3.Connection, path: str, content_hash: str) -> bool:
    row = conn.execute(
        "SELECT content_hash FROM documents WHERE path=?", (path,)
    ).fetchone()
    return row is None or row["content_hash"] != content_hash


def promo_tag_wanted(level: str, in_file: bool) -> bool:
    """一次索引重建之后,`flag/软广` 那一行该在不该在 —— 这条规则只有这一份。

    `upsert_document` 先按 frontmatter 重建 `doc_tags`、紧接着
    `_align_promo_tag` 按落库那份判定纠正它,所以末态是:判定是 hard 就有、
    是 none/soft 就没有,**还没判过(`''`)就跟着文件** —— 那时 md 那行是提取
    阶段写进去的唯一记录,索引不去覆盖它。

    抽成纯函数是因为有第二个消费方:登记「这份文件已经索引过了」那道判据要
    回答「这一份现在重新索引一次,库里会变吗」
    (`kb.doc_rewrite.index_projection_matches`)。它原来无条件把这一行从两边
    排除掉,于是**还没判过**的那一篇上,外来编辑增删 md 里那行 flag 判据完全
    看不见:收据照签,而真实索引这时是跟着文件的 —— 按这个标签筛选从此停在
    旧结果上,再同步也不恢复(2026-10-14 审查增删两个方向各实测一次)。

    **这里和下面那两句 SQL 是两份实现**(判据必须待在写语句自己的 WHERE 里),
    所以有一条回归真跑 `upsert_document` 再和这个函数对账,四档判定 × 文件
    有/无 八个组合全覆盖:哪天一边改了,那条会红。
    """
    if level == "hard":
        return True
    if level in ("none", "soft"):
        return False
    return in_file


def _align_promo_tag(conn: sqlite3.Connection, path: str) -> None:
    """重建 doc_tags 时,`flag/软广` 那一行按**库里的判定**定,不按 frontmatter。

    **末态那条规则抽在 `promo_tag_wanted` 里**(登记那道判据也要它),这里是它
    的 SQL 实现 —— 两边改一边就会在那条对账回归里红。

    `upsert_document` 把 doc_tags 整表删掉再按 md 的 tags 重建,而它不碰
    `promo_level` —— 于是 md 里一行过时的 flag 就成了这张表的唯一真相来源:
    判定早已改成 none,一次完全合法的索引又把那行插回库里,而按 `flag/软广`
    过滤的查询从此照旧捞得到这篇已经纠正为正常内容的文章。md 那行可能过时的
    路子不止一条(回写摘标签时碰上没权限/磁盘满/进程中断,或者有人手工改了
    frontmatter),**所以判据不能是「上一次回写成功了没有」,只能是落库的那份
    判定**:判定明确不是 hard 就把那行摘掉,明确是 hard 就补上。

    `promo_level = ''`(还没判过)两样都不做 —— 那时 md 那行是提取阶段写进去的
    唯一记录(重建过 kb.db 的库就是这样),按「矛盾」摘掉等于凭空丢数据。

    判据待在两条写语句自己的 WHERE 里(不在 Python 这边先 SELECT 一次),口径
    同 `kb.extract_promo.save_promo`。
    """
    from kb.extract_promo import PROMO_TAG

    conn.execute(
        "DELETE FROM doc_tags WHERE path=? AND tag=?"
        " AND (SELECT promo_level FROM documents WHERE path=?) IN ('none','soft')",
        (path, PROMO_TAG, path))
    conn.execute(
        "INSERT OR IGNORE INTO doc_tags (path, tag)"
        " SELECT ?, ? WHERE"
        " (SELECT promo_level FROM documents WHERE path=?) = 'hard'",
        (path, PROMO_TAG, path))


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
    _align_promo_tag(conn, doc.path)

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
    """删掉 vault 里已不存在的文档(含级联的 chunk/tag/entity 和向量)。

    **停用来源(`config.Source.enabled=False`)的文档一律算存活。** 它们的树已经
    不再被 `scanner.scan_all` 遍历,所以 `alive_paths` 里必然一条都没有 —— 不豁免
    的话"停止收新内容"会在下一轮索引里顺手把库里那一批连 chunk、向量、标签一起
    删掉,而面板上只多一个 removed 计数。停用是「不再往里加」,不是「把存量清掉」。
    判据落在这里而不是三个调用方各写一遍(`pipeline_stages` / `cli` /
    `scripts/feed_trim_history`):这是唯一的删除口,口径只该有一份。
    """
    off = {s.name for s in SOURCES if not s.enabled}
    rows = conn.execute("SELECT path, source FROM documents").fetchall()
    dead = [r["path"] for r in rows
            if r["path"] not in alive_paths and r["source"] not in off]
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


def doc_body_version(conn: sqlite3.Connection, path: str) -> tuple[str, str]:
    """正文 + **它所属的那一版** content_hash,一条语句读出来。

    拆成两句读是不安全的:提取 worker 读完正文、主线程落「这一版没有文字」终态
    之间,另一条连接的索引完全可能把这一篇换成一版**有正文**的内容(合法交错,
    提取不持维护锁)。那时两句分别落在换版前后,终态就会被盖到新版上 —— 新正文
    从此既不提取也不提醒(2026-10-09 审查实测)。一条语句只有一个快照,正文和
    版本号必然同属一版;「这一版还是不是当前那一版」由写入侧的条件 UPDATE 回答
    (见 kb/extract.py 的 mark_no_body)。

    没有这一行文档时返回 ("", "")。
    """
    snap = doc_input_snapshot(conn, path)
    return (snap["version"], snap["body"]) if snap else ("", "")


def doc_input_snapshot(conn: sqlite3.Connection, path: str) -> dict | None:
    """一篇文章喂给模型的那组输入 + 它所属的那一版,**一条语句一个快照**。

    `doc_body_version` 的全量版:多带上 title / account,因为这两样也进 prompt
    (软广判定按「这个号在卖什么」读文末那段,账号名是判据的一部分)。分两次
    查询读的话,两次之间另一条连接的索引可以把这一篇换版,于是候选里带的是
    **旧标题配新正文**,而后面那道按 `content_hash` 的条件写拿到的是新版本号、
    照样放行 —— 判的是一份没人写过的东西。一条语句只有一个快照,四样必然同属
    一版;「这一版还是不是当前那一版」由写入侧回答(见 kb/extract.py 的
    `mark_no_body` 与 `kb/extract_promo.py` 的 `commit_promo`)。

    没有这一行文档时返回 None。chunks 一条都没有时 body 是空串(不是 None)。
    """
    row = conn.execute(
        "SELECT (SELECT title FROM documents WHERE path=?) AS title,"
        " (SELECT account FROM documents WHERE path=?) AS account,"
        " (SELECT content_hash FROM documents WHERE path=?) AS version,"
        " (SELECT count(*) FROM documents WHERE path=?) AS alive,"
        " group_concat(text, char(10)) AS body FROM "
        "(SELECT text FROM chunks WHERE path=? ORDER BY seq)",
        (path, path, path, path, path),
    ).fetchone()
    if row is None or not row["alive"]:
        return None
    return {"path": path, "title": row["title"] or "",
            "account": row["account"] or "",
            "version": row["version"] or "", "body": row["body"] or ""}
