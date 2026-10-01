"""共用库要能被 wechat-oa 分支同时打开:主干不做破坏性的 schema 变更。

凭证采集那条通道留在 `wechat-oa` 分支上永久并行(见仓库 CLAUDE.md),而两个
分支的 setup.sh 默认链的是**同一份** data/ —— 代码分叉了,库没有。那个分支是
冻结的,改不动:它照旧往 `articles.cover_path` 里写,核对改名照旧读写
`accounts.display_checked_at`。主干 DROP 掉这两列之后,实测按主干跑一次
(自动迁到 v16)、再切回保留分支采集,第一篇新文章就是
`OperationalError: table articles has no column named cover_path`。

所以这里钉的不是"列在不在",而是**那条冻结分支的 SQL 还跑不跑得通** ——
列在不在是手段,它能继续采集才是要保住的事。

而且**判据是「对方不重启会不会当场挂」,不是「对方重启后能不能自己长回来」**。
v14 原来 DROP 掉 `article_fts`,理由是旧分支 schema.sql 里那句
`CREATE VIRTUAL TABLE IF NOT EXISTS` 会重建它 —— 可那只在它**首次打开库**时跑
(`ensure_database_ready` 的缓存是进程内的)。旧分支正在同步、主干在另一个进程里
第一次升级这个库时,它采下一篇就 `no such table: article_fts`,那一轮中途失败、
那个号这一批剩下的文章都不再正常入库,要关掉重开才恢复。所以下面有一组测试
**攥着一个升级前就建好的连接**跨过整条迁移链,重开连接不算。
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from src.db import connection as conn_mod
from src.db import migrations
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
# 旧分支每入库一篇都写一次全文索引(src/db/fts.py 的 upsert_article_fts,由
# repository.upsert_article_from_blog 和 api._ingest_crawl_article 调)。
FROZEN_FTS_DELETE = "DELETE FROM article_fts WHERE article_id = ?"
FROZEN_FTS_INSERT = """
    INSERT INTO article_fts (article_id, title_tok, digest_tok, body_tok)
    VALUES (?, ?, ?, ?)
