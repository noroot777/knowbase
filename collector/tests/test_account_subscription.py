from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
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

        # CLI 的 accounts add 走的就是这一条。重新订阅的是**同一个号**(__biz 不会
        # 变),所以 fakeid 照旧;换一个 fakeid 现在会被拒,见下面那条同名冲突的测试
        self.repo.enable_crawl_account("复现号", "fake-old")
        self.repo.commit()

        names = [a["name"] for a in self.repo.list_accounts(crawl_config_only=False)]
        self.assertIn("复现号", names)
        self.assertEqual("fake-old", self.repo.get_name2fakeid().get("复现号"))
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


class SameNameDifferentAccountTests(unittest.TestCase):
    """两个公众号同名时,第二个不许顶掉第一个的档案。

    `accounts.name` 是主键,`articles.account_name` 是指向它的外键、导出的 vault
    目录也按它归档。原来那句 `ON CONFLICT(name) DO UPDATE SET fakeid=excluded.fakeid`
    会把那一行的归属直接换成新号:外键全都合法、谁也不报错,而第一个号的全部历史
    文章(链接里还是第一个 `__biz`)从此挂在第二个号的身份下,订阅列表看起来只是
    少了一个号(第 2 轮审查实测:两条同名候选都解析出归属之后点第二条,提示
    「已添加」、列表仍只有一条,id 已经换人)。
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

    def shape(self) -> list[tuple[str, str]]:
        return [(r["name"], r["fakeid"]) for r in self.repo.conn.execute(
            "SELECT name, fakeid FROM accounts ORDER BY name").fetchall()]

    def test_the_second_account_is_refused_and_the_first_one_is_untouched(self) -> None:
        from src.db.repository import AccountNameTaken

        self.repo.enable_crawl_account("同名公众号", "biz-first")
        self.repo.conn.execute(
            "INSERT INTO articles (id, account_name, title, create_time, link)"
            " VALUES (?, ?, ?, ?, ?)",
            ("art-1", "同名公众号", "第一个号的旧文章", "2026-09-01 10:00",
             "https://mp.weixin.qq.com/s?__biz=biz-first&mid=1&idx=1"),
        )
        self.repo.commit()

        with self.assertRaises(AccountNameTaken) as caught:
            self.repo.enable_crawl_account("同名公众号", "biz-second")
        self.assertEqual(("biz-first", "biz-second"),
                         (caught.exception.held, caught.exception.wanted))

        self.repo.commit()
        # 库里一个字都没动:归属还是第一个号,旧文章还挂在它名下
        self.assertEqual([("同名公众号", "biz-first")], self.shape())
        self.assertEqual(
            ("同名公众号", "https://mp.weixin.qq.com/s?__biz=biz-first&mid=1&idx=1"),
            tuple(self.repo.conn.execute(
                "SELECT account_name, link FROM articles WHERE id='art-1'").fetchone()),
        )
        self.assertEqual(
            [], self.repo.conn.execute("PRAGMA foreign_key_check").fetchall())

    def test_the_name_frees_up_once_the_wrong_account_is_removed(self) -> None:
        """点错了号有退路:没有历史文章的号移除时整行就删了,名字跟着空出来。

        拒绝不能变成死路——这条路走不通的话,"两个同名号里第一次点错了"就永久
        卡住了,只能手工改库。
        """
        self.repo.enable_crawl_account("同名公众号", "biz-wrong")
        self.repo.commit()
        self.repo.remove_account_from_list("同名公众号")
        self.repo.commit()

        self.assertEqual("同名公众号",
                         self.repo.enable_crawl_account("同名公众号", "biz-right"))
        self.repo.commit()
        self.assertEqual([("同名公众号", "biz-right")], self.shape())

    def test_an_empty_fakeid_does_not_wipe_the_stored_identity(self) -> None:
        """没带归属的调用只是在说"确保这个名字在采集列表里",不是在声明身份。

        原来 `fakeid=excluded.fakeid` 会把已有的 `__biz` 抹成空串。
        """
        self.repo.enable_crawl_account("某个号", "biz-a")
        self.repo.commit()
        self.repo.enable_crawl_account("某个号", "")
        self.repo.commit()
        self.assertEqual([("某个号", "biz-a")], self.shape())

    def race(self, name: str, bizs: tuple[str, str]) -> tuple[list[str], list[str]]:
        """两条**各自独立的连接**同时拿不同 fakeid 抢同一个名字。

        两个 sqlite3 连接在锁上跟两个进程是一回事(没有共享缓存),所以这既是
        「同一个 kb-web 里两个请求」也是「两个独立 CLI 进程」的模型;下面另有
        一条真起两个进程的,把这件事钉死。
        """
        from src.db.repository import AccountNameTaken, ArticleRepository

        ready, won, lost = threading.Barrier(2), [], []

        def add(biz: str) -> None:
            repo = ArticleRepository()
            try:
                ready.wait(timeout=10)
                repo.enable_crawl_account(name, biz)
                repo.commit()
                won.append(biz)
            except AccountNameTaken:
                lost.append(biz)
            finally:
                repo.close()

        threads = [threading.Thread(target=add, args=(b,)) for b in bizs]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        return won, lost

    def test_two_connections_racing_for_one_name_cannot_both_win(self) -> None:
        """并发下也只能有一个号拿到这个档案名。

        第 3 轮审查实测:闸门原来是写之前单独 SELECT 一次,两条连接都读到"没人
        占",随后第二条 UPSERT 把第一条刚写进去的 fakeid 盖掉——**两边都返回成功,
        库里只剩一条归属**,50 组复现 50 组。判定搬进 UPSERT 的 WHERE 之后,
        SQLite 对写入的串行化就是这道闸的互斥。
        """
        for n in range(8):
            name, bizs = f"并发同名号{n}", (f"biz-first-{n}", f"biz-second-{n}")
            won, lost = self.race(name, bizs)
            rows = self.repo.conn.execute(
                "SELECT fakeid FROM accounts WHERE name=?", (name,)).fetchall()
            self.assertEqual((1, 1), (len(won), len(lost)), f"第 {n} 组: {won} / {lost}")
            # 活下来的那一行必须正是报成功的那个号——否则就是"报了成功却被盖掉"
            self.assertEqual([won[0]], [r["fakeid"] for r in rows])

    def test_the_loser_of_a_race_does_not_steal_the_winners_articles(self) -> None:
        """抢输的那一边不许把赢家的历史文章带走。"""
        name = "并发同名号·带文章"
        won, lost = self.race(name, ("biz-keep", "biz-steal"))
        self.assertEqual((1, 1), (len(won), len(lost)))
        self.repo.conn.execute(
            "INSERT INTO articles (id, account_name, title, create_time, link)"
            " VALUES (?, ?, ?, ?, ?)",
            ("art-race", name, "赢家的文章", "2026-09-01 10:00",
             f"https://mp.weixin.qq.com/s?__biz={won[0]}&mid=1&idx=1"))
        self.repo.commit()

        # 输的那个再试一次(用户重试、或者另一个标签页),照旧拒掉
        from src.db.repository import AccountNameTaken, ArticleRepository

        again = ArticleRepository()
        try:
            with self.assertRaises(AccountNameTaken):
                again.enable_crawl_account(name, lost[0])
        finally:
            again.close()
        self.assertEqual(won[0], self.repo.conn.execute(
            "SELECT fakeid FROM accounts WHERE name=?", (name,)).fetchone()[0])
        self.assertEqual(name, self.repo.conn.execute(
            "SELECT account_name FROM articles WHERE id='art-race'").fetchone()[0])

    def test_two_independent_cli_processes_racing_cannot_both_win(self) -> None:
        """两个**真进程**同时跑 `accounts add`,只有一个能拿到这个名字。

        进程内的锁对这条路一点用都没有(面板上那个 tgtBusy 只管单个页面),所以
        闸门必须落在库里。这条直接起两个 `cli accounts add --fakeid`,各自建自己
        的连接、各自提交。
        """
        root = Path(conn_mod.__file__).resolve().parents[2]
        data = Path(self._tmp.name)
        boot = (
            "import sys, pathlib\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "from src.db import connection as c\n"
            f"c.DATA_DIR = pathlib.Path({str(data)!r})\n"
            "c.DEFAULT_DB_PATH = c.DATA_DIR / 'test.db'\n"
            "import cli\n"
            "sys.exit(cli.main(['accounts', 'add', sys.argv[1],"
            " '--fakeid', sys.argv[2], '--json']))\n"
        )
        name = "两个进程抢的号"
        procs = [subprocess.Popen([sys.executable, "-c", boot, name, biz],
                                  cwd=root, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
                 for biz in ("biz-proc-a", "biz-proc-b")]
        outs = [(pr.wait(timeout=60), *pr.communicate()) for pr in procs]
        codes = sorted(code for code, _, _ in outs)
        self.assertEqual([0, 1], codes, outs)
        rows = self.repo.conn.execute(
            "SELECT fakeid FROM accounts WHERE name=?", (name,)).fetchall()
        self.assertEqual(1, len(rows))
        ok = next(out for code, out, _ in outs if code == 0)
        self.assertIn(rows[0]["fakeid"], ok)       # 活下来的正是报成功的那个
        bad = next(out for code, out, _ in outs if code == 1)
        self.assertIn("已经是另一个号的", bad)      # 另一个拿到的是那句冲突提示

    def test_a_name_without_attribution_can_still_be_claimed(self) -> None:
        """归属还没核实的名字也不算"另一个号",真 `__biz` 认出来之后要填得进去。

        和占位键那条是同一类豁免(见 `enable_crawl_account` 那条 WHERE):空
        fakeid 的行没有任何归属可丢,拦下来只会让这个名字永远订阅不了。单独钉
        一条是因为逐句反向验证时发现 `accounts.fakeid = ''` 那一句去掉之后整个
        套件照旧全绿——另外三句各有测试,只有它没有。
        """
        self.repo.enable_crawl_account("待核实的号", "")
        self.repo.commit()
        self.assertEqual([("待核实的号", "")], self.shape())
        self.assertEqual("待核实的号",
                         self.repo.enable_crawl_account("待核实的号", "biz-real"))
        self.repo.commit()
        self.assertEqual([("待核实的号", "biz-real")], self.shape())

    def test_an_import_placeholder_can_still_be_claimed(self) -> None:
        """链接导入留下的占位键不是"另一个号",别把这类存量账号永久锁死。"""
        self.repo.upsert_import_account("导入过的号")
        self.repo.commit()
        self.assertEqual("导入过的号",
                         self.repo.enable_crawl_account("导入过的号", "biz-real"))
        self.repo.commit()
        self.assertEqual([("导入过的号", "biz-real")], self.shape())


if __name__ == "__main__":
    unittest.main()
