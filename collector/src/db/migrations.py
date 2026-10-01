# -*- coding: utf-8 -*-
"""SQLite 增量迁移。"""

from __future__ import annotations

import sqlite3
import re

from src.utils.helpers import time_now

LATEST_SCHEMA_VERSION = 17


def apply_migrations(conn: sqlite3.Connection) -> None:
    # schema_migrations 是本函数自己的记账本,不能指望调用方先建好。connection.py
    # 那边是"旧库先补列、再跑 schema.sql",于是本函数会在 schema.sql 之前先跑一次
    # ——而 schema.sql 才是建这张表的地方。全新数据目录、或旧 WeChatOA 库里没有
    # 这张表时,下面第一句 SELECT 就抛 no such table,cli status/accounts/collect
    # 全卡在仓储初始化上。
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
          version     INTEGER PRIMARY KEY,
          applied_at  TEXT NOT NULL
        )
        """
    )
    row = conn.execute("SELECT MAX(version) AS v FROM schema_migrations").fetchone()
    current = int(row["v"]) if row and row["v"] is not None else 0

    if current < 2:
        _migrate_to_v2(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (2, time_now()),
        )
        conn.commit()
        current = 2

    if current < 3:
        _migrate_to_v3(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (3, time_now()),
        )
        conn.commit()
        current = 3

    if current < 4:
        _migrate_to_v4(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (4, time_now()),
        )
        conn.commit()
        current = 4

    if current < 5:
        _migrate_to_v5(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (5, time_now()),
        )
        conn.commit()
        current = 5

    if current < 6:
        _migrate_to_v6(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (6, time_now()),
        )
        conn.commit()
        current = 6

    if current < 7:
        _migrate_to_v7(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (7, time_now()),
        )
        conn.commit()
        current = 7

    if current < 8:
        _migrate_to_v8(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (8, time_now()),
        )
        conn.commit()
        current = 8

    if current < 9:
        _migrate_to_v9(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (9, time_now()),
        )
        conn.commit()
        current = 9

    if current < 10:
        _migrate_to_v10(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (10, time_now()),
        )
        conn.commit()
        current = 10

    if current < 11:
        _migrate_to_v11(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (11, time_now()),
        )
        conn.commit()
        current = 11

    if current < 12:
        _migrate_to_v12(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (12, time_now()),
        )
        conn.commit()
        current = 12

    if current < 13:
        _migrate_to_v13(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (13, time_now()),
        )
        conn.commit()
        current = 13

    if current < 14:
        _migrate_to_v14(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (14, time_now()),
        )
        conn.commit()
        current = 14

    if current < 15:
        _migrate_to_v15(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (15, time_now()),
        )
        conn.commit()
        current = 15

    if current < 16:
        _migrate_to_v16(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (16, time_now()),
        )
        conn.commit()
        current = 16

    if current < 17:
        _migrate_to_v17(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (17, time_now()),
        )
        conn.commit()


def _migrate_to_v2(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_annotations'"
    ).fetchone()
    if not row:
        return
    cols = {
        r[1] for r in conn.execute("PRAGMA table_info(article_annotations)").fetchall()
    }
    if "is_read" not in cols:
        conn.execute(
            "ALTER TABLE article_annotations ADD COLUMN is_read INTEGER NOT NULL DEFAULT 0"
        )
    if "read_at" not in cols:
        conn.execute("ALTER TABLE article_annotations ADD COLUMN read_at TEXT")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_annotations_unread
        ON article_annotations (is_read) WHERE is_read = 0
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_annotations_starred
        ON article_annotations (starred) WHERE starred = 1
        """
    )