"""


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
        conn.execute(FROZEN_FTS_DELETE, ("art-1",))
        conn.execute(FROZEN_FTS_INSERT, ("art-1", "标题", "摘要", "正文"))
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

    def test_the_full_text_index_is_not_dropped_either(self):
        """表和列一视同仁:主干的迁移只增不减。

        v14 原来 DROP 掉它,理由是旧分支会自己重建 —— 那只对重启成立,见
        `AnOldProcessThatSpansTheUpgrade`。
        """
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        self.assertIsNotNone(
            conn.execute("SELECT 1 FROM sqlite_master WHERE name='article_fts'").fetchone())

    def test_the_index_definition_matches_what_the_frozen_branch_inserts(self):
        """列名或 tokenize 对不上,旧分支那句 INSERT 照样失败。

        而它重启时的 `CREATE VIRTUAL TABLE IF NOT EXISTS` 是空操作 —— 建错了
        它自己改不回来,所以这一份定义就是唯一那份。

        这里**先把表删掉再跑迁移**,验的才是 v18 自己那份 CREATE:不删的话
        `IF NOT EXISTS` 直接空操作,schema.sql 建好的那张会把它遮住,v18 里
        写错列名这一条压根测不出来。
        """
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        conn.execute("DROP TABLE IF EXISTS article_fts")
        conn.execute("DELETE FROM schema_migrations WHERE version >= 18")
        conn.commit()
        apply_migrations(conn)
        conn.execute(FROZEN_FTS_INSERT, ("art-9", "标题", "摘要", "正文"))
        conn.execute(FROZEN_FTS_DELETE, ("art-9",))
        self.assertEqual(
            ["article_id", "title_tok", "digest_tok", "body_tok"],
            [r[1] for r in conn.execute("PRAGMA table_info(article_fts)")])


class AnOldProcessThatSpansTheUpgrade(_Base):
    """旧分支那个进程**不重启**,攥着升级前就建好的连接继续采集。

    这是第 2 轮审查复现的那条路:它正在同步(等下一篇、等网络),主干在另一个
    进程里第一次升级这个库。重开一个连接不算 —— 那是另一回事,早就通了。
    """

    def connection_opened_before_the_upgrade(self) -> sqlite3.Connection:
        """照旧分支起跑那一刻的样子建一个库,然后把连接留在手里。

        旧分支只认到 v13,所以版本号停在那儿;它 schema.sql 里有 `article_fts`,
        这里也有(主干 v18 之后两边一致),所以直接拿主干这份建、再把版本退回去。
        """
        conn = self.opened_by_main()
        conn.execute("DELETE FROM schema_migrations WHERE version > 13")
        conn.commit()
        conn_mod._db_ready = False
        return conn

    def upgrade_in_another_process(self) -> int:
        """另一个连接跑迁移 —— 跟上面那个是不同的连接,互不知情。"""
        other = conn_mod.get_connection()
        try:
            conn_mod.init_database(other)
            return other.execute(
                "SELECT MAX(version) FROM schema_migrations").fetchone()[0]
        finally:
            other.close()

    def test_it_keeps_collecting_right_through_the_migration(self):
        conn = self.connection_opened_before_the_upgrade()
        self.addCleanup(conn.close)
        self.frozen_branch_collects(conn)          # 升级前这一篇
        self.assertEqual(LATEST_SCHEMA_VERSION, self.upgrade_in_another_process())
        # 同一个连接,没重开过:这一篇必须照样进得去
        conn.execute(FROZEN_FTS_DELETE, ("art-2",))
        conn.execute(FROZEN_FTS_INSERT, ("art-2", "第二篇", "摘要", "正文"))
        conn.execute(FROZEN_ARTICLE_INSERT,
                     ("art-2", "审查号", "第二篇", "摘要",
                      "https://mp.weixin.qq.com/s?__biz=BIZ_REVIEW&mid=2&idx=1",
                      "", "", "2026-10-01 11:00"))
        conn.execute(FROZEN_CHECKED_WRITE, ("2026-10-01 11:00", "审查号"))
        conn.commit()
        self.assertEqual(
            2, conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])

    def test_the_index_it_was_writing_to_is_still_there_afterwards(self):
        """表被换成另一张新表也不行:它手里那些行得还在。"""
        conn = self.connection_opened_before_the_upgrade()
        self.addCleanup(conn.close)
        self.frozen_branch_collects(conn)
        self.upgrade_in_another_process()
        self.assertEqual(
            1, conn.execute(
                "SELECT COUNT(*) FROM article_fts WHERE article_id='art-1'").fetchone()[0])


class TheMigrationChainOnlyEverAdds(_Base):
    """口径收成一句话,并且按「整条链跑完有没有东西消失」来验。

    逐个 DROP 去堵是堵不完的(第 1 轮堵了两列,第 2 轮就出在一张表上)。
    """

    def test_no_migration_removes_a_table_or_a_column(self):
        """**逐版**比,不是只比首尾。

        只比首尾的话「v14 删掉、v18 又建回来」看起来毫无变化 —— 可那中间是两次
        独立提交,在途的旧进程正好在那个窗口里写就挂了,而且它升级前写进索引的
        那些行连同表一起没了。所以每过一版都要求「一样都没少」。
        """
        conn = self.opened_by_main()
        self.addCleanup(conn.close)
        shape = self.shape(conn)
        # apply_migrations 一次就把整条链跑完,拿它没法逐版看 —— 直接按版本号顺序
        # 调每一个 _migrate_to_vN(链条里真正跑的就是它们),一版一停。
        for version in range(2, LATEST_SCHEMA_VERSION + 1):
            getattr(migrations, f"_migrate_to_v{version}")(conn)
            conn.commit()
            nxt = self.shape(conn)
            for name, cols in shape.items():
                self.assertIn(name, nxt, f"某一版把 {name} 删掉了(跑到 v{version})")
                self.assertEqual(set(), cols - nxt[name],
                                 f"某一版把 {name} 的列删掉了:{cols - nxt[name]}")
            shape = nxt

    def shape(self, conn: sqlite3.Connection) -> dict[str, set[str]]:
        """每张表(含虚拟表)有哪些列。影子表不算,它们跟着主表走。"""
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'article_fts_%'")]
        return {n: {r[1] for r in conn.execute(f"PRAGMA table_info({n})")}
                for n in names}


if __name__ == "__main__":
    unittest.main()
