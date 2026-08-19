# -*- coding: utf-8 -*-
"""收藏 API:备忘待试清单(整篇或片段,inbox 待试 / done 已处理 / archived 沉底)。

行动卡提取与整体分诊在 kb.favbrain,这里只管存取与状态流转。
"""

from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from kb import favbrain, llm, store
from kb.web.common import VAULT_NAME, now as _now

router = APIRouter()


# ── 收藏(备忘待试清单:整篇或片段,inbox 待试 / done 已处理 / archived 沉底) ──────

STALE_DAYS = 60  # 待试超过这个天数算"陈旧",提示一键沉底

_FAV_STATUSES = ("inbox", "done", "archived")


class FavoriteRequest(BaseModel):
    path: str


class SnippetRequest(BaseModel):
    path: str
    quote: str
    prefix: str = ""   # 前后文锚点,回跳原文时定位用
    suffix: str = ""
    note: str = ""


class FavoritePatch(BaseModel):
    status: str | None = None
    note: str | None = None


def _stale_cutoff() -> str:
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=STALE_DAYS)).isoformat(timespec="seconds")


@router.get("/api/favorites")
def api_favorites(status: str = "inbox"):
    """收藏列表(带文档元数据),按收藏时间倒序。

    只返回一个状态的条目,同时附带各状态计数、陈旧数(inbox 超过 STALE_DAYS
    没动过的)和整篇收藏 path 集合(搜索卡片/reader 的 ★ 状态用,不分状态)。
    """
    if status not in _FAV_STATUSES:
        raise HTTPException(400, f"status 只能是 {'/'.join(_FAV_STATUSES)}")
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT f.id, f.path, f.quote, f.prefix, f.suffix, f.note, f.status,"
            " f.action_json, f.created_at, f.updated_at,"
            " d.source, d.title, d.account, d.published, d.url, d.summary"
            " FROM favorites f JOIN documents d ON d.path = f.path"
            " WHERE f.status = ? ORDER BY f.created_at DESC",
            (status,),
        ).fetchall()
        favorites = []
        for r in rows:
            item = {**dict(r), "vault": VAULT_NAME, "action": None}
            aj = item.pop("action_json", "")
            if aj:
                try:
                    item["action"] = json.loads(aj)
                except ValueError:
                    pass
            favorites.append(item)
        counts = {s: 0 for s in _FAV_STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM favorites GROUP BY status"):
            if r["status"] in counts:
                counts[r["status"]] = r["n"]
        stale = conn.execute(
            "SELECT COUNT(*) FROM favorites WHERE status='inbox' AND updated_at < ?",
            (_stale_cutoff(),),
        ).fetchone()[0]
        doc_paths = [r["path"] for r in conn.execute(
            "SELECT DISTINCT path FROM favorites WHERE quote=''"
        )]
        return {
            "favorites": favorites,
            "counts": counts, "stale": stale, "stale_days": STALE_DAYS,
            "doc_paths": doc_paths,
        }
    finally:
        conn.close()


@router.post("/api/favorites/toggle")
def api_favorite_toggle(body: FavoriteRequest):
    """收藏/取消收藏一篇文章(整篇条目,quote='';片段收藏不受影响)。"""
    conn = store.connect()
    try:
        if conn.execute(
            "SELECT 1 FROM documents WHERE path=?", (body.path,)
        ).fetchone() is None:
            raise HTTPException(404, "文档不在索引里")
        cur = conn.execute(
            "DELETE FROM favorites WHERE path=? AND quote=''", (body.path,)
        )
        favorited = cur.rowcount == 0
        new_id = None
        if favorited:
            now = _now()
            cur = conn.execute(
                "INSERT INTO favorites (path, created_at, updated_at) VALUES (?, ?, ?)",
                (body.path, now, now),
            )
            new_id = cur.lastrowid
        conn.commit()
        if new_id:
            favbrain.extract_action_async(new_id)  # 行动卡后台补全,不阻塞收藏
        return {"favorited": favorited}
    finally:
        conn.close()


@router.post("/api/favorites")
def api_favorite_snippet(body: SnippetRequest):
    """收藏文章的一个片段(doc 阅读页选中文字后调用)。"""
    quote = body.quote.strip()
    if not quote:
        raise HTTPException(400, "片段内容不能为空")
    conn = store.connect()
    try:
        if conn.execute(
            "SELECT 1 FROM documents WHERE path=?", (body.path,)
        ).fetchone() is None:
            raise HTTPException(404, "文档不在索引里")
        now = _now()
        cur = conn.execute(
            "INSERT INTO favorites (path, quote, prefix, suffix, note, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (body.path, quote, body.prefix, body.suffix, body.note.strip(), now, now),
        )
        conn.commit()
        favbrain.extract_action_async(cur.lastrowid)  # 行动卡后台补全
        return {"id": cur.lastrowid}
    finally:
        conn.close()


@router.patch("/api/favorites/{fav_id}")
def api_favorite_patch(fav_id: int, body: FavoritePatch):
    """状态流转(待试/已处理/沉底)或改备注。"""
    if body.status is not None and body.status not in _FAV_STATUSES:
        raise HTTPException(400, f"status 只能是 {'/'.join(_FAV_STATUSES)}")
    sets, args = ["updated_at=?"], [_now()]
    if body.status is not None:
        sets.append("status=?")
        args.append(body.status)
    if body.note is not None:
        sets.append("note=?")
        args.append(body.note.strip())
    conn = store.connect()
    try:
        cur = conn.execute(
            f"UPDATE favorites SET {', '.join(sets)} WHERE id=?", (*args, fav_id)
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "收藏不存在")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@router.delete("/api/favorites/{fav_id}", status_code=204)
def api_favorite_delete(fav_id: int):
    conn = store.connect()
    try:
        cur = conn.execute("DELETE FROM favorites WHERE id=?", (fav_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "收藏不存在")
        conn.commit()
    finally:
        conn.close()


@router.post("/api/favorites/archive-stale")
def api_favorite_archive_stale():
    """一键沉底:待试超过 STALE_DAYS 没动过的全部转 archived。"""
    conn = store.connect()
    try:
        cur = conn.execute(
            "UPDATE favorites SET status='archived', updated_at=?"
            " WHERE status='inbox' AND updated_at < ?",
            (_now(), _stale_cutoff()),
        )
        conn.commit()
        return {"archived": cur.rowcount}
    finally:
        conn.close()


class FavSettingsRequest(BaseModel):
    model: str = ""   # 空 = 清除,回落默认问答模型


@router.get("/api/favorites/settings")
def api_favorite_settings():
    """收藏整理(行动卡/分诊)当前用的模型;下拉候选用 /api/models。"""
    return {"model": llm.get_fav_model()}


@router.post("/api/favorites/settings")
def api_favorite_settings_set(body: FavSettingsRequest):
    llm.set_fav_model(body.model)
    return {"model": llm.get_fav_model()}


@router.get("/api/favorites/triage")
def api_favorite_triage():
    """分诊状态 + 缓存结果。fresh=False 表示 inbox 变过,结果可能过时(仍可展示)。"""
    return favbrain.triage_status()


@router.post("/api/favorites/triage/run")
def api_favorite_triage_run():
    """后台重跑分诊;前端轮询 GET /api/favorites/triage 直到 running=False。"""
    started = favbrain.run_triage_async()
    return {"started": started}


@router.post("/api/favorites/{fav_id}/extract")
def api_favorite_extract(fav_id: int):
    """手动补一张行动卡(收藏时提取失败/老数据没有时,前端点重试)。"""
    action = favbrain.extract_action(fav_id)
    if action is None:
        raise HTTPException(502, "行动卡生成失败(检查模型与 key 配置)")
    return {"action": action}
