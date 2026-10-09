"""Feed-backed module UI API; collector execution remains in the shared pipeline."""
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from kb import pipeline
from kb.feeds import registry as reg
from kb.feeds import x_channels
from kb.source_types import catalog

router = APIRouter(prefix='/api/intake')


def kind_checked(kind):
    try:
        return reg.check_kind(kind)
    except ValueError as e:
        raise HTTPException(404,str(e)) from None


def guard(kind):
    kind_checked(kind)
    state = pipeline.get_state()
    running = {scope['channel'] for scope in pipeline.crawl_channels(state.get('crawl_filter'))}
    if state['running'] and kind in running:
        raise HTTPException(409,'该来源正在同步，请等待结束后修改订阅')


def call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ValueError as e:
        raise HTTPException(400,str(e)) from None
    except LookupError as e:
        raise HTTPException(404,str(e)) from None


@router.get('/modules')
def modules():
    return {'modules':catalog()}


@router.get('/{kind}')
def overview(kind: str):
    kind_checked(kind)
    sources = reg.subscriptions(kind)
    settings = reg.get_settings(kind)
    for source in sources:
        source['configuration_error'] = reg.configuration_error(source, settings)
    with reg.db() as c:
        count = c.execute('SELECT count(*) FROM feed_items WHERE kind=?',(kind,)).fetchone()[0]
    return {'sources':sources, 'settings':settings, 'last_run':reg.latest_run(kind),
            'total':count, 'enabled':sum(s['enabled'] for s in sources),
            'ready':sum(s['enabled'] and not s['configuration_error'] for s in sources),
            'unconfigured':sum(s['enabled'] and bool(s['configuration_error']) for s in sources),
            'errors':sum(bool(s['last_error']) and s['enabled'] for s in sources)}


@router.get('/{kind}/items')
def list_items(kind: str, source_id: int | None = None,
               offset: Annotated[int,Query(ge=0)] = 0, limit: Annotated[int,Query(ge=1,le=100)] = 30,
               q: Annotated[str,Query(max_length=200)] = ''):
    kind_checked(kind)
    return reg.items(kind,source_id,offset,limit,q)


class Subscription(BaseModel):
    name: str = Field(min_length=1,max_length=120)
    url: str = Field(max_length=2048)
    feed_url: str = Field(default='',max_length=2048)
    group_name: str = Field(default='',max_length=80)
    enabled: bool = True
    # 「去原文链接把正文抓回来」(见 kb/feeds/fulltext.py)。默认关:只有链接
    # 聚合源需要它,对本来就给全文的 feed 再抓一次是拿启发式提取的结果去换
    # 作者亲手给的全文。
    fulltext: bool = False


class SubscriptionPatch(BaseModel):
    name: str | None = Field(default=None,min_length=1,max_length=120)
    url: str | None = Field(default=None,max_length=2048)
    feed_url: str | None = Field(default=None,max_length=2048)
    group_name: str | None = Field(default=None,max_length=80)
    enabled: bool | None = None
    fulltext: bool | None = None


@router.post('/{kind}/sources',status_code=201)
def add_source(kind: str, body: Subscription):
    guard(kind)
    return call(reg.save_subscription,kind,body.model_dump())


@router.patch('/{kind}/sources/{source_id}')
def update_source(kind: str,source_id: int,body: SubscriptionPatch):
    guard(kind)
    return call(reg.save_subscription,kind,body.model_dump(exclude_none=True),source_id)


@router.delete('/{kind}/sources/{source_id}')
def delete_source(kind: str,source_id: int):
    guard(kind)
    call(reg.delete_subscription,kind,source_id)
    return {'ok':True,'detail':'已删除订阅，已收录内容保留'}


class Settings(BaseModel):
    enabled: bool
    interval_minutes: int = Field(ge=15,le=10080)
    bridges: list[str] | None = None
    x_channel: str | None = None
    x_feedgrab_bin: str | None = None
    x_chrome_path: str | None = None


