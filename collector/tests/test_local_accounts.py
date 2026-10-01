"""订阅候选来自本机绿泡泡会话,不需要扫码登录公众平台。

钉住的几件事:归属优先从发现索引里查(一条推送都不翻)、裸列时绝不现翻、
给了查询词才现翻且有上限、已订阅按 __biz 认(改过名也认得出)、单个会话
读不动不让整次搜索失败、按名字添加不去碰公众平台。
"""

from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

from src.collect import local_accounts
from src.collect.local_accounts import _table
from src.crawler.local_wechat import LocalReader, LocalReaderError
from src.db import connection as conn_mod


def push(biz="biz-a", mid="101", idx="1", published="2026-09-15 18:30", **extra):
    url = (f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}"
           f"&sn=signature&chksm=checksum")
    fields = {"title": "测试文章", "url": url, "digest": "摘要", "cover": "",
              "pub_time": str(int(datetime.strptime(published, "%Y-%m-%d %H:%M").timestamp())),
              **extra}
    return {"text": "<msg><appmsg><mmreader><category><item>"
            + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in fields.items())
            + "</item></category></mmreader></appmsg></msg>", "create_time": 1}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready)
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = (
            root / "test.db", root, False)
        from src.db.repository import ArticleRepository

        self.repo = ArticleRepository()
        self._seq = 0
        # 库里存的是旧名字,微信里这个号已经改叫「订阅号·新名」——
        # 「已订阅」必须按 biz 认出来,否则用户会照新名字再加一遍。
        self.repo.enable_crawl_account("订阅号·旧名", "biz-sub")
        self.repo.commit()
        self.reader = LocalReader()
        self.reader.check = Mock()
        self.reader.accounts = Mock(return_value=[
            {"username": "gh_sub", "display_name": "订阅号·新名", "last_timestamp": 300},
            {"username": "gh_indexed", "display_name": "索引里的号", "last_timestamp": 200},
            {"username": "gh_fresh", "display_name": "索引外的号", "last_timestamp": 100},
        ])
        self.reader.messages = Mock(return_value=[push(biz="biz-fresh")])
        self.index("gh_sub", "biz-sub")
        self.index("gh_indexed", "biz-indexed")

    def tearDown(self):
        self.repo.close()
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self.saved
        self.tmp.cleanup()

    def index(self, username, biz, *, count=1):
        for _ in range(count):
            self._seq += 1
            self.repo.conn.execute(
                "INSERT INTO local_wechat_articles (source_key, id, biz, create_time,"
                " message_time, local_id, article_json) VALUES (?,?,?,'2026-09-15 18:30',1,?,'{}')",
                (f"biz_message_0.db:{_table(username)}", f"a{self._seq}", biz, self._seq))
        self.repo.commit()

    def discover(self, query="", **kwargs):
        return local_accounts.discover(self.repo, query, reader=self.reader, **kwargs)

    def find(self, rows, name):
        return next(r for r in rows if r["name"] == name)


class DiscoverTests(_Base):
    def test_index_answers_attribution_without_reading_any_push(self):
        rows = self.discover("")
        self.reader.messages.assert_not_called()
        self.assertEqual("biz-indexed", self.find(rows, "索引里的号")["fakeid"])
        self.assertEqual("biz-sub", self.find(rows, "订阅号·新名")["fakeid"])
        # 索引外的那个在裸列时留空:翻它要一次子进程调用,280 个会话就是几分钟
        self.assertEqual("", self.find(rows, "索引外的号")["fakeid"])
        self.assertEqual(["订阅号·新名", "索引里的号", "索引外的号"],
                         [r["name"] for r in rows])   # 最近有推送的排前面

    def test_subscription_is_matched_by_biz_so_a_renamed_account_is_not_added_twice(self):
        row = self.find(self.discover(""), "订阅号·新名")
        self.assertTrue(row["subscribed"])
        self.assertEqual("订阅号·旧名", row["stored_name"])
        self.assertFalse(self.find(self.discover(""), "索引里的号")["subscribed"])

    def test_a_query_resolves_the_ones_missing_from_the_index(self):
        rows = self.discover("索引外")
        self.assertEqual(["索引外的号"], [r["name"] for r in rows])
        self.assertEqual("biz-fresh", rows[0]["fakeid"])
        self.assertEqual(1, self.reader.messages.call_count)

    def test_live_resolution_is_capped_so_a_broad_query_stays_interactive(self):
        self.reader.accounts.return_value = [
            {"username": f"gh_{n}", "display_name": f"候选 {n}", "last_timestamp": n}
            for n in range(30)]
        rows = self.discover("候选", resolve_limit=4)
        self.assertEqual(30, len(rows))
        self.assertEqual(4, self.reader.messages.call_count)
        self.assertEqual(4, sum(1 for r in rows if r["fakeid"]))

    def test_matching_is_case_insensitive_and_also_accepts_the_session_id(self):
        self.reader.accounts.return_value = [
            {"username": "gh_abc", "display_name": "LatePost", "last_timestamp": 1}]
        self.index("gh_abc", "biz-late")
        self.assertEqual(["LatePost"], [r["name"] for r in self.discover("latepost")])
        self.assertEqual(["LatePost"], [r["name"] for r in self.discover("GH_ABC")])
        self.assertEqual([], self.discover("别的号"))

    def test_one_unreadable_session_does_not_fail_the_whole_search(self):
        self.reader.messages.side_effect = LocalReaderError("这个会话读不动")
        rows = self.discover("的号")
        self.assertEqual("", self.find(rows, "索引外的号")["fakeid"])
        self.assertEqual("biz-indexed", self.find(rows, "索引里的号")["fakeid"])

    def test_a_broken_push_is_skipped_instead_of_killing_the_session(self):
        self.reader.messages.return_value = [{"text": "<mmreader>"}, push(biz="biz-fresh")]
        self.assertEqual("biz-fresh", self.discover("索引外")[0]["fakeid"])

    def test_database_must_be_readable_before_any_candidate_is_listed(self):
        self.reader.check.side_effect = LocalReaderError("绿泡泡数据库尚未就绪")
        with self.assertRaises(LocalReaderError):
            self.discover("")
        self.reader.accounts.assert_not_called()

    def test_the_busiest_biz_wins_when_one_session_maps_to_several(self):
        self.index("gh_indexed", "biz-stray")
        self.index("gh_indexed", "biz-indexed", count=3)
        self.assertEqual("biz-indexed", self.find(self.discover(""), "索引里的号")["fakeid"])


