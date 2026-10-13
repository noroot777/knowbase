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
    first_days: int = Field(default=daily.FIRST_DAYS, ge=1, le=7)
    channels: list[str] | None = None


@router.post('/settings')
def set_settings(body: SettingsRequest):
    try:
        return daily_run.save_settings(body.enabled, body.time, body.channels,
                                       body.sync, body.first_days)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get('/status')
def status():
    return daily_run.get_state()


@router.get('/window')
def window():
    """下一份日报默认覆盖的区间 + 现在攒了多少还没上报过的素材。界面拿它预填。

    `pending` 回答的是"上一份日报之后又进来了什么" —— 日报是一份钉在某一天的
    产物,生成完不会自己跟着新内容变;重试失败项或新加一个订阅之后,用户得有
    办法知道"有 N 条还没进过任何一份日报",再决定要不要重新生成(那会再花一次
    模型钱)。零模型调用,所以放在这条每次打开面板都会调的路上。查不出来时整个
    键给 null,窗口预填照旧 —— 它只是提示。
    """
    settings = daily_run.load_settings()
    try:
        info = daily.pending(settings['first_days'])
    except Exception:                               # noqa: BLE001 - 提示失败不该让面板打不开
        since, until = daily.next_window(settings['first_days'])
        info = {'since': since, 'until': until, 'items': None, 'late': None}
    return {'since': info['since'].isoformat(timespec='minutes'),
            'until': info['until'].isoformat(timespec='minutes'),
            'first_days': settings['first_days'],
            'pending': (None if info['items'] is None else
                        {'items': info['items'], 'late': info['late'],
                         'total': info['items'] + info['late']})}


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
    accepted, detail = daily_run.request_stop()
    if not accepted:
        # 没在跑、或者已经进了生成阶段(那一档停不了,原因照搬 daily_run 的说明)
        raise HTTPException(409, detail)
    return {'ok': True, 'detail': detail}


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
