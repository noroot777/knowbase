# -*- coding: utf-8 -*-
"""一个阶段被几个来源各跑一遍时的汇报,和各来源自己留下的失败名单。

从 `pipeline_state` 里分出来的:那边是**一轮运行的状态与收尾**,这边是**阶段汇报
的分段合并**。两件事的判据不相干,而 `pipeline_state` 顶着单文件 700 行上限
(CLAUDE.md 那条)。

分段汇报漏一段的后果写在下面各段注释里(面板上只剩最后那个来源的成败);「重试失败
项」那份名单的硬约束在 `docs/decisions/retry-scope.md`。

**依赖方向只有一个:本模块 → `pipeline_state`。** 那边只在要用的函数里延迟 import
反过来的方向(同 `pipeline_merge` 那条),所以这边可以在模块级 import 它。公开的
三个名字(`stage_segment` / `clear_segments` / `note_retry_targets`)**直接从本模块
取**,不在 `pipeline_state` 那边再导出一次 —— 再导出会把"模块级互相 import"重新
引回来,而那种环炸不炸取决于谁先被 import。
"""
from __future__ import annotations

from contextlib import contextmanager

from kb import pipeline_state as ps


# ── 多来源分段汇报 ──────────────────────────────────────────────────────────────
# 一次同步可以同时抓几个来源,而 crawl / export 两个阶段是每个来源各跑一遍的。
# 各自直接写 _state["stages"][name] 的话,后一个来源的结论会把前一个整个盖掉——
# 面板上只剩最后那个来源的成败,前面几个是成功、失败还是跳过都看不见了。
_segments: dict[str, list[dict]] = {}

# 合并后的状态取"最要紧"的那一档:有一个红就是红,还有没跑完的就是执行中,
# 剩下全跳过才算跳过。
_MERGE_ORDER = ("error", "running", "pending", "stopped", "skipped", "ok")


def _merge_segments(segments: list[dict]) -> dict:
    status = next((s for s in _MERGE_ORDER
                   if any(seg["status"] == s for seg in segments)), "ok")
    entry: dict = {
        "status": status,
        "detail": "；".join(f"{seg['label']}：{seg['detail']}"
                            for seg in segments if seg.get("detail")),
    }
    failures = [item for seg in segments for item in seg.get("failures", [])]
    if failures:
        # 各段自己已经可能截过:总数取"各段记下的总数之和",而不是截完之后的长度
        ps._cap_failures(entry, failures)
        total = sum(seg.get("failures_total") or len(seg.get("failures") or [])
                    for seg in segments)
        if total > len(entry["failures"]):
            entry["failures_total"] = total
    return entry


@contextmanager
def stage_segment(name: str, label: str):
    """把这一段时间里对 name 阶段的汇报记成"label 这个来源的那一段"。"""
    segments = _segments.setdefault(name, [])
    segments.append({"label": label, "status": "running", "detail": ""})
    try:
        yield
    finally:
        current = segments[-1]
        if current["status"] == "running":
            # 采集器连一次最终状态都没报(异常穿出去了):不替它编一个成功
            current.update({"status": "error", "detail": current["detail"] or "没有返回结果"})
        ps._state["stages"][name] = _merge_segments(segments)


def clear_segments() -> None:
    _segments.clear()


# ── 「重试失败项」要重试谁 ─────────────────────────────────────────────────────
# 报得出"这一轮哪几个目标失败了"的采集器在这里留一份**稳定身份**的名单(订阅是
# 它的 id),「重试失败项」据此把下一轮的抓取范围收到失败项上。
#
# **名单必须在失败那一刻记下来,不能事后反推。** 事后能查的只有订阅行上的
# `last_error`,而用户修正地址时 `save_subscription` 的 reset 会把它连同退避一起
# 清空 —— 那是正当的改址行为、不是"抓成功了"的证据,照它反推就把「已改正、还没
# 验证过」的那条当成「已经恢复」筛掉:用户修完地址点重试,那个账号一条都不抓,
# 界面还显示「已跳过」(2026-10-09 第 1 轮审查实测,而那正是这颗按钮的来由)。
#
# **而且必须按来源各记一份。** 一轮可以同时抓几个来源,而 `_merge_segments` 把各
# 段的失败明细扁平合并、项里不带来源归属;拿"合并之后有没有失败项"当一个全局开关
# 的话,RSS 的一条逐项失败会让**所有**来源都按失败项收窄,而整批失败的那个来源
# (一条订阅都没碰到、一份名单都没留下)就被收窄成空集 —— 恢复之后点重试,RSS 补抓
# 了,大V一条也没抓,面板上还不再显示有失败要处理(同轮审查实测)。所以判据是
# 「**这个来源**留下名单了吗」:没留下 = 整批失败、或者它报不出名单(公众号的失败
# 是文章级的,`--targets` 是账号级的,压根对不上),一律按原范围重跑。
#
# 名单带着这一轮的 run id。下一轮一份名单都没留下时(来源级整轮故障),陈旧名单会
# 连同这一轮的状态一起落盘,拿它收窄就是按上一轮的失败去抓 —— 带上身份之后这件事
# 不必在每条启动路径上各清一次,少一处就是少一个会漏的地方。

def note_retry_targets(channel: str, targets, at: str) -> None:
    """记下「这个来源这一轮有哪几个目标失败了」。

    `targets` 是稳定身份(订阅 id 这类),不是显示名 —— 显示名会被改。

    `at` 是这份名单的时刻,用来回答"这个目标后来有没有成功同步过"。**必填,而且
    要用采集器自己那套时间格式**(订阅那边是 `reg.now()` 的 UTC ISO,不是这里
    `_now()` 的本地空格格式):名单只有写它的那个采集器解释得了,而两种格式直接
    比字符串是错的 —— ISO 的 `T`(0x54)排在空格(0x20)后面,同一天里 ISO 那个
    **恒大于**空格那个,于是"它后来成功过"这一问会一律答成"是",整份名单当场
    被摘空。所以这里不给默认值、也不替它回落到 `_now()`:格式由写名单的人负责,
    少传一个参数会当场报错,而回落只会把"填错了"变成"一个很久以后才表现成『重试
    什么都不抓』的坏时钟"。空串原样存着 —— 那等于不设时间判定,保守多抓一次。
    """
    run = ps._state.get("run_id") or ""
    note = ps._state.get("crawl_retry") or {}
    if note.get("run") != run:
        note = {"run": run, "channels": {}}
        ps._state["crawl_retry"] = note
    note["channels"][channel] = {"targets": [str(t) for t in targets], "at": at}


def fold(name: str, entry: dict, detail: str) -> tuple[dict, str] | None:
    """这一阶段正被几个来源各跑一遍时,把 `entry` 并进**当前这一段**。

    返回 `(对外的那条结论, 带来源前缀的 detail)`;这一阶段压根没在分段就返回
    `None` —— 「没在分段」和「分段合成出来是空」是两件事,所以不用空字典表示。
    `_stage` 是唯一的调用方:判据(哪一档最要紧、总数怎么算)只有 `_merge_segments`
    一处,那边既服务这条路,也服务 `stage_segment` 收尾时那一次。
    """
    segments = _segments.get(name)
    if not segments:
        return None
    segments[-1].update(entry)
    return (_merge_segments(segments),
            f"{segments[-1]['label']}：{detail}" if detail else detail)
