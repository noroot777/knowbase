"""覆盖诊断回归：正常未同步、历史文章、未知归属、同名冲突。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.local_source_fixture import SqlReader
from tests.test_local_wechat import push
from src.collect.local_runner import collect
from src.crawler.local_wechat import LocalReaderError
from src.db import connection
from src.db.repository import ArticleRepository


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = connection.DATA_DIR, connection.DEFAULT_DB_PATH, connection._db_ready
        connection.DATA_DIR = Path(self.tmp.name)
        connection.DEFAULT_DB_PATH = connection.DATA_DIR / "collector.db"
        connection._db_ready = False
        self.repo = ArticleRepository()
        self.repo.enable_crawl_account("甲", "biz-a")
        self.repo.commit()
        self.reader = SqlReader()
        self.events = []

    def tearDown(self):
        self.reader.close()
        self.repo.close()
        connection.DATA_DIR, connection.DEFAULT_DB_PATH, connection._db_ready = self.saved
        self.tmp.cleanup()

    def run_collect(self, **kwargs):
        self.events.clear()
        # 只测发现和诊断，不请求正文或写真实数据。
        with patch("src.collect.local_runner._download"):
            return collect(self.events.append, reader=self.reader, **kwargs)

    def test_absent_accounts_are_normal_missing_even_alongside_known_bad_pushes(self):
        for i in range(14):
            self.repo.enable_crawl_account(f"未同步{i}", f"absent-{i}")
        self.repo.commit()
        self.reader.add("gh_a", [push(pub_time="invalid")])
        for _ in range(2):
            result = self.run_collect()
            self.assertEqual([], result["errors"])
            self.assertEqual(14, len(result["missing_accounts"]))
            self.assertEqual([], result["coverage_unknown_accounts"])
            self.assertEqual(0, result["unattributed_messages"])
            self.assertEqual([{"name": "甲", "messages": 1, "identity_confirmed": True}],
                             result["unreadable_accounts"])

    def test_completed_discovery_updates_both_normal_and_unreadable_accounts_without_new_articles(self):
        self.repo.enable_crawl_account("乙", "biz-b")
        self.repo.commit()
        self.reader.add("gh_a", [push()])
        self.reader.add("gh_b", [push(biz="biz-b")])
        self.run_collect()
        self.repo.conn.execute("UPDATE accounts SET latest_crawl_at='2020-01-01 00:00'")
        self.repo.commit()
        self.reader.add("gh_a", [{"text": "<mmreader>"}] * 5)
        for i in range(2):
            now = f"2026-09-16 1{i}:00"
            with patch("src.utils.helpers.time_now", return_value=now):
                result = self.run_collect(since="2026-09-16", until="2026-09-16")
            self.assertEqual([], result["errors"])
            self.assertEqual(5, result["unreadable_messages"])
            self.assertEqual([{"name": "甲", "messages": 5, "identity_confirmed": True}],
                             result["unreadable_accounts"])
            times = dict(self.repo.conn.execute("SELECT name,latest_crawl_at FROM accounts"))
            self.assertEqual({"甲": now, "乙": now}, times)
        self.assertEqual(0, next(e for e in reversed(self.events)
                                if e.get("phase") == "discovery")["scanned_messages"])
        # 修复这些消息后清除诊断，时间仍按本次完成检查更新。
        self.reader.add("gh_a", [{**push(idx=str(i)), "local_id": i} for i in range(2, 7)])
        result = self.run_collect()
        self.assertEqual([], result["unreadable_accounts"])
        self.assertEqual(0, result["unreadable_messages"])
        self.assertNotEqual("2020-01-01 00:00", self.repo.conn.execute(
            "SELECT latest_crawl_at FROM accounts WHERE name='甲'").fetchone()[0])

    def test_unknown_identity_is_counted_once_without_exposing_session_name(self):
        self.reader.add("gh_renamed", [{"text": b"\xff"}, {"text": "broken", "compression": 4},
                                       {"text": push()["text"] + push(biz="biz-b")["text"]}])
        self.reader.sessions[0]["display_name"] = "未核实的新名字"
        for _ in range(2):
            result = self.run_collect()
            self.assertEqual([], result["errors"])
            self.assertEqual(["甲"], result["missing_accounts"])
            self.assertEqual([], result["coverage_unknown_accounts"])
            self.assertEqual([], result["unreadable_accounts"])
            self.assertEqual(0, result["unreadable_messages"])
            self.assertEqual(3, result["unattributed_messages"])
            self.assertNotIn("未核实的新名字", str(self.events))

    def test_unknown_stranger_push_does_not_change_fourteen_missing_accounts(self):
        for i in range(14):
            self.repo.enable_crawl_account(f"未同步{i}", f"absent-{i}")
        self.repo.commit()
        self.reader.add("gh_a", [push()])
        baseline = self.run_collect()
        self.assertEqual(14, len(baseline["missing_accounts"]))
        self.reader.add("gh_stranger", [{"text": b"\xff"}])
        self.reader.sessions[-1]["display_name"] = "无关的陌生号"
        for _ in range(2):
            result = self.run_collect()
            self.assertEqual([], result["errors"])
            self.assertEqual(baseline["missing_accounts"], result["missing_accounts"])
            self.assertEqual([], result["coverage_unknown_accounts"])
            self.assertEqual([], result["unreadable_accounts"])
            self.assertEqual(1, result["unattributed_messages"])
            self.assertNotIn("无关的陌生号", str(self.events))

    def test_permanent_issue_does_not_freeze_later_successful_article_discovery(self):
        self.repo.enable_crawl_account("乙", "biz-b")
        self.repo.commit()
        self.reader.add("gh_a", [push(), {"text": "<mmreader>"}])
        self.reader.add("gh_b", [push(biz="biz-b")])
        self.run_collect()
        for i in range(1, 4):
            self.reader.add("gh_a", [push(mid=str(1000 + i))])
            self.reader.add("gh_b", [push(biz="biz-b", mid=str(1000 + i))])
            now = f"2026-09-16 00:0{i}"
            with patch("src.utils.helpers.time_now", return_value=now):
                result = self.run_collect()
            self.assertEqual(2 + 2 * i, result["matched_articles"])
            self.assertEqual(1, result["unreadable_messages"])
            self.assertEqual([{"name": "甲", "messages": 1, "identity_confirmed": True}],
                             result["unreadable_accounts"])
            self.assertEqual({"甲": now, "乙": now}, dict(self.repo.conn.execute(
                "SELECT name,latest_crawl_at FROM accounts")))
        with patch("src.utils.helpers.time_now", return_value="2026-09-16 00:04"):
            result = self.run_collect()
        self.assertEqual(8, result["matched_articles"])
        self.assertEqual("2026-09-16 00:04", self.repo.conn.execute(
            "SELECT latest_crawl_at FROM accounts WHERE name='甲'").fetchone()[0])

    def test_completed_history_rescan_updates_timestamp_without_duplicating_articles(self):
        self.reader.add("gh_a", [push(), {"text": "<mmreader>"}])
        self.run_collect()
        self.repo.set_account_latest_crawl("甲", "2020-01-01 00:00")
        self.repo.commit()
        self.reader.add("gh_a", [{"text": "<mmreader><broken>", "local_id": 2}])
        self.reader.add("gh_a", [push()], "biz_message_1.db")
        with patch("src.utils.helpers.time_now", return_value="2026-09-16 11:00"):
            result = self.run_collect()
        self.assertEqual(1, result["matched_articles"])
        self.assertEqual(1, result["unreadable_messages"])
        self.assertEqual("2026-09-16 11:00", self.repo.conn.execute(
            "SELECT latest_crawl_at FROM accounts WHERE name='甲'").fetchone()[0])

    def test_first_page_failure_in_new_shard_uses_session_identity_from_old_shard(self):
        self.repo.enable_crawl_account("乙", "biz-b")
        self.repo.commit()
        self.reader.add("gh_a", [push()], "biz_message_0.db")
        self.reader.add("gh_b", [push(biz="biz-b")], "biz_message_0.db")
        with patch("src.utils.helpers.time_now", return_value="2026-09-16 10:00"):
            self.run_collect()
        # 表名由稳定的 gh_ 身份生成，改名也不能丢失跨分片关联。
        self.reader.sessions[0]["display_name"] = "改名后的甲"
        self.reader.add("gh_a", [push(mid=str(i)) for i in range(1000, 1501)], "biz_message_1.db")
        original = self.reader._call

        def fail_new_shard(command, *args):
            if command == "sql" and "content_hex" in args[0] and "biz_message_1.db" in args:
                raise LocalReaderError("新分片读取失败")
            return original(command, *args)

        self.reader.call.side_effect = fail_new_shard
        for hour in (11, 12):
            now = f"2026-09-16 {hour}:00"
            with patch("src.utils.helpers.time_now", return_value=now):
                result = self.run_collect()
            self.assertEqual(["新分片读取失败"], result["errors"])
            self.assertEqual({"甲": "2026-09-16 10:00", "乙": now}, dict(self.repo.conn.execute(
                "SELECT name,latest_crawl_at FROM accounts")))
            self.assertEqual(0, self.repo.conn.execute(
                "SELECT COUNT(*) FROM local_wechat_articles WHERE source_key LIKE 'biz_message_1.db:%'").fetchone()[0])
        self.reader.call.side_effect = original
        with patch("src.utils.helpers.time_now", return_value="2026-09-16 13:00"):
            result = self.run_collect()
        self.assertEqual([], result["errors"])
        self.assertEqual(503, result["matched_articles"])
        self.assertEqual({"甲": "2026-09-16 13:00", "乙": "2026-09-16 13:00"},
                         dict(self.repo.conn.execute("SELECT name,latest_crawl_at FROM accounts")))

    def test_unrelated_failed_session_does_not_freeze_covered_account(self):
        self.reader.add("gh_a", [push()])
        self.run_collect()
        self.reader.add("gh_stranger", [push(biz="other")], "biz_message_1.db")
        self.reader.sessions[-1]["display_name"] = "无关的陌生号"
        original = self.reader._call

        def fail_stranger(command, *args):
            if command == "sql" and "content_hex" in args[0] and "biz_message_1.db" in args:
                raise LocalReaderError("陌生会话读取失败")
            return original(command, *args)

        self.reader.call.side_effect = fail_stranger
        with patch("src.utils.helpers.time_now", return_value="2026-09-16 11:00"):
            result = self.run_collect()
        self.assertEqual(["陌生会话读取失败"], result["errors"])
        self.assertEqual("2026-09-16 11:00", self.repo.conn.execute(
            "SELECT latest_crawl_at FROM accounts WHERE name='甲'").fetchone()[0])

    def test_new_discovery_does_not_override_a_real_reader_failure(self):
        self.reader.add("gh_a", [push(), {"text": "<mmreader>"}])
        self.run_collect()
        self.repo.set_account_latest_crawl("甲", "2020-01-01 00:00")
        self.repo.commit()
        self.reader.add("gh_a", [push(mid=str(i)) for i in range(1000, 1501)])
        pages = 0
        original = self.reader._call

        def fail_second_page(command, *args):
            nonlocal pages
            if command == "sql" and "content_hex" in args[0]:
                pages += 1
                if pages == 2:
                    raise LocalReaderError("第二页读取失败")
            return original(command, *args)

        self.reader.call.side_effect = fail_second_page
        result = self.run_collect()
        self.assertEqual(["第二页读取失败"], result["errors"])
        self.assertEqual(501, result["matched_articles"])
        self.assertEqual("2020-01-01 00:00", self.repo.conn.execute(
            "SELECT latest_crawl_at FROM accounts WHERE name='甲'").fetchone()[0])

    def test_duplicate_display_names_do_not_duplicate_messages_or_account_counts(self):
        self.repo.enable_crawl_account("乙", "biz-b")
        self.repo.conn.execute("UPDATE accounts SET display_name='重名'")
        self.repo.enable_crawl_account("丙", "biz-c")
        self.repo.commit()
        self.reader.add("gh_shared", [{"text": "<mmreader>"}] * 2)
        self.reader.sessions[0]["display_name"] = "重名"
        result = self.run_collect()
        self.assertEqual(2, result["unattributed_messages"])
        self.assertEqual(0, result["unreadable_messages"])
        self.assertEqual([], result["unreadable_accounts"])
        self.assertEqual({"甲", "乙"}, set(result["coverage_unknown_accounts"]))
        self.assertEqual(["丙"], result["missing_accounts"])

    def test_known_unselected_issue_remains_silent_and_does_not_make_missing_uncertain(self):
        self.reader.add("gh_other", [push(biz="other", pub_time="invalid")])
        self.reader.sessions[0]["display_name"] = "甲"
        result = self.run_collect()
        self.assertEqual(["甲"], result["missing_accounts"])
        self.assertEqual([], result["coverage_unknown_accounts"])
        self.assertEqual(0, result["unattributed_messages"])
        self.assertEqual(0, result["unreadable_messages"])
        self.assertEqual([], result["unreadable_accounts"])


if __name__ == "__main__":
    unittest.main()