def _migrate_to_v3(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='article_bodies'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(article_bodies)").fetchall()}
    if "body_html" not in cols:
        conn.execute("ALTER TABLE article_bodies ADD COLUMN body_html TEXT")


def _migrate_to_v4(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "source" not in cols:
        conn.execute(
            "ALTER TABLE articles ADD COLUMN source TEXT NOT NULL DEFAULT 'crawl'"
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_source_time
        ON articles (source, create_time DESC, id DESC)
        """
    )
    conn.execute(
        """
        UPDATE articles
        SET source = 'import'
        WHERE source = 'crawl'
          AND (
            account_name = '链接导入'
            OR account_name IN (
              SELECT name FROM accounts WHERE fakeid LIKE 'import:%'
            )
          )
        """
    )


def _migrate_to_v5(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "import_listed" not in cols:
        conn.execute(
            "ALTER TABLE articles ADD COLUMN import_listed INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_import_listed_time
        ON articles (import_listed, create_time DESC, id DESC)
        WHERE import_listed = 1
        """
    )
    # 曾在「导入」中的文章：import_listed=1
    conn.execute("UPDATE articles SET import_listed=1 WHERE source='import'")
    # 订阅文章被误标 source=import：恢复 origin=crawl，保留在导入列表
    conn.execute(
        """
        UPDATE articles
        SET source='crawl', import_listed=1
        WHERE source='import'
          AND account_name != '链接导入'
          AND account_name NOT IN (
            SELECT name FROM accounts WHERE fakeid LIKE 'import:%'
          )
        """
    )


def _migrate_to_v6(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS article_notes (
          article_id   TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
          content      TEXT NOT NULL DEFAULT '',
          created_at   TEXT NOT NULL,
          updated_at   TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_article_notes_updated_at
        ON article_notes (updated_at DESC)
        """
    )


def _migrate_to_v7(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
    if "import_order" not in cols:
        conn.execute(
            "ALTER TABLE articles ADD COLUMN import_order INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_import_listed_order
        ON articles (import_listed, import_order DESC, id DESC)
        WHERE import_listed = 1
        """
    )


def _migrate_to_v8(conn: sqlite3.Connection) -> None:
    """
    回填历史导入文章的 import_order，避免仍按发布时间观感排序。
    规则：按 updated_at/create_time 从旧到新分配递增序号，最新导入排最前。
    """
    conn.execute(
        """
        WITH ranked AS (
          SELECT
            id,
            ROW_NUMBER() OVER (
              ORDER BY
                COALESCE(NULLIF(updated_at, ''), NULLIF(create_time, ''), id) ASC,
                id ASC
            ) AS rn
          FROM articles
          WHERE import_listed = 1
            AND COALESCE(import_order, 0) = 0
        )
        UPDATE articles
        SET import_order = (
          SELECT rn FROM ranked WHERE ranked.id = articles.id
        )
        WHERE id IN (SELECT id FROM ranked)
        """
    )


def _migrate_to_v9(conn: sqlite3.Connection) -> None:
    """修复历史导入文章字数偏小（未提交事务导致回退到标题长度）的数据。"""
    rows = conn.execute(
        """
        SELECT ar.id, ar.title, ab.body_text
        FROM articles ar
        LEFT JOIN article_bodies ab ON ab.article_id = ar.id
        WHERE ar.import_listed = 1
        """
    ).fetchall()
    now = time_now()
    for row in rows:
        article_id = str(row["id"] or "")
        if not article_id:
            continue
        raw = row["body_text"]
        if raw is None:
            text = ""
        else:
            text = str(raw)
        cleaned = re.sub(r"\s+", "", text)
        if cleaned:
            wc = len(cleaned)
        else:
            wc = len(re.sub(r"\s+", "", str(row["title"] or "")))
        conn.execute(
            "UPDATE articles SET word_count=?, updated_at=? WHERE id=?",
            (wc, now, article_id),
        )


def _migrate_to_v10(conn: sqlite3.Connection) -> None:
    """区分爬取配置账号与链接导入占位账号。"""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "in_crawl_config" not in cols:
        conn.execute(
            """
            ALTER TABLE accounts
            ADD COLUMN in_crawl_config INTEGER NOT NULL DEFAULT 1
            CHECK (in_crawl_config IN (0, 1))
            """
        )
    conn.execute(
        "UPDATE accounts SET in_crawl_config = 0 WHERE fakeid LIKE 'import:%'"
    )
    conn.execute(
        """
        UPDATE accounts SET in_crawl_config = 0
        WHERE in_crawl_config = 1
          AND (latest_crawl_at IS NULL OR latest_crawl_at = '')
          AND name IN (
            SELECT account_name FROM articles
            GROUP BY account_name
            HAVING SUM(CASE WHEN source != 'import' THEN 1 ELSE 0 END) = 0
               AND COUNT(*) > 0
          )
        """
    )


def _migrate_to_v11(conn: sqlite3.Connection) -> None:
    """accounts 加"已移出订阅列表"标记。

    在这之前"移除"是 PRAGMA foreign_keys=OFF 之后直接 DELETE 掉 accounts 那一行,
    而 articles.account_name 是指向它的外键——已采集的文章于是全成了孤儿。
    改成标记后,行还在(外键成立),列表查询按 removed=0 过滤把它藏起来。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "removed" not in cols:
        conn.execute(
            """
            ALTER TABLE accounts
            ADD COLUMN removed INTEGER NOT NULL DEFAULT 0
            CHECK (removed IN (0, 1))
            """
        )


def _migrate_to_v12(conn: sqlite3.Connection) -> None:
    """accounts 加"微信上的当前昵称"。

    公众号会改名,而 name 是主键、还被 articles.account_name(外键)和已导出的
    vault 目录名引用着——改它要么被外键拒绝,要么让 6000 多篇文章和磁盘上的
    markdown 对不上。所以改名只更新这一列,name 继续当不变的档案键。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "display_name" not in cols:
        conn.execute(
            "ALTER TABLE accounts ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
        )


def _migrate_to_v13(conn: sqlite3.Connection) -> None:
    """accounts 加"上次核对昵称的时间"。

    v12 的 display_name 只回答"当前叫什么",回答不了"这个名字什么时候核对的"。
    补名字那条路径原来靠 display_name 空不空来决定要不要抓——补上一次之后
    就再也不看第二眼,停更号后续再改名就永远发现不了。有了这一列才能按
    间隔重新核对(见 runner._refresh_display_name)。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if not row:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
    if "display_checked_at" not in cols:
        conn.execute("ALTER TABLE accounts ADD COLUMN display_checked_at TEXT")


def _migrate_to_v14(conn: sqlite3.Connection) -> None:
    """删掉 article_fts 全文索引。

    它从头到尾只有写入方:每次入库/改正文/改标签都重新分词写一遍,而唯一的
    读取口 `Repository.search_articles` 没有任何调用者——文章检索在知识库那
    一侧按 vault 的 md 做,采集器这边只管抓和导出。实测这张虚拟表连同它的
    影子表占 134 MB,纯粹是每次抓取都要付一次的分词开销。

    DROP 虚拟表会连 article_fts_{data,idx,content,docsize,config} 一起删掉。
    """
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='article_fts'"
    ).fetchone()
    if row:
        conn.execute("DROP TABLE article_fts")


def _migrate_to_v15(conn: sqlite3.Connection) -> None:
    """退役:原来在这里 `DROP articles.cover_path`,现在什么都不做(见 v17)。

    留着这个空壳而不是把编号抹掉:版本号是写进 `schema_migrations` 的,早就
    有库记着"15 跑过了"。抹掉编号只会让那些库的版本号对不上任何一段代码。
    """


def _migrate_to_v16(conn: sqlite3.Connection) -> None:
    """退役:原来在这里 `DROP accounts.display_checked_at`,现在什么都不做(见 v17)。"""


def _migrate_to_v17(conn: sqlite3.Connection) -> None:
    """把 v15/v16 删掉的两列补回来 —— 这个库要能被两个分支同时打开。

    封面不再下载到本地、昵称核对不再按天节流,所以 `articles.cover_path` 和
    `accounts.display_checked_at` 在主干上确实零个读取方,v15/v16 当时就把它们
    DROP 了。**但这个库不只有主干在读。** 凭证采集那条通道留在 `wechat-oa`
    分支上永久并行(见仓库 CLAUDE.md),两个分支的 setup.sh 默认链的是同一份
    data/ —— 代码分叉了,库没有。而那个分支是冻结的:它的 `upsert_article_from_blog`
    照旧往 `cover_path` 里写,核对改名照旧读写 `display_checked_at`。

    于是实测出来的后果是:按主干跑一次(自动迁到 v16),再切回保留分支采集,
    第一篇新文章就 `OperationalError: table articles has no column named
    cover_path`,那条分支从此写不进任何文章 —— 而「保留」这件事就名存实亡了。
    旧分支自己救不回来:它只认到 v13,看到 `MAX(version)=16` 什么都不跑,
    它 schema.sql 里的 `CREATE TABLE IF NOT EXISTS` 对已存在的表也是空操作。

    所以把两列加回来,并且**主干从此不在这个库上做破坏性的 schema 变更**:
    要停用一列就停止读写它,别 DROP。两个空列的代价是几十字节,换的是另一条
    分支还能跑。v14 删 `article_fts` 不在此列 —— 那是整张表,旧分支 schema.sql
    里的 `CREATE VIRTUAL TABLE IF NOT EXISTS` 会把它重建出来(内容要靠它自己
    那条重建路径补,但写入不会失败)。

    这一步也是已经迁到 v16 的存量库的唯一出路:列是真的被 DROP 掉了,
    光把 v15/v16 改成空操作并不会让它们自己长回来。
    """
    articles = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='articles'"
    ).fetchone()
    if articles:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(articles)").fetchall()}
        if "cover_path" not in cols:
            conn.execute("ALTER TABLE articles ADD COLUMN cover_path TEXT")
    accounts = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='accounts'"
    ).fetchone()
    if accounts:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(accounts)").fetchall()}
        if "display_checked_at" not in cols:
            conn.execute("ALTER TABLE accounts ADD COLUMN display_checked_at TEXT")
