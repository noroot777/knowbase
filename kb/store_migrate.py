# -*- coding: utf-8 -*-
"""老库升级:收藏表的建表 DDL,以及 documents / entities / favorites 的补列与重建。

**从 `kb/store.py` 搬出来的一块,职责是「老库怎么变成现在这个形状」**;
`store.py` 只剩下建表与读写。两边各自的入口仍然只有一个:`store.connect()`
按顺序跑这里的几个 `migrate_*`,交出去的连接一定是升过级的。

这一块的硬约束在 `docs/decisions/fav-snippets.md`(收藏表)和 `promo.md`
(documents 补列):**主干的迁移只增不减**,判据是「另一条分支不重启会不会当场
坏」;补列的「读列集合」和 ALTER 必须待在同一把写锁里(见 `add_columns`)。
"""

from __future__ import annotations

import sqlite3

# 收藏(web UI;整篇或片段,语义是"待试备忘"而非典藏,所以带状态流转。
# 文档从索引移除时级联清掉,重索引 upsert 不影响)。
# 单独抽出来:favorites 的几次迁移都要独立重建这张表。
#
# **`path` 可空**(v4)。「收藏此段」原来只长在 `/doc/…` 阅读页上,片段必然对应
# 一篇 vault 文档;现在日报正文、问答回答、还没导出的订阅条目也能选中收藏,而
# 这三样**在 documents 里没有行**(日报写在 `daily/`,不在任何 `SOURCES.root`
# 底下;回答压根不是文件)。拿空串占位会被这条外键当场拒掉 —— 空串不是 NULL,
# 外键照查。所以把非空约束去掉,由 `origin` 这一族列说明它来自哪里。
# **只增不减**:`path` 非空时外键和级联语义一个字没变。
FAVORITES_TABLE = """
CREATE TABLE IF NOT EXISTS favorites (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  path        TEXT REFERENCES documents(path) ON DELETE CASCADE,  -- NULL = 没有对应的 vault 文档
  quote       TEXT NOT NULL DEFAULT '',      -- 片段原文;'' = 整篇收藏
  prefix      TEXT NOT NULL DEFAULT '',      -- 片段定位锚:前文
  suffix      TEXT NOT NULL DEFAULT '',      -- 片段定位锚:后文
  note        TEXT NOT NULL DEFAULT '',      -- 一句话:当时为什么收
  status      TEXT NOT NULL DEFAULT 'inbox', -- inbox 待试 / done 已处理 / archived 沉底
  action_json TEXT NOT NULL DEFAULT '',      -- LLM 行动卡 {name,what,cmd,eta,kind},收藏时异步生成
  origin      TEXT NOT NULL DEFAULT 'doc',   -- 这段话是从哪个界面选的:doc/daily/chat/feed
  origin_ref  TEXT NOT NULL DEFAULT '',      -- origin 内部的定位键(日报日期 / 会话:消息 id / 条目 key)
  origin_title TEXT NOT NULL DEFAULT '',     -- 收藏那一刻的标题快照,path 为空时列表只靠它
  origin_url  TEXT NOT NULL DEFAULT '',      -- 站外原文地址(可空)
  -- **采集那一刻这段引文在那一篇里是不是只有一处。** 回跳时两侧上下文都失配、
  -- 退到「只找引文」那一档时按它决定能不能采信:当时就有两处、收的是第二处,
  -- 后来第二处被删掉,剩下的第一处按裸引文找正好唯一 —— 采信它等于把另一处同
  -- 句冒充成原位置。NULL = 老数据没记(按说不清办,不走那一档)。
  quote_unique INTEGER,
  -- **采集那一刻这条锚点定不定得住**(1 = 整段锚点在那一篇里唯一落回用户选的那
  -- 一处)。回跳那一刻记着 0 的一律不定位:采集那一刻前后各两百字也一字不差的
  -- 两处,谁也说不出用户选的是第几处;后来原处被整节删掉,剩下的那一处成了整段
  -- 锚点的唯一命中 —— 采信它就是把一处从没被收藏过的文字高亮出来。
  -- NULL = 老数据没记(按「不知道」办,原样放行;折成 0 会把存量收藏全废掉)。
  anchor_exact INTEGER,
  -- **采集那一刻核对过的是哪一版正文**(`scanner.body_version`;落库前后端又按
  -- 盘上此刻那一份核过一次,所以这个数是「写进来那一刻盘上就是这一版」)。
  -- 回跳那一刻拿它和阅读页此刻的版本号相比:**相等 = 这一篇没动过**,于是锚点
  -- 一侧失配只能是两种渲染摊出来的文本不一样,可以只听另一侧、必要时退到裸引文;
  -- **不等(或者老数据为空)= 这一篇改过/说不清**,那时一侧的「唯一命中」证明
  -- 不了它就是原处,一律如实说位置说不清(见 `snip_text.js` 的 `snipSeek`)。
  -- 只对绑了文档的收藏有意义;path 为空时为 NULL。
  doc_version TEXT,
  -- **问答片段出自哪一条已经落库的消息。** 会话号非空只说明那个对话存在;这一
  -- 列是后端真正核验过的消息身份(见 favorites_api 的问答归属核验)。0/NULL =
  -- 不是问答片段,或者老数据。
  origin_msg  INTEGER,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL                  -- 状态/备注最后一次变动,陈旧判断基于它
);
"""
# **索引只由这一处管。** `connect()` 在 `migrate_favorites` 之后无条件跑一次
# `executescript(FAVORITES_DDL)`,所以不论哪条迁移分支中间把它弄没了(索引跟着
# `RENAME TO` 挂到临时表上、临时表一删就跟着没),连接交出去之前都会被补回来。
# 迁移里不要再各自 DROP/CREATE 一遍:实测在重建里加那两句,单点注入去掉任何一句
# 回归都红 0 条 —— 那是夹在两道带中间的死代码,而它的注释会让下一个人以为它顶事。
FAVORITES_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_favorites_path ON favorites(path)")
FAVORITES_DDL = FAVORITES_TABLE + FAVORITES_INDEX + ";\n"

