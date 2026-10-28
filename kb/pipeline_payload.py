# -*- coding: utf-8 -*-
"""一轮运行的终态记录长什么样(`run_payload`),和「这一轮的类型计划带不带」那道核对。

从 `pipeline_state` 里分出来的,**只因为行数**(那边顶着 700 行上限)。契约本身没换:
落盘的那份、按 id 留的那份、并进批次汇总的那份是同一份,`pipeline_state.run_payload()`
照旧是唯一入口(它把 `_state` 递进来),调用方和打桩点一个都不用改。

**这里的函数一律不碰 `_state`**,要哪一份传哪一份进来(口径同 `pipeline_persist`)。
"""
from __future__ import annotations

from copy import deepcopy

from kb import pipeline_results as _results


def run_payload(state: dict) -> dict:
    """一轮运行的终态记录。**落盘的那份和按 id 留的那份是同一份**。

    两处曾经各写各的:`_finished` 只留了身份、结束时刻、stopped 和 stages。于是
    "没亲眼看到自己那一轮收尾、照 id 去取"的调用方(`daily_sync._sync_round` 被
    接替的那条路)拿到的是一份**缺计划、缺来源范围、缺失败名单**的记录 —— 它看着
    像一份完整结果,合并时却因为没有 `planned_stages` 被整个过滤掉,那一轮真实
    的抓取失败就此消失。契约只有一份,两边都用它。
    """
    return {
        "started_at": state["started_at"],
        "finished_at": state["finished_at"],
        "stopped": state.get("stopped", False),
        "run_kind": state.get("run_kind", ""),
        "planned_stages": state.get("planned_stages"),
        "stages": deepcopy(state["stages"]),
        "crawl_filter": deepcopy(state["crawl_filter"]),
        # 导出范围单独落一份:下一次重试要从**它**恢复,不是从收窄后的抓取范围
        "export_filter": deepcopy(state.get("export_filter") or {}),
        # run_id 跟名单一起落盘:retry_notes 要拿它认"这份名单是不是这一轮的"
        "run_id": state.get("run_id", ""),
        # 完成序号 + 发号世代:恢复时靠它们分出两份落盘各记着哪一轮(见
        # `pipeline_persist._seq_of`)。**合并出来的那份也要带**,否则整批那一份一
        # 落盘就是「没有顺序凭据」,重启之后反而被判成比子轮更旧。
        "run_seq": state.get("run_seq", 0),
        "seq_gen": state.get("seq_gen"),
        "crawl_retry": deepcopy(state.get("crawl_retry") or {}),
        # 结果账:逐来源的结论和它实际覆盖的范围(`kb/pipeline_results.py`)。
        # **跟着这份契约走**,当日账本折的就是它 —— 落不下来的话折账只能退回
        # 「把整步那张合并卡片摊到每个来源上」,一轮里成功的来源也被记成欠着。
        "results": _results.snapshot(),
        # 这一轮按类型排的那条线(各类起止时刻、跳过 / 提前结束了谁)。**也跟着这份
        # 契约走**:只进历史那一份的话,重启之后最新那一轮的类型耗时就没了,而点历史
        # 里标着「最新」的那一行回到的正是这一份(`docs/decisions/sync-flow.md`)。
        **own_flow(state),
        # 这一批的结果发布到第几版(`own_revision`)。合并那一份才有,子轮自己没有。
        **own_revision(state),
        "log": list(state["log"]),
    }


def own_flow(state: dict) -> dict:
    """这一轮自己的类型计划(`{"flow": ...}`),不是它的就是 `{}`。

    **只认身份对得上的那一份**:进程内那一格可能是上一轮留下的、或者是别人那份计划,
    存进来就是张冠李戴(前端同一道核对在 `pipe_flow.js` 的 `pipeFlowTrusted`)。
    """
    told = state.get("flow")
    if isinstance(told, dict) and told.get("run") and told.get("run") == state.get("run_id"):
        return {"flow": deepcopy(told)}
    return {}


def own_revision(state: dict) -> dict:
    """这一轮结果的**业务修订号**(`{"revision": {"run", "n"}}`),不是它的就是 `{}`。

    一批日报的身份(合并结果的 `run_id`)在出报前后是同一个,而出报的结论是在最后一个
    子轮收尾**之后**才发布的(`pipeline_batch.batch_note`)。光凭身份,前端分不出「同
    一轮、结论更新了」,历史那份缓存就一直停在出报之前的样子 —— 「生成日报」那一步
    没有结论、整行照样写「已完成」。所以批次每发布一次就进一版,`result_version`
    带上它,状态和历史两边一起变。

    **只认身份对得上的那一份**(口径同 `own_flow`):恢复进内存的那一格会一直留到被
    覆盖,而下一轮的 run id 不同,带着它就是把上一批的版本号安到别人头上。
    """
    rev = state.get("revision")
    if not isinstance(rev, dict) or not rev.get("run") or rev.get("run") != state.get("run_id"):
        return {}
    n = rev.get("n")
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        return {}
    return {"revision": {"run": rev["run"], "n": n}}
