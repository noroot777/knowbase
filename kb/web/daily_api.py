# -*- coding: utf-8 -*-
"""日报 API:定时设置、立即生成、运行状态、已生成日报的读取与删除。

生成本身在 kb.daily / kb.daily_run,这里只是它们的 HTTP 外壳——与管道面板
(kb.web.pipeline_api)同一个分工。
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from kb import daily, daily_run

router = APIRouter(prefix='/api/daily')


def _check_day(value: str | None) -> str | None:
    if value in (None, ''):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise HTTPException(400, '日期须为 YYYY-MM-DD') from None


def _check_time(value: str | None) -> datetime | None:
    """把界面上的 datetime-local(`2026-09-29T08:00`)解析成本地时刻。"""
    if value in (None, ''):
        return None
    try:
        return datetime.fromisoformat(value).replace(second=0, microsecond=0)
    except ValueError:
        raise HTTPException(400, '时刻须为 YYYY-MM-DDTHH:MM') from None


@router.get('/settings')
def get_settings():
    from kb.collectors import collection_channels
    return {'settings': daily_run.load_settings(),
            'channels': collection_channels(),
            'default_channels': daily_run.default_channels()}


class SettingsRequest(BaseModel):
    enabled: bool
    time: str = '08:00'
    sync: bool = True
    max_days: int = Field(default=daily.MAX_WINDOW_DAYS, ge=1, le=7)
    channels: list[str] | None = None


@router.post('/settings')
def set_settings(body: SettingsRequest):
    try:
        return daily_run.save_settings(body.enabled, body.time, body.channels,
                                       body.sync, body.max_days)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get('/status')
def status():
    return daily_run.get_state()


@router.get('/window')
def window():
    """下一份日报默认覆盖的区间:上一份的截止时刻 → 现在。界面拿它预填。"""
    since, until, clamped = daily.next_window(daily_run.load_settings()['max_days'])
    return {'since': since.isoformat(timespec='minutes'),
            'until': until.isoformat(timespec='minutes'), 'clamped': clamped,
            'max_days': daily_run.load_settings()['max_days']}


class RunRequest(BaseModel):
    since: str | None = None
    until: str | None = None
    sync: bool = True
    channels: list[str] | None = None


@router.post('/run')
def run(body: RunRequest | None = None):
    body = body or RunRequest()
    try:
        started = daily_run.start(_check_time(body.since), _check_time(body.until),
                                  sync=body.sync, channels=body.channels)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if not started:
        raise HTTPException(409, '日报任务已在运行中')
    return {'ok': True}


@router.post('/stop')
def stop():
    if not daily_run.request_stop():
        raise HTTPException(409, '当前没有正在运行的日报任务')
    return {'ok': True, 'detail': '当前阶段结束后停止，已同步的内容会保留'}


@router.get('/reports')
def list_reports(limit: int = 60):
    return {'reports': daily.reports(max(1, min(200, limit)))}


@router.get('/reports/{day}')
def read_report(day: str):
    found = daily.report(_check_day(day))
    if not found:
        raise HTTPException(404, '这一天还没有生成日报')
    return found


@router.delete('/reports/{day}')
def delete_report(day: str):
    if not daily.delete(_check_day(day)):
        raise HTTPException(404, '这一天还没有生成日报')
    return {'ok': True, 'detail': '日报已删除'}