# v2/v3 带过来的列。重建时按**老表实际有的**那几列拼 INSERT:v2 没有
# action_json,写死一份列名会让它在 `no such column` 上翻车。
_FAV_CARRIED = ("quote", "prefix", "suffix", "note", "status", "action_json",
                "quote_unique", "anchor_exact", "origin_msg", "doc_version",
                "created_at", "updated_at")


def _carry_sequence(conn: sqlite3.Connection, old: str, new: str) -> None:
    """重建表时把自增高水位从 `old` 搬到 `new`,只升不降。

    **重建一张 AUTOINCREMENT 表必须跟着搬这一笔。** `ALTER TABLE … RENAME TO`
    会把 `sqlite_sequence` 那一行一起改名,但新表那行是按"插进去的最大 id"重新
    算的 —— 编号最大的那条**已经删掉**时,新表的高水位就低于历史分配过的,于是
    下一条新收藏**复用一个已经发出去过的编号**。后果不是编号不好看:收藏列表上
    那几颗按钮、`favorites_api` 的 PATCH/DELETE、`favbrain` 的行动卡写回全都只
    拿着 id —— 不复用时过期的那一页最多得到 404,复用之后同一个请求会**成功作用
    在另一条收藏上**(审查实测:删掉最高编号那条、升级、再存一条,旧页面点删除
    返回 204,删掉的是升级后新存的那条)。
    """
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?",
                       (old,)).fetchone()
    if row is None or row[0] is None:
        return                      # 老表压根没分配过编号(v1 不是自增表)
    high = int(row[0])
    cur = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = ?",
                       (new,)).fetchone()
    if cur is None:
        # 新表还没分配过编号(老表一行都没搬过来):这一行就是它的起点
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)",
                     (new, high))
    elif int(cur[0]) < high:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?",
                     (high, new))


