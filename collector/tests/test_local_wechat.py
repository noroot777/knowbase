from __future__ import annotations

import tempfile
import unittest
import contextlib
import io
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, call, patch
from xml.sax.saxutils import escape

from src.crawler.local_wechat import LocalReader, LocalReaderError, parse_push, resolve_accounts
from src.db import connection as conn_mod
from src.utils.article_identity import article_identity, stable_article_id


def push(biz="biz-a", mid="101", idx="1", published="2026-09-15 18:30", **extra):
    url = f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn=signature&scene=123"
    fields = {"title": "测试文章", "url": url, "digest": "摘要", "cover": "",
              "pub_time": str(int(datetime.strptime(published, "%Y-%m-%d %H:%M").timestamp())),
              **extra}
    return {"text": "<msg><appmsg><mmreader><category><item>"
            + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in fields.items())
            + "</item></category></mmreader></appmsg></msg>",
            "create_time": 1}


class PushTests(unittest.TestCase):
    def test_cli_local_failure_does_not_fall_back_to_remote(self):
        import cli

        failed = {"new_articles": 0, "errors": ["数据库不可读"], "auth_error": False}
        with patch("src.collect.local_runner.collect", return_value=failed) as local, \
             patch("src.collect.runner.collect") as remote, \
             contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["collect", "--channel", "local", "--targets", "指定号"])
        self.assertEqual(cli.EXIT_ERROR, code)
        self.assertEqual(["指定号"], local.call_args.kwargs["targets"])
        remote.assert_not_called()

    def test_identity_is_shared_across_query_order_tracking_and_timestamp(self):
        article = parse_push(push())[0]
        changed = parse_push(push(published="2026-09-14 19:00"))[0]
        self.assertEqual(article["id"], changed["id"])
        self.assertNotIn("scene", article["link"])
        self.assertIn("sn=signature", article["link"])
        self.assertEqual("2026-09-15 18:30", article["create_time"])
        self.assertEqual(article_identity(article["link"]), article_identity(
            "http://mp.weixin.qq.com/s?idx=1&mid=101&__biz=biz-a&scene=99"))
        self.assertNotEqual(article["id"], parse_push(push(idx="2"))[0]["id"])
        self.assertNotEqual(article["id"], parse_push(push(biz="other"))[0]["id"])

    def test_multi_article_push_preserves_secondary_items(self):
        first, second = push(), push(idx="2")
        item = second["text"].split("<item>")[1].split("</item>")[0]
        first["text"] = first["text"].replace("</category>", f"<item>{item}</item></category>")
        self.assertEqual(2, len(parse_push(first)))

    def test_notifications_deleted_video_and_external_links_are_excluded(self):
        for message in [{"text": "服务通知"}, {"text": "<msg><appmsg><url>https://mp.weixin.qq.com/s</url></appmsg></msg>"},
                        push(del_flag="1"), push(itemshowtype="5"),
                        push(url="https://example.com/s?__biz=a&mid=1&idx=1")]:
            self.assertEqual([], parse_push(message))

    def test_malformed_push_is_an_error_not_an_empty_success(self):
        for text in ["<mmreader>", '<!DOCTYPE msg [<!ENTITY x "a">]><mmreader/>']:
            with self.assertRaises(LocalReaderError):
                parse_push({"text": text})

    def test_readiness_requires_database_not_notification_preview(self):
        reader = LocalReader()
        reader.call = Mock(return_value={"status": {"status": {
            "live_database_read_ok": False, "notification_preview_ok": True}}})
        with self.assertRaisesRegex(LocalReaderError, "通知预览"):
            reader.check()

    def test_private_chat_is_rejected_before_reader_call(self):
        reader = LocalReader()
        reader.call = Mock()
        with self.assertRaises(LocalReaderError):
            reader.messages("wxid_private")
        reader.call.assert_not_called()

    def test_pagination_reads_next_page_and_deduplicates(self):
        reader = LocalReader()
        reader.messages = Mock(side_effect=[[push()] * 500, [push(), push(idx="2")]])
        articles = list(reader.articles("gh_test"))
        self.assertEqual(2, len(articles))
        self.assertEqual(500, reader.messages.call_args.kwargs["offset"])

    def test_mapping_uses_biz_even_when_names_collide_or_change(self):
        reader = LocalReader()
        reader.accounts = Mock(return_value=[{"username": "gh_wrong", "display_name": "档案名"},
                                            {"username": "gh_right", "display_name": "改名后的号"}])
        reader.messages = Mock(side_effect=lambda u, **kw: [push(biz="other" if u == "gh_wrong" else "biz-a")])
        result = resolve_accounts(reader, [{"name": "档案名", "fakeid": "biz-a"}], {})
        self.assertEqual({"biz-a": "gh_right"}, result)

    def test_mapping_finds_older_push_across_message_and_page_boundaries(self):
        for recent_count in (20, 499, 500, 1000):
            with self.subTest(recent_count=recent_count):
                reader = LocalReader()
                reader.accounts = Mock(return_value=[{"username": "gh_test", "display_name": "档案名"}])
                history = [{"text": "服务通知"}] * recent_count + [push()]
                # 匹配后还有整页历史；所有目标已找到时不应继续读取。
                history += [{"text": "服务通知"}] * 1000
                reader.messages = Mock(side_effect=lambda u, limit=500, offset=0: history[offset:offset + limit])
                found = resolve_accounts(reader, [{"name": "档案名", "fakeid": "biz-a"}], {})
                self.assertEqual({"biz-a": "gh_test"}, found)
                self.assertEqual([call("gh_test", offset=offset)
                                  for offset in range(0, recent_count + 1, 500)],
                                 reader.messages.call_args_list)

    def test_mapping_exhausts_history_before_reporting_no_match(self):
        reader = LocalReader()
        reader.accounts = Mock(return_value=[{"username": "gh_test", "display_name": "档案名"}])
        reader.messages = Mock(side_effect=[[{"text": "服务通知"}] * 500, []])
        found = resolve_accounts(reader, [{"name": "档案名", "fakeid": "biz-a"}], {})
        self.assertEqual({}, found)
        self.assertEqual([call("gh_test", offset=0), call("gh_test", offset=500)],
                         reader.messages.call_args_list)

    def test_one_unreadable_account_does_not_block_other_matches(self):
        reader = LocalReader()
        reader.accounts = Mock(return_value=[{"username": "gh_bad"}, {"username": "gh_ok"}])
        reader.messages = Mock(side_effect=[[{"text": "服务通知"}] * 500,
                                           LocalReaderError("读取失败"), [push()]])
        errors = []
        found = resolve_accounts(reader, [{"name": "档案名", "fakeid": "biz-a"}], {}, on_error=errors.append)
        self.assertEqual({"biz-a": "gh_ok"}, found)
        self.assertEqual(1, len(errors))


class LocalCollectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.saved = (conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready)
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = root / "test.db", root, False
        from src.db.repository import ArticleRepository

        self.repo = ArticleRepository()
        self.repo.enable_crawl_account("档案名", "biz-a")
        self.repo.enable_crawl_account("停用号", "biz-off")
        self.repo.conn.execute("UPDATE accounts SET in_crawl_config=0 WHERE name='停用号'")
        self.repo.commit()
        self.reader = LocalReader()
        self.reader.check = Mock()
        self.reader.accounts = Mock(return_value=[{"username": "gh_test", "display_name": "档案名"}])
        self.reader.messages = Mock(return_value=[push()])

        def fill_body(aid, link, *, repo):
            repo.set_body_text(aid, ["真实正文测试内容"])
            repo.set_body_html(aid, "<p>真实正文测试内容</p>")
            repo.commit()
            return ""

        self.fill = patch("api._fetch_article_detail_text", side_effect=fill_body).start()
        patch("api.download_cover").start()

    def tearDown(self):
        patch.stopall()
        self.repo.close()
        conn_mod.DEFAULT_DB_PATH, conn_mod.DATA_DIR, conn_mod._db_ready = self.saved
        self.tmp.cleanup()

    def collect(self, **kwargs):
        from src.collect.local_runner import collect

        return collect(lambda event: None, reader=self.reader, **kwargs)

    def test_local_collect_twice_is_idempotent_and_does_not_touch_disabled_accounts(self):
        first, second = self.collect(), self.collect()
        self.assertEqual([], first["errors"])
        self.assertEqual((1, 0), (first["new_articles"], second["new_articles"]))
        self.assertEqual(1, first["total"])
        self.assertEqual(1, self.fill.call_count)
        self.assertEqual(1, self.repo.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])

    def test_local_collect_discovers_and_ingests_older_push_without_cached_mapping(self):
        history = [{"text": "服务通知"}] * 500 + [push()]
        self.reader.messages.side_effect = lambda u, limit=500, offset=0: history[offset:offset + limit]
        result = self.collect()
        self.assertEqual([], result["errors"])
        self.assertEqual(1, result["new_articles"])
        self.assertEqual(1, self.fill.call_count)
        self.assertEqual(stable_article_id(parse_push(push())[0]["link"]),
                         self.repo.conn.execute("SELECT id FROM articles").fetchone()[0])

    def test_remote_then_local_then_remote_reuses_original_id(self):
        import api

        article = parse_push(push())[0]
        article["id"] = "legacy-profile-id"
        api._ingest_crawl_article(self.repo, "档案名", article)
        self.assertEqual(0, self.collect()["new_articles"])
        article["id"] = "different-profile-time-id"
        article["link"] += "&scene=999"
        self.assertFalse(api._ingest_crawl_article(self.repo, "档案名", article))
        self.assertEqual(["legacy-profile-id"], [r[0] for r in self.repo.conn.execute("SELECT id FROM articles")])

    def test_deleted_article_does_not_return_through_other_channel(self):
        import api

        self.collect()
        aid = stable_article_id(parse_push(push())[0]["link"])
        self.repo.remove_article(aid, "档案名")
        self.repo.commit()
        article = parse_push(push())[0]
        article["id"] = "remote-profile-id"
        self.assertFalse(api._ingest_crawl_article(self.repo, "档案名", article))
        self.assertEqual(0, self.collect()["new_articles"])
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0])

    def test_failed_body_is_reported_and_retried_without_new_metadata(self):
        self.fill.side_effect = lambda *args, **kwargs: ""
        first = self.collect()
        self.assertIn("正文获取失败", first["errors"][0])
        second = self.collect()
        self.assertEqual(0, second["new_articles"])
        self.assertEqual(2, self.fill.call_count)

    def test_empty_body_row_does_not_prevent_refill(self):
        article = parse_push(push())[0]
        self.repo.upsert_article_from_blog("档案名", article)
        self.repo.set_body_text(article["id"], [])
        self.repo.commit()
        result = self.collect()
        self.assertEqual([], result["errors"])
        self.assertEqual(0, result["new_articles"])
        self.assertEqual(1, self.fill.call_count)

    def test_date_range_includes_entire_end_day_and_uses_publish_time(self):
        self.reader.messages.return_value = [push(idx="1", published="2026-09-14 23:59"),
                                             push(idx="2", published="2026-09-15 23:59"),
                                             push(idx="3", published="2026-09-16 00:00")]
        result = self.collect(since="2026-09-15", until="2026-09-15")
        self.assertEqual(1, result["new_articles"])
        self.assertIn("idx=2", self.repo.conn.execute("SELECT link FROM articles").fetchone()[0])

    def test_unmatched_account_and_disabled_selection_are_not_silent_success(self):
        self.reader.messages.return_value = []
        result = self.collect(targets=["档案名", "停用号"])
        self.assertEqual(2, len(result["errors"]))
        self.assertEqual(0, result["new_articles"])

    def test_unavailable_reader_never_starts_discovery_or_ingest(self):
        self.reader.check.side_effect = LocalReaderError("未就绪")
        result = self.collect()
        self.assertEqual(["未就绪"], result["errors"])
        self.reader.accounts.assert_not_called()
        self.fill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
