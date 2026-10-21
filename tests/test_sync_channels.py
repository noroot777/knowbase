from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kb import llm, pipeline, pipeline_retry, pipeline_segments, pipeline_stages
from kb import pipeline_state
from kb.collectors import FetchScope
from kb.web import pipeline_api


class ChannelAPITests(unittest.TestCase):
    def setUp(self):
        # secrets.json 指到 tmp:测试不该读开发机上那份真配置,更不该改它。
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original = llm._SECRETS_PATH
        llm._SECRETS_PATH = Path(tmp.name) / "secrets.json"
        self.addCleanup(lambda: setattr(llm, "_SECRETS_PATH", original))
        self.app = FastAPI()
        self.app.include_router(pipeline_api.router)
        self.client = TestClient(self.app)

    def test_default_is_the_local_database_and_explicit_scope_is_preserved(self):
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            self.assertEqual(200, self.client.post("/api/pipeline/run", json={}).status_code)
            self.assertEqual("wechat_local", start.call_args.kwargs["crawl_filter"]["channel"])
            body = {"channel": "wechat_local", "accounts": ["指定号"],
                    "since_date": "2026-09-15", "until_date": "2026-09-15"}
            self.assertEqual(200, self.client.post("/api/pipeline/run", json=body).status_code)
            # 多出来的 force 是「人按的那一轮跳过退避」,见
            # test_a_run_through_this_api_is_a_hand_press_and_skips_the_backoff
            self.assertEqual({**body, "force": True},
                             start.call_args.kwargs["crawl_filter"])

    def test_a_source_sensitive_request_without_a_source_is_refused(self):
        """**点名了抓取/导出、却一个来源都没给:拒,不许替他挑一个。**

        没有这一道时兜底会把「没说」整理成默认采集器(实测 `wechat_local`):只勾
        「导出文章」的那一轮于是导出了公众号,而用户选的是订阅源 —— 面板上一个字
        都不说(第 3 轮审查 R1,前端那一侧在 `tests/pipe_stages_probe.mjs`)。
        导出也是按来源跑的(`_run_export` 走 `_per_channel`),所以 `export` 自己
        就够格要求来源,不必和 `crawl` 一起出现。
        """
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            for stages in (["export"], ["crawl"], ["export", "index"]):
                r = self.client.post("/api/pipeline/run", json={"stages": stages})
                self.assertEqual(400, r.status_code, stages)
                self.assertIn("请至少选择一个来源", r.json()["detail"])
            self.assertFalse(start.called, "拒掉的那一次压根不许起跑")

    def test_whole_library_stages_still_run_without_a_source(self):
        """**反过来钉一边**:只剩全库那几步时没有来源照旧放行。

        `index` 扫整个 vault、`extract` 扫全库待提取队列,它们连通道参数都不收 ——
        按「缺来源一律拒」去拦,只勾这几步的那一轮压根起不了跑。
        """
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            r = self.client.post("/api/pipeline/run",
                                 json={"stages": ["index", "extract"]})
            self.assertEqual(200, r.status_code, r.text)
            self.assertTrue(start.called)

    def test_a_run_through_this_api_is_a_hand_press_and_skips_the_backoff(self):
        """走到这个接口的都是**人按的**,所以这一轮得**跳过来源退避**。

        红的是 `crawl_filter` 里有没有 `force` —— 从那儿往下
        (`pipeline_stages._fetch` → `sync.collect` 那道 `retry_at`)另有自己的红
        (`tests/test_retry_scope.py`)。不带的后果是:用户点了同步,退避中的那几条被
        静默跳过(只记一笔 `deferred`),而页面说「同步已开始」—— 而「失败之后手动点
        一次同步」恰恰是退避唯一的出路(CLAUDE.md「服务与运行时」)。

        **判在 `kb.run_trigger` 一处**:入口只声明自己怎么被触发的,所以这里钉的是
        「**什么都不声明**也算人按的」—— 定时那一条压根不走 HTTP(`kb.daily_run.tick`
        直接进管道),将来谁忘了声明,落在「多抓一次」那一侧而不是「静默不同步」。

        **反过来钉一边**:明说了是定时轮次的那一轮照旧不带 —— 退避本来就是给它用的。
        """
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            r = self.client.post("/api/pipeline/run", json={"channel": "rss"})
            self.assertEqual(200, r.status_code, r.text)
            self.assertIs(True, start.call_args.kwargs["crawl_filter"].get("force"))
            self.client.post("/api/pipeline/run",
                             json={"channel": "rss", "trigger": "schedule"})
            self.assertNotIn("force", start.call_args.kwargs["crawl_filter"])

    def test_an_old_tab_still_sending_force_gets_the_same_round(self):
        """浏览器里缓存着的老页面发的是 `force: true` —— 那个字段现在没人读了。

        钉的是「老页面不会因此换一种行为」:它要的就是跳过退避,而现在默认就跳。
        后果不对称 —— 若把未知字段当成「按定时跑」,老页面点一次同步会静默跳过刚
        失败的那几条。
        """
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            r = self.client.post("/api/pipeline/run",
                                 json={"channel": "rss", "force": True})
            self.assertEqual(200, r.status_code, r.text)
            self.assertIs(True, start.call_args.kwargs["crawl_filter"].get("force"))

    def test_a_retired_channel_name_runs_as_the_one_that_replaced_it(self):
        """老页面/老脚本里写着的 `wechat_oa` 不该变成 400。

        那条凭证通道退役了,可盘上和别人脚本里的旧名字还在。按"不认识的通道"
        拒掉等于让一次本来该跑的同步静静地失败,而它要抓的号一个没变。
        """
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            r = self.client.post("/api/pipeline/run", json={"channel": "wechat_oa"})
            self.assertEqual(200, r.status_code)
            self.assertEqual("wechat_local", start.call_args.kwargs["crawl_filter"]["channel"])

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

    def test_several_sources_run_in_one_pass_with_their_own_subscriptions(self):
        body = {"channels": [{"channel": "wechat_local", "accounts": ["甲"]},
                             {"channel": "rss"}],
                "since_date": "2026-09-15", "until_date": "2026-09-15"}
        with patch.object(pipeline, "start_pipeline", return_value=True) as start:
            self.assertEqual(200, self.client.post("/api/pipeline/run", json=body).status_code)
        crawl_filter = start.call_args.kwargs["crawl_filter"]
        self.assertEqual([{"channel": "wechat_local", "accounts": ["甲"]},
                          {"channel": "rss", "accounts": []}], crawl_filter["channels"])
        # 单来源的老字段仍然填着第一个来源,旧的读取方不用改
        self.assertEqual(("wechat_local", ["甲"]), (crawl_filter["channel"], crawl_filter["accounts"]))

    def test_repeated_empty_or_unknown_sources_are_rejected_before_run(self):
        bodies = [{"channels": []},
                  {"channels": [{"channel": "rss"}, {"channel": "rss"}]},
                  {"channels": [{"channel": "bad"}]},
                  {"channels": [{"channel": "rss", "accounts": [" "]}]}]
        with patch.object(pipeline, "start_pipeline") as start:
            for body in bodies:
                self.assertEqual(400, self.client.post("/api/pipeline/run", json=body).status_code, body)
            start.assert_not_called()