class AddByNameTests(_Base):
    def add(self, *argv):
        import cli

        with contextlib.redirect_stderr(io.StringIO()) as err:
            code = cli.main(["accounts", "add", *argv])
        return code, err.getvalue()

    def test_adding_by_name_resolves_locally_and_never_touches_the_platform(self):
        import api

        # 公众平台那条路连函数都不剩了:加号从此只认本机会话,没有凭证可过期
        self.assertFalse(hasattr(api, "_search_biz"))
        with patch("src.collect.local_accounts.LocalReader", return_value=self.reader), \
             patch("requests.get", side_effect=AssertionError("加号不该走网络")):
            code, _ = self.add("索引里的号")
        self.assertEqual(0, code)
        self.assertEqual(("索引里的号", "biz-indexed"), self.repo.conn.execute(
            "SELECT name, fakeid FROM accounts WHERE fakeid='biz-indexed'").fetchone()[:2])

    def test_an_account_with_no_parsable_push_reports_why_instead_of_being_added(self):
        """「有会话但没有图文」和「压根没这个号」是两回事,说错了用户照着做也没用。"""
        self.reader.messages.return_value = [{"text": "服务通知"}]
        with patch("src.collect.local_accounts.LocalReader", return_value=self.reader):
            code, out = self.add("索引外的号")
            self.assertEqual(1, code)
            self.assertIn("还没有能解析出归属的图文推送", out)
            missing = self.add("从来没关注过的号")
        self.assertEqual(1, missing[0])
        self.assertIn("本机没有名叫", missing[1])
        self.assertIsNone(self.repo.conn.execute(
            "SELECT 1 FROM accounts WHERE name='索引外的号'").fetchone())

    def test_two_sessions_with_the_same_name_require_an_explicit_fakeid(self):
        self.reader.accounts.return_value = [
            {"username": "gh_one", "display_name": "同名号", "last_timestamp": 2},
            {"username": "gh_two", "display_name": "同名号", "last_timestamp": 1}]
        self.index("gh_one", "biz-one")
        self.index("gh_two", "biz-two")
        with patch("src.collect.local_accounts.LocalReader", return_value=self.reader):
            code, out = self.add("同名号")
            self.assertEqual((1, True), (code, "--fakeid" in out))
            self.assertEqual(0, self.add("同名号", "--fakeid", "biz-two")[0])
        self.assertEqual("biz-two", self.repo.conn.execute(
            "SELECT fakeid FROM accounts WHERE name='同名号'").fetchone()[0])


class CliDiscoverTests(_Base):
    """面板那一侧按 `--json` 的最后一行解析,候选字段得和原通道的 search 同形。"""

    def run_cli(self, *argv):
        import cli

        out, err = io.StringIO(), io.StringIO()
        with patch("src.collect.local_accounts.LocalReader", return_value=self.reader), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["accounts", "discover", *argv])
        return code, out.getvalue(), err.getvalue()

    def test_json_candidates_carry_the_same_fields_the_old_search_did(self):
        import json

        code, out, _ = self.run_cli("索引里", "--json")
        payload = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(0, code)
        self.assertEqual([{"name": "索引里的号", "fakeid": "biz-indexed",
                           "alias": "gh_indexed", "signature": "最近推送 1970-01-01",
                           "subscribed": False, "stored_name": ""}],
                         payload["candidates"])

    def test_an_already_subscribed_renamed_account_says_which_key_it_is_stored_under(self):
        import json

        payload = json.loads(self.run_cli("订阅号", "--json")[1].strip().splitlines()[-1])
        candidate = payload["candidates"][0]
        self.assertEqual((True, "订阅号·旧名"),
                         (candidate["subscribed"], candidate["stored_name"]))
        self.assertIn("订阅号·旧名", candidate["signature"])

    def test_the_human_listing_marks_which_ones_can_actually_be_added(self):
        code, _, err = self.run_cli()
        self.assertEqual(0, code)
        self.assertIn("其中 1 个可以添加", err)      # 已订阅的和无图文的都不算
        self.assertIn("本机暂无可解析的图文推送", err)

    def test_an_unreadable_database_fails_loudly_instead_of_listing_nothing(self):
        self.reader.check.side_effect = LocalReaderError("绿泡泡数据库尚未就绪")
        code, _, err = self.run_cli("索引里")
        self.assertEqual(2, code)
        self.assertIn("尚未就绪", err)
