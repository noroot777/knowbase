# -*- coding: utf-8 -*-
"""日报 API:定时设置、立即生成、运行状态、已生成日报的读取与删除。

生成本身在 kb.daily / kb.daily_run,这里只是它们的 HTTP 外壳——与管道面板
(kb.web.pipeline_api)同一个分工。
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from kb import daily, daily_run, fetch_rounds
from kb.config import ScheduleUnreadable
from kb.web.common import unreadable_config

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
    try:
        settings = daily_run.load_settings()
    except ScheduleUnreadable as e:
        raise unreadable_config(e) from None
    return {'settings': settings,
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
    except ScheduleUnreadable as e:
        # 保存要先读旧那份来合并(last_fired / next_run)。读不出来时**宁可存不下**
        # 也不拿缺省值当底写一份出去:那会把"今天已经出过报"这类事实一起抹掉。
        raise unreadable_config(e) from None
    except OSError as e:
        # 写不进去也要如实说(界面照它显示「未保存」并保留草稿)。原来那份还在
        # (原子替换,见 `kb.config.write_config_file`)。
        raise HTTPException(503, f'没能保存定时设置({e})。原来那份还在。') from None
    except ValueError as e:
        raise HTTPException(400, str(e)) from None


@router.get('/status')
def status():
    return daily_run.get_state()


@router.get('/window')
def window():
    """下一份日报默认覆盖的区间 + 今天第几次抓取抓了多少。界面拿它预填。

    `fetch` 是**抓取自己的事实**(`kb.fetch_rounds`):今天第 N 次同步往 vault 新落了
    多少篇。按钮上报的就是它 —— 一天内同步第二次之后,用户要知道"这次抓了多少"
    才能决定要不要重新生成那份日报(那会再花一次模型钱)。生成、重新生成、删掉
    日报都不改变这个数。`pending` 是另一回事(队列里一共还积着多少没进过日报的),
    给 tooltip 用。两条都零模型调用,所以放在这条每次打开面板都会调的路上。

    **`fetch: null` 是「不可确认」,不是「没抓到」**(`fetch_rounds.today` 读不出
    账本、或者有行没落下去时就给 null;`{'n': 0}` 才是确定今天还没抓过)。
    两者混成一句的话,一次写失败之后面板会一直笃定地说「今天还没有抓取」。
    `fetch.partial` 是第三件事:次数确定、而**这一轮的条数只是下界**(某个来源
    的成果说不出来)。三种都原样透传,前端各有文案,见 `daily.js` 的
    `renderDailyGenButton`。
    查不出来时窗口预填照旧 —— 这两条只是提示,不该让面板打不开。
    """
    # 这条只给窗口预填(回溯几天),不报"定时开没开" —— 所以读不出来时退回缺省
    # 天数照常给出窗口,而不是让面板打不开。那句"开没开"由 /settings 回答。
    try:
        first_days = daily_run.load_settings()['first_days']
    except ScheduleUnreadable:
        first_days = daily.FIRST_DAYS
    settings = {'first_days': first_days}
    try:
        info = daily.pending(settings['first_days'])
    except Exception:                               # noqa: BLE001 - 提示失败不该让面板打不开
        since, until = daily.next_window(settings['first_days'])
        info = {'since': since, 'until': until, 'items': None, 'late': None}
    try:
        got = fetch_rounds.today()
    except Exception:                               # noqa: BLE001 - 同上
        got = None
    return {'since': info['since'].isoformat(timespec='minutes'),
            'until': info['until'].isoformat(timespec='minutes'),
            'first_days': settings['first_days'],
            'fetch': got,
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
    except ScheduleUnreadable as e:
        # 手动生成也要读设置拿同步范围。拿缺省范围替它跑等于**悄悄换了同步范围**
        # (比如用户只留了 RSS,缺省却带上公众号),所以这里也如实报读不出来。
        raise unreadable_config(e) from None
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
