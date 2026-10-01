"""共用库要能被 wechat-oa 分支同时打开:主干不做破坏性的 schema 变更。

凭证采集那条通道留在 `wechat-oa` 分支上永久并行(见仓库 CLAUDE.md),而两个
分支的 setup.sh 默认链的是**同一份** data/ —— 代码分叉了,库没有。那个分支是
冻结的,改不动:它照旧往 `articles.cover_path` 里写,核对改名照旧读写
`accounts.display_checked_at`。主干 DROP 掉这两列之后,实测按主干跑一次
(自动迁到 v16)、再切回保留分支采集,第一篇新文章就是
`OperationalError: table articles has no column named cover_path`。

所以这里钉的不是"列在不在",而是**那条冻结分支的 SQL 还跑不跑得通** ——
列在不在是手段,它能继续采集才是要保住的事。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db import connection as conn_mod
from src.db.migrations import LATEST_SCHEMA_VERSION, apply_migrations

# 下面两段是 wechat-oa 分支上真实的写法(src/db/repository.py 的
# upsert_article_from_blog / display_name_checked_at / mark_display_name_checked),
# 只删掉与这件事无关的列。主干这边一个字都不改它们。
FROZEN_ARTICLE_INSERT = """
    INSERT INTO articles (
      id, account_name, title, digest, link, cover_url, cover_path,
      create_time, item_show_type
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
    ON CONFLICT(id) DO UPDATE SET
      cover_path=COALESCE(excluded.cover_path, articles.cover_path)
"""
FROZEN_CHECKED_READ = "SELECT display_checked_at FROM accounts WHERE name=?"
FROZEN_CHECKED_WRITE = "UPDATE accounts SET display_checked_at=? WHERE name=?"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready)
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = (
            root / "wechatoa.db", root, False)

    def tearDown(self):
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self.saved
        self.tmp.cleanup()

    def opened_by_main(self) -> sqlite3.Connection:
        """主干怎么开这个库,这里就怎么开。"""
        conn = conn_mod.get_connection()
        conn_mod.init_database(conn)
        return conn

    def frozen_branch_collects(self, conn: sqlite3.Connection) -> None:
        """照 wechat-oa 分支的写法采一篇新文章、核对一次改名。

        它抛任何 sqlite3 异常都算那条分支挂了 —— 不吞掉、不改写成断言失败,
        原始 OperationalError 的那句话本身就是最好的报错。
        """
        conn.execute(
            "INSERT OR IGNORE INTO accounts (name, fakeid, in_crawl_config) "
            "VALUES ('审查号', 'BIZ_REVIEW', 1)")
        conn.execute(FROZEN_ARTICLE_INSERT,
                     ("art-1", "审查号", "标题", "摘要",
                      "https://mp.weixin.qq.com/s?__biz=BIZ_REVIEW&mid=1&idx=1",
                      "https://example.org/c.jpg", "covers/art-1.jpg",
                      "2026-10-01 10:00"))
        conn.execute(FROZEN_CHECKED_READ, ("审查号",)).fetchone()
        conn.execute(FROZEN_CHECKED_WRITE, ("2026-10-01 10:00", "审查号"))
        conn.commit()

    def columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


class AFreshDatabase(_Base):
    def test_keeps_the_two_columns_the_frozen_branch_writes(self):
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        self.assertIn("cover_path", self.columns(conn, "articles"))
        self.assertIn("display_checked_at", self.columns(conn, "accounts"))

    def test_the_frozen_branch_can_still_collect_into_it(self):
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        self.frozen_branch_collects(conn)
        self.assertEqual(
            "covers/art-1.jpg",
            conn.execute("SELECT cover_path FROM articles WHERE id='art-1'").fetchone()[0])


class ADatabaseTheBrokenVersionAlreadyMigrated(_Base):
    """v15/v16 真的 DROP 过那两列,改成空操作并不会让它们自己长回来。

    用户那份 832M 的库 2026-10-01 实测就停在这个状态(v16、两列都没了),
    所以 v17 是存量库唯一的出路。
    """

    def wreck(self) -> None:
        conn = self.opened_by_main()
        conn.execute("ALTER TABLE articles DROP COLUMN cover_path")
        conn.execute("ALTER TABLE accounts DROP COLUMN display_checked_at")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 17")
        conn.commit()
        conn.close()
        conn_mod._db_ready = False

    def test_the_frozen_branch_is_broken_before_the_fix_runs(self):
        """反向钉住:这就是审查复现出来的那个状态,别让它看起来一直没事。"""
        self.wreck()
        conn = conn_mod.get_connection()
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.frozen_branch_collects(conn)
        self.assertIn("cover_path", str(caught.exception))

    def test_opening_it_with_main_puts_the_columns_back(self):
        self.wreck()
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        self.assertIn("cover_path", self.columns(conn, "articles"))
        self.assertIn("display_checked_at", self.columns(conn, "accounts"))
        self.assertEqual(
            LATEST_SCHEMA_VERSION,
            conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0])

    def test_and_then_the_frozen_branch_collects_again(self):
        self.wreck()
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        self.frozen_branch_collects(conn)


class TheWholeMigrationChain(_Base):
    """从 v13 那一代的库一路迁上来,两列必须活着到最后。

    这一条是给**以后**的迁移留的闸门:谁再往链条末尾加一个 DROP COLUMN,
    这里当场红 —— 不必依赖下一个人读到 v17 的那段注释。
    """

    def v13_shaped_database(self) -> sqlite3.Connection:
        conn = self.opened_by_main()
        # 把版本号退回 13,列保持 v13 那时的样子(两列都在)。
        conn.execute("DELETE FROM schema_migrations WHERE version > 13")
        conn.commit()
        return conn

    def test_survives_every_migration_from_v13_on(self):
        conn = self.v13_shaped_database()
        self.addCleanup(conn.close)
        apply_migrations(conn)
        self.assertIn("cover_path", self.columns(conn, "articles"))
        self.assertIn("display_checked_at", self.columns(conn, "accounts"))
        self.frozen_branch_collects(conn)

    def test_article_fts_is_the_one_drop_that_is_allowed(self):
        """v14 删的是整张表,而旧分支 schema.sql 里的
        `CREATE VIRTUAL TABLE IF NOT EXISTS` 会把它重建出来 —— 所以那一步不会
        让旧分支写入失败。`CREATE TABLE IF NOT EXISTS` 对已存在的表却是空操作,
        补不回被 DROP 的列,这正是两者的区别。
        """
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        self.assertIsNone(
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='article_fts'").fetchone())
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS article_fts USING fts5(article_id)")
        conn.execute("INSERT INTO article_fts (article_id) VALUES ('art-1')")
        conn.commit()


if __name__ == "__main__":
    unittest.main()
