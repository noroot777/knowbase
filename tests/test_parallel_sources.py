# -*- coding: utf-8 -*-
"""各来源同时跑特有的那几件(`kb/pipeline_sources.Lanes`、`kb/pipeline_lanes`、
`kb/pipeline_flow` 的门)。硬约束在 `docs/decisions/sync-flow.md` 第一节。

判据落在后果上:哪一路的导出**什么时候**真跑了、「交完了」记在哪一刻、哪一句结论记到了
哪个来源那一格、日志里谁的那几行还在 —— 不是「标志打上没有」。

两路要「同时在跑」的用例都用屏障 / 事件把时序摆死,不靠 sleep 碰运气;摆不出来的那一档
(屏障超时)会让那一路的桩抛出去,`Lanes.broken` 随之翻成 True,用例当场红。
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from kb import pipeline_flow as flow
from kb import pipeline_lanes as lanes
from kb import pipeline_results as results
from kb import pipeline_segments
from kb import pipeline_sources as srcs
from kb import pipeline_state as ps

SCOPES = [{"channel": "a", "accounts": []}, {"channel": "b", "accounts": []}]
OWNER = "run-1"


def _plan(channels=("a", "b")):
    with patch.object(flow, "_labels", return_value={"a": "甲", "b": "乙"}):
        nodes = flow.source_nodes(channels, ("crawl", "export", "index")) + \
            flow.group_nodes(("crawl", "export", "index"))
    flow._plans[OWNER] = flow._new(OWNER, "sync", nodes)


def _until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


class _Base(unittest.TestCase):
    def setUp(self):
        flow._plans.clear()
        state = patch.dict(ps._state, {"stop_requested": False, "running": True,
                                       "run_id": OWNER, "batch": "", "current_stage": "",
                                       "planned_stages": ["crawl", "export", "index"],
                                       "stages": {s: {"status": "", "detail": ""}
                                                  for s in ps.STAGES},
                                       "log": []})
        state.start()
        self.addCleanup(state.stop)
        self.addCleanup(flow._plans.clear)
        results.clear()
        self.addCleanup(results.clear)
        pipeline_segments.clear_segments()
        self.addCleanup(pipeline_segments.clear_segments)


class _Rig:
    """桩掉执行层(`_guarded` / `_settle` / 开轮 / 「交完了」),`Lanes` 与计划是真的。"""

    def __init__(self, on_crawl=None):
        self.calls, self.settled, self.ended = [], [], []
        self.on_crawl = on_crawl
        self._patches = [
            patch.object(srcs, "crawl_channels", return_value=SCOPES),
            patch.object(srcs, "_collector", side_effect=lambda s: (object(), None)),
            patch.object(srcs, "round_key", return_value=("k", "seg")),
            patch.object(srcs, "_guarded", side_effect=self._guarded),
            patch.object(srcs, "_settle", side_effect=lambda st, sc, c, status, detail:
                         self.settled.append((st, sc["channel"], status))),
            patch.object(srcs, "_open", side_effect=lambda *a: None),
            patch("kb.fetch_rounds.note_end", side_effect=lambda *a: self.ended.append(a)),
        ]

    def _guarded(self, stage, scope, collector, run):
        self.calls.append((stage, scope["channel"]))
        if self.on_crawl and stage == "crawl":
            self.on_crawl(scope["channel"])

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()


class ConcurrencyTest(_Base):
    def test_sources_crawl_at_the_same_time(self):
        """串着跑的话第一路会在屏障上等满超时、抛出去 —— 只有两路同时在抓才过得去。"""
        _plan()
        both = threading.Barrier(2, timeout=5)
        with _Rig(on_crawl=lambda ch: both.wait()) as rig:
            run = srcs.run_sources(["crawl", "export"], None, OWNER)
            run.wait_gate()
            run.join()
        self.assertFalse(run.broken)
        self.assertEqual(4, len(rig.calls))
        self.assertEqual([("k", "seg")], rig.ended)
        # 门前一路都没剩下:门直接开,谁都不算「没等」
        self.assertEqual(("open", []), (flow._plans[OWNER]["gate"], flow._plans[OWNER]["late"]))


class GateTest(_Base):
    """b 卡在抓取上、a 已经跑完:门在等 b,用户点「不等了」。"""

    def _start(self):
        _plan()
        self.release_b = threading.Event()

        def crawl(ch):
            if ch == "b":
                self.release_b.wait(5)
        self.rig = _Rig(on_crawl=crawl).__enter__()
        self.addCleanup(self.rig.__exit__)
        self.addCleanup(self.release_b.set)
        self.run_ = srcs.run_sources(["crawl", "export"], None, OWNER)
        self.assertTrue(_until(lambda: ("export", "a") in self.rig.calls))
        self.gate = threading.Thread(target=self.run_.wait_gate)
        self.gate.start()
        self.assertTrue(_until(lambda: flow._plans[OWNER]["gate"] == "wait"))

    def test_go_marks_only_the_unfinished_source_late_and_defers_its_export(self):
        self._start()
        self.assertEqual((True, ""), flow.request(OWNER, "ingest", "go"))
        self.gate.join(5)
        self.assertFalse(self.gate.is_alive(), "点了「不等了」门还关着")
        plan = flow._plans[OWNER]
        self.assertEqual(("go", ["src:b"]), (plan["gate"], plan["late"]))
        # b 抓完了:导出不许在全库那几步之前跑,手上那一步收掉(面板上不再像在抓)
        self.release_b.set()
        self.assertTrue(_until(lambda: flow._plans[OWNER]["lanes"].get("src:b") == ""))
        time.sleep(0.2)
        self.assertNotIn(("export", "b"), self.rig.calls)
        self.assertEqual([], self.rig.ended, "没跑完的那一路还在,「交完了」不许先记")
        self.rig.calls.append("tail")
        self.run_.join()
        self.assertGreater(self.rig.calls.index(("export", "b")), self.rig.calls.index("tail"))
        self.assertEqual(1, len(self.rig.ended))
        self.assertFalse(self.run_.broken)
        self.assertTrue(any("这一轮不等 乙" in line for line in ps._state["log"]))

    def test_without_go_the_gate_waits_and_nobody_is_late(self):
        """反过来钉一边:不点「不等了」,门等到 b 跑完,b 不算没等、导出照常在门前跑。"""
        self._start()
        time.sleep(0.2)
        self.assertTrue(self.gate.is_alive())
        self.release_b.set()
        self.gate.join(5)
        self.assertFalse(self.gate.is_alive())
        self.assertIn(("export", "b"), self.rig.calls, "门开之前 b 的导出就该跑完")
        self.assertEqual(("open", []), (flow._plans[OWNER]["gate"], flow._plans[OWNER]["late"]))
        self.run_.join()

    def test_stop_while_waiting_marks_nobody_late(self):
        """等的时候整轮被停:不算「没等」,没跑完的那一路的导出记成被停了。"""
        self._start()
        ps._state["stop_requested"] = True
        self.gate.join(5)
        self.assertEqual([], flow._plans[OWNER]["late"])
        self.release_b.set()
        self.run_.join()
        self.assertNotIn(("export", "b"), self.rig.calls)
        self.assertIn(("export", "b", "stopped"), self.rig.settled)

    def test_go_is_refused_unless_the_gate_is_waiting(self):
        _plan()
        self.assertEqual((False, "现在没有在等谁"), flow.request(OWNER, "ingest", "go"))
        flow.gate(OWNER, "wait")
        self.assertFalse(flow.request(OWNER, "src:a", "go")[0], "来源那一类不能「不等」")
        self.assertEqual((True, ""), flow.request(OWNER, "ingest", "go"))
        self.assertTrue(flow.released(OWNER))


class CuttingPerLaneTest(_Base):
    def test_a_cut_reaches_only_its_own_lane(self):
        _plan()
        for key in ("src:a", "src:b"):
            flow.enter(OWNER, key)
            flow.advance(OWNER, key, "crawl")
        self.assertEqual((True, ""), flow.request(OWNER, "src:a", "cut"))
        seen = {}

        def ask(ch):
            with lanes.bind(ch):
                seen[ch] = flow.halted()
        for ch in ("a", "b"):
            t = threading.Thread(target=ask, args=(ch,))
            t.start()
            t.join()
        self.assertEqual({"a": True, "b": False}, seen)
        # 没绑通道的线程(全库那几步)在两路同时开着时不认任何一路的提前结束
        self.assertFalse(flow.halted())


class AttributionTest(_Base):
    """两路同时报结论:各记各的那一段、那一格,谁也不写进别人那里。"""

    def test_concurrent_reports_land_on_their_own_segment_and_cell(self):
        both = threading.Barrier(2, timeout=5)
        errors = []

        def lane(ch, label):
            try:
                with lanes.bind(ch), pipeline_segments.stage_segment("crawl", label), \
                        results.channel_run("crawl", ch, object(), []):
                    both.wait()                 # 两段都开着
                    if ch == "b":
                        both.wait()             # a 先报,b 后开口,但 a 那一段是后开的也一样
                    ps._stage("crawl", "ok" if ch == "a" else "error", f"{ch} 说的")
                    if ch == "a":
                        both.wait()
            except Exception as e:              # noqa: BLE001
                errors.append(e)
        ts = [threading.Thread(target=lane, args=(ch, lb)) for ch, lb in (("a", "甲"), ("b", "乙"))]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        self.assertEqual([], errors)
        cells = results.snapshot()["crawl"]
        self.assertEqual(("ok", "a 说的"), (cells["a"]["status"], cells["a"]["detail"]))
        self.assertEqual(("error", "b 说的"), (cells["b"]["status"], cells["b"]["detail"]))
        merged = ps._state["stages"]["crawl"]
        self.assertEqual("error", merged["status"])
        self.assertIn("甲：a 说的", merged["detail"])
        self.assertIn("乙：b 说的", merged["detail"])
        tagged = [lanes.who(line) for line in ps._state["log"] if "说的" in line]
        self.assertEqual({"a", "b"}, set(tagged))

    def test_an_unbound_thread_with_two_open_lanes_is_credited_to_nobody(self):
        """保守档:几路同时开着时,没绑通道的线程报的那一句不许猜一个来源记上去。"""
        entered, done = threading.Barrier(3, timeout=5), threading.Event()

        def lane(ch):
            with lanes.bind(ch), results.channel_run("crawl", ch, object(), []):
                ps._stage("crawl", "ok", f"{ch} 好了")
                entered.wait()
                done.wait(5)
        ts = [threading.Thread(target=lane, args=(ch,)) for ch in ("a", "b")]
        for t in ts:
            t.start()
        entered.wait()
        ps._stage("crawl", "error", "工作线程说的")
        done.set()
        for t in ts:
            t.join(5)
        cells = results.snapshot()["crawl"]
        self.assertEqual({"a 好了", "b 好了"}, {cells["a"]["detail"], cells["b"]["detail"]})

    def test_a_lane_does_not_pull_the_current_stage_back_from_the_library_steps(self):
        ps._state["current_stage"] = "index"
        with lanes.bind("a"):
            ps._stage("export", "running", "")
        self.assertEqual("index", ps._state["current_stage"])
        # 反过来:全库那几步还没开始时,来源那一路照常把它挪到导出
        ps._state["current_stage"] = "crawl"
        with lanes.bind("a"):
            ps._stage("export", "running", "")
        self.assertEqual("export", ps._state["current_stage"])


class LogTrimTest(unittest.TestCase):
    def test_each_source_keeps_its_own_last_lines(self):
        noisy = [f"[11-03 09:00:{i:02d}] <youtube> 进度 {i}" for i in range(60)]
        lines = ["[11-03 08:59:00] <rss> 订阅源 3 条失败"] + noisy + \
            ["[11-03 09:01:00] index: 已索引 5 篇"]
        kept = lanes.trim(lines, keep=40)
        self.assertIn("[11-03 08:59:00] <rss> 订阅源 3 条失败", kept, "刷屏那一路挤掉了别人的失败原因")
        self.assertEqual(40, sum(1 for line in kept if lanes.who(line) == "youtube"))
        self.assertEqual(noisy[-1], kept[-2])
        self.assertEqual(lines[-1], kept[-1], "顺序不变")
        # 反过来:每一类都没超的时候一行不丢
        self.assertEqual(lines[:5], lanes.trim(lines[:5], keep=40))

    def test_log_lines_are_tagged_by_the_lane_and_untagged_outside(self):
        with patch.dict(ps._state, {"log": []}):
            with lanes.bind("rss"):
                ps._log("检查 9/9 个来源")
            ps._log("已索引 5 篇")
            got = list(ps._state["log"])
        self.assertEqual(["rss", ""], [lanes.who(line) for line in got])
        self.assertTrue(got[0].endswith("<rss> 检查 9/9 个来源"))


if __name__ == "__main__":
    unittest.main()
