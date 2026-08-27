from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
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

    def _age_the_check(self, days: int = 8) -> None:
        """把"上次核对时间"往前推,模拟节流窗口已经过去。"""
        old = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
        self.repo.conn.execute(
            "UPDATE accounts SET display_checked_at=? WHERE name='旧名字'", (old,))
        self.repo.commit()

    def _run(self, nickname: str = "新名字", *, boom: bool = False, by_link=None):
        """跑一次核对。by_link 给"哪条链接返回什么昵称",没给就一律返回 nickname。"""
        from src.collect import runner

        calls = []

        def fetch(link, num=0):
            calls.append(link)
            if boom:
                raise RuntimeError("凭证没了")
            got = (by_link or {}).get(link, nickname) if by_link else nickname
            return "正文", "<p>正文</p>", got

        with mock.patch("src.utils.wechat_body.fetch_article_body_full", fetch):
            runner._refresh_display_name(self.repo, "旧名字")
        self.repo.commit()
        return calls

    def _add_article(self, aid: str, link: str, when: str, *,
                     wx_deleted: int = 0, user_deleted: int = 0) -> None:
        self.repo.conn.execute(
            "INSERT INTO articles (id, account_name, title, link, create_time, "
            "is_wx_deleted, is_user_deleted) VALUES (?, '旧名字', 't', ?, ?, ?, ?)",
            (aid, link, when, wx_deleted, user_deleted))
        self.repo.commit()

    def test_deleted_article_does_not_block_the_refresh(self) -> None:
        """最新那篇被微信删了,不能就此卡住——真实库里就有这么一个号。

        已删文章的链接打开是"该内容已被发布者删除",页面上没有 var nickname。
        多图文的次条被删尤其常见,而它和头条 create_time 一样、排在头条前面。
        """
        self._add_article("a2", "https://bad.example", "2026-08-28 12:00",
                          wx_deleted=1)
        self._add_article("a3", "https://good.example", "2026-08-28 11:00")

        calls = self._run(nickname="新名字")
        self.assertNotIn("https://bad.example", calls)   # 压根不该去请求它
        self.assertEqual("新名字", self._display())

    def test_falls_back_to_an_older_article_when_the_page_has_no_nickname(self) -> None:
        """打开了但读不出昵称(刚被删还没标上),往下一篇退,而不是这轮放弃。"""
        self._add_article("a2", "https://blank.example", "2026-08-28 12:00")
        self._add_article("a3", "https://good.example", "2026-08-28 11:00")

        calls = self._run(by_link={"https://blank.example": "",
                                   "https://good.example": "新名字"})
        self.assertEqual(["https://blank.example", "https://good.example"], calls)
        self.assertEqual("新名字", self._display())

    def test_fallback_is_bounded(self) -> None:
        """备选有上限,不能一个号把整本历史翻一遍。"""
        from src.collect import runner

        for i in range(6):
            self._add_article(f"b{i}", f"https://blank{i}.example",
                              f"2026-08-2{i} 12:00")
        calls = self._run(nickname="")
        self.assertEqual(runner._NICKNAME_CANDIDATES, len(calls))
        self.assertEqual("", self._display())            # 全没读到,不记核对时间

    def test_picks_up_the_new_name_without_a_new_article(self) -> None:
        self.assertEqual(1, len(self._run()))
        self.assertEqual("新名字", self._display())

    def test_second_run_costs_nothing(self) -> None:
        """核对过就别再抓:44 个号每轮各白抓一篇正文是实打实的开销。"""
        self._run()
        self.assertEqual([], self._run())

    def test_recheck_finds_a_later_rename(self) -> None:
        """节流到期后要能发现第二次改名。

        只看 display_name 空不空的话,补上一次之后就再也不抓第二次——停更号
        以后再改名就永远停在旧显示名上,而停更号正是这条路径要救的对象。
        """
        self._run(nickname="第一次改名")
        self.assertEqual("第一次改名", self._display())
        self.assertEqual([], self._run())            # 还在节流窗口里

        self._age_the_check()
        self.assertEqual(1, len(self._run(nickname="第二次改名")))
        self.assertEqual("第二次改名", self._display())

    def test_recheck_interval_is_tunable(self) -> None:
        self._run()
        with mock.patch.dict(os.environ, {"GZH_DISPLAY_RECHECK_DAYS": "0"}):
            # 下限是 1 天,乱填/填 0 都不会退化成每轮都抓
            self.assertEqual([], self._run())
        self._age_the_check()
        with mock.patch.dict(os.environ, {"GZH_DISPLAY_RECHECK_DAYS": "30"}):
            self.assertEqual([], self._run())        # 30 天窗口里,8 天前核对过不算过期

    def test_unreadable_timestamp_is_treated_as_never_checked(self) -> None:
        self._run()
        self.repo.conn.execute(
            "UPDATE accounts SET display_checked_at='不是时间' WHERE name='旧名字'")
        self.repo.commit()
        self.assertEqual(1, len(self._run(nickname="又改了")))
        self.assertEqual("又改了", self._display())

    def test_unchanged_name_is_still_throttled(self) -> None:
        """昵称跟主键一样也要记一笔核对时间,否则这个号每轮都会重新抓。"""
        self._run(nickname="旧名字")
        self.assertEqual("旧名字", self._display())
        self.assertEqual([], self._run())

    def test_unreadable_nickname_does_not_count_as_checked(self) -> None:
        """页面读不出昵称就不算核对过,下一轮还要再试。"""
        self._run(nickname="")
        self.assertEqual("", self._display())
        self.assertEqual(1, len(self._run(nickname="新名字")))

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
