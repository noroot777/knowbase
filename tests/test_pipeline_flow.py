# -*- coding: utf-8 -*-
"""同步中心那条「按类型排的线」:跳过、提前结束、按来源串着跑(`kb/pipeline_flow.py`、
`kb/pipeline_sources.py`、`kb/pipeline._sources`)。硬约束在 `docs/decisions/sync-flow.md`。

判据落在后果上:哪个来源哪一步**真被调用了**、没跑的那几格记成了什么、账本那一轮开没开、
「交完了」记没记 —— 不是「标志打上没有」。每一条都反过来钉一边(该放行的照旧放行)。

编排层用桩替掉真执行(`_guarded` 记下「这一步真跑了」,`_settle` 记下「这一步没跑、记成
什么」),计划本身(`pipeline_flow`)一律是真的。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from kb import daily_run
from kb import daily_state
from kb import pipeline as pl
from kb import pipeline_batch as pb
from kb import pipeline_failures as pf
from kb import pipeline_flow as flow
from kb import pipeline_persist
from kb import pipeline_results as results
from kb import pipeline_segments
from kb import pipeline_sources as srcs
from kb import pipeline_state as ps
from kb.web import pipeline_api
from tests.pipeline_case import HistoryCase

SCOPES = [{"channel": "a", "accounts": []}, {"channel": "b", "accounts": []}]
OWNER = "run-1"


def _plan(channels=("a", "b"), stages=("crawl", "export", "index", "extract")):
    """直接摆一份手动同步的计划(不走 `begin`:它要去解析真采集器)。"""
    with patch.object(flow, "_labels", return_value={}):
        nodes = flow.source_nodes(channels, stages) + flow.group_nodes(stages)
    flow._plans[OWNER] = flow._new(OWNER, "sync", nodes)


def _mine() -> dict:
    return flow._plans[OWNER]


class _Base(unittest.TestCase):
    def setUp(self):
        flow._plans.clear()
        # 此刻在跑的就是 OWNER 那一轮(提前结束只落在它自己的采集器上,见 `cutting`)
        state = patch.dict(ps._state, {"stop_requested": False, "running": True,
                                       "run_id": OWNER, "batch": "",
                                       "planned_stages": ["crawl", "export"],
                                       "stages": {s: {"status": "", "detail": ""}
                                                  for s in ps.STAGES},
                                       "log": []})
        state.start()
        self.addCleanup(state.stop)
        # 状态接口会顺手把「恢复过了」记成模块级的一格:还回去,别漏给别的文件
        restored = patch.object(ps, "_restored", ps._restored)
        restored.start()
        self.addCleanup(restored.stop)
        self.addCleanup(flow._plans.clear)
        results.clear()
        self.addCleanup(results.clear)
        self.addCleanup(pipeline_segments.clear_segments)


class FlowRequestTest(_Base):
    def test_skip_waiting_then_unskip_before_its_turn(self):
        _plan()
        self.assertEqual((True, ""), flow.request(OWNER, "src:b", "skip"))
        self.assertFalse(flow.enter(OWNER, "src:b"))
        # 进过门(被跳过)之后就不许撤回了
        self.assertFalse(flow.request(OWNER, "src:b", "unskip")[0])
        _plan()
        flow.request(OWNER, "src:b", "skip")
        self.assertEqual((True, ""), flow.request(OWNER, "src:b", "unskip"))
        self.assertTrue(flow.enter(OWNER, "src:b"))

    def test_skip_refused_once_running_and_owner_must_match(self):
        _plan()
        self.assertTrue(flow.enter(OWNER, "src:a"))
        ok, why = flow.request(OWNER, "src:a", "skip")
        self.assertFalse(ok)
        self.assertIn("结束本类型", why)
        # 别人那份计划(或者页面上开着的是上一轮)一律不受理,计划一个字不动
        self.assertEqual((False, flow.STALE), flow.request("run-0", "src:b", "skip"))
        self.assertEqual([], _mine()["skips"])

    def test_cut_only_while_running_and_not_on_last_step(self):
        _plan()
        self.assertFalse(flow.request(OWNER, "src:a", "cut")[0])      # 还没轮到
        flow.enter(OWNER, "src:a")
        flow.advance(OWNER, "src:a", "crawl")
        self.assertEqual((True, ""), flow.request(OWNER, "src:a", "cut"))
        self.assertTrue(flow.halted())
        # 导出已经是来源这一类的最后一步:受理了也兑现不了,所以不受理
        _plan()
        flow.enter(OWNER, "src:a")
        flow.advance(OWNER, "src:a", "export")
        self.assertFalse(flow.request(OWNER, "src:a", "cut")[0])
        self.assertFalse(flow.halted())

    def test_daily_node_cannot_be_cut(self):
        with patch.object(flow, "_labels", return_value={}):
            flow.open_daily("batch-1", ["a"], library=True)
        flow.enter("batch-1", "daily")
        ok, why = flow.request("batch-1", "daily", "cut")
        self.assertFalse(ok)
        # 生成那一档整轮停止也不受理(`daily_run.request_stop`),不许把人指到那颗按钮上
        self.assertIn("没法中途结束", why)
        self.assertNotIn("用「停止整轮」", why)

    def test_refused_once_stop_requested(self):
        _plan()
        ps._state["stop_requested"] = True
        self.assertFalse(flow.request(OWNER, "src:b", "skip")[0])
        self.assertEqual([], _mine()["skips"])


class AdvanceTest(_Base):
    def test_cut_accepted_during_crawl_stops_export_from_starting(self):
        """受理和换步在同一把锁里:受理之后,下一步一定不开跑。"""
        _plan()
        flow.enter(OWNER, "src:a")
        self.assertTrue(flow.advance(OWNER, "src:a", "crawl"))
        flow.request(OWNER, "src:a", "cut")
        self.assertFalse(flow.advance(OWNER, "src:a", "export"))
        self.assertNotIn("export", _mine()["times"]["src:a"]["steps"])

    def test_without_cut_steps_switch_and_close_the_previous_one(self):
        _plan()
        flow.enter(OWNER, "src:a")
        self.assertTrue(flow.advance(OWNER, "src:a", "crawl"))
        self.assertTrue(flow.advance(OWNER, "src:a", "export"))
        steps = _mine()["times"]["src:a"]["steps"]
        self.assertTrue(steps["crawl"][1], "上一步换走时要收尾")
        self.assertEqual("", steps["export"][1])

    def test_cut_ends_with_the_node(self):
        """提前结束只管这一类:离开之后下一类不受它影响(halted 回到 False)。"""
        _plan()
        flow.enter(OWNER, "src:a")
        flow.advance(OWNER, "src:a", "crawl")
        flow.request(OWNER, "src:a", "cut")
        flow.leave(OWNER, "src:a")
        self.assertFalse(flow.halted())
        self.assertTrue(flow.enter(OWNER, "src:b"))
        self.assertTrue(flow.advance(OWNER, "src:b", "crawl"))

    def test_unmanaged_owner_is_never_blocked(self):
        _plan()
        flow.request(OWNER, "src:a", "skip")
        self.assertTrue(flow.enter("", "src:a"))
        self.assertTrue(flow.enter("someone-else", "src:a"))
        self.assertTrue(flow.advance("someone-else", "src:a", "crawl"))


class BeginTest(_Base):
    def test_daily_plan_is_shared_by_its_batch_and_not_by_a_manual_sync(self):
        with patch.object(flow, "_labels", return_value={}):
            flow.open_daily("batch-1", ["a"], library=False)
        self.assertEqual("batch-1", flow.begin("r1", "batch-1", ["crawl"], None))
        flow.request("batch-1", "src:a", "skip")
        # 两轮日报之间挤进来的手动同步:拿它自己那一份,日报那一份(连同跳过名单)原样留着
        with patch("kb.pipeline_stages.crawl_channels", return_value=[SCOPES[0]]), \
                patch.object(flow, "_labels", return_value={}):
            self.assertEqual("r2", flow.begin("r2", "", ["crawl", "export"], None))
        self.assertEqual(["src:a"], flow._plans["batch-1"]["skips"])
        self.assertEqual(["src:a"], [n["key"] for n in flow._plans["r2"]["nodes"]])
        self.assertTrue(flow.enter("r2", "src:a"), "日报那一份的跳过不许拦住别人那一轮")
        # 手动那一轮收尾只收它自己那一份
        flow.end("r2")
        self.assertIn("batch-1", flow._plans)
        self.assertNotIn("r2", flow._plans)


class RunSourcesTest(_Base):
    """多来源那一档:按来源串着跑,没跑的那几格各记一笔。"""

    def _run(self, on_crawl=None):
        calls, settled, opened, ended = [], [], [], []

        def guarded(stage, scope, collector, run):
            calls.append((stage, scope["channel"]))
            if on_crawl and stage == "crawl":
                on_crawl(scope["channel"])

        with patch.object(srcs, "crawl_channels", return_value=SCOPES), \
                patch.object(srcs, "_collector", side_effect=lambda s: (object(), None)), \
                patch.object(srcs, "round_key", return_value=("k", "seg")), \
                patch.object(srcs, "_guarded", side_effect=guarded), \
                patch.object(srcs, "_settle", side_effect=lambda st, sc, c, status, detail:
                             settled.append((st, sc["channel"], status, detail))), \
                patch.object(srcs, "_open", side_effect=lambda *a: opened.append(a)), \
                patch("kb.fetch_rounds.note_end", side_effect=lambda *a: ended.append(a)):
            srcs.run_sources(["crawl", "export"], None, OWNER)
        return calls, settled, opened, ended

    def test_each_source_runs_both_steps_before_the_next(self):
        _plan()
        calls, settled, opened, ended = self._run()
        self.assertEqual([("crawl", "a"), ("export", "a"), ("crawl", "b"), ("export", "b")], calls)
        self.assertEqual([], settled)
        self.assertEqual(1, len(opened))
        self.assertEqual([("k", "seg")], ended)

    def test_skipped_source_gets_skipped_cells_and_the_other_still_runs(self):
        _plan()
        flow.request(OWNER, "src:a", "skip")
        calls, settled, opened, _ = self._run()
        self.assertEqual([("crawl", "b"), ("export", "b")], calls)
        self.assertEqual([("crawl", "a", "skipped", srcs.SKIPPED),
                          ("export", "a", "skipped", srcs.SKIPPED)], settled)
        self.assertEqual(1, len(opened), "账本那一轮照开(b 真跑了)")

    def test_all_skipped_opens_no_round_and_records_no_end(self):
        """一个来源都没跑的那一轮不是一次抓取:不开轮,不记「交完了」。"""
        _plan()
        flow.request(OWNER, "src:a", "skip")
        flow.request(OWNER, "src:b", "skip")
        calls, settled, opened, ended = self._run()
        self.assertEqual([], calls)
        self.assertEqual(4, len(settled))
        self.assertEqual([], opened)
        self.assertEqual([], ended)

    def test_cut_during_crawl_skips_its_export_and_moves_on(self):
        _plan()
        seen = []

        def cut(channel):
            if channel == "a":
                self.assertEqual((True, ""), flow.request(OWNER, "src:a", "cut"))
                seen.append(flow.halted())      # 采集器在这一步里问「该停了吗」
        calls, settled, _, ended = self._run(on_crawl=cut)
        self.assertEqual([True], seen)
        self.assertEqual([("crawl", "a"), ("crawl", "b"), ("export", "b")], calls)
        self.assertEqual([("export", "a", "skipped", srcs.CUT)], settled)
        self.assertEqual(1, len(ended))
        self.assertFalse(flow.halted())

    def test_a_daily_batch_opening_mid_round_does_not_void_an_accepted_cut(self):
        """手动同步正在抓、刚受理了「结束本类型」,这时定时日报开张建它自己的计划:
        那一下照样兑现 —— 这个来源的导出不跑、采集器照样读到「该停了」。

        原来全局只有一格计划,日报一开张就把它换掉,手动那一轮回头再问时找不到自己的
        计划,被当成「本来就不受控」放行,导出照跑。
        """
        _plan()
        seen = []

        def cut_then_daily(channel):
            if channel == "a":
                self.assertEqual((True, ""), flow.request(OWNER, "src:a", "cut"))
                with patch.object(flow, "_labels", return_value={}):
                    flow.open_daily("batch-9", ["a", "b"], library=False)
                seen.append(flow.halted())
        calls, settled, _, _ = self._run(on_crawl=cut_then_daily)
        self.assertEqual([True], seen, "日报开张后,采集器读不到这一轮已经受理的提前结束")
        self.assertNotIn(("export", "a"), calls)
        self.assertEqual([("export", "a", "skipped", srcs.CUT)], settled)
        # 反过来钉一边:另一个来源照常跑完,日报那一份计划完好、没沾上这一轮的请求
        self.assertEqual([("crawl", "a"), ("crawl", "b"), ("export", "b")], calls)
        self.assertEqual({}, flow._plans["batch-9"]["cuts"])
        self.assertEqual([], flow._plans["batch-9"]["done"])

    def test_stop_settles_the_rest_as_stopped(self):
        _plan()

        def stop(channel):
            ps._state["stop_requested"] = True
        calls, settled, _, ended = self._run(on_crawl=stop)
        self.assertEqual([("crawl", "a")], calls)
        self.assertEqual([("export", "a", "stopped", srcs.STOPPED),
                          ("crawl", "b", "stopped", srcs.STOPPED),
                          ("export", "b", "stopped", srcs.STOPPED)], settled)
        # 停下来的那一轮照样走完了循环:「交完了」照记
        self.assertEqual(1, len(ended))

    def test_without_a_plan_everything_runs(self):
        calls, settled, _, _ = self._run()
        self.assertEqual(4, len(calls))
        self.assertEqual([], settled)


class SingleSourceTest(_Base):
    """只有一个来源时走老路:`_run_crawl` / `_run_export` 各调一次,参数原样。"""

    def _run(self, between=None):
        got = []
        filt = {"channel": "a", "since_date": "2026-10-01"}

        def crawl(f):
            got.append(("crawl", f))
            if between:
                between()
        with patch.object(pl, "crawl_channels", return_value=[SCOPES[0]]), \
                patch.object(pl, "_run_crawl", side_effect=crawl), \
                patch.object(pl, "_run_export", side_effect=lambda f: got.append(("export", f))), \
                patch.object(pl, "_stage", side_effect=lambda *a, **k: got.append(("stage",) + a[:3])):
            pl._sources(["crawl", "export"], filt, OWNER)
        return got, filt

    def test_old_path_keeps_its_arguments(self):
        _plan(channels=("a",))
        got, filt = self._run()
        self.assertEqual([("crawl", filt), ("export", filt)], got)

    def test_cut_skips_export_with_its_reason(self):
        _plan(channels=("a",))
        got, filt = self._run(between=lambda: flow.request(OWNER, "src:a", "cut"))
        self.assertEqual([("crawl", filt), ("stage", "export", "skipped", pl.CUT_EXPORT)], got)

    def test_skipped_source_runs_nothing(self):
        _plan(channels=("a",))
        flow.request(OWNER, "src:a", "skip")
        got, _ = self._run()
        self.assertEqual([("stage", "crawl", "skipped", pl.SKIPPED),
                          ("stage", "export", "skipped", pl.SKIPPED)], got)


class RecordTest(_Base):
    def test_own_flow_only_when_identity_matches(self):
        told = {"run": "r1", "owner": "r1", "nodes": []}
        self.assertEqual({"flow": told}, pipeline_persist._own_flow({"run_id": "r1", "flow": told}))
        self.assertEqual({}, pipeline_persist._own_flow({"run_id": "r2", "flow": told}))
        self.assertEqual({}, pipeline_persist._own_flow({"run_id": "r1", "flow": None}))

    def test_strip_reaches_result_cells_but_counts_cards_once(self):
        item = {"title": "x", "reason": "失败", "drop": {"channel": "rss", "id": "7"}}
        run = {"stages": {"crawl": {"status": "error", "failures": [dict(item)]}},
               "results": {"crawl": {"rss": {"status": "error", "failures": [dict(item)]}}}}
        keys = set(pf._item_keys(item))
        self.assertTrue(keys, "这条明细要有身份,否则这条用例什么都没验")
        self.assertEqual(1, pf.strip(run, keys))
        self.assertEqual([], run["stages"]["crawl"]["failures"])
        self.assertEqual([], run["results"]["crawl"]["rss"]["failures"])
        # 反过来:别的明细一条不动
        other = {"title": "y", "reason": "失败", "drop": {"channel": "rss", "id": "8"}}
        keep = {"results": {"crawl": {"rss": {"failures": [dict(other)]}}}}
        self.assertEqual(0, pf.strip(keep, keys))
        self.assertEqual(1, len(keep["results"]["crawl"]["rss"]["failures"]))


class StopIdentityTest(_Base):
    """停止要停的是确认框开着时那一轮:换了一轮就不许落到新那一轮头上。"""

    def test_pipeline_stop_refuses_a_round_that_is_no_longer_running(self):
        ps._state["run_id"] = "run-2"                  # 确认框开着时,run-1 收尾、run-2 接上
        with self.assertRaises(HTTPException) as hit:
            pipeline_api.pipeline_stop(pipeline_api.StopRequest(run_id="run-1"))
        self.assertEqual(409, hit.exception.status_code)
        self.assertFalse(ps._state["stop_requested"], "后来那一轮一个字节都不许动")
        # 反过来:身份对得上照停;不带请求体的老调用照旧停此刻那一轮
        pipeline_api.pipeline_stop(pipeline_api.StopRequest(run_id="run-2"))
        self.assertTrue(ps._state["stop_requested"])
        ps._state["stop_requested"] = False
        pipeline_api.pipeline_stop(None)
        self.assertTrue(ps._state["stop_requested"])

    def test_daily_stop_refuses_another_batch(self):
        with patch.dict(daily_state._state, {"running": True, "phase": "sync", "batch": "batch-2",
                                             "pipeline_run": "", "stop_requested": False,
                                             "log": []}):
            self.assertEqual((False, daily_run.STOP_STALE), daily_run.request_stop("batch-1"))
            self.assertFalse(daily_state._state["stop_requested"])
            self.assertTrue(daily_run.request_stop("batch-2")[0])
            self.assertTrue(daily_state._state["stop_requested"])


class CuttingOwnerTest(_Base):
    def test_a_daily_cut_does_not_reach_someone_elses_collector(self):
        """日报那一批进了某一类、却还排在别人那轮手动同步后面等:那一类上的提前结束不许
        让别人那一轮的采集器读成「该停了」。"""
        with patch.object(flow, "_labels", return_value={}):
            flow.open_daily("batch-1", ["a", "b"], library=False)
        flow.enter("batch-1", "src:a")
        self.assertEqual((True, ""), flow.request("batch-1", "src:a", "cut"))
        ps._state.update(run_id="manual-9", batch="")
        self.assertFalse(flow.halted())
        # 反过来:轮到它自己那一轮管道(属于这一批)时照样生效
        ps._state.update(run_id="r-x", batch="batch-1")
        self.assertTrue(flow.halted())
        ps._state["running"] = False
        self.assertFalse(flow.halted(), "没有管道在跑就没有谁可停")


class StoppingOwnerTest(_Base):
    """「这一轮已经要停了」只认归属相符的那一份停止标记。"""

    def setUp(self):
        super().setUp()
        with patch.object(flow, "_labels", return_value={}):
            flow.open_daily("batch-1", ["a", "b"], library=False)
        daily = patch.dict(daily_state._state, {"stop_requested": False, "batch": "batch-1"})
        daily.start()
        self.addCleanup(daily.stop)

    def test_another_rounds_stop_does_not_block_the_daily_plan(self):
        ps._state.update(run_id="manual-9", batch="", stop_requested=True)
        self.assertEqual((True, ""), flow.request("batch-1", "src:b", "skip"))
        self.assertFalse(flow.stopping("batch-1", "daily"))

    def test_its_own_stop_still_refuses(self):
        """反过来钉一边:这一批自己那一轮管道被要求停止、或者日报这一批被要求停止,照旧不受理。"""
        ps._state.update(run_id="r-x", batch="batch-1", stop_requested=True)
        self.assertFalse(flow.request("batch-1", "src:b", "skip")[0])
        ps._state["stop_requested"] = False
        daily_state._state["stop_requested"] = True
        self.assertFalse(flow.request("batch-1", "src:b", "skip")[0])
        # 别的批次的日报停止不算这一批的
        daily_state._state["batch"] = "batch-0"
        self.assertEqual((True, ""), flow.request("batch-1", "src:b", "skip"))


class LiveTest(_Base):
    """面板此刻画哪一份:跑着画这一轮自己的,空着画日报那一批的。"""

    def setUp(self):
        super().setUp()
        _plan()
        with patch.object(flow, "_labels", return_value={}):
            flow.open_daily("batch-1", ["a"], library=False)

    def test_running_round_gets_its_own_plan(self):
        self.assertEqual(OWNER, flow.live(ps._state)["owner"])
        # 这一轮属于日报那一批时画日报那一份
        ps._state.update(run_id="r-x", batch="batch-1")
        self.assertEqual("batch-1", flow.live(ps._state)["owner"])

    def test_a_round_without_a_plan_is_not_shown_someone_elses(self):
        ps._state.update(run_id="r-none", batch="")
        self.assertIsNone(flow.live(ps._state))

    def test_idle_shows_the_daily_batch(self):
        ps._state["running"] = False
        self.assertEqual("batch-1", flow.live(ps._state)["owner"])
        flow.end("batch-1")
        self.assertIsNone(flow.live(ps._state), "手动那一份不在空闲时冒出来")


class _Collector:
    label = channel_label = "A 来源"


class SourceEvidenceTest(_Base):
    """进了门却没起得来的来源,要留下它自己那一格失败(同步中心按类型看的就是它)。"""

    def test_collector_failure_leaves_an_error_cell_per_step(self):
        with patch.object(srcs, "_collector", return_value=(None, "RuntimeError: 装不上")):
            srcs._one_source(SCOPES[0], ["crawl", "export"], {}, "k", OWNER, "src:a")
        got = results.snapshot()
        for stage in ("crawl", "export"):
            cell = got[stage]["a"]
            self.assertEqual("error", cell["status"])
            self.assertIn("装不上", cell["detail"])
            # 一个目标都没跑成:不许拿它去清任何旧账
            self.assertEqual(([], False), (cell["covered"], cell["whole"]))
        self.assertEqual("error", ps._state["stages"]["crawl"]["status"])

    def test_bookkeeping_failure_inside_the_guard_leaves_an_error_cell(self):
        """建账那一下(`channel_run` 进门)就炸:自己那一格还没建,由隔离那一层补上。"""
        with patch.object(results, "planned_targets", side_effect=OSError("订阅库读不出")):
            srcs._guarded("crawl", SCOPES[0], _Collector(), lambda c, s: None)
        cell = results.snapshot()["crawl"]["a"]
        self.assertEqual("error", cell["status"])
        self.assertIn("订阅库读不出", cell["detail"])

    def test_an_existing_cell_is_not_overwritten(self):
        """反过来:执行时交回来的那一格是事实,补记那一下不许盖掉它。"""
        with patch.object(results, "planned_targets", return_value=[]):
            srcs._guarded("crawl", SCOPES[0], _Collector(),
                          lambda c, s: ps._stage("crawl", "ok", "检查 1/1 个来源"))
        results.failed("crawl", "a", [], "不该出现")
        self.assertEqual("ok", results.snapshot()["crawl"]["a"]["status"])


class StatusStripTest(_Base):
    def test_live_result_cells_are_filtered_too(self):
        """跑着的时候挂上去的那份实时结果账,同样不许带着已经处理掉的那几条。"""
        item = {"title": "x", "reason": "失败", "drop": {"channel": "rss", "id": "7"}}
        results._cells["crawl"] = {"rss": {"status": "error", "failures": [dict(item)]}}
        with patch.object(pf, "dropped_keys", return_value=set(pf._item_keys(item))), \
                patch.object(pl, "last_result", return_value={}):
            got = pipeline_api.pipeline_status(source=False)
        self.assertEqual([], got["results"]["crawl"]["rss"]["failures"])
        # 底层那份原样不动(过滤只在出口)
        self.assertEqual(1, len(results._cells["crawl"]["rss"]["failures"]))


class PlanRestoreTest(HistoryCase):
    """类型计划跟着状态文件那份终态走:重启之后最新那一轮的各类耗时还在。"""

    def tearDown(self):
        flow._plans.clear()
        super().tearDown()

    def _run(self, run_id, plan):
        self.reset(run_id)
        ps.mark_open(ps._state)
        if plan:
            with patch.object(flow, "_labels", return_value={}):
                flow._plans[run_id] = flow._new(run_id, "sync", flow.source_nodes(["a"], ["crawl", "export"]))
        with patch.object(pl, "crawl_channels", return_value=[SCOPES[0]]), \
                patch.object(pl, "_run_crawl", side_effect=lambda f: ps._stage("crawl", "ok", "检查 1/1")), \
                patch.object(pl, "_run_export", side_effect=lambda f: ps._stage("export", "ok", "已保存 1 条")):
            pl._run_pipeline(["crawl", "export"], owner=run_id if plan else "")

    def _after_restart(self):
        self.restart()
        ps._state.pop("flow", None)                    # 新进程的 `_state` 里压根没有这一格
        return ps.get_state()

    def test_the_latest_rounds_plan_survives_a_restart(self):
        self._run("run-f", plan=True)
        got = self._after_restart()
        self.assertEqual("run-f", got["run_id"])
        told = got.get("flow") or {}
        self.assertEqual("run-f", told.get("run"))
        self.assertTrue(told["times"]["src:a"]["finished_at"], "各类的起止时刻要跟着回来")

    def test_a_round_without_a_plan_restores_no_plan(self):
        """反过来:这一轮没有计划,恢复回来的也不许凭空带一份(更不许是上一轮的)。"""
        self._run("run-f", plan=True)
        self._run("run-g", plan=False)
        got = self._after_restart()
        self.assertEqual("run-g", got["run_id"])
        self.assertNotEqual("run-g", (got.get("flow") or {}).get("run"))
        self.assertNotIn("flow", ps.run_payload())


class BatchRevisionTest(HistoryCase):
    """出报的结论在最后一个子轮**之后**才发布,身份却没变:版本要跟着变,两边一起。

    后果落在两个端点的回包上(状态的 `result_version`、历史首行的 `version`)—— 前端
    就是拿这两个数判「历史那份缓存要不要重取」、「这一行是不是屏幕上那一轮」的。
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(pb._remember_aggregate, None)
        self.addCleanup(pb.close_batch)

    def _sub_round(self, run_id, batch):
        self.reset(run_id)
        ps._state["batch"] = batch
        ps.mark_open(ps._state)
        with patch.object(pl, "_run_crawl", side_effect=lambda f: ps._stage("crawl", "ok", "检查 1/1")), \
                patch.object(pl, "_run_export", side_effect=lambda f: ps._stage("export", "ok", "已保存 1 条")):
            pl._run_pipeline(["crawl", "export"])

    def _versions(self):
        status = pipeline_api.pipeline_status(source=False)
        runs = pipeline_api.pipeline_runs()
        return status["result_version"], runs

    def test_the_report_verdict_moves_both_versions_together(self):
        batch = pb.open_batch("daily")
        self._sub_round("run-d", batch)
        before, runs = self._versions()
        key = runs["runs"][0]["key"]
        self.assertTrue(pb.batch_note("日报生成失败: Claude 503", batch))
        after, runs = self._versions()
        self.assertNotEqual(before, after, "结论更新了,状态那份版本却没动:前端认不出要重取历史")
        first = runs["runs"][0]
        self.assertEqual(after, first["version"])
        self.assertEqual(after, runs["version"])
        self.assertTrue(first["is_current"])
        self.assertTrue(any("Claude 503" in line for line in first["log"]), "历史首行还是出报之前那一份")
        # 反过来钉一边:选中这一行的键是身份,结论更新了它不许变
        self.assertEqual(key, first["key"])
        self.assertEqual("run-d", first["run_id"])

    def test_versions_still_line_up_after_a_restart(self):
        batch = pb.open_batch("daily")
        self._sub_round("run-d", batch)
        pb.batch_note("日报已生成:12 条素材", batch)
        pb.close_batch()
        want, _ = self._versions()
        self.restart()
        ps._state.pop("revision", None)                # 新进程的 `_state` 里压根没有这一格
        got, runs = self._versions()
        self.assertEqual(want, got, "重启后状态那份版本少了修订号")
        self.assertEqual(got, runs["runs"][0]["version"])

    def test_the_next_round_does_not_inherit_the_revision(self):
        """反过来钉一边:恢复进内存的修订号是上一批的,下一轮(别的身份)不许带着它。"""
        batch = pb.open_batch("daily")
        self._sub_round("run-d", batch)
        pb.batch_note("日报已生成:12 条素材", batch)
        pb.close_batch()
        self.restart()
        ps.get_state()
        self.assertEqual("run-d", (ps._state.get("revision") or {}).get("run"))
        self._sub_round("run-e", "")
        self.assertNotIn("revision", ps.run_payload())
        self.assertEqual("run-e", pl.result_version(pl.last_result()))

    def test_a_superseded_batch_still_moves_the_history_fingerprint(self):
        """日报在出报时被另一轮手动同步接替:最新那一轮没变,它自己那一行原位补上结论。

        前端拿「最新那一轮的版本」判不出这一行变了,所以清单整份另有一个指纹
        (`history_rev`),状态和历史两个端点各带一份、取值一致。
        """
        batch = pb.open_batch("daily")
        self._sub_round("run-d", batch)
        self._sub_round("run-m", "")
        version, runs = self._versions()
        status_rev = pipeline_api.pipeline_status(source=False)["history_rev"]
        self.assertEqual(status_rev, runs["history_rev"])
        self.assertEqual(status_rev, pipeline_api.pipeline_status(source=False)["history_rev"],
                         "没写过历史,指纹不许自己变(否则前端一直补读)")
        self.assertTrue(pb.batch_note("日报生成失败: Claude 503", batch))
        after, runs = self._versions()
        self.assertEqual("run-m", after, "前提:最新那一轮还是手动那一轮")
        self.assertEqual(version, after)
        moved = pipeline_api.pipeline_status(source=False)["history_rev"]
        self.assertNotEqual(status_rev, moved, "非首行原位改了,清单指纹却没动")
        self.assertEqual(moved, runs["history_rev"])
        row = next(r for r in runs["runs"] if r["run_id"] == "run-d")
        self.assertTrue(any("Claude 503" in line for line in row["log"]))

    def test_history_stays_alignable_while_another_round_runs(self):
        """另一轮正在跑时,状态照样给出历史清单该对齐的那一版,而且和历史端点拼得上。

        跑着时 `result_version` 说的是正在跑这一轮(它还不在历史里);前端要是拿它当历史的
        目标,只能在跑着时整个放弃对齐 —— 被接替的那批日报补上的失败原因就进不了页面。
        """
        batch = pb.open_batch("daily")
        self._sub_round("run-d", batch)
        self.reset("run-live")                         # 另一轮正在跑(running=True)
        ps._state["batch"] = ""

        def pair():
            status = pipeline_api.pipeline_status(source=False)
            runs = pipeline_api.pipeline_runs()
            self.assertTrue(status["running"])
            self.assertEqual("run-live", status["result_version"])
            return ((status["settled_version"], status["history_rev"]),
                    (runs["version"], runs["history_rev"]))
        before, listed = pair()
        self.assertEqual(listed, before)
        self.assertTrue(pb.batch_note("日报生成失败: Claude 503", batch))
        after, listed = pair()
        self.assertNotEqual(before, after, "跑着的时候历史变了,状态给的目标却没动")
        self.assertEqual(listed, after)

    def test_a_plain_round_has_no_revision(self):
        """反过来钉一边:不在批次里的一轮,版本照旧就是它的身份。"""
        self._sub_round("run-p", "")
        self.assertEqual("run-p", pl.result_version(pl.last_result()))
        self.assertEqual("run-p", pipeline_api.pipeline_runs()["runs"][0]["version"])


@unittest.skipUnless(shutil.which("node"), "需要 node 执行类型状态归约回归")
class FlowStateFrontendTests(unittest.TestCase):
    """前端那一层的归约:失败归不归「已经发生」、跳过只当旁注(`pipe_flow.js`)。"""

    def test_a_failure_stays_visible_in_the_type_summary(self):
        probe = Path(__file__).with_name("pipe_flow_state_test.mjs")
        done = subprocess.run([shutil.which("node"), "--test", str(probe)],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(0, done.returncode, done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
