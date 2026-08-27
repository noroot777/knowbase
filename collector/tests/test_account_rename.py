from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.db import connection as conn_mod
from src.utils.wechat_body import extract_account_nickname


class NicknameExtractionTests(unittest.TestCase):
    """从正文页里认出"这篇文章是哪个号发的"。

    公众号改名后,新名字只出现在正文页里:列表接口 getmsg 的返回没有昵称,
    wxdown 截下来的凭证里 name 字段恒为 null。所以这个提取一旦认错号,
    面板上的名字就会被写成别人的。
    """

    def test_var_nickname_htmldecode(self) -> None:
        html = 'x<script>var nickname = htmlDecode("GitHub猿");</script>'
        self.assertEqual("GitHub猿", extract_account_nickname(html))

    def test_var_nickname_plain_quotes(self) -> None:
        html = "var nickname = '程序员成长指北';"
        self.assertEqual("程序员成长指北", extract_account_nickname(html))

    def test_js_name_anchor_as_fallback(self) -> None:
        html = '<a id="js_name" class="profile_nickname">\n   某某号 &amp; Co  </a>'
        self.assertEqual("某某号 & Co", extract_account_nickname(html))

    def test_bare_nickname_key_is_not_trusted(self) -> None:
        """页里还塞着推荐位/关联号的昵称,不能拿裸的 `nickname:` 当本号。

        实测 GitHub猿 那篇文章,页面里第一个 `nickname: '…'` 是"编程技术圈"
        ——另一个号。只认 var nickname / js_name 这两个明确指向本号的位置。
        """
        html = (
            "<script>var d = { nickname: '编程技术圈', biz: 'other' };</script>"
            '<script>var nickname = htmlDecode("GitHub猿");</script>'
        )
        self.assertEqual("GitHub猿", extract_account_nickname(html))

    def test_nothing_to_find(self) -> None:
        self.assertEqual("", extract_account_nickname(""))
        self.assertEqual("", extract_account_nickname("<html><body>x</body></html>"))
        # 空昵称不算数,否则会把好好的名字覆盖成空
        self.assertEqual("", extract_account_nickname('var nickname = htmlDecode("");'))


class DisplayNameTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._saved = (conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready)
        conn_mod.DEFAULT_DB_PATH = tmp / "test.db"
        conn_mod.DATA_DIR = tmp
        conn_mod._db_ready = False

        from src.db.repository import ArticleRepository

        self.repo = ArticleRepository()
        self.repo.enable_crawl_account("旧名字", "fake-1")
        self.repo.commit()

    def tearDown(self) -> None:
        self.repo.close()
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self._saved
        self._tmp.cleanup()

    def _display(self, name: str = "旧名字") -> str:
        row = self.repo.conn.execute(
            "SELECT display_name FROM accounts WHERE name=?", (name,)
        ).fetchone()
        return row["display_name"]

    def test_rename_is_recorded_without_touching_the_key(self) -> None:
        self.repo.conn.execute(
            "INSERT INTO articles (id, account_name, title, create_time) "
            "VALUES (?, ?, ?, ?)",
            ("art-1", "旧名字", "标题", "2026-08-27 10:00"),
        )
        self.repo.commit()

        self.assertTrue(self.repo.set_account_display_name("旧名字", "新名字"))
        self.repo.commit()

        self.assertEqual("新名字", self._display())
        # 主键没动,文章也就还挂得住——外键没有 ON UPDATE CASCADE,动了就是孤儿
        self.assertEqual(
            "旧名字",
            self.repo.conn.execute(
                "SELECT account_name FROM articles WHERE id='art-1'"
            ).fetchone()[0],
        )
        self.assertEqual([], self.repo.conn.execute("PRAGMA foreign_key_check").fetchall())
        self.assertIn("旧名字", self.repo.get_name2fakeid())

    def test_repeat_write_is_a_noop(self) -> None:
        """名字没变就别写:每篇文章都翻一次 updated_at 是白写。"""
        self.assertTrue(self.repo.set_account_display_name("旧名字", "新名字"))
        self.repo.commit()
        self.assertFalse(self.repo.set_account_display_name("旧名字", "新名字"))

    def test_blank_and_unknown_account_are_ignored(self) -> None:
        # 读不到昵称时不能把已有的名字冲掉
        self.assertTrue(self.repo.set_account_display_name("旧名字", "新名字"))
        self.repo.commit()
        self.assertFalse(self.repo.set_account_display_name("旧名字", ""))
        self.assertFalse(self.repo.set_account_display_name("旧名字", "   "))
        self.assertEqual("新名字", self._display())
        # 链接导入的文章不属于任何订阅号,别为它凭空插一行
        self.assertFalse(self.repo.set_account_display_name("查无此号", "随便"))

    def test_list_accounts_carries_display_name(self) -> None:
        self.repo.set_account_display_name("旧名字", "新名字")
        self.repo.commit()
        row = next(a for a in self.repo.list_accounts() if a["name"] == "旧名字")
        self.assertEqual("新名字", row["display_name"])


