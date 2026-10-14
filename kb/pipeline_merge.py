# -*- coding: utf-8 -*-
"""一次业务轮里的几轮管道,合并成一份结果。**纯合并:不判资格、不写盘。**

从 `kb/pipeline_state.py` 拆出来**只因为行数**(那边到了 700 行上限)。这一层
没有任何自己的可变状态,整段原样搬过来,唯一的调用方仍是
`pipeline_state._publish_in_batch`(它在函数里延迟 import 本模块 —— 本模块要用
那边的 `STAGES` / `_cap_failures` 等原语,两边直接互相 import 会成环)。

**不要在这里开一个"事后聚合"的公开出口**:原因见 `_merge_runs` 的 docstring。
"""

from __future__ import annotations

from copy import deepcopy

from kb.pipeline_state import STAGES, _cap_failures, _now, new_run_id

# ── 一次业务轮里的几轮管道,合并成一份结果 ──────────────────────────────────────
# 日报那一轮不是一次管道运行,而是 N+1 次:每个来源各一轮 crawl→export,再一轮
# 全库的 index→extract。而「最近一次运行」只有一份,**后跑的会把先跑的盖掉** ——
# 于是 RSS 抓取失败之后,同步中心显示的是最后那一轮入库的「同步已完成」,失败明细
# 和「重试失败项」一起消失(`retry_failed_plan` 读的也是这一份)。两段式之前就已经
# 是这样了(只有最后一个来源的结果留得下来),改成两段式之后变成每次必然发生。
# 所以收尾时把这一批合并成一份发布,让查看和重试面对的是**整个业务轮**。

# 合并时谁盖谁。只在**各自计划里有这个阶段**的记录之间比:没计划过的那一档是
# `skipped/未选择`,让它参与比较会把另一轮真实的 ok 盖成「跳过」。
_MERGE_RANK = {"error": 4, "stopped": 3, "skipped": 2, "pending": 1, "running": 1, "ok": 0}


def _merge_stage(owned: list[tuple[str, dict]]) -> dict:
    """同一个阶段在几轮里的结果,合成**不丢东西**的一份。

    `owned` 是 (这一轮的来源名, 那一轮这个阶段的结果),按时间顺序。

    不能只挑最重的那一个对象:RSS 和网页专栏各挂一条订阅时,两轮的 crawl 都是
    error,挑一个等于把另一个来源的原因和失败名单整个扔掉 —— 面板上「来源:
    RSS + 网页专栏」却只列得出一条明细。status 仍取最重的那一档。

    **按来源挂名,不按文本去重。** 两个来源各失败一条时两边的 detail 一模一样
    (都是「失败 1 个」),照文本去重就把两条说成一条,而卡片上写着来源是两个。
    `failures` 同理,而且更严重:显示用的标题压根不是身份 —— 两条订阅可以同名
    (两个来源各有一个「每日速览」),同名同因就被并掉一条,面板上少一行、
    计数也少一个,看上去就是"只有一个来源出问题"。这里**一条都不并**,原样按
    轮次接起来;真有重复只能是同一轮被记了两遍,那是别处的 bug,并掉等于替它
    遮住。上限跟 `_stage` 一致(见 `_cap_failures`:截断了就把总数一起留下)。
    """
    worst = max((stage for _, stage in owned), key=_merge_rank)
    merged = {k: v for k, v in deepcopy(worst).items()
              if k not in ("detail", "failures", "failures_total")}
    details, failures = [], []
    for scope, stage in owned:
        text = (stage.get("detail") or "").strip()
        if text:
            details.append(f"{scope}：{text}" if scope else text)
        failures.extend(deepcopy(item) for item in stage.get("failures") or [])
    if details:
        merged["detail"] = "；".join(details)[:400]
    if failures:
        _cap_failures(merged, failures)
        total = sum(stage.get("failures_total") or len(stage.get("failures") or [])
                    for _, stage in owned)
        if total > len(merged["failures"]):
            merged["failures_total"] = total
    return merged


def _merge_rank(stage: dict) -> int:
    from kb.pipeline_retry import _FAIL_DETAIL_RE
    # status=ok 但 detail 里写着「失败 3 个」也是失败(判据和面板按钮、
    # `retry_failed_plan` 共用那一条正则,三处不一致的话按钮会亮着而计划是空的)。
    if _FAIL_DETAIL_RE.search(stage.get("detail") or ""):
        return _MERGE_RANK["error"]
    return _MERGE_RANK.get(stage.get("status") or "", 0)


