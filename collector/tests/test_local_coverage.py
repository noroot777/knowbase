"""覆盖诊断回归：正常未同步、历史文章、未知归属、同名冲突。"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.local_source_fixture import SqlReader
from tests.test_local_wechat import push
from src.collect.local_runner import collect
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

    def test_history_does_not_hide_new_bad_pushes_or_refresh_affected_timestamp(self):
        self.repo.enable_crawl_account("乙", "biz-b")
        self.repo.commit()
        self.reader.add("gh_a", [push()])
        self.reader.add("gh_b", [push(biz="biz-b")])
        self.run_collect()
        self.repo.conn.execute("UPDATE accounts SET latest_crawl_at='2020-01-01 00:00'")
        self.repo.commit()
        self.reader.add("gh_a", [{"text": "<mmreader>"}] * 5)
        for _ in range(2):
            result = self.run_collect(since="2026-09-16", until="2026-09-16")
            self.assertEqual([], result["errors"])
            self.assertEqual(5, result["unreadable_messages"])
            self.assertEqual([{"name": "甲", "messages": 5, "identity_confirmed": True}],
                             result["unreadable_accounts"])
            times = dict(self.repo.conn.execute("SELECT name,latest_crawl_at FROM accounts"))
            self.assertEqual("2020-01-01 00:00", times["甲"])
            self.assertNotEqual("2020-01-01 00:00", times["乙"])
        self.assertEqual(0, next(e for e in reversed(self.events)
                                if e.get("phase") == "discovery")["scanned_messages"])
        # 修复这些消息后，恢复正常覆盖和采集时间。
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
            self.assertEqual([], result["missing_accounts"])
            self.assertEqual(["甲"], result["coverage_unknown_accounts"])
            self.assertEqual([], result["unreadable_accounts"])
            self.assertEqual(0, result["unreadable_messages"])
            self.assertEqual(3, result["unattributed_messages"])
            self.assertNotIn("未核实的新名字", str(self.events))

    def test_duplicate_display_names_do_not_duplicate_messages_or_account_counts(self):
        self.repo.enable_crawl_account("乙", "biz-b")
        self.repo.conn.execute("UPDATE accounts SET display_name='重名'")
        self.repo.commit()
        self.reader.add("gh_shared", [{"text": "<mmreader>"}] * 2)
        self.reader.sessions[0]["display_name"] = "重名"
        result = self.run_collect()
        self.assertEqual(2, result["unattributed_messages"])
        self.assertEqual(0, result["unreadable_messages"])
        self.assertEqual([], result["unreadable_accounts"])
        self.assertEqual({"甲", "乙"}, set(result["coverage_unknown_accounts"]))
        self.assertEqual([], result["missing_accounts"])

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