class ReSubscribeByNewNameTests(unittest.TestCase):
    """改名后按新名字添加,不能变成第二条订阅。

    这是改名功能自己造出来的路径:列表上还是旧名、微信里搜不到旧名,用户
    只能拿新名字来搜索添加。要是照新名字再插一行,库里就有两条同 fakeid 的
    订阅——同一个号的新文章分别写进两套主键,导出的 vault 目录跟着裂开,
    外键还全都合法,谁也不会报错。
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

    def _rows(self) -> list[tuple[str, str, str]]:
        return [(r["name"], r["fakeid"], r["display_name"]) for r in self.repo.conn.execute(
            "SELECT name, fakeid, display_name FROM accounts ORDER BY name")]

    def test_same_fakeid_new_name_updates_instead_of_inserting(self) -> None:
        self.repo.enable_crawl_account("旧名字", "fake-same")
        self.repo.commit()

        stored = self.repo.enable_crawl_account("新名字", "fake-same")
        self.repo.commit()

        self.assertEqual("旧名字", stored)          # 档案键没变,调用方据此给文案
        self.assertEqual([("旧名字", "fake-same", "新名字")], self._rows())
        # 采集目标也只有一个,不会同一个号采两遍写进两套主键
        self.assertEqual({"旧名字": "fake-same"}, self.repo.get_name2fakeid())

    def test_removed_account_is_revived_not_duplicated(self) -> None:
        """移除过的号按新名字重新添加,该复活旧行。"""
        self.repo.enable_crawl_account("旧名字", "fake-same")
        self.repo.conn.execute(
            "INSERT INTO articles (id, account_name, title, create_time) "
            "VALUES ('a1', '旧名字', 't', '2026-08-27 10:00')")
        self.repo.commit()
        self.repo.remove_account_from_list("旧名字")
        self.repo.commit()

        stored = self.repo.enable_crawl_account("新名字", "fake-same")
        self.repo.commit()

        self.assertEqual("旧名字", stored)
        row = self.repo.conn.execute(
            "SELECT removed, in_crawl_config FROM accounts WHERE name='旧名字'").fetchone()
        self.assertEqual((0, 1), (row["removed"], row["in_crawl_config"]))
        self.assertEqual(1, len(self._rows()))

    def test_genuinely_new_account_still_inserts(self) -> None:
        """别矫枉过正:fakeid 不同就是另一个号,照常新增。"""
        self.repo.enable_crawl_account("甲号", "fake-a")
        stored = self.repo.enable_crawl_account("乙号", "fake-b")
        self.repo.commit()
        self.assertEqual("乙号", stored)
        self.assertEqual(2, len(self._rows()))

    def test_same_name_same_fakeid_is_idempotent(self) -> None:
        self.repo.enable_crawl_account("甲号", "fake-a")
        stored = self.repo.enable_crawl_account("甲号", "fake-a")
        self.repo.commit()
        self.assertEqual("甲号", stored)
        # 名字没变就别往 display_name 里塞东西,免得列表凭空多出"已改名"那行
        self.assertEqual([("甲号", "fake-a", "")], self._rows())

    def test_blank_fakeid_falls_back_to_name(self) -> None:
        """没有 fakeid 就没法认号,只能按名字走老路,不能瞎认。"""
        self.repo.enable_crawl_account("甲号", "")
        stored = self.repo.enable_crawl_account("乙号", "")
        self.repo.commit()
        self.assertEqual("乙号", stored)
        self.assertEqual(2, len(self._rows()))


class RefreshDisplayNameTests(unittest.TestCase):
    """不发新文章的号也得能改名。

    改名原本只搭在"抓新文章正文"这趟车上,可最需要看到新名字的恰恰是那些
    安静下来、又在微信里改了名的号——它们永远等不到那趟车。
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
        self.repo.enable_crawl_account("旧名字", "fake-1")
        self.repo.conn.execute(
            "INSERT INTO articles (id, account_name, title, link, create_time) "
            "VALUES ('a1', '旧名字', 't', 'https://mp.weixin.qq.com/s?x=1', "
            "'2026-08-27 10:00')")
        self.repo.commit()

    def tearDown(self) -> None:
        self.repo.close()
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self._saved
        self._tmp.cleanup()

    def _display(self) -> str:
        return self.repo.conn.execute(
            "SELECT display_name FROM accounts WHERE name='旧名字'").fetchone()[0]

    def _run(self, nickname: str = "新名字", *, boom: bool = False):
        from src.collect import runner

        calls = []

        def fake_fetch(link, num=0):
            calls.append(link)
            if boom:
                raise RuntimeError("凭证没了")
            return "正文", "<p>正文</p>", nickname

        with mock.patch("src.utils.wechat_body.fetch_article_body_full", fake_fetch):
            runner._refresh_display_name(self.repo, "旧名字")
        self.repo.commit()
        return calls

    def test_picks_up_the_new_name_without_a_new_article(self) -> None:
        self.assertEqual(1, len(self._run()))
        self.assertEqual("新名字", self._display())

    def test_second_run_costs_nothing(self) -> None:
        """核对过就别再抓:44 个号每轮各白抓一篇正文是实打实的开销。"""
        self._run()
        self.assertEqual([], self._run())

    def test_unchanged_name_is_still_recorded(self) -> None:
        """昵称跟主键一样也要写进去,否则这个号每轮都会重新抓一遍。"""
        self._run(nickname="旧名字")
        self.assertEqual("旧名字", self._display())
        self.assertEqual([], self._run())

    def test_failure_is_swallowed(self) -> None:
        """抓不动是常事(凭证过期/限流),不能让它把这一轮采集带崩。"""
        self._run(boom=True)
        self.assertEqual("", self._display())

    def test_account_without_articles_is_skipped(self) -> None:
        """新订阅还没采过的号没有正文可读,跳过就是了。"""
        from src.collect import runner

        self.repo.enable_crawl_account("空号", "fake-2")
        self.repo.commit()
        calls = []
        # 别用 side_effect 抛异常来断言"没被调用":_refresh_display_name 捕获
        # Exception,AssertionError 也是 Exception,会被吞掉,测试就成了假通过
        with mock.patch("src.utils.wechat_body.fetch_article_body_full",
                        lambda link, num=0: calls.append(link) or ("", "", "")):
            runner._refresh_display_name(self.repo, "空号")
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
