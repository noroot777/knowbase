"""Feed-backed module UI API; collector execution remains in the shared pipeline."""
import time
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from kb import pipeline
from kb.feeds import registry as reg
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
    if state['running'] and state.get('crawl_filter',{}).get('channel') == kind:
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
    with reg.db() as c:
        count = c.execute('SELECT count(*) FROM feed_items WHERE kind=?',(kind,)).fetchone()[0]
    return {'sources':sources, 'settings':reg.get_settings(kind), 'last_run':reg.latest_run(kind),
            'total':count, 'enabled':sum(s['enabled'] for s in sources),
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


class SubscriptionPatch(BaseModel):
    name: str | None = Field(default=None,min_length=1,max_length=120)
    url: str | None = Field(default=None,max_length=2048)
    feed_url: str | None = Field(default=None,max_length=2048)
    group_name: str | None = Field(default=None,max_length=80)
    enabled: bool | None = None


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


@router.put('/{kind}/settings')
def set_settings(kind: str,body: Settings):
    kind_checked(kind)
    values = body.model_dump(exclude_none=True)
    # Changing the interval starts a new interval; enabling starts within the next tick.
    old = reg.get_settings(kind)
    values['next_run'] = (time.time() if body.enabled and not old['enabled'] else
                          time.time()+body.interval_minutes*60)
    return call(reg.save_settings,kind,values)


class SyncRequest(BaseModel):
    source_ids: list[int] = Field(default_factory=list,max_length=500)


@router.post('/{kind}/sync')
def sync(kind: str,body: SyncRequest):
    kind_checked(kind)
    active = [s for s in reg.subscriptions(kind) if s['enabled']]
    selected = [s for s in active if not body.source_ids or s['id'] in body.source_ids]
    if not selected or (body.source_ids and len(selected) != len(set(body.source_ids))):
        raise HTTPException(400,'请选择存在且已启用的订阅')
    if not pipeline.start_pipeline(['crawl','export','index'],crawl_filter={
            'channel':kind,'accounts':[str(s['id']) for s in selected], 'force':True}):
        raise HTTPException(409,'其他同步正在运行，请结束后重试；自动同步会顺延')
    return {'ok':True,'detail':'同步已开始，完成后可在全文检索中查找'}
