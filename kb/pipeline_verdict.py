# -*- coding: utf-8 -*-
"""一格阶段结果**红没红**、**跑没跑** —— 这两份判据只有这一处。

**硬约束在 `docs/decisions/retry-scope.md` 第一节。** 从 `kb.pipeline_retry` 拆出来的
纯函数(那边原样再导出,老的 `pipeline_retry.stage_failed` 照旧能用)。
"""

from __future__ import annotations

import re

# 阶段 detail 里写了失败条数就算失败(状态本身是 ok 也算),与面板按钮同一套判定
_FAIL_DETAIL_RE = re.compile(r"失败 [1-9]|\d+ 个账号失败")


def stage_failed(stage: dict | None) -> bool:
    """这一格阶段结果**算不算失败**。

    **「算失败」的判据只有这一份。** 三处消费:重试的计划(`retry_failed_plan`)、
    几轮合并时谁盖谁(`pipeline_merge._merge_rank`)、当日账本里哪一格还欠着
    (`pipeline_daybook`)。面板按钮那边的同口径在 `kb/static/js/pipe_view.js` 的
    `pipeHasFailure`,改这里要跟着改它 —— 两边不一致的话按钮亮着而计划是空的,
    用户点下去只收到 409。

    `status=ok` 但 detail 里写着「失败 3 个」也算:逐项失败的那一档整个阶段是绿的。
    """
    stage = stage or {}
    return (stage.get("status") == "error"
            or bool(_FAIL_DETAIL_RE.search(stage.get("detail") or "")))


#: 这几种状态算**真执行过**。整张词表在 `pipeline_segments._MERGE_ORDER` /
#: `pipeline_merge._MERGE_RANK`,余下那几种都是「没执行」:`skipped`(跳过)、
#: `stopped`(用户停了)、`pending`(排着没轮到)、`running`(中途没了)。
_RAN_STATUS = ("ok", "error")


def stage_ran(stage: dict | None) -> bool:
    """这一格阶段结果算不算**真执行过**(有执行事实可言)。

    **「执行过没有」的判据只有这一份**,和 `stage_failed` 是一对:那一份答「这一步
    红没红」,这一份答「这一步跑没跑」。只有一处消费 —— 当日账本折账时判「**推断
    出来的**那份覆盖范围算不算成功证据」(`pipeline_daybook._coverage`)。采集器
    自己交回来的覆盖范围(`kb.collectors.Coverage`)**不过这一道**:它是执行事实,
    中途被停掉的那一轮里真抓成了的那几条照旧结清。

    **白名单,不是黑名单。** 认不出来的状态一律算没执行:算错成「执行过」是拿一步
    压根没跑的绿色清掉今天早些时候那笔真失败(静默丢数据),算错成「没执行」顶多
    把补好的那一步再跑一次(元规则那条)。
    """
    return ((stage or {}).get("status") or "") in _RAN_STATUS