class ChannelDispatchTests(unittest.TestCase):
    def test_pending_bodies_are_visible_without_red_stage_or_immediate_retry(self):
        from kb.collectors import wechat_local

        body = {"title": "目标 · 测试文章", "reason": "正文暂未获取，已安排自动重试"}
        result = {"new_articles": 2, "errors": [], "download_succeeded": 1,
                  "body_failures": [body], "unreadable_messages": 1}
        with patch.object(wechat_local, "_stage") as stage:
            wechat_local.WeChatLocalCollector._report(0, result)
        self.assertEqual(("crawl", "ok"), stage.call_args.args[:2])
        self.assertEqual([{**body, "pending": True}], stage.call_args.kwargs["failures"])
        detail = stage.call_args.args[2]
        self.assertIn("正文待重试 1 篇", detail)
        self.assertNotIn("失败", detail)
        with patch.object(pipeline_retry, "last_result", return_value={
                "stages": {"crawl": {"status": "ok", "detail": detail, "failures": [body]}}}), \
             patch("kb.store.connect"), patch("kb.extract.pending_docs", return_value=[]):
            self.assertEqual([], pipeline_retry.retry_failed_plan())

    def test_a_crawl_that_never_ran_reports_zero_coverage(self):
        """**审查 R1 的另一半**:采集器没装好/起不来那一轮,覆盖范围是空的。

        这两档的阶段结论是 `skipped`,而 skipped 不算失败 —— 按请求范围记账的话,
        一句「跳过抓取」会去结清今天早些时候那笔真失败(当日账本那一层的后果和退避
        那一档一模一样,见 `tests/test_retry_daybook.py`)。

        **反过来钉一边**:真跑完的那一轮照旧按请求范围报 —— 公众号的失败是文章级
        的,摊不到号上,账号那一层这一轮确实都跑过。
        """
        from kb.collectors import Coverage, wechat_local

        scope = FetchScope(("甲",))
        with patch.object(wechat_local, "_stage"):
            with patch.object(wechat_local, "_backend_problem", return_value="没装"):
                self.assertEqual(Coverage(),
                                 wechat_local.WeChatLocalCollector().fetch(scope))
            with patch.object(wechat_local, "_backend_problem", return_value=""), \
                 patch.object(wechat_local.WeChatLocalCollector, "_stream",
                              side_effect=OSError("起不来")):
                self.assertEqual(Coverage(),
                                 wechat_local.WeChatLocalCollector().fetch(scope))
            with patch.object(wechat_local, "_backend_problem", return_value=""), \
                 patch.object(wechat_local.WeChatLocalCollector, "_stream",
                              return_value=(0, {})), \
                 patch.object(wechat_local.WeChatLocalCollector, "_report"):
                self.assertEqual(Coverage(targets=("甲",)),
                                 wechat_local.WeChatLocalCollector().fetch(scope))
                self.assertEqual(Coverage(whole=True),
                                 wechat_local.WeChatLocalCollector().fetch(FetchScope()))

    def test_export_missing_bodies_reach_panel_as_pending_details(self):
        import subprocess
        from kb.collectors import wechat_local

        payload = {"detail": "完成: 导出 0, 待补正文 2", "no_body": ["号甲 · 文甲", "号乙 · 文乙"]}
        with patch.object(wechat_local, "_backend_problem", return_value=""), \
             patch.object(wechat_local, "_run_cli", return_value=subprocess.CompletedProcess(
                 [], 0, stdout=json.dumps(payload), stderr="")), patch.object(wechat_local, "_stage") as stage:
            wechat_local.WeChatLocalCollector().materialize()
        self.assertEqual(("export", "ok"), stage.call_args.args[:2])
        items = stage.call_args.kwargs["failures"]
        self.assertEqual(2, len(items))
        self.assertTrue(all(item["pending"] for item in items))
        self.assertIn("保留待补", items[0]["reason"])
        self.assertNotIn("is_wx_deleted", items[0]["reason"])

    def test_local_progress_renders_discovery_and_body_counts_without_changing_old_channel(self):
        from kb.collectors.wechat_local import WeChatLocalCollector

        detail = WeChatLocalCollector._progress_detail
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
        from kb.collectors import wechat_local
        from kb.collectors.wechat_local import WeChatLocalCollector

        for fail in (False, True):
            with self.subTest(fail=fail):
                collector = WeChatLocalCollector()
                collector._cache = (0, object())
                with patch.object(wechat_local, "_backend_problem", return_value=""), \
                     patch.object(wechat_local, "_stage"), \
                     patch.object(collector, "_stream", return_value=(0, {}),
                                  side_effect=subprocess.TimeoutExpired("collect", 1) if fail else None):
                    collector.fetch(FetchScope())
                self.assertIsNone(collector._cache)

    def test_local_coverage_and_deferred_bodies_do_not_look_like_fresh_failures(self):
        from kb.collectors import wechat_local

        result = {"new_articles": 2, "errors": [], "missing_accounts": ["未订阅"],
                  "deferred_articles": 3, "unavailable_articles": 1}
        with patch.object(wechat_local, "_stage") as stage:
            wechat_local.WeChatLocalCollector._report(0, result)
        self.assertEqual(("crawl", "ok"), stage.call_args.args[:2])
        self.assertIn("本机暂无推送 1 个号（在绿泡泡关注并同步后可采集）", stage.call_args.args[2])
        self.assertIn("等待重试 3 篇", stage.call_args.args[2])
        self.assertEqual([], stage.call_args.kwargs["failures"])
        self.assertNotIn("覆盖待核实", stage.call_args.args[2])

    def test_missing_fourteen_accounts_do_not_become_permanent_pending_details(self):
        from kb.collectors import wechat_local

        result = {"new_articles": 0, "errors": [], "download_succeeded": 0,
                  "missing_accounts": [f"未同步{i}" for i in range(14)]}
        for _ in range(2):
            with patch.object(wechat_local, "_stage") as stage:
                wechat_local.WeChatLocalCollector._report(0, result)
            self.assertEqual(("crawl", "ok"), stage.call_args.args[:2])
            self.assertIn("本机暂无推送 14 个号", stage.call_args.args[2])
            self.assertEqual([], stage.call_args.kwargs["failures"])

    def test_unattributed_messages_are_one_pending_total_not_per_account_counts(self):
        from kb.collectors import wechat_local

        result = {"errors": [], "download_succeeded": 0, "unattributed_messages": 2,
                  "coverage_unknown_accounts": ["甲", "乙"]}
        with patch.object(wechat_local, "_stage") as stage:
            wechat_local.WeChatLocalCollector._report(0, result)
        self.assertEqual(("crawl", "ok"), stage.call_args.args[:2])
        self.assertIn("另有归属待核实的不可读推送 2 条（存量）", stage.call_args.args[2])
        self.assertNotIn("本机暂无推送", stage.call_args.args[2])
        self.assertNotIn("推送解析待核查 2 个号", stage.call_args.args[2])
        items = stage.call_args.kwargs["failures"]
        self.assertEqual(1, len(items))
        self.assertTrue(items[0]["pending"])
        self.assertIn("未计入各号条数", items[0]["reason"])
        self.assertIn("可能相关账号：甲、乙（归属待核实）", items[0]["reason"])

    def test_unknown_push_summary_preserves_missing_account_advice_without_naming_them(self):
        from kb.collectors import wechat_local

        result = {"errors": [], "download_succeeded": 0, "unattributed_messages": 1,
                  "missing_accounts": [f"未同步{i}" for i in range(14)],
                  "coverage_unknown_accounts": []}
        with patch.object(wechat_local, "_stage") as stage:
            wechat_local.WeChatLocalCollector._report(0, result)
        self.assertIn("未匹配到本机推送 14 个号", stage.call_args.args[2])
        self.assertIn("若未关注或未同步，请在绿泡泡关注并同步", stage.call_args.args[2])
        self.assertIn("另有 1 条无法确认归属的推送，可能属于其中某个号", stage.call_args.args[2])
        self.assertNotIn("推送覆盖待核实", stage.call_args.args[2])
        items = stage.call_args.kwargs["failures"]
        self.assertEqual(1, len(items))
        self.assertIn("另有 1 条本机推送无法解析", items[0]["reason"])
        self.assertNotIn("可能相关账号", items[0]["reason"])
        self.assertNotIn("未同步", str(items))

    def test_renamed_unreadable_account_gets_qualification_not_unconditional_subscribe_advice(self):
        from kb.collectors import wechat_local

        result = {"errors": [], "download_succeeded": 0, "unattributed_messages": 3,
                  "missing_accounts": ["旧名"], "coverage_unknown_accounts": []}
        with patch.object(wechat_local, "_stage") as stage:
            wechat_local.WeChatLocalCollector._report(0, result)
        detail = stage.call_args.args[2]
        self.assertIn("未匹配到本机推送 1 个号", detail)
        self.assertIn("另有 3 条无法确认归属的推送，可能属于其中某个号", detail)
        self.assertNotIn("本机暂无推送", detail)
        self.assertNotIn("关注并同步后可采集", detail)
        items = stage.call_args.kwargs["failures"]
        self.assertEqual(1, len(items))
        self.assertIn("这些推送也可能来自未匹配到推送的账号", items[0]["reason"])

    def test_account_diagnostics_survive_fifty_body_failures_and_errors_stay_first(self):
        from kb.collectors import wechat_local

        saved = deepcopy(pipeline_state._state)
        try:
            result = {"errors": ["真实读取失败"], "download_succeeded": 0,
                      "unreadable_messages": 3, "unattributed_messages": 2,
                      "unreadable_accounts": [{"name": "甲", "messages": 3, "identity_confirmed": True}],
                      "body_failures": [{"title": f"正文{i}", "reason": "等待自动重试"} for i in range(60)]}
            wechat_local.WeChatLocalCollector._report(0, result)
            entry = pipeline_state._state["stages"]["crawl"]
            self.assertEqual("error", entry["status"])
            self.assertEqual(50, len(entry["failures"]))
            self.assertEqual(["真实读取失败", "甲", "推送归属待核实"],
                             [item["title"] for item in entry["failures"][:3]])
            self.assertIn("所选公众号现存不可读推送 3 条", entry["detail"])
            self.assertNotIn("尚未发现可采集文章", entry["failures"][1]["reason"])
        finally:
            pipeline_state._state.clear()
            pipeline_state._state.update(saved)

    def test_all_unreadable_account_is_named_with_correct_remedy_and_no_immediate_retry(self):
        from kb.collectors import wechat_local

        for confirmed in (True, False):
            result = {"new_articles": 0, "errors": [], "download_succeeded": 0,
                      "missing_accounts": [], "unreadable_messages": 3,
                      "unreadable_accounts": [{"name": "目标", "messages": 3,
                                               "identity_confirmed": confirmed}]}
            with patch.object(wechat_local, "_stage") as stage:
                wechat_local.WeChatLocalCollector._report(0, result)
            self.assertEqual(("crawl", "ok"), stage.call_args.args[:2])
            detail = stage.call_args.args[2]
            self.assertIn("推送解析待核查 1 个号", detail)
            self.assertNotIn("本机暂无推送", detail)
            items = stage.call_args.kwargs["failures"]
            self.assertEqual("目标", items[0]["title"])
            self.assertIn("3 条推送无法解析", items[0]["reason"])
            self.assertIn("请检查读取器或推送格式", items[0]["reason"])
            self.assertEqual(not confirmed, "归属待核实" in items[0]["reason"])
            with patch.object(pipeline_retry, "last_result", return_value={"stages": {
                    "crawl": {"status": "ok", "detail": detail, "failures": items}}}), \
                 patch("kb.store.connect"), patch("kb.extract.pending_docs", return_value=[]):
                self.assertEqual([], pipeline_retry.retry_failed_plan())

    def test_collector_cli_gets_scope_and_reports_read_failures(self):
        from kb.collectors import wechat_local
        from kb.collectors.wechat_local import collector

        result = {"new_articles": 0, "errors": ["本机推送未同步"]}
        with patch.object(wechat_local, "_backend_problem", return_value=""), \
             patch.object(collector, "_stream", return_value=(0, result)) as stream, \
             patch.object(wechat_local, "_stage") as stage:
            collector.fetch(FetchScope(("指定号",), "2026-09-01", "2026-09-15"))
        argv = stream.call_args.args[0]
        self.assertEqual(["collect"], argv[:1])
        self.assertEqual("指定号", argv[argv.index("--targets") + 1])
        self.assertEqual("2026-09-15", argv[argv.index("--until") + 1])
        self.assertEqual(("crawl", "error"), stage.call_args.args[:2])

    def test_crawl_passes_target_and_dates_to_selected_collector(self):
        collector = Mock()
        filt = {"channel": "wechat_local", "accounts": ["指定号"],
                "since_date": "2026-09-01", "until_date": "2026-09-15"}
        with patch.object(pipeline_stages, "active_collector", return_value=collector) as active:
            pipeline_stages._run_crawl(filt)
        active.assert_called_once_with("wechat_local")
        collector.fetch.assert_called_once_with(FetchScope(("指定号",), "2026-09-01", "2026-09-15"))

    def test_each_source_fetches_its_own_targets_and_keeps_the_others_result(self):
        """多来源同抓:一个来源红了不能把另一个来源的结论顶掉。"""
        saved = deepcopy(pipeline_state._state)
        try:
            pipeline_segments.clear_segments()
            pipeline_state._state["stages"] = {s: {"status": "", "detail": ""}
                                               for s in pipeline_state.STAGES}
            local, rss = Mock(), Mock()
            local.channel_label, rss.channel_label = "本机库", "RSS"
            local.fetch.side_effect = lambda scope: pipeline_state._stage("crawl", "ok", "新增 2 篇")
            rss.fetch.side_effect = lambda scope: pipeline_state._stage(
                "crawl", "error", "连接失败", failures=[{"title": "某源", "reason": "超时"}])
            filt = {"channel": "wechat_local",
                    "channels": [{"channel": "wechat_local", "accounts": ["甲"]},
                                 {"channel": "rss", "accounts": []}]}
            with patch.object(pipeline_stages, "active_collector", side_effect=[local, rss]):
                pipeline_stages._run_crawl(filt)
            local.fetch.assert_called_once_with(FetchScope(("甲",)))
            rss.fetch.assert_called_once_with(FetchScope())
            entry = pipeline_state._state["stages"]["crawl"]
            self.assertEqual("error", entry["status"])
            self.assertEqual("本机库：新增 2 篇；RSS：连接失败", entry["detail"])
            self.assertEqual([{"title": "某源", "reason": "超时"}], entry["failures"])
        finally:
            pipeline_segments.clear_segments()
            pipeline_state._state.clear()
            pipeline_state._state.update(saved)

    def test_one_source_blowing_up_does_not_cancel_the_rest(self):
        """采集器直接抛异常时,勾了的其余来源照样要跑完,面板上也得看见是谁挂的。"""
        saved = deepcopy(pipeline_state._state)
        try:
            pipeline_segments.clear_segments()
            pipeline_state._state["stages"] = {s: {"status": "", "detail": ""}
                                               for s in pipeline_state.STAGES}
            broken, rss = Mock(), Mock()
            broken.channel_label, rss.channel_label = "本机库", "RSS"
            broken.fetch.side_effect = RuntimeError("读取器没装")
            rss.fetch.side_effect = lambda scope: pipeline_state._stage("crawl", "ok", "新增 3 篇")
            filt = {"channel": "wechat_local",
                    "channels": [{"channel": "wechat_local"}, {"channel": "rss"}]}
            with patch.object(pipeline_stages, "active_collector", side_effect=[broken, rss]):
                pipeline_stages._run_crawl(filt)
            rss.fetch.assert_called_once_with(FetchScope())
            entry = pipeline_state._state["stages"]["crawl"]
            self.assertEqual("error", entry["status"])
            self.assertIn("本机库：RuntimeError: 读取器没装", entry["detail"])
            self.assertIn("RSS：新增 3 篇", entry["detail"])
        finally:
            pipeline_segments.clear_segments()
            pipeline_state._state.clear()
            pipeline_state._state.update(saved)

    def test_a_source_whose_prep_blows_up_does_not_cancel_the_rest(self):
        """**建账那一步也在隔离里面**:它炸了也只终结这一个来源。

        建账要先算「这一步在这个来源上实际覆盖哪几个目标」,点名了目标就要**读一次
        订阅库**(`pipeline_results.planned_targets` → `FeedCollector.stable_targets`)
        —— 这一步在 fetch **之前**。隔离原来只包 fetch 那一句,于是这次读失败时异常
        是从 `with` 那一行抛的:连这个来源自己的 try 都还没进去,整个来源循环当场
        跳出。勾了订阅源和公众号两个来源、订阅库临时读不出来,**公众号一次都不跑**
        (第 4 轮审查 R1 实测 `wechat_calls=[]`)。

        判据落在**后面那个健康来源的 `fetch` 被叫了没有**,外加面板上这一步写的是
        谁挂的 —— 不然「整步记成 error」照样能把这一局糊过去,而那恰恰会把这一轮里
        别的来源也一起染红。
        """
        saved = deepcopy(pipeline_state._state)
        try:
            pipeline_segments.clear_segments()
            pipeline_state._state["stages"] = {s: {"status": "", "detail": ""}
                                               for s in pipeline_state.STAGES}
            broken, healthy = Mock(), Mock()
            broken.channel_label, healthy.channel_label = "RSS", "本机库"
            # 注入的是**建账那一步**要读的那次库(不是 fetch):`stable_targets` 正是
            # 结果账算覆盖范围时问的那一下。
            broken.stable_targets.side_effect = OSError("订阅库暂时读不出来")
            healthy.fetch.side_effect = lambda scope: pipeline_state._stage(
                "crawl", "ok", "新增 3 篇")
            filt = {"channel": "rss",
                    "channels": [{"channel": "rss", "accounts": ["A"]},
                                 {"channel": "wechat_local", "accounts": []}]}
            with patch.object(pipeline_stages, "active_collector",
                              side_effect=[broken, healthy]):
                pipeline_stages._run_crawl(filt)
            healthy.fetch.assert_called_once()
            broken.fetch.assert_not_called()      # 它压根没走到 fetch 那一步
            entry = pipeline_state._state["stages"]["crawl"]
            self.assertEqual("error", entry["status"])
            self.assertIn("RSS：OSError: 订阅库暂时读不出来", entry["detail"],
                          "面板上看不出是谁挂的")
            self.assertIn("本机库：新增 3 篇", entry["detail"],
                          "健康来源的结论被那个失败盖掉了")
        finally:
            pipeline_segments.clear_segments()
            pipeline_state._state.clear()
            pipeline_state._state.update(saved)

    def test_stop_request_keeps_the_remaining_sources_untouched(self):
        """停止是对整次同步说的:点了之后不许再起下一个来源。"""
        saved = deepcopy(pipeline_state._state)
        try:
            pipeline_segments.clear_segments()
            pipeline_state._state["stages"] = {s: {"status": "", "detail": ""}
                                               for s in pipeline_state.STAGES}
            pipeline_state._state["stop_requested"] = False
            first, second = Mock(), Mock()
            first.channel_label, second.channel_label = "本机库", "RSS"

            def _stop(scope):
                pipeline_state._state["stop_requested"] = True
                pipeline_state._stage("crawl", "stopped", "已停止")

            first.fetch.side_effect = _stop
            filt = {"channel": "wechat_local",
                    "channels": [{"channel": "wechat_local"}, {"channel": "rss"}]}
            with patch.object(pipeline_stages, "active_collector", side_effect=[first, second]):
                pipeline_stages._run_crawl(filt)
            first.fetch.assert_called_once()
            second.fetch.assert_not_called()
        finally:
            pipeline_segments.clear_segments()
            pipeline_state._state.clear()
            pipeline_state._state.update(saved)

    def test_export_materialises_every_selected_source(self):
        local, rss = Mock(), Mock()
        filt = {"channel": "wechat_local",
                "channels": [{"channel": "wechat_local"}, {"channel": "rss"}]}
        with patch.object(pipeline_stages, "active_collector", side_effect=[local, rss]):
            pipeline_stages._run_export(filt)
        local.materialize.assert_called_once_with()
        rss.materialize.assert_called_once_with()

    def test_retry_preserves_original_channel_and_range(self):
        filt = {"channel": "wechat_local", "accounts": ["指定号"],
                "since_date": "2026-09-01", "until_date": "2026-09-15"}
        with patch.object(pipeline_retry, "_run_crawl") as crawl, \
             patch.object(pipeline_retry, "_run_export") as export, \
             patch.object(pipeline_retry, "_end_run"):
            pipeline_retry._run_retry_failed(["crawl", "export"], filt)
        crawl.assert_called_once_with(filt)
        # 导出也要拿到整份范围:多来源那一轮的导出得把每个来源各跑一遍
        export.assert_called_once_with(filt)

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
