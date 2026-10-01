from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.local_source_fixture import SqlReader
from tests.test_local_wechat import push
from src.collect.local_index import ArticleIndex
from src.collect.local_runner import collect
from src.crawler.local_wechat import LocalReaderError, parse_push
from src.db import connection as conn_mod
from src.db.repository import ArticleRepository


class IndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready
        conn_mod.DATA_DIR = Path(self.tmp.name)
        conn_mod.DEFAULT_DB_PATH, conn_mod._db_ready = conn_mod.DATA_DIR / "test.db", False
        self.repo = ArticleRepository()
        self.repo.enable_crawl_account("目标", "biz-a")
        self.repo.commit()
        self.index = ArticleIndex(self.repo)
        self.reader = SqlReader()
        self.events, self.errors = [], []

    def tearDown(self):
        self.reader.close()
        self.repo.close()
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self.saved
        self.tmp.cleanup()

    def refresh(self):
        self.events.clear()
        self.errors.clear()
        return self.index.refresh(self.reader, self.events.append, self.errors.append)

    def query(self, active, since="", until=""):
        return self.index.query(self.repo.list_accounts(), active, since, until)

    def test_index_is_separate_from_ingestion_and_query_filters_publish_day_and_biz(self):
        self.reader.add("gh_target", [push(published="2026-09-14 23:59"),
                                      push(idx="2", published="2026-09-15 23:59"),
                                      push(idx="3", published="2026-09-16 00:00")])
        self.reader.add("gh_unselected", [push(biz="other")])
        active = self.refresh()
        result = self.query(active, "2026-09-15", "2026-09-15")
        self.assertEqual(1, len(result))
        self.assertIn("idx=2", result[0][1]["link"])
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
        self.assertEqual(4, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_articles").fetchone()[0])
        self.assertNotIn("gh_unselected", json.dumps(self.events))
        self.assertNotIn("article_json", json.dumps(self.events))

    def test_unchanged_second_run_reads_no_pushes_and_new_old_publication_is_found(self):
        self.reader.add("gh_target", [push()])
        self.refresh()
        self.reader.call.reset_mock()
        self.refresh()
        self.assertEqual([], self.reader.content_queries)
        self.assertEqual(0, self.events[-1]["scanned_messages"])
        # 新 local_id、旧接收时间、旧发布时间，不得被时间水位过滤掉。
        self.reader.add("gh_target", [{**push(idx="2", published="2020-01-01 00:00"), "create_time": 0}])
        active = self.refresh()
        self.assertEqual(1, self.events[-1]["scanned_messages"])
        self.assertEqual(1, len(self.query(active, "2020-01-01", "2020-01-01")))

    def test_more_than_one_page_of_new_messages_with_identical_timestamps(self):
        self.reader.add("gh_target", [push()])
        self.refresh()
        self.reader.call.reset_mock()
        self.reader.add("gh_target", [push(mid=str(i)) for i in range(1000, 2101)])
        active = self.refresh()
        self.assertEqual([], self.errors)
        self.assertEqual(1102, len(self.query(active)))
        self.assertEqual(3, len(self.reader.content_queries))
        self.assertEqual(1101, self.events[-1]["scanned_messages"])
        counts = [e["scanned_messages"] for e in self.events]
        self.assertEqual(sorted(counts), counts)

    def test_source_shards_have_independent_ids_and_cross_shard_articles_deduplicate(self):
        self.reader.add("gh_target", [push(), push(idx="2")], "biz_message_0.db")
        self.reader.add("gh_target", [push(), push(idx="3")], "biz_message_1.db")
        active = self.refresh()
        self.assertEqual(3, len(self.query(active)))
        self.reader.add("gh_target", [push(idx="4")], "biz_message_1.db")
        active = self.refresh()
        self.assertEqual(4, len(self.query(active)))
        self.assertEqual(1, self.events[-1]["scanned_messages"])

    def test_partial_sql_failure_does_not_advance_checkpoint_and_retries_missing_page(self):
        self.reader.add("gh_target", [push(mid=str(i)) for i in range(501)])
        original = self.reader._call
        pages = 0

        def fail_second_page(command, *args):
            nonlocal pages
            if command == "sql" and "content_hex" in args[0]:
                pages += 1
                if pages == 2:
                    raise LocalReaderError("读取中断")
            return original(command, *args)

        self.reader.call.side_effect = fail_second_page
        self.refresh()
        self.assertEqual(["读取中断"], self.errors)
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_sources").fetchone()[0])
        self.reader.call.side_effect = original
        self.assertEqual(501, len(self.query(self.refresh())))
        self.assertEqual([], self.errors)

    def test_malformed_push_is_recorded_without_losing_valid_or_future_repaired_message(self):
        self.reader.add("gh_target", [{"text": "<mmreader>", "local_id": 1}, push()])
        active = self.refresh()
        self.assertEqual(1, len(self.query(active)))
        self.assertEqual([], self.errors)
        self.assertEqual(1, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_sources").fetchone()[0])
        self.assertEqual(1, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_message_issues").fetchone()[0])
        self.reader.add("gh_target", [{**push(idx="2"), "local_id": 1}])
        self.assertEqual(2, len(self.query(self.refresh())))
        self.assertEqual([], self.errors)
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_message_issues").fetchone()[0])

    def test_insert_below_cursor_and_source_reset_trigger_reconciliation(self):
        db, table = self.reader.add("gh_target", [{**push(), "local_id": 1},
                                                   {**push(idx="3"), "local_id": 3}])
        self.refresh()
        self.reader.add("gh_target", [{**push(idx="2"), "local_id": 2}])
        self.assertEqual(3, len(self.query(self.refresh())))
        self.assertEqual(3, self.events[-1]["scanned_messages"])
        db.execute(f'DELETE FROM "{table}"')
        self.reader.add("gh_target", [{**push(idx=str(i + 10)), "local_id": i} for i in range(1, 4)])
        result = self.query(self.refresh())
        self.assertEqual(3, len(result))
        self.assertTrue(all("idx=1" in a["link"] for _, a in result))
        self.assertEqual(3, self.events[-1]["scanned_messages"])

    def test_permanent_bad_push_does_not_repeat_full_scan_or_error(self):
        for tail in (False, True):
            with self.subTest(after_checkpoint=tail):
                username = "gh_tail" if tail else "gh_initial"
                self.reader.add(username, [push(mid=str(i)) for i in range(300)])
                if tail:
                    self.refresh()
                self.reader.add(username, [{"text": "<mmreader>"}])
                self.refresh()
                self.assertEqual([], self.errors)
                self.assertEqual(1 if tail else 301, self.events[-1]["scanned_messages"])
                for _ in range(2):
                    with patch("src.collect.local_message_issues.parse_push") as parse:
                        self.refresh()
                    parse.assert_not_called()
                    self.assertEqual([], self.errors)
                    self.assertEqual(0, self.events[-1]["scanned_messages"])
                self.assertEqual(1 + int(tail), self.repo.conn.execute(
                    "SELECT COUNT(*) FROM local_wechat_sources").fetchone()[0])

    def test_repaired_middle_message_is_rechecked_without_rebuilding_source(self):
        self.reader.add("gh_target", [push(), {"text": "<mmreader>", "local_id": 2}, push(idx="3")])
        self.refresh()
        self.reader.add("gh_target", [{**push(idx="2"), "local_id": 2}])
        self.assertEqual(3, len(self.query(self.refresh())))
        self.assertEqual(0, self.events[-1]["scanned_messages"])
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_message_issues").fetchone()[0])

    def test_bad_encoding_is_isolated_but_reader_dependency_failure_is_not(self):
        self.reader.add("gh_target", [push(), {"text": b"\xff"},
                                      {"text": b"broken zstd", "compression": 4}])
        self.refresh()
        self.assertEqual([], self.errors)
        self.assertEqual(2, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_message_issues").fetchone()[0])
        self.reader.add("gh_target", [push(idx="2")])
        with patch("src.collect.local_message_issues.decode_push", side_effect=LocalReaderError("缺少依赖")):
            self.refresh()
        self.assertEqual(["缺少依赖"], self.errors)
        self.assertEqual(2, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_message_issues").fetchone()[0])
        self.assertEqual(3, json.loads(self.repo.conn.execute(
            "SELECT checkpoint FROM local_wechat_sources").fetchone()[0])["max_id"])

    def test_all_bad_pushes_are_counted_from_article_urls_without_successful_articles(self):
        self.reader.add("gh_target", [push(idx=str(i), pub_time="invalid") for i in range(3)])
        with patch("api._ingest_crawl_article") as ingest:
            for _ in range(2):
                result = collect(lambda e: None, reader=self.reader)
                self.assertEqual([], result["errors"])
                self.assertEqual([], result["missing_accounts"])
                self.assertEqual(3, result["unreadable_messages"])
                self.assertEqual([{"name": "目标", "messages": 3, "identity_confirmed": True}],
                                 result["unreadable_accounts"])
            ingest.assert_not_called()
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_articles").fetchone()[0])
        self.assertFalse(self.repo.conn.execute("SELECT latest_crawl_at FROM accounts").fetchone()[0])

    def test_all_bad_same_name_session_is_visible_as_unverified_not_as_identity(self):
        self.reader.add("gh_target", [{"text": "<mmreader>"}] * 3)
        self.reader.sessions[0]["display_name"] = "目标"
        result = collect(lambda e: None, reader=self.reader)
        self.assertEqual([], result["errors"])
        self.assertEqual([], result["missing_accounts"])
        self.assertEqual(3, result["unreadable_messages"])
        self.assertEqual([{"name": "目标", "messages": 3, "identity_confirmed": False}],
                         result["unreadable_accounts"])
        self.assertEqual(0, result["matched_articles"])
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
        self.assertFalse(self.repo.conn.execute("SELECT latest_crawl_at FROM accounts").fetchone()[0])

    def test_unidentifiable_bad_push_is_not_assigned_to_an_arbitrary_selected_account(self):
        self.reader.add("gh_unknown", [{"text": "<mmreader>"}])
        result = collect(lambda e: None, reader=self.reader)
        self.assertEqual([], result["errors"])
        self.assertEqual(["目标"], result["missing_accounts"])
        self.assertEqual([], result["coverage_unknown_accounts"])
        self.assertEqual(1, result["unattributed_messages"])
        self.assertEqual([], result["unreadable_accounts"])

    def test_unselected_biz_hint_overrides_name_collision_and_is_not_reported(self):
        self.reader.add("gh_other", [push(biz="other", pub_time="invalid")])
        self.reader.sessions[0]["display_name"] = "目标"
        result = collect(lambda e: None, reader=self.reader)
        self.assertEqual([], result["errors"])
        self.assertEqual(["目标"], result["missing_accounts"])
        self.assertEqual(0, result["unreadable_messages"])
        self.assertEqual([], result["unreadable_accounts"])

    def test_legacy_issue_hints_are_backfilled_and_removed_when_message_recovers(self):
        self.reader.add("gh_target", [{**push(pub_time="invalid"), "local_id": 1}])
        self.refresh()
        self.repo.conn.execute("DELETE FROM local_wechat_issue_biz")
        self.repo.conn.execute("UPDATE local_wechat_message_issues SET content_hash='old-parser'")
        self.repo.commit()
        active = self.refresh()
        self.assertEqual(0, self.events[-1]["scanned_messages"])
        self.assertEqual(1, self.index.unreadable_messages(self.repo.list_accounts(), active))
        self.reader.add("gh_target", [{**push(), "local_id": 1}])
        self.refresh()
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_issue_biz").fetchone()[0])

    def test_malformed_xml_hint_requires_a_unique_complete_wechat_article_url(self):
        from src.collect.local_message_issues import _biz_hint

        message = push()
        message["text"] = message["text"].removesuffix("</msg>")
        self.assertEqual("biz-a", _biz_hint(message))
        self.assertEqual("", _biz_hint({"text": message["text"] + push(biz="other")["text"]}))
        self.assertEqual("", _biz_hint(push(url="https://example.com/s?__biz=biz-a&mid=1&idx=1")))
        self.assertEqual("", _biz_hint(push(url="http://[broken")))

    def test_unselected_bad_source_does_not_affect_selected_account(self):
        self.reader.add("gh_target", [push()])
        self.reader.add("gh_stranger", [{"text": "<mmreader>"}])
        with patch("src.utils.request_pacing.RequestPacer.wait"), \
             patch("src.utils.wechat_body.fetch_article_body_full", return_value=(["正文"], "<p>正文</p>", "")):
            for _ in range(3):
                result = collect(lambda e: None, reader=self.reader)
                self.assertEqual([], result["errors"])
                self.assertEqual(0, result["unreadable_messages"])
                self.assertTrue(self.repo.conn.execute("SELECT latest_crawl_at FROM accounts").fetchone()[0])

    def test_mixed_body_results_update_both_accounts_and_cli_returns_success(self):
        import cli
        import contextlib
        import io

        self.repo.enable_crawl_account("其他", "biz-b")
        self.repo.commit()
        self.reader.add("gh_target", [push()])
        self.reader.add("gh_other", [push(biz="biz-b")])
        with patch("src.utils.request_pacing.RequestPacer.wait"), \
             patch("src.utils.wechat_body.fetch_article_body_full", side_effect=[
                 ("请求错误", None, ""), (["正文"], "<p>正文</p>", "")]):
            result = collect(lambda e: None, reader=self.reader)
        self.assertEqual([], result["errors"])
        self.assertEqual(1, len(result["body_failures"]))
        self.assertEqual((1, 1), (result["download_succeeded"], result["download_failed"]))
        self.assertTrue(all(r[0] for r in self.repo.conn.execute("SELECT latest_crawl_at FROM accounts")))
        with patch("src.collect.local_runner.collect", return_value=result), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["collect"]))

    def test_database_failure_does_not_prevent_other_source_timestamp(self):
        self.repo.enable_crawl_account("其他", "biz-b")
        self.repo.commit()
        self.reader.add("gh_target", [push()], "message_0.db")
        self.reader.add("gh_other", [push(biz="biz-b")], "message_1.db")
        self.refresh()
        self.reader.add("gh_target", [push(idx="2")], "message_0.db")
        self.reader.add("gh_other", [push(biz="biz-b", idx="2")], "message_1.db")
        original = self.reader._call

        def fail_one_file(command, *args):
            if command == "sql" and "content_hex" in args[0] and "message_1.db" in args:
                raise LocalReaderError("分片读取失败")
            return original(command, *args)

        self.reader.call.side_effect = fail_one_file
        with patch("src.collect.local_runner._download"):
            result = collect(lambda e: None, reader=self.reader)
        self.assertEqual(["分片读取失败"], result["errors"])
        times = dict(self.repo.conn.execute("SELECT name,latest_crawl_at FROM accounts"))
        self.assertTrue(times["目标"])
        self.assertFalse(times["其他"])

    def test_rewritten_boundary_checksum_updates_index(self):
        self.reader.add("gh_target", [{**push(), "local_id": 1}])
        self.refresh()
        self.reader.add("gh_target", [{**push(url=parse_push(push())[0]["link"] + "-new"), "local_id": 1}])
        result = self.query(self.refresh())
        self.assertIn("checksum-new", result[0][1]["link"])

    def test_compressed_pushes_and_absent_primary_content(self):
        import zstandard

        message = push()
        self.reader.add("gh_target", [{"compressed": zstandard.ZstdCompressor().compress(
            message["text"].encode()), "compression": 4}])
        self.assertEqual(1, len(self.query(self.refresh())))
        self.assertEqual([], self.errors)

    def test_only_official_sessions_are_read_even_if_schema_includes_private_tables(self):
        self.reader.add("gh_target", [push()])
        self.reader.add("wxid_private", [push(biz="private")])
        # accounts() 的生产过滤已拒绝私人会话；这里模拟其返回契约。
        self.reader.sessions = [s for s in self.reader.sessions if s["username"].startswith("gh_")]
        self.assertEqual(1, len(self.query(self.refresh())))
        self.assertEqual(1, self.events[-1]["scanned_messages"])

    def test_changing_selection_and_retrying_body_reuses_index_and_shows_article_progress(self):
        self.repo.enable_crawl_account("其他", "biz-b")
        self.repo.enable_crawl_account("停用", "biz-off")
        self.repo.conn.execute("UPDATE accounts SET in_crawl_config=0 WHERE name='停用'")
        self.repo.commit()
        self.reader.add("gh_target", [push()])
        self.reader.add("gh_other", [push(biz="biz-b")])
        self.reader.add("gh_disabled", [push(biz="biz-off")])
        with patch("src.utils.request_pacing.RequestPacer.wait"), \
             patch("src.utils.wechat_body.fetch_article_body_full", return_value=("请求错误", None, "")) as body:
            events = []
            first = collect(events.append, targets=["目标"], reader=self.reader)
            self.assertEqual((1, 1), (first["download_done"], first["download_failed"]))
            self.assertEqual(1, self.repo.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
            self.assertEqual(["discovery", "download"], list(dict.fromkeys(e.get("phase") for e in events if e.get("phase"))))
            body.return_value = (["正文"], "<p>正文</p>", "")
            self.repo.conn.execute("UPDATE article_fetch_state SET retry_at=0")
            self.repo.commit()
            self.reader.call.reset_mock()
            second = collect(events.append, reader=self.reader)
            self.assertEqual(2, second["download_succeeded"])
            self.assertEqual([], self.reader.content_queries)
            self.assertEqual(2, self.repo.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])
            progress = [e["download_done"] for e in events if e.get("phase") == "download"]
            self.assertEqual(2, progress[-1])
            self.assertEqual([], second["errors"])


if __name__ == "__main__":
    unittest.main()