def migrate_favorites(conn: sqlite3.Connection) -> None:
    """favorites 结构升级,幂等。

    v1(path 主键、仅整篇星标)→ v2(id 主键 + 片段/状态)→ v3(+action_json)
    → v4(path 可空 + origin 一族:片段可以来自日报/问答/未导出的订阅条目)。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(favorites)").fetchall()}
    if not cols:
        return
    if "id" not in cols:  # v1 → 重建(新 DDL 已经是 v4,origin 默认 doc 正合 v1 语义)
        conn.execute("ALTER TABLE favorites RENAME TO favorites_v1")
        conn.executescript(FAVORITES_DDL)
        conn.execute(
            "INSERT INTO favorites (path, created_at, updated_at)"
            " SELECT path, created_at, created_at FROM favorites_v1"
        )
        _carry_sequence(conn, "favorites_v1", "favorites")
        conn.execute("DROP TABLE favorites_v1")
        conn.commit()
    elif "origin" not in cols:  # v2/v3 → 重建
        _rebuild_favorites_v4(conn)


def _rebuild_favorites_v4(conn: sqlite3.Connection) -> None:
    """把 `path` 的非空约束去掉并补上 origin 一族。幂等。

    **只能重建**:加列 ALTER 办得到,去掉 NOT NULL 办不到(SQLite 没有
    `ALTER COLUMN`)。

    **整段在一个写事务里,并在锁里重新读一次列集合** —— 口径同 `add_columns`:
    `connect()` 是页面 API、提取 worker、CLI 共用的入口,升级后第一次并发使用
    会有一批连接同时跑到这里,判据和重建分在两个不持锁的语句里就是两个连接各
    重建一次,后一个撞 `table favorites_v3 already exists`、那个请求当场 500。
    常态(已经是 v4)在上面那句 PRAGMA 就返回了,一次锁都不取。

    **孤儿行的 path 落 NULL,不让它把整次迁移带崩。** 外键是 ON 的,INSERT 回去
    每行都查一次 documents —— 历史上留下的、文档已经不在索引里的收藏(原来
    INNER JOIN 下就看不见、却仍占着计数)会让这一句整条失败,于是 `connect()`
    本身再也成功不了。置空之后它们按"没有对应文档"显示,比打不开库好。
    `executescript` 在执行前会隐式提交,所以这里逐条 execute。
    """
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(favorites)").fetchall()}
        if not cols or "origin" in cols:   # 别人刚重建完
            conn.commit()
            return
        carried = [c for c in _FAV_CARRIED if c in cols]
        names = ", ".join(carried)
        conn.execute("ALTER TABLE favorites RENAME TO favorites_v3")
        conn.execute(FAVORITES_TABLE)
        conn.execute(
            f"INSERT INTO favorites (id, path, {names})"
            " SELECT id, CASE WHEN EXISTS (SELECT 1 FROM documents d"
            "   WHERE d.path = favorites_v3.path) THEN path ELSE NULL END,"
            f" {names} FROM favorites_v3"
        )
        # 搬完行再搬高水位,都在同一个写事务里 —— DROP 之后 `favorites_v3`
        # 那行 sqlite_sequence 就跟着没了,顺序不能反。
        _carry_sequence(conn, "favorites_v3", "favorites")
        conn.execute("DROP TABLE favorites_v3")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_entities(conn: sqlite3.Connection) -> None:
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


def add_columns(conn: sqlite3.Connection, table: str,
                cols: tuple[tuple[str, str], ...]) -> None:
    """给老表补列。幂等。**缺列才取写锁,并在锁里重新读一次列集合。**

    「读列集合」和 ALTER 分在两个语句里、中间不持锁的话,两个连接同时首访同一个
    老库会读到同一份旧列集合,后一个 ALTER 撞 `duplicate column name` —— 那一个
    请求当场 500,而 connect() 是页面 API、提取 worker、CLI 共用的入口,于是
    「升级后第一次并发使用」有一批操作直接报错(2026-10-09 审查实测:12 个并发
    连接 11 个抛 `duplicate column name`)。WAL 和 SQLite 的写锁不会自动把前面
    那句 PRAGMA 一起包进事务,所以判据必须和这次 ALTER 待在同一个写事务里 ——
    口径同 feeds registry 那次「补列和它前面那次列集合读取要在同一把写锁里」。

    常态(列都在)只花一句 PRAGMA、不取锁:connect() 每次都会跑到这里,
    为一件一辈子只发生一次的事去抢写锁是把升级的代价摊给了每一次连接。
    """
    def missing() -> list[tuple[str, str]]:
        have = {r["name"] for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()}
        return [(n, d) for n, d in cols if n not in have]

    if not missing():
        return
    # 这里只会是 connect() 自己前面几步迁移留下的事务(各自都 commit 过);
    # 显式落一次是因为 BEGIN IMMEDIATE 不能嵌在事务里。
    if conn.in_transaction:
        conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, ddl in missing():          # 锁里重新读:别人刚补完就什么都不做
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate_promo(conn: sqlite3.Connection) -> None:
    """documents 加软广判定三列并建索引。幂等。

    老库 documents 已存在,_SCHEMA 的 CREATE TABLE IF NOT EXISTS 不会补列,
    只能 ALTER;索引要等列补完才能建,所以不写进 _SCHEMA(同 migrate_entities)。
    """
    add_columns(conn, "documents",
                (("promo_level", "TEXT NOT NULL DEFAULT ''"),
                 ("promo_reason", "TEXT NOT NULL DEFAULT ''"),
                 ("promo_at", "TEXT"),
                  # 「判定落库了,但 md frontmatter 那行 flag 还没对齐」的持久化
                  # 待办。交付分两步落在两个不同的存储上(kb.db 的事务管不着
                  # vault 文件),第二步失败必须留下记录 —— 否则下一轮按
                  # `promo_level` 非空把这一篇滤掉,两处永久不一致。
                  # 写它的口径只有一份:`kb.extract_promo.mark_promo_tag_pending`。
                 ("promo_tag_pending", "INTEGER NOT NULL DEFAULT 0")))
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_promo ON documents(promo_level)")
    # 软广排除要按 url 从条目回溯原文(见 promo_exclude_sql),没索引就是每行一次全表扫
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_documents_url ON documents(url)")
    conn.commit()


def migrate_fav_anchor(conn: sqlite3.Connection) -> None:
    """favorites 加回跳要用的那几列:采集时唯一性、锚点定不定得住、问答消息身份、
    正文版本。幂等。

    老库 favorites 已存在,`CREATE TABLE IF NOT EXISTS` 不补列,只能 ALTER
    (同 `migrate_promo`)。四列都可空:**老数据的空值是「没记下」,既不是 0 也
    不是空串** —— `quote_unique` 为空时回跳不走裸引文那一档(判不准一律扣留),
    `anchor_exact` 为空时不知道当时定不定得住、按原样放行(折成 0 会把存量收藏
    一次性废掉),`origin_msg` 为空时问答归属只核到会话一级,`doc_version` 为空
    时证明不了「要搜的这一篇就是采集时那一篇」,于是弱档一律不采信。
    """
    add_columns(conn, "favorites",
                (("quote_unique", "INTEGER"), ("anchor_exact", "INTEGER"),
                 ("origin_msg", "INTEGER"), ("doc_version", "TEXT")))


def migrate_no_body(conn: sqlite3.Connection) -> None:
    """documents 加 no_body_hash 列。幂等(理由同 migrate_promo:老库只能 ALTER)。

    不建索引:它只在 pending_docs 那一个查询里当过滤条件,而那个查询本来就
    已经被 extracted_at IS NULL 收窄到几十行。
    """
    add_columns(conn, "documents",
                (("no_body_hash", "TEXT NOT NULL DEFAULT ''"),))