@router.put('/{kind}/settings')
def set_settings(kind: str,body: Settings):
    kind_checked(kind)
    values = body.model_dump(exclude_none=True)
    if not x_channels.supports_channels(kind):
        # X 的通道字段只对 social 有意义。不摘掉的话它们会被原样存进 rss 的设置里
        # (那一类压根不走 `x_channels.validate`),留下一份谁也不读、也没校验过的垃圾。
        for key in ('x_channel', 'x_feedgrab_bin', 'x_chrome_path'):
            values.pop(key, None)
    # Changing the interval starts a new interval; enabling starts within the next tick.
    old = reg.get_settings(kind)
    values['next_run'] = (time.time() if body.enabled and not old['enabled'] else
                          time.time()+body.interval_minutes*60)
    return call(reg.save_settings,kind,values)


# ── X 的采集通道(只有 social 这一类有)──────────────────────────────────────


def _x_kind(kind: str) -> dict:
    """确认这一类来源有通道可选,并把当前设置取出来。"""
    kind_checked(kind)
    if not x_channels.supports_channels(kind):
        raise HTTPException(404, '该来源没有可选的采集通道')
    return reg.get_settings(kind)


@router.get('/{kind}/x-channel')
def x_channel(kind: str):
    """通道清单 + 当前选择 + 便宜的就绪判断。面板每次打开都调它,所以不连网。"""
    settings = _x_kind(kind)
    ready, why, hint = x_channels.readiness(settings)
    current = x_channels.channel_of(settings)
    # 这里不发别名表:通道选择只存在服务端的 feed_settings 里,而 `get_settings`
    # 读出来就已经 resolve 过(见 `x_channels.resolve`),浏览器手里拿不到退役名。
    # 公众号那边要发别名是因为那份选择存在 localStorage —— 在后端的射程之外。
    return {
        'current': current,
        'channels': [{'id': c, 'label': x_channels.LABELS[c],
                      'hint': x_channels.HINTS[c]} for c in x_channels.CHANNELS],
        'ready': ready, 'why': why, 'hint': hint,
        'login_command': x_channels.login_command(settings),
        'needs_login_window': current == x_channels.BROWSER,
    }


@router.post('/{kind}/x-channel/login')
def x_channel_login(kind: str):
    """开一个有头窗口让用户登录(只有浏览器通道走这条)。起完就返回。"""
    settings = _x_kind(kind)
    if x_channels.channel_of(settings) != x_channels.BROWSER:
        raise HTTPException(400, '只有「我的浏览器登录态」这条通道需要在这里登录')
    from kb.feeds import x_browser
    try:
        detail = x_browser.open_login_window(str(x_channels.BROWSER_PROFILE),
                                             settings.get('x_chrome_path', ''))
    except Exception as e:                                # noqa: BLE001 - 成品文案
        raise HTTPException(400, str(e)) from None
    return {'ok': True, 'detail': detail}


@router.post('/{kind}/x-channel/check')
def x_channel_check(kind: str):
    """真连一次。这是用户点「检查连接」才做的事,所以允许慢。"""
    settings = _x_kind(kind)
    ready, detail = call(x_channels.check, settings)
    return {'ready': ready, 'detail': detail}


class SyncRequest(BaseModel):
    source_ids: list[int] = Field(default_factory=list,max_length=500)


@router.post('/{kind}/sync')
def sync(kind: str,body: SyncRequest):
    kind_checked(kind)
    active = [s for s in reg.subscriptions(kind) if s['enabled']]
    selected = [s for s in active if not body.source_ids or s['id'] in body.source_ids]
    if not selected or (body.source_ids and len(selected) != len(set(body.source_ids))):
        raise HTTPException(400,'请选择存在且已启用的订阅')
    settings = reg.get_settings(kind)
    if all(reg.configuration_error(s, settings) for s in selected):
        raise HTTPException(400, reg.configuration_error(selected[0], settings))
    if not pipeline.start_pipeline(['crawl','export','index'],crawl_filter={
            'channel':kind,'accounts':[str(s['id']) for s in selected], 'force':True}):
        raise HTTPException(409,'其他同步正在运行，请结束后重试；自动同步会顺延')
    return {'ok':True,'detail':'同步已开始，完成后可在全文检索中查找'}
