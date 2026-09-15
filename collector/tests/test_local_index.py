from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_source_fixture import SqlReader
from test_local_wechat import push
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

    def test_malformed_push_reports_error_without_losing_valid_or_future_repaired_message(self):
        self.reader.add("gh_target", [{"text": "<mmreader>", "local_id": 1}, push()])
        active = self.refresh()
        self.assertEqual(1, len(self.query(active)))
        self.assertTrue(self.errors)
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM local_wechat_sources").fetchone()[0])
        self.reader.add("gh_target", [{**push(idx="2"), "local_id": 1}])
        self.assertEqual(2, len(self.query(self.refresh())))
        self.assertEqual([], self.errors)

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
        with patch("src.utils.request_pacing.RequestPacer.wait"), patch("api.download_cover"), \
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
