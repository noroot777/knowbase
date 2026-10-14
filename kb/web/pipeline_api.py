# -*- coding: utf-8 -*-
"""数据管道 API:状态、手动触发、失败重试、定时同步配置。

真正的阶段实现在 kb.pipeline;这里只是它的 HTTP 外壳。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kb import pipeline as pl
from kb.config import ScheduleUnreadable
from kb.web.common import unreadable_config
from kb.collectors import (
    CollectorError,
    active_collector,
    channel_labels,
    collection_channels,
    retired_channels,
)

router = APIRouter()


def _channel(value: str | None) -> str:
    try:
        return active_collector(value).name
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/pipeline/channels")
def pipeline_channels():
    """可勾选的来源。

    `labels` 还多给几条**已经退役的通道名**:历史运行记录里写着的可能正是它们
    (公众号早先那条凭证通道),面板不该把它显示成一个裸 id。`aliases` 则是给
    浏览器里存着的那些选择用的——localStorage 里写着的可能正是一条退役通道。
    """
    return {"channels": collection_channels(), "default": active_collector().name,
            "labels": channel_labels(), "aliases": retired_channels()}


class ScheduleRequest(BaseModel):
    enabled: bool
    time: str = "07:30"
    channel: str | None = None
    channels: list[str] | None = None


@router.get("/api/pipeline/schedule")
def pipeline_schedule():
    try:
        return pl.load_schedule()
    except ScheduleUnreadable as e:
        raise unreadable_config(e) from None


@router.post("/api/pipeline/schedule")
def pipeline_schedule_set(body: ScheduleRequest):
    # channels 是新写法(可以定几个来源一起同步),channel 是老的单个写法
    channels = body.channels if body.channels is not None else body.channel
    try:
        return pl.save_schedule(body.enabled, body.time, channels)
    except ScheduleUnreadable as e:
        raise unreadable_config(e) from None
    except OSError as e:
        raise HTTPException(503, f'没能保存定时设置({e})。原来那份还在。') from None
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/pipeline/status")
def pipeline_status(pending: bool = False, source: bool = True, channel: str | None = None):
    state = dict(pl.get_state())
    state["stop_supported"] = True
    # 来源状态要起采集器 CLI 并触网探测,冷缓存时几十秒都可能。只想知道
    # "管道在不在跑"的调用方(kb.sh 的停机保护就是)带 source=false 跳过它,
    # 否则一个慢探测会把"在不在跑"这个本来一读内存就有的答案也拖到超时。
    if source:
        selected = channel or (state.get("crawl_filter") or {}).get("channel")
        state["source"] = pl.source_status(_channel(selected))
    # **`last_result()` 而不是 `load_last_run()`** —— 前端空闲时优先渲染这一份
    # (`pipeResult`),而磁盘那份在最近状态写失败那一轮会落后整整一轮:面板会显示
    # 上一轮的结论,而日志写着这一轮的失败。判据放在这里一处,前端照旧。
    latest = pl.last_result() or None
    state["last_run"] = latest
    # **这一份回包自证它对应哪一轮终态**,前端拿它当「待处理数 / 历史清单是不是
    # 这一轮的」的凭据。顶层那几个字段不够:新进程里它们是空的(恢复之后才有),
    # 而从没跑过任何一轮的新库里永远是空的 —— 前端只好把空串同时当成「还不知道」
    # 和「已经追平」,两种处理都错过一档(忙着重查 / 永远不补)。
    # 跑着的时候报的是**正在跑这一轮**的版本,附属数据本来就该等它收尾。
    state["result_version"] = pl.result_version(state if state["running"] else (latest or {}))
    # 非空 = 这个进程启动时只从历史恢复到最新那一轮,它的重试范围没留下。面板据此
    # 把「重试失败项」按不下去并如实说一句(后端那道闸在 `start_retry_failed`)。
    state["restore_gap"] = pl.restore_gap()
    # 待处理数要扫文件,只有显式要求且空闲时才算
    if pending and not state["running"]:
        state["pending"] = pl.pending_counts(_channel(channel)) if channel else pl.pending_counts()
    return state


@router.get("/api/pipeline/runs")
def pipeline_runs():
    """最近几轮同步的终态,最新在前。

    **面板上那份当前状态只留最后一轮**,而一轮的红很容易在被人看见之前就被下一轮
    覆盖掉:上午那轮翻译报了 503、导出阶段判了红,晚上一次「重试失败项」跑完,
    面板上就只剩那一轮的结论了 —— 那个红没有任何地方查得到。所以历史单独落一份。

    形状和 `/api/pipeline/status` 里的 `last_run` 一致,前端用同一套渲染。
    """
    # `error` 如实带上最近一次留档失败的异常类型 —— 留档是旁路,它失败不该声张成
    # 同步失败(那一轮的结论照旧发布),但也不许静默:只看页面的话,「历史里没有这
    # 一轮」和「这一轮还没跑过」长得一模一样。
    runs = pl.load_history()
    # **每条记录附两个字段,它们是两个不同的契约。**
    # `version`:这一轮的完成版本,前端拿首条那个判「清单是不是最新那一轮的」——
    #   所以它要跟状态端点一个口径,空清单也得有值。
    # `key`:**逐行唯一**的选择键,面板拿它认「点的是哪一行」、再次定位和高亮。
    #   `run_id` 是后来才加的字段,升级前那几条没有它(属性里是空字符串,点下去
    #   查找要求 `undefined === ''`,那一行看得到却打不开);而完成版本在「两条都
    #   缺身份、收尾时间又相同」时会撞,点较早那行打开的是较新那行。
    #   两件事共用一个字段的话,一边的降级口径就会变成另一边的 bug。
    # `invalid`:这一行**算不算一轮运行**,判据过的是恢复那边同一个 `valid_round`
    #   (要是字典、而且有收尾时间)。各写一遍的话恢复拒绝的那种记录会在清单上显示
    #   成一轮正常运行 —— 实测形态是历史首部多了个 `{}`(坏文件、半截写入),面板上
    #   那一行写着「时间未知 · 同步 · 未记录来源 · 已完成」,一轮根本没跑过的
    #   「成功」。
    taken: set[str] = set()
    for run in runs:
        run["version"] = pl.result_version(run)
        run["key"] = pl.row_key(run, taken)
        run["invalid"] = not pl.valid_round(run)
    # **清单的版本和状态端点同一个口径**,前端才比得出「这份清单是不是最新那一轮
    # 的」。空清单也有值(「从没跑过」),理由在 `result_version` 那边。
    # **版本取第一个有效行**:拿无效首行去算的话版本是 `none`,而状态端点报的是真实
    # 那一轮 —— 两边永远对不上,于是面板一直提示「历史还没追平」、补读一直在跑,点
    # 「重新读取」也不消失。
    first = next((r for r in runs if not r["invalid"]), {})
    return {"runs": runs, "error": pl.persist_error(),
            "version": pl.result_version(first)}


class ChannelScope(BaseModel):
    """一次同步里的一个来源:抓它的哪些订阅。accounts 空 = 该来源的全部订阅。"""

    channel: str
    accounts: list[str] | None = None


class PipelineRunRequest(BaseModel):
    stages: list[str] | None = None  # None = 全部
    # 以下几个用于范围抓取(只影响 crawl 阶段):
    # 填了日期区间则按 [since_date, until_date] 补抓;accounts 空/None = 来源的全部目标
    accounts: list[str] | None = None
    since_date: str | None = None   # "YYYY-MM-DD"
    until_date: str | None = None   # "YYYY-MM-DD"
    channel: str | None = None
    # 多来源一起抓:每个来源各带自己的订阅。给了它就不看上面的 channel/accounts
    channels: list[ChannelScope] | None = None


def _scopes(body: PipelineRunRequest | None) -> list[dict]:
    """把请求里的来源整理成 [{"channel", "accounts"}],顺序就是抓取顺序。"""
    if body and body.channels is not None:
        if not body.channels:
            raise HTTPException(400, "请至少选择一个来源")
        scopes, seen = [], set()
        for item in body.channels:
            name = _channel(item.channel)
            if name in seen:
                raise HTTPException(400, "同一个来源只能选一次")
            seen.add(name)
            scopes.append({"channel": name, "accounts": _accounts(item.accounts)})
        return scopes
    return [{"channel": _channel(body.channel if body else None),
             "accounts": _accounts(body.accounts if body else None)}]


def _accounts(names: list[str] | None) -> list[str]:
    if names and any(not name.strip() for name in names):
        raise HTTPException(400, "订阅名称不能为空")
    return list(names or [])


@router.post("/api/pipeline/run")
def pipeline_run(body: PipelineRunRequest | None = None):
    stages = body.stages if body else None
    scopes = _scopes(body)
    crawl_filter: dict = {"channel": scopes[0]["channel"]}
    # 只有一个来源时仍写成老样子的 channel + accounts:存量状态文件、重试记录
    # 和还没升级的读取方因此一行都不用改
    if len(scopes) > 1:
        crawl_filter["channels"] = scopes
    if body and (body.since_date or body.until_date):
        if not (body.since_date and body.until_date):
            raise HTTPException(400, "起始和结束日期必须同时提供")
        try:
            dates = [date.fromisoformat(d) for d in (body.since_date, body.until_date)]
            if dates[0] > dates[1] or [d.isoformat() for d in dates] != [body.since_date, body.until_date]:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "日期须为 YYYY-MM-DD，且起始日期不能晚于结束日期") from None
    # 只限定目标、不限定时间也要成立(「就把这几个号的新文章抓一下」)。
    # 以前的条件只看日期,给了 accounts 却没给日期就退回 crawl_filter=None,
    # 于是全部订阅目标照抓——筛选被静默忽略,比报错更难发现。
    if body and (body.since_date or body.until_date
                 or any(scope["accounts"] for scope in scopes)):
        crawl_filter.update({
            "accounts": scopes[0]["accounts"],
            "since_date": body.since_date or "",
            "until_date": body.until_date or "",
        })
    # **收下真实原因再报**:没起跑有两种,而「管道已在运行中」只覆盖一种 —— 另一种
    # 是这一轮的持久化边界落不下来(数据目录写不进去),照着报前者等于把一个要动手的
    # 故障说成「等一下再点」。
    why: list[str] = []
    if not pl.start_pipeline(stages, crawl_filter=crawl_filter, refused=why):
        raise HTTPException(409, why[0] if why else "管道已在运行中")
    return {"ok": True}


@router.post("/api/pipeline/stop")
def pipeline_stop():
    if not pl.request_stop():
        raise HTTPException(409, "当前没有正在执行的同步")
    return {"ok": True, "detail": "当前执行中的阶段结束后停止，已完成的结果会保留"}


@router.post("/api/pipeline/retry-failed")
def pipeline_retry_failed():
    """一键重试所有失败项:按上次各阶段的失败状态自动编排
    抓取/导出/回填空正文/索引/提取的重试链。"""
    ok, msg = pl.start_retry_failed()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "plan": msg}


@router.get("/api/pipeline/accounts")
def pipeline_accounts(channel: str | None = None):
    """来源里可筛选的目标列表(公众号名等),给管道面板的筛选下拉用。"""
    try:
        return {"accounts": pl.source_targets(_channel(channel))}
    except CollectorError as e:
        raise HTTPException(400, str(e)) from None
