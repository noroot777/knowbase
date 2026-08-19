# -*- coding: utf-8 -*-
"""数据管道 API:状态、手动触发、失败重试、定时同步配置。

真正的阶段实现在 kb.pipeline;这里只是它的 HTTP 外壳。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kb import pipeline as pl

router = APIRouter()


class ScheduleRequest(BaseModel):
    enabled: bool
    time: str = "07:30"


@router.get("/api/pipeline/schedule")
def pipeline_schedule():
    return pl.load_schedule()


@router.post("/api/pipeline/schedule")
def pipeline_schedule_set(body: ScheduleRequest):
    try:
        return pl.save_schedule(body.enabled, body.time)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/pipeline/status")
def pipeline_status(pending: bool = False):
    state = dict(pl.get_state())
    state["wechatoa"] = pl.wechatoa_status()
    state["last_run"] = pl.load_last_run()
    # 待处理数要扫文件,只有显式要求且空闲时才算
    if pending and not state["running"]:
        state["pending"] = pl.pending_counts()
    return state


class PipelineRunRequest(BaseModel):
    stages: list[str] | None = None  # None = 全部
    # 以下三个用于范围抓取(只影响 crawl 阶段):
    # 填了日期区间则按 [since_date, until_date] 补抓;accounts 空/None = 全部订阅号
    accounts: list[str] | None = None
    since_date: str | None = None   # "YYYY-MM-DD"
    until_date: str | None = None   # "YYYY-MM-DD"


@router.post("/api/pipeline/run")
def pipeline_run(body: PipelineRunRequest | None = None):
    stages = body.stages if body else None
    crawl_filter = None
    if body and (body.since_date or body.until_date):
        if not (body.since_date and body.until_date):
            raise HTTPException(400, "起始和结束日期必须同时提供")
        crawl_filter = {
            "accounts": body.accounts or [],
            "since_date": body.since_date,
            "until_date": body.until_date,
        }
    if not pl.start_pipeline(stages, crawl_filter=crawl_filter):
        raise HTTPException(409, "管道已在运行中")
    return {"ok": True}


@router.post("/api/pipeline/retry-failed")
def pipeline_retry_failed():
    """一键重试所有失败项:按上次各阶段的失败状态自动编排
    抓取/导出/回填空正文/索引/提取的重试链。"""
    ok, msg = pl.start_retry_failed()
    if not ok:
        raise HTTPException(409, msg)
    return {"ok": True, "plan": msg}


@router.get("/api/pipeline/accounts")
def pipeline_accounts():
    """WeChatOA 订阅的公众号列表,给管道面板的账号筛选下拉用。"""
    return {"accounts": pl.wechatoa_accounts()}
