# -*- coding: utf-8 -*-
"""采集目标(订阅了哪些公众号 / RSS 源…)的增删改。

只跟 kb.collectors 的 TargetManager 契约打交道,不认识任何具体来源:
名字叫 targets 而不是 accounts,搜索/添加的语义也是通用的"找候选、加进订阅"。
换来源时这一层和前端都不用改。

采集器不支持目标管理时,GET /api/sources/targets 返回 manageable=false,
前端据此隐藏入口——而不是报错。
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kb.collectors import CollectorError, active_collector, target_manager

router = APIRouter()


def _manager():
    mgr = target_manager()
    if mgr is None:
        raise HTTPException(400, "当前采集器不支持在这里维护订阅列表")
    return mgr


def _guard(fn, *args, **kwargs) -> dict:
    """把采集器的失败翻成 HTTP。detail 是采集器给的成品文案,原样透传。"""
    try:
        return {"ok": True, "detail": fn(*args, **kwargs)}
    except CollectorError as e:
        raise HTTPException(400, str(e))


@router.get("/api/sources/targets")
def list_targets():
    collector = active_collector()
    mgr = target_manager(collector)
    if mgr is None:
        return {"manageable": False, "label": collector.label, "targets": []}
    try:
        targets = [t.as_dict() for t in mgr.list_targets()]
    except CollectorError as e:
        raise HTTPException(400, str(e))
    return {"manageable": True, "label": collector.label, "targets": targets}


@router.get("/api/sources/targets/search")
def search_targets(q: str = ""):
    q = q.strip()
    if not q:
        return {"candidates": []}
    try:
        return {"candidates": [t.as_dict() for t in _manager().search_targets(q)]}
    except CollectorError as e:
        raise HTTPException(400, str(e))


class TargetRef(BaseModel):
    # 名字里可能有 / 和各种符号,走 body 而不是路径参数,免去转义那一堆坑
    name: str
    id: str = ""


class TargetToggle(BaseModel):
    name: str
    enabled: bool


@router.post("/api/sources/targets/add")
def add_target(body: TargetRef):
    if not body.name.strip():
        raise HTTPException(400, "名字不能为空")
    return _guard(_manager().add_target, body.name.strip(), body.id.strip())


@router.post("/api/sources/targets/remove")
def remove_target(body: TargetRef):
    return _guard(_manager().remove_target, body.name)


@router.post("/api/sources/targets/toggle")
def toggle_target(body: TargetToggle):
    return _guard(_manager().set_target_enabled, body.name, body.enabled)
