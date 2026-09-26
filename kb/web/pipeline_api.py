# -*- coding: utf-8 -*-
"""数据管道 API:状态、手动触发、失败重试、定时同步配置。

真正的阶段实现在 kb.pipeline;这里只是它的 HTTP 外壳。
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kb import pipeline as pl
from kb.collectors import CollectorError, active_collector, collection_channels

router = APIRouter()


def _channel(value: str | None) -> str:
    try:
        return active_collector(value).name
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/api/pipeline/channels")
def pipeline_channels():
    return {"channels": collection_channels(), "default": active_collector().name}


class ScheduleRequest(BaseModel):
    enabled: bool
    time: str = "07:30"
    channel: str | None = None


@router.get("/api/pipeline/schedule")
def pipeline_schedule():
    return pl.load_schedule()


@router.post("/api/pipeline/schedule")
def pipeline_schedule_set(body: ScheduleRequest):
    try:
        return pl.save_schedule(body.enabled, body.time, body.channel)
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
    state["last_run"] = pl.load_last_run()
    # 待处理数要扫文件,只有显式要求且空闲时才算
    if pending and not state["running"]:
        state["pending"] = pl.pending_counts()
    return state


class PipelineRunRequest(BaseModel):
    stages: list[str] | None = None  # None = 全部
    # 以下三个用于范围抓取(只影响 crawl 阶段):
    # 填了日期区间则按 [since_date, until_date] 补抓;accounts 空/None = 来源的全部目标
    accounts: list[str] | None = None
    since_date: str | None = None   # "YYYY-MM-DD"
    until_date: str | None = None   # "YYYY-MM-DD"
    channel: str | None = None


@router.post("/api/pipeline/run")
def pipeline_run(body: PipelineRunRequest | None = None):
    stages = body.stages if body else None
    crawl_filter = {"channel": _channel(body.channel if body else None)}
    if body and (body.since_date or body.until_date):
        if not (body.since_date and body.until_date):
            raise HTTPException(400, "起始和结束日期必须同时提供")
        try:
            dates = [date.fromisoformat(d) for d in (body.since_date, body.until_date)]
            if dates[0] > dates[1] or [d.isoformat() for d in dates] != [body.since_date, body.until_date]:
                raise ValueError
        except ValueError:
            raise HTTPException(400, "日期须为 YYYY-MM-DD，且起始日期不能晚于结束日期") from None
    if body and body.accounts and any(not name.strip() for name in body.accounts):
        raise HTTPException(400, "公众号名称不能为空")
    # 只限定目标、不限定时间也要成立(「就把这几个号的新文章抓一下」)。
    # 以前的条件只看日期,给了 accounts 却没给日期就退回 crawl_filter=None,
    # 于是全部订阅目标照抓——筛选被静默忽略,比报错更难发现。
    if body and (body.since_date or body.until_date or body.accounts):
        crawl_filter.update({
            "accounts": body.accounts or [],
            "since_date": body.since_date or "",
            "until_date": body.until_date or "",
        })
    if not pl.start_pipeline(stages, crawl_filter=crawl_filter):
        raise HTTPException(409, "管道已在运行中")
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