def _scope_name(run: dict) -> str:
    """这一轮作用在哪几个来源上,写成人话。全库那一轮回空串(它不属于谁)。"""
    from kb.collectors import channel_labels     # 延迟:它反过来 import 本模块
    from kb.pipeline_stages import run_scopes
    labels = channel_labels()
    return " + ".join(labels.get(s["channel"], s["channel"]) for s in run_scopes(run))


def _merge_scopes(records: list[dict], key: str) -> dict:
    """把几轮的来源范围并起来。同一个来源出现多次时按先到的那份算。"""
    from kb.pipeline_stages import crawl_channels   # 延迟:它反过来 import 本模块
    merged: list[dict] = []
    for run in records:
        planned = run.get("planned_stages") or []
        if not ({"crawl", "export"} & set(planned)):
            continue              # 全库那一轮没有来源范围可言,见 `run_scopes`
        for scope in crawl_channels(run.get(key) or run.get("crawl_filter")):
            if scope["channel"] and not any(s["channel"] == scope["channel"] for s in merged):
                merged.append(scope)
    if not merged:
        return {}
    # 一个来源时写回老样子的 channel + accounts:存量读取方(和 `crawl_channels`
    # 自己)两种都认,但旧的那种更窄,能被更多消费方直接读。
    if len(merged) == 1:
        return {"channel": merged[0]["channel"], "accounts": merged[0]["accounts"]}
    return {"channel": merged[0]["channel"], "channels": merged}


def _merge_runs(records: list[dict], events: list[str] | None = None) -> dict:
    """把一个业务批次里跑过的几轮合成**不丢东西**的一份。纯合并:不判资格、不写盘。

    `events` 是这一批自己的事件(已经带时间戳),接在合并后的日志末尾 —— 子轮快照
    是落盘之前拍的,不走这条路的话保存失败那行字在下一轮成功之后就没了。
    **别叫 notes**:这个函数里已经有一个 `notes`(失败名单),同名会把参数遮住,
    于是日志里接上去的是那份名单的键(实测:那行保存失败照旧找不着)。

    **唯一的调用方是 `_publish_in_batch`**。不要再开一个"事后聚合"的公开出口:
    那正是这件事原来错的地方 —— 子轮先写一份会删掉前序失败的局部结果,再指望还
    活着的调用方稍后补回,中间进程一死那几条红就永远不在了。合并要么发生在子轮
    收尾的同一步里,要么不发生。

    逐阶段合成(见 `_merge_stage`),而且只在计划过这个阶段的记录之间比。来源
    范围、失败名单(`crawl_retry`)按来源并起来,这样「重试失败项」拿得到那一轮的
    目标和原导出范围,不必从当前订阅状态反推。

    合并后的身份**沿用这一批最后那一轮的 run_id**,不另发一个:另发等于把一份
    早先的批次伪装成"刚刚产生的最新结果",而最新指针是有先后的。
    """
    run_id = records[-1].get("run_id") or new_run_id()
    scopes = [_scope_name(r) for r in records]
    stages: dict[str, dict] = {}
    for name in STAGES:
        owned = [(scopes[i], r["stages"][name]) for i, r in enumerate(records)
                 if name in (r.get("planned_stages") or []) and (r.get("stages") or {}).get(name)]
        stages[name] = (_merge_stage(owned) if owned
                        else {"status": "skipped", "detail": "未选择"})
    planned = [s for s in STAGES
               if any(s in (r.get("planned_stages") or []) for r in records)]
    notes: dict = {}
    for run in records:
        note = run.get("crawl_retry") or {}
        if note.get("run") == (run.get("run_id") or ""):
            notes.update(note.get("channels") or {})
    started = [r.get("started_at") for r in records if r.get("started_at")]
    finished = [r.get("finished_at") for r in records if r.get("finished_at")]
    merged = {
        "started_at": min(started) if started else "",
        "finished_at": max(finished) if finished else _now(),
        "stopped": any(r.get("stopped") for r in records),
        "run_kind": "sync",
        "planned_stages": planned,
        "stages": stages,
        "crawl_filter": _merge_scopes(records, "crawl_filter"),
        "export_filter": _merge_scopes(records, "export_filter"),
        "run_id": run_id,
        "crawl_retry": {"run": run_id, "channels": notes} if notes else {},
        "log": _merge_log(records, events),
    }
    return merged


def _merge_log(records: list[dict], events: list[str] | None) -> list[str]:
    """子轮日志接起来 + 这一批自己的事件。**批次事件不许被 80 行上限挤掉。**"""
    kept = list(events or [])[-20:]
    lines = [line for r in records for line in (r.get("log") or [])]
    return lines[-(80 - len(kept)):] + kept
