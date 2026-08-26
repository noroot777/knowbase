from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.db import connection as conn_mod


class AccountSubscriptionTests(unittest.TestCase):
    """订阅列表的移除 / 重新添加。

    这两件事之所以值得单独测:移除一个有历史文章的号不能真删 accounts 那一行
    (articles.account_name 是指向它的外键,删了就是一堆孤儿),所以实现是打
    removed 标记;而一旦有了这个标记,"重新添加"就必须记得把它清掉——漏了的话
    用户会看到"已添加"的成功提示,号却仍然不在列表、不在同步目标、也不会被采集。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._saved = (conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready)
        conn_mod.DEFAULT_DB_PATH = tmp / "test.db"
        conn_mod.DATA_DIR = tmp
        conn_mod._db_ready = False

        from src.db.repository import ArticleRepository

        self.repo = ArticleRepository()

    def tearDown(self) -> None:
        self.repo.close()
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self._saved
        self._tmp.cleanup()

    def _add_article(self, account: str, article_id: str) -> None:
        self.repo.conn.execute(
            "INSERT INTO articles (id, account_name, title, create_time) VALUES (?, ?, ?, ?)",
            (article_id, account, "标题", "2026-08-26 12:00"),
        )
        self.repo.commit()

    def test_remove_keeps_articles_and_foreign_keys_intact(self) -> None:
        self.repo.enable_crawl_account("有文章的号", "fake-1")
        self.repo.commit()
        self._add_article("有文章的号", "art-1")

        self.repo.remove_account_from_list("有文章的号")
        self.repo.commit()

        names = [a["name"] for a in self.repo.list_accounts(crawl_config_only=False)]
        self.assertNotIn("有文章的号", names)
        self.assertNotIn("有文章的号", self.repo.get_name2fakeid())
        self.assertFalse(self.repo.account_in_crawl_config("有文章的号"))
        # 文章还在,而且没有变成孤儿
        self.assertEqual(
            1,
            self.repo.conn.execute(
                "SELECT COUNT(*) FROM articles WHERE account_name=?", ("有文章的号",)
            ).fetchone()[0],
        )
        self.assertEqual([], self.repo.conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_readd_after_remove_makes_account_visible_again(self) -> None:
        self.repo.enable_crawl_account("复现号", "fake-old")
        self.repo.commit()
        self._add_article("复现号", "art-2")
        self.repo.remove_account_from_list("复现号")
        self.repo.commit()

        # CLI 的 accounts add 走的就是这一条
        self.repo.enable_crawl_account("复现号", "fake-new")
        self.repo.commit()

        names = [a["name"] for a in self.repo.list_accounts(crawl_config_only=False)]
        self.assertIn("复现号", names)
        self.assertEqual("fake-new", self.repo.get_name2fakeid().get("复现号"))
        self.assertTrue(self.repo.account_in_crawl_config("复现号"))

    def test_remove_account_without_articles_is_deleted_outright(self) -> None:
        self.repo.enable_crawl_account("空号", "fake-2")
        self.repo.commit()
        self.repo.remove_account_from_list("空号")
        self.repo.commit()

        # 一篇文章都没有,没必要留一行隐身记录
        self.assertIsNone(
            self.repo.conn.execute(
                "SELECT 1 FROM accounts WHERE name=?", ("空号",)
            ).fetchone()
        )

    def test_crawl_time_upsert_does_not_resurrect_removed_account(self) -> None:
        """采集过程里的 upsert_account 不该把已移除的号悄悄弄回来。"""
        self.repo.enable_crawl_account("已移除的号", "fake-3")
        self.repo.commit()
        self._add_article("已移除的号", "art-3")
        self.repo.remove_account_from_list("已移除的号")
        self.repo.commit()

        self.repo.upsert_account("已移除的号", "fake-3", "2026-08-26 13:00")
        self.repo.commit()

        self.assertNotIn("已移除的号", self.repo.get_name2fakeid())


if __name__ == "__main__":
    unittest.main()
