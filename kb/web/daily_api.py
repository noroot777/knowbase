# -*- coding: utf-8 -*-
"""日报 API:定时设置、立即生成、运行状态、已生成日报的读取与删除。

生成本身在 kb.daily / kb.daily_run,这里只是它们的 HTTP 外壳——与管道面板
(kb.web.pipeline_api)同一个分工。
"""

from __future__ import annotations

from datetime import date, datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from kb import daily, daily_run, daily_trans, fetch_rounds
from kb.config import ScheduleUnreadable
from kb.daily_materials import material_view
from kb.web.common import VAULT_NAME, unreadable_config

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
    """下一份日报默认覆盖的区间 + 再生成一次能多带进来多少 + 今天抓了几次。界面拿它预填。

    `pending` 答的是**取材**那一侧的两个问题,别混:`gain` 是「再生成一次会多带进来
    几条没进过日报的内容」—— 按钮上那句「有 N 条新内容」和「这颗按钮还要不要显示」
    都只认它;`items`/`late`/`total` 是「队列里一共还积着多少没进过日报的」,只进
    tooltip(名额每轮只带得走固定条数,生产库实测积压 1097 条而重新生成一次只多出
    11 条,写到按钮边上会让人照着它花一次模型钱)。口径在 `daily.pending`。

    `fetch` 是**抓取自己的事实**(`kb.fetch_rounds`):今天第 N 次同步往 vault 新落了
    多少篇。它和 `gain` 回答的不是一个问题(抓回来 860 篇、而其中会进下一份日报的
    只有一部分:名额、补录回溯范围、软广与衍生树都在中间),所以**不许互相冒充** ——
    抓取事实整条进 tooltip。生成、重新生成、删掉日报都不改变这个数。三条都零模型
    调用,所以放在这条每次打开面板都会调的路上。

    **`fetch: null` 是「不可确认」,不是「没抓到」**(`fetch_rounds.today` 读不出
    账本、或者有行没落下去时就给 null;`{'n': 0}` 才是确定今天还没抓过)。
    两者混成一句的话,一次写失败之后面板会一直笃定地说「今天还没有抓取」。
    `fetch.partial` 是第三件事:次数确定、而**这一轮的条数只是下界** —— 某个来源
    的成果说不出来,这一轮**参与的子轮**没把结束行交齐(进程在收尾前就没了;一轮
    日报里每个来源各算一个子轮,见 `fetch_rounds.note_end`),有一行证据在读账
    故障里没留住(见 `fetch_rounds._mark`),或者**别的进程(重启前的自己)还扣着
    这一轮的行没落盘**(`kb.fetch_pending` 那笔凭证)。四种都原样透传,前端各有
    文案,见 `daily_chip.js` 的 `renderDailyGenButton`。

    `fetch.added` 还带着**它由什么构成**:`fresh`(这一次新抓到的)/ `carried`
    (之前抓到、这一次才补导进来的)/ `unsure`(说不清是哪一次抓到的),三个加
    起来等于 `added`。`added` 报的是「这次同步之后日报能多拿到多少」,而它和
    「远端这次新发现多少」在补导场景下必然不等 —— **差额如实拆开交给前端,不从
    `added` 里减掉**(口径与两个数为什么不等见 `docs/decisions/daily.md`)。
    这几个数一并原样透传,这里不做任何合并。
    查不出来时窗口预填照旧 —— 这两条只是提示,不该让面板打不开。
    **`pending: null`(算不出来)前端照旧显示按钮** —— 那是"不知道",不是"没有
    新内容",藏掉按钮等于拿一次读取故障把出报这条路堵死。**`pending.gain: null`
    是同一回事的更细一档**:两个数算出来了,只有"会不会多带进内容"这一问答不上来
    (今天那份里有一条账本没记下当时那一版,见 `daily_gain._will_add` 第三档)。

    `day` / `has_report` 是**藏按钮要的另一半**:这一次会写到哪一天、那一天的日报
    文件**此刻**在不在。只有 `gain == 0` 且 `has_report is true` 才允许把按钮藏掉
    —— 两件事必须同一趟回包给出来,界面手里那份日报列表缓存答不了"此刻"
    (空闲轮询只刷新这条,不刷新列表;在 Obsidian 里把今天那份移走之后,缓存会一直
    说"还在",按钮就一直藏着)。`null` = 不知道,不藏。
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
        # 算不出来时**连"那一天有没有日报"也一起报不知道**:界面藏按钮要两个
        # 条件一起成立,这里给 False 等于替它答了一个我们并没有核实的答案。
        info = {'since': since, 'until': until, 'items': None, 'late': None,
                'gain': None, 'day': None, 'has_report': None}
    try:
        got = fetch_rounds.today()
    except Exception:                               # noqa: BLE001 - 同上
        got = None
    return {'since': info['since'].isoformat(timespec='minutes'),
            'until': info['until'].isoformat(timespec='minutes'),
            'first_days': settings['first_days'],
            'fetch': got,
            'day': info['day'], 'has_report': info['has_report'],
            'pending': (None if info['items'] is None else
                        {'items': info['items'], 'late': info['late'],
                         'total': info['items'] + info['late'],
                         'gain': info['gain']})}


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
    return {**found, 'material_view': material_view(found['body'])}


@router.delete('/reports/{day}')
def delete_report(day: str):
    if not daily.delete(_check_day(day)):
        raise HTTPException(404, '这一天还没有生成日报')
    return {'ok': True, 'detail': '日报已删除'}


class TranslationsRequest(BaseModel):
    urls: list[str] = Field(default_factory=list)


@router.post('/translations')
def translations(body: TranslationsRequest):
    """这批出处地址里,哪几条在本地读得到中文译文(判据见 kb.daily_trans)。

    面板按回包在出处编号后面补一个入口,点开从左边滑出阅读器读 vault 里那一篇。
    **没有的那些一个字都不说** —— 回包里只有找到的,别的照旧只有原文链接。
    """
    return {'docs': daily_trans.translations(body.urls), 'vault': VAULT_NAME}
