from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import contextlib
import io
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import Mock, call, patch
from xml.sax.saxutils import escape

from src.crawler.local_wechat import LocalReader, LocalReaderError, parse_push, resolve_accounts
from src.db import connection as conn_mod
from src.utils.article_identity import article_identity, stable_article_id


def push(biz="biz-a", mid="101", idx="1", published="2026-09-15 18:30", **extra):
    url = f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn=signature&chksm=checksum&scene=123&xtrack=1"
    fields = {"title": "测试文章", "url": url, "digest": "摘要", "cover": "",
              "pub_time": str(int(datetime.strptime(published, "%Y-%m-%d %H:%M").timestamp())),
              **extra}
    return {"text": "<msg><appmsg><mmreader><category><item>"
            + "".join(f"<{k}>{escape(v)}</{k}>" for k, v in fields.items())
            + "</item></category></mmreader></appmsg></msg>",
            "create_time": 1}


class PushTests(unittest.TestCase):
    def test_collect_has_exactly_one_path_and_a_failure_is_not_reported_as_success(self):
        """采集只剩本机数据库这一条路:没有"另一条通道"可以悄悄退回去。

        原来 `--channel` 两选一,而 profile_ext 那条在缺凭证时会一路报成功;
        现在那条路连代码都不剩了,读不动数据库就是非零退出码。
        """
        import cli

        failed = {"new_articles": 0, "errors": ["数据库不可读"]}
        with patch("src.collect.local_runner.collect", return_value=failed) as local, \
             contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["collect", "--targets", "指定号"])
        self.assertEqual(cli.EXIT_ERROR, code)
        self.assertEqual(["指定号"], local.call_args.kwargs["targets"])
        with self.assertRaises(SystemExit):     # --channel 这个开关没有了
            with contextlib.redirect_stderr(io.StringIO()):
                cli.main(["collect", "--channel", "local"])

    def test_identity_is_shared_across_query_order_tracking_and_timestamp(self):
        article = parse_push(push())[0]
        changed = parse_push(push(published="2026-09-14 19:00"))[0]
        self.assertEqual(article["id"], changed["id"])
        self.assertNotIn("scene", article["link"])
        self.assertIn("sn=signature", article["link"])
        self.assertIn("chksm=checksum", article["link"])
        self.assertNotIn("xtrack", article["link"])
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

    def test_checksum_reaches_the_body_request(self):
        from src.utils.wechat_body import fetch_article_body_full

        def response(url, **kwargs):
            self.assertEqual(["checksum"], parse_qs(urlparse(url).query)["chksm"])
            return Mock(text='<div class="rich_media_content"><p>正文内容</p></div>')

        with patch("src.utils.wechat_body.requests.get", side_effect=response) as request:
            text, html, _ = fetch_article_body_full(parse_push(push())[0]["link"])
        self.assertEqual(["正文内容"], text)
        self.assertIn("正文内容", html)
        self.assertEqual(1, request.call_count)

    def test_discovery_stops_after_identifying_unrelated_session(self):
        reader = LocalReader()
        reader.accounts = Mock(return_value=[{"username": "gh_known"}, {"username": "gh_other"}])
        reader.messages = Mock(return_value=[push(biz="other")] * 500)
        accounts = [{"name": "已找到", "fakeid": "biz-a"}, {"name": "未订阅", "fakeid": "missing"}]
        found = resolve_accounts(reader, accounts, {"biz-a": "gh_known"})
        self.assertEqual({"biz-a": "gh_known"}, found)
        reader.messages.assert_called_once_with("gh_other", offset=0)

    def test_discovery_caches_absence_and_invalidates_on_new_push_or_expiry(self):
        for changed_timestamp in (True, False):
            with self.subTest(changed_timestamp=changed_timestamp):
                reader = LocalReader()
                sessions = [{"username": "gh_other", "last_timestamp": 1},
                            {"username": "gh_empty", "last_timestamp": 1}]
                reader.accounts = Mock(return_value=sessions)
                reader.messages = Mock(side_effect=lambda u, **kw: [push(biz="other")] if u == "gh_other" else [])
                discovery = {}
                accounts = [{"name": "目标", "fakeid": "biz-a"}]
                with patch("src.crawler.local_wechat.time.time", return_value=1000):
                    self.assertEqual({}, resolve_accounts(reader, accounts, {}, discovery=discovery))
                    self.assertEqual({}, resolve_accounts(reader, accounts, {}, discovery=discovery))
                self.assertEqual(2, reader.messages.call_count)
                sessions[1]["last_timestamp"] = 2 if changed_timestamp else 1
                reader.messages.return_value = [push()]
                reader.messages.side_effect = None
                with patch("src.crawler.local_wechat.time.time", return_value=1001 if changed_timestamp else 1301):
                    self.assertEqual({"biz-a": "gh_empty"},
                                     resolve_accounts(reader, accounts, {}, discovery=discovery))
                self.assertEqual(3, reader.messages.call_count)

    def test_bad_push_does_not_block_later_history_and_is_reported(self):
        reader = LocalReader()
        reader.messages = Mock(side_effect=[[{"text": "<mmreader>"}] + [{"text": "通知"}] * 499,
                                           [push()]])
        errors = []
        self.assertEqual(1, len(list(reader.articles("gh_test", on_error=errors.append))))
        self.assertEqual(1, len(errors))

    def test_xml_declaration_literal_in_cdata_is_ordinary_content(self):
        message = push()
        message["text"] = message["text"].replace("<digest>摘要</digest>",
            "<digest><![CDATA[讲解 <!DOCTYPE html> 和 <!ENTITY> 字面量]]></digest>")
        self.assertEqual(1, len(parse_push(message)))

    def test_pacer_waits_between_requests_and_body_retry_waits_between_attempts(self):
        from src.utils.request_pacing import RequestPacer
        from src.utils.wechat_body import fetch_article_body_full

        with patch("src.utils.request_pacing.time.monotonic", return_value=100), \
             patch("src.utils.request_pacing.random.uniform", return_value=3), \
             patch("src.utils.request_pacing.time.sleep") as sleep:
            pacer = RequestPacer()
            pacer.wait()
            sleep.assert_not_called()
            pacer.wait()
            sleep.assert_called_once_with(3)
        with patch("src.utils.wechat_body._fetch_page", side_effect=OSError), \
             patch("src.utils.wechat_body.time.sleep") as sleep:
            self.assertEqual("请求错误", fetch_article_body_full("https://mp.weixin.qq.com/s")[0])
            self.assertEqual(3, sleep.call_count)


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
        from tests.local_source_fixture import TimelineFixture

        self.sql_fixture = TimelineFixture(self.reader)
        self.reader.call = self.sql_fixture.call

        self.fill = patch("src.utils.wechat_body.fetch_article_body_full", return_value=(
            ["真实正文测试内容"], "<p>真实正文测试内容</p>", "")).start()
        patch("src.utils.request_pacing.RequestPacer.wait").start()

    def tearDown(self):
        self.sql_fixture.close()
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
        self.fill.return_value = ("请求错误", None, "")
        first = self.collect()
        self.assertEqual([], first["errors"])
        self.assertIn("自动重试", first["body_failures"][0]["reason"])
        second = self.collect()
        self.assertEqual(0, second["new_articles"])
        self.assertEqual([], second["errors"])
        self.assertEqual(1, second["deferred_articles"])
        self.assertEqual(1, self.fill.call_count)
        self.repo.conn.execute("UPDATE article_fetch_state SET retry_at=0")
        self.repo.commit()
        self.fill.return_value = (["恢复正文"], "<p>恢复正文</p>", "")
        third = self.collect()
        self.assertEqual([], third["errors"])
        self.assertEqual(2, self.fill.call_count)
        self.assertEqual(0, self.repo.conn.execute("SELECT COUNT(*) FROM article_fetch_state").fetchone()[0])

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
        self.assertEqual(1, len(result["errors"]))
        self.assertEqual(["档案名"], result["missing_accounts"])
        self.assertEqual(0, result["new_articles"])

    def test_unavailable_reader_never_starts_discovery_or_ingest(self):
        self.reader.check.side_effect = LocalReaderError("未就绪")
        result = self.collect()
        self.assertEqual(["未就绪"], result["errors"])
        self.reader.accounts.assert_not_called()
        self.fill.assert_not_called()

    def test_new_checksum_bypasses_old_failed_link_cooldown(self):
        self.fill.return_value = ("请求错误", None, "")
        self.collect()
        self.reader.messages.return_value = [push(url=(parse_push(push())[0]["link"] + "-new"))]
        self.fill.return_value = (["正文"], "<p>正文</p>", "")
        result = self.collect()
        self.assertEqual([], result["errors"])
        self.assertEqual(2, self.fill.call_count)

    def test_confirmed_unavailable_body_is_deferred_without_permanent_deletion(self):
        self.fill.return_value = ("该内容已被发布者删除", None, "")
        first, second = self.collect(), self.collect()
        self.assertEqual([], first["errors"])
        self.assertEqual(1, first["unavailable_articles"])
        self.assertEqual(1, second["deferred_articles"])
        self.assertEqual(1, self.fill.call_count)
        self.assertEqual(0, self.repo.conn.execute("SELECT is_wx_deleted FROM articles").fetchone()[0])

    def test_violation_page_counts_as_unavailable_not_body_failure(self):
        # 违规下架跟作者删除一样读不回来:按临时失败退避的话,它每天都会
        # 重试一次、还永远挂在面板的「正文待重试」里(判定见 message_unavailable)。
        self.fill.return_value = ("此内容因违规无法查看", None, "")
        result = self.collect()
        self.assertEqual([], result["errors"])
        self.assertEqual(1, result["unavailable_articles"])
        self.assertEqual([], result["body_failures"])
        self.assertEqual("unavailable", self.repo.conn.execute(
            "SELECT reason FROM article_fetch_state").fetchone()[0])

    def test_auto_retired_article_can_recover_in_either_channel(self):
        import api

        article = parse_push(push())[0]
        self.repo.upsert_article_from_blog("档案名", article)
        self.repo.conn.execute("UPDATE articles SET is_wx_deleted=1")
        self.repo.commit()
        self.assertEqual([], self.collect()["errors"])
        self.assertEqual(0, self.repo.conn.execute("SELECT is_wx_deleted FROM articles").fetchone()[0])
        self.repo.conn.execute("DELETE FROM article_bodies")
        self.repo.conn.execute("UPDATE articles SET is_wx_deleted=1")
        self.repo.commit()
        article["id"] = "remote-profile-id"
        self.assertFalse(api._ingest_crawl_article(self.repo, "档案名", article))
        self.assertEqual(0, self.repo.conn.execute("SELECT is_wx_deleted FROM articles").fetchone()[0])
        self.assertEqual(1, self.repo.conn.execute("SELECT COUNT(*) FROM article_bodies").fetchone()[0])

    def test_bad_push_is_recorded_while_valid_article_is_ingested(self):
        self.reader.messages.return_value = [{"text": "<mmreader>"}, push()]
        result = self.collect()
        self.assertEqual([], result["errors"])
        self.assertEqual(1, result["unreadable_messages"])
        self.assertEqual(1, result["new_articles"])

    def _export_event(self, out):
        """跑一次真导出脚本,再让 `cli.cmd_export` 把它的输出收成结构化事件。

        两段都走真的:清单是脚本自己逐篇打出来的,`out` 和每条的 `id`/`round`
        都由 cli 从那些行里解析。中间任何一段只留给人看的话,知识库那一侧就只能
        回去扫全库猜归属,而差集分不出"谁写的"。
        """
        import sys
        from scripts import export_obsidian

        with patch.object(sys, "argv",
                          ["export", "--out", out, "--dedup-root", out, "--no-images"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, export_obsidian.main())
        raw = output.getvalue()
        # 一篇都没导的时候也得有这一行:上层靠它把清单里的「天/文件名」还原成
        # vault 相对路径。
        self.assertIn(f"[export] 输出目录: {out}", raw)
        self.human = raw

        import cli
        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=raw, stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        event = json.loads(event_output.getvalue())
        # 这里就把 `items` 这个键钉住,别留给各条用例去下标 —— 键名漂了的话每条
        # 用例红成 KeyError(ERROR),而我们要的是一句说得出后果的 FAIL:
        # 知识库那一侧读不到清单就只能把整轮报成「不可确认」。
        self.assertIn("items", event,
                      "事件里没有 items 清单 —— 知识库那侧只能报「不可确认」")
        return event

    def test_export_hands_up_what_it_wrote_with_each_items_identity(self):
        """导出脚本逐篇打的那行清单,连同输出目录一起收进结构化事件。

        每条带 `id`(稳定内容身份)和 `round`(发现它的那一次业务抓取)——
        知识库拿它当成果回执,并据此分开「这一次新抓到的」和「补导之前抓到的」
        (口径在 kb/collectors/__init__.py 的 materialize 契约)。
        """
        from pathlib import Path

        self.reader.messages.return_value = [push()]
        self.fill.return_value = (["正文"], "<p>正文</p>", "")
        self.collect(round_key="batch-today")
        out = str(Path(self.tmp.name) / "export2")
        event = self._export_event(out)
        self.assertIn("  [ok] ", self.human)       # 给人看的那一行还在
        # 先判「交上来了没有」再判内容:缺键的话 KeyError 会把红变成 ERROR,
        # 而「清单压根没交」正是这一条要拦的那种改动。
        self.assertEqual({"out", "items"}, {"out", "items"} & set(event),
                         f"事件里没有成果回执:{sorted(event)}")
        self.assertEqual(out, event["out"])
        self.assertEqual(1, len(event["items"]))
        one = event["items"][0]
        self.assertTrue(one["name"].endswith(".md"))
        # 回执指的得是真落在盘上的那个文件,不是一句好看的文案
        self.assertTrue((Path(out) / one["name"]).is_file())
        self.assertTrue(one["id"])                             # 稳定内容身份
        self.assertEqual("batch-today", one["round"])          # 发现它的那一轮

    def test_the_round_that_found_an_article_is_not_the_round_that_exports_it(self):
        """**这一条是本任务的要害**:正文当天没抓到、隔一轮补上才导出。

        回执里记的仍是**发现**它的那一轮(`batch-day1`),不是导出它的这一轮 ——
        否则药丸会把一篇前几天就抓到的文章算成「这一次新抓到的」,而远端那天
        压根没发新文章。`INSERT OR IGNORE` 保证第二轮不改写它。
        """
        from pathlib import Path

        self.reader.messages.return_value = [push()]
        # 第一轮:发现了它,但正文一个字都没拿到 —— 导出会跳过它(无正文)
        self.fill.return_value = ([], "", "读取失败")
        self.collect(round_key="batch-day1")
        out = str(Path(self.tmp.name) / "export-carried")
        self.assertEqual([], self._export_event(out)["items"])

        # 第二轮:正文补上了,这一轮才真写进 vault。先清掉上一轮留下的退避状态,
        # 不然它会被当成「还没到重试时间」整轮跳过。
        with self.repo.conn:
            self.repo.conn.execute("DELETE FROM article_fetch_state")
        self.fill.return_value = (["补上的正文"], "<p>补上的正文</p>", "")
        self.collect(round_key="batch-day3")
        items = self._export_event(out)["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("batch-day1", items[0]["round"])

    def test_a_collect_without_a_round_leaves_the_identity_blank(self):
        """在终端里手动跑一次采集(没有业务抓取身份):不许编一个。

        空串是「说不清」,知识库那一侧归到 unsure 那一档;硬写个值等于凭空造
        「这一篇是哪一次抓到的」这个事实。
        """
        from pathlib import Path

        self.reader.messages.return_value = [push()]
        self.fill.return_value = (["正文"], "<p>正文</p>", "")
        self.collect()                                  # 不传 round_key
        out = str(Path(self.tmp.name) / "export-anon")
        self.assertEqual("", self._export_event(out)["items"][0]["round"])

    def test_a_torn_item_line_voids_the_whole_list(self):
        """清单里有一行读不懂:**整份作废,不挑着收**。

        挑着收的后果是交上去一份少记的清单,而上层把它当确定值用(「这一次抓了
        N 篇」)—— 少记是静默丢数据,报「不可确认」顶多让人多同步一次。
        判据落在后果上:事件里**没有** `items` 这个键,知识库那侧据此报不可确认。
        """
        import cli

        raw = ("[export] 输出目录: /tmp/whatever\n"
               '  [item] {"id": "a1", "round": "batch-x", "name": "2026-10-15/a.md"}\n'
               "  [item] {\"id\": \"a2\", \"round\"\n"        # 写到一半
               "[export] 完成: 导出 2, 跳过(已存在) 0, 空正文 0\n")
        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=raw, stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        event = json.loads(event_output.getvalue())
        self.assertNotIn("items", event)
        # 反过来钉一边:整份清单都读得懂时照旧给键(不是一出错就永久不给了)。
        good = raw.replace("  [item] {\"id\": \"a2\", \"round\"\n", "")
        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=good, stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        self.assertEqual([{"id": "a1", "round": "batch-x",
                           "name": "2026-10-15/a.md"}],
                         json.loads(event_output.getvalue())["items"])

    def test_export_keeps_old_empty_article_retryable(self):
        from scripts import export_obsidian
        import sys

        self.reader.messages.return_value = [push(published="2020-01-01 12:00")]
        self.fill.return_value = ("请求错误", None, "")
        self.collect()
        out = str(Path(self.tmp.name) / "export")
        with patch.object(sys, "argv", ["export", "--out", out, "--dedup-root", out, "--no-images"]), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, export_obsidian.main())
        self.assertIn("待补正文 1", output.getvalue())
        self.assertIn("无正文,跳过 档案名 · 测试文章", output.getvalue())
        self.assertNotIn("失败 0", output.getvalue())
        import cli
        import subprocess

        with patch("cli.subprocess.run", return_value=subprocess.CompletedProcess(
                [], 0, stdout=output.getvalue(), stderr="")), \
             contextlib.redirect_stdout(io.StringIO()) as event_output, \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.EXIT_OK, cli.main(["export", "--no-images"]))
        import json

        self.assertEqual(["档案名 · 测试文章"], json.loads(event_output.getvalue())["no_body"])
        self.assertEqual(0, self.repo.conn.execute("SELECT is_wx_deleted FROM articles").fetchone()[0])
        self.repo.conn.execute("UPDATE article_fetch_state SET retry_at=0")
        self.repo.commit()
        self.fill.return_value = (["恢复正文"], "<p>恢复正文</p>", "")
        self.assertEqual([], self.collect()["errors"])
        self.assertEqual(1, self.repo.conn.execute("SELECT COUNT(*) FROM article_bodies").fetchone()[0])

    def test_recovery_only_resets_affected_local_articles_and_creates_backup(self):
        from scripts.recover_local_articles import recover
        import sqlite3

        for idx in range(1, 6):
            article = parse_push(push(idx=str(idx)))[0]
            if idx != 5:
                article["link"] = article["link"].replace("&chksm=checksum", "")
            if idx == 4:
                article["id"] = "legacy-id"
            self.repo.upsert_article_from_blog("档案名", article)
            if idx == 2:
                self.repo.set_body_text(article["id"], ["已有正文"])
            if idx == 3:
                self.repo.add_deleted_blacklist(article["id"])
        self.repo.conn.execute("UPDATE articles SET is_wx_deleted=1")
        self.repo.commit()
        db = conn_mod.DEFAULT_DB_PATH
        self.assertEqual({"candidates": 1, "restored": 0}, recover(db))
        backup = Path(self.tmp.name) / "before.db"
        self.assertEqual({"candidates": 1, "restored": 1}, recover(db, backup=backup))
        with sqlite3.connect(backup) as saved:
            self.assertEqual(5, saved.execute("SELECT SUM(is_wx_deleted) FROM articles").fetchone()[0])
        self.assertEqual(4, self.repo.conn.execute("SELECT SUM(is_wx_deleted) FROM articles").fetchone()[0])
        self.assertEqual({"candidates": 0, "restored": 0}, recover(db))


if __name__ == "__main__":
    unittest.main()
