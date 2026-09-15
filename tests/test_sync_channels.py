from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import pipeline, pipeline_retry, pipeline_stages, pipeline_state
from kb.collectors import FetchScope
from kb.web import pipeline_api


class ChannelAPITests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(pipeline_api.router)
        self.client = TestClient(self.app)

    def test_default_is_original_and_explicit_local_preserves_scope(self):
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            self.assertEqual(200, self.client.post("/api/pipeline/run", json={}).status_code)
            self.assertEqual("wechat_oa", start.call_args.kwargs["crawl_filter"]["channel"])
            body = {"channel": "wechat_local", "accounts": ["指定号"],
                    "since_date": "2026-09-15", "until_date": "2026-09-15"}
            self.assertEqual(200, self.client.post("/api/pipeline/run", json=body).status_code)
            self.assertEqual(body, start.call_args.kwargs["crawl_filter"])

    def test_invalid_channel_date_and_blank_names_are_rejected_before_run(self):
        bodies = [{"channel": "bad"}, {"accounts": [" "]}, {"since_date": "2026-09-15"},
                  {"since_date": "2026-09-16", "until_date": "2026-09-15"},
                  {"since_date": "2026-02-30", "until_date": "2026-09-15"},
                  {"since_date": "20260901", "until_date": "20260915"}]
        with patch.object(pipeline, "start_pipeline") as start:
            for body in bodies:
                self.assertEqual(400, self.client.post("/api/pipeline/run", json=body).status_code, body)
            start.assert_not_called()

    def test_status_and_targets_read_selected_channel(self):
        with patch.object(pipeline, "source_status", return_value={}) as status:
            self.client.get("/api/pipeline/status?channel=wechat_local")
            status.assert_called_once_with("wechat_local")
        with patch.object(pipeline, "source_targets", return_value=["指定号"]) as targets:
            result = self.client.get("/api/pipeline/accounts?channel=wechat_local")
            self.assertEqual(["指定号"], result.json()["accounts"])
            targets.assert_called_once_with("wechat_local")

    def test_schedule_persists_channel_and_preserves_old_clients(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(pipeline, "_SCHED_FILE", Path(tmp) / "schedule.json"):
            self.assertEqual("wechat_oa", pipeline.load_schedule()["channel"])
            body = {"enabled": True, "time": "08:15", "channel": "wechat_local"}
            response = self.client.post("/api/pipeline/schedule", json=body)
            self.assertEqual("wechat_local", response.json()["channel"])
            self.client.post("/api/pipeline/schedule", json={"enabled": False, "time": "08:20"})
            self.assertEqual("wechat_local", pipeline.load_schedule()["channel"])


class ChannelDispatchTests(unittest.TestCase):
    def test_local_progress_renders_discovery_and_body_counts_without_changing_old_channel(self):
        from kb.collectors.wechat_oa import WeChatOACollector

        detail = WeChatOACollector._progress_detail
        self.assertIn("首次建立索引", detail({"phase": "discovery", "initial_index": True}))
        self.assertIn("已读推送 500/501 条", detail({"phase": "discovery",
                                              "scanned_messages": 500, "message_total": 501}))
        text = detail({"phase": "download", "download_done": 2, "download_total": 3,
                       "download_succeeded": 1, "download_failed": 1,
                       "existing_articles": 4, "current": "目标 · 标题"})
        for expected in ("下载正文 2/3 篇", "成功 1", "失败 1", "已有正文 4", "目标 · 标题"):
            self.assertIn(expected, text)
        self.assertEqual("采集中 2/44 — 目标", detail({"done": 2, "total": 44, "current": "目标"}))

    def test_local_fetch_invalidates_its_own_status_cache(self):
        import subprocess
        from kb.collectors import wechat_oa
        from kb.collectors.wechat_local import WeChatLocalCollector

        for fail in (False, True):
            with self.subTest(fail=fail):
                collector = WeChatLocalCollector()
                collector._local_cache = (0, object())
                with patch.object(wechat_oa, "_backend_problem", return_value=""), \
                     patch.object(wechat_oa, "_stage"), \
                     patch.object(collector, "_stream", return_value=(0, {}),
                                  side_effect=subprocess.TimeoutExpired("collect", 1) if fail else None):
                    collector.fetch(FetchScope())
                self.assertIsNone(collector._local_cache)

    def test_local_coverage_and_deferred_bodies_do_not_look_like_fresh_failures(self):
        from kb.collectors import wechat_oa

        result = {"new_articles": 2, "errors": [], "missing_accounts": ["未订阅"],
                  "deferred_articles": 3, "unavailable_articles": 1}
        with patch.object(wechat_oa, "_stage") as stage:
            wechat_oa.WeChatOACollector._report(0, result)
        self.assertEqual(("crawl", "ok"), stage.call_args.args[:2])
        self.assertIn("本机暂无推送 1 个号", stage.call_args.args[2])
        self.assertIn("等待重试 3 篇", stage.call_args.args[2])
        self.assertEqual([], stage.call_args.kwargs["failures"])

    def test_collector_cli_gets_selected_channel_and_reports_read_failures(self):
        from kb.collectors import wechat_oa
        from kb.collectors.wechat_local import collector

        result = {"new_articles": 0, "errors": ["本机推送未同步"]}
        with patch.object(wechat_oa, "_backend_problem", return_value=""), \
             patch.object(collector, "_stream", return_value=(0, result)) as stream, \
             patch.object(wechat_oa, "_stage") as stage:
            collector.fetch(FetchScope(("指定号",), "2026-09-01", "2026-09-15"))
        argv = stream.call_args.args[0]
        self.assertEqual(["collect", "--channel", "local"], argv[:3])
        self.assertEqual("指定号", argv[argv.index("--targets") + 1])
        self.assertEqual("2026-09-15", argv[argv.index("--until") + 1])
        self.assertEqual(("crawl", "error"), stage.call_args.args[:2])

    def test_scheduler_dispatches_saved_channel(self):
        schedule = {"enabled": True, "time": "08:15", "channel": "wechat_local", "last_fired": ""}
        with patch.object(pipeline, "load_schedule", return_value=schedule), \
             patch.object(pipeline, "datetime") as clock, \
             patch.object(pipeline.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
             patch.object(pipeline, "_mark_fired"), \
             patch.object(pipeline, "start_pipeline", return_value=True) as start:
            clock.now.return_value = datetime(2026, 9, 15, 8, 15)
            with self.assertRaises(KeyboardInterrupt):
                pipeline._scheduler_loop()
        start.assert_called_once_with(None, crawl_filter={"channel": "wechat_local"})

    def test_crawl_passes_target_and_dates_to_selected_collector(self):
        collector = Mock()
        filt = {"channel": "wechat_local", "accounts": ["指定号"],
                "since_date": "2026-09-01", "until_date": "2026-09-15"}
        with patch.object(pipeline_stages, "active_collector", return_value=collector) as active:
            pipeline_stages._run_crawl(filt)
        active.assert_called_once_with("wechat_local")
        collector.fetch.assert_called_once_with(FetchScope(("指定号",), "2026-09-01", "2026-09-15"))

    def test_retry_preserves_original_channel_and_range(self):
        filt = {"channel": "wechat_local", "accounts": ["指定号"],
                "since_date": "2026-09-01", "until_date": "2026-09-15"}
        with patch.object(pipeline_retry, "_run_crawl") as crawl, \
             patch.object(pipeline_retry, "_run_export") as export, \
             patch.object(pipeline_retry, "_end_run"):
            pipeline_retry._run_retry_failed(["crawl", "export"], filt)
        crawl.assert_called_once_with(filt)
        export.assert_called_once_with("wechat_local")

    def test_start_freezes_channel_and_scope_in_saved_run(self):
        saved = deepcopy(pipeline_state._state)
        try:
            pipeline_state._state["running"] = False
            filt = {"channel": "wechat_local", "accounts": ["指定号"]}
            with patch.object(pipeline.threading, "Thread"), tempfile.TemporaryDirectory() as tmp, \
                 patch.object(pipeline_state, "_STATE_FILE", Path(tmp) / "state.json"):
                self.assertTrue(pipeline.start_pipeline(["crawl"], filt))
                filt["channel"] = "wechat_a"
                filt["accounts"].append("另一号")
                pipeline_state._end_run()
                last = pipeline_state.load_last_run()
                self.assertEqual({"channel": "wechat_local", "accounts": ["指定号"]}, last["crawl_filter"])
        finally:
            pipeline_state._state.clear()
            pipeline_state._state.update(saved)


if __name__ == "__main__":
    unittest.main()
