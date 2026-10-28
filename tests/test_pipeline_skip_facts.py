# -*- coding: utf-8 -*-
"""「意图不许改写事实」这条不变量的两层:后端跳过收尾、前端类型状态归约。

硬约束在 `docs/decisions/sync-flow.md` 第一节。后端那半:跳过 / 提前结束只管还没做的
那部分,已经发生的失败照实留着(`pipeline._skip_step` / `_settle_skipped`);前端那半在
`tests/pipe_flow_state_test.mjs`,这里只负责把它跑起来(`state` 只答走到哪了,`issues`
答有没有红,两样正交)。

判据一律落在**最后那张阶段卡片是什么**:这一轮收尾之后面板和历史都按它念,被刷成
skipped 的话那条红只剩在日志里。每条都反过来钉一边。
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

from kb import pipeline as pl
from kb import pipeline_state as ps
from tests.test_pipeline_flow import _Base


class _Pre:
    """装成那条并行跑的存量提取线程:只需要 join 得动。"""

    def join(self):
        return None


class SkipKeepsFailuresTest(_Base):
    """跳过 / 提前结束只管**还没做的那部分**,已经发生的失败照实留着(`_skip_step`)。

    判据落在阶段卡片最后是什么:这一轮收尾之后面板和历史都按它念,被刷成 skipped
    的话那条红只剩在日志里(重试也捞不回来)。每条都反过来钉一边。
    """

    def test_a_blocked_queue_survives_the_skip(self):
        """队列压根没读出来:一篇都没经手,但已经红了 —— 跳过要收它自己那份结论。

        判据落在**明细**上:生产端当场那一句只有正文(`pipeline_stages` 读队列
        失败那一笔),收尾那一遍才会把「提取队列」这条明细挂上去(面板展开看的
        就是它)。只问 status 的话,下面那道兜底一个人就能答对 —— 两份实现各要
        有自己的红。
        """
        stats = pl._new_extract_stats()
        stats["blocked"] = "读不到订阅的提取开关:隔离冒烟"
        ps._stage("extract", "error", stats["blocked"])       # 真实生产端发布的那条红
        pl._skip_step("extract", _Pre(), stats, pl.SKIPPED)
        card = ps._state["stages"]["extract"]
        self.assertEqual("error", card["status"])
        self.assertIn("提取开关", card["detail"])
        self.assertEqual(["提取队列"], [f["title"] for f in card.get("failures", [])])

    def test_a_quiet_pre_pass_still_takes_the_skip(self):
        """反过来钉一边:存量那一遍什么也没留下,这一步照旧记「本次跳过」。"""
        pl._skip_step("extract", _Pre(), pl._new_extract_stats(), pl.SKIPPED)
        self.assertEqual({"status": "skipped", "detail": pl.SKIPPED},
                         ps._state["stages"]["extract"])

    def test_a_worked_pre_pass_reports_its_own_verdict(self):
        """存量真提过几篇:收它自己的结论,不是「本次跳过」。"""
        stats = pl._new_extract_stats()
        stats["ok"] = 3
        pl._skip_step("extract", _Pre(), stats, pl.SKIPPED)
        card = ps._state["stages"]["extract"]
        self.assertEqual("ok", card["status"])
        self.assertIn("成功 3", card["detail"])

    def test_a_published_failure_on_any_step_is_not_rewritten(self):
        """兜底那一道:哪一步已经发布过红,跳过 / 提前结束都不许改写它。"""
        ps._stage("index", "error", "隔离冒烟:索引炸了")
        pl._skip_step("index", None, {}, pl.CUT_REST)
        self.assertEqual("error", ps._state["stages"]["index"]["status"])

    def test_a_step_without_a_verdict_takes_the_skip(self):
        """反过来钉一边:没有结论的那一步照旧记「这一类提前结束」。"""
        pl._skip_step("projects", None, {}, pl.CUT_REST)
        self.assertEqual({"status": "skipped", "detail": pl.CUT_REST},
                         ps._state["stages"]["projects"])


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
