"""wxdown-service Credential 接收与状态 API。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from src.crawler.profile_ext import (
    credential_public_status,
    profile_credential_store,
)
from src.services.clash_wxdown_route import (
    ClashRouteError,
    clash_wxdown_route,
)
from src.services.wechat_credential_refresh import (
    WeChatRefreshError,
    trigger_wechat_credential_refresh,
)


router = APIRouter(prefix="/api/profile-credentials", tags=["profile-credentials"])


class CredentialIngestRequest(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class CredentialRefreshRequest(BaseModel):
    biz: str = Field(min_length=1, max_length=256)


def _build_status() -> dict[str, Any]:
    records = profile_credential_store.records()
    record_status = {
        biz: credential_public_status(credential)
        for biz, credential in records.items()
    }
    accounts: list[dict[str, Any]] = []
    linked: set[str] = set()
    try:
        from src.db.repository import ArticleRepository

        with ArticleRepository() as repo:
            configured = repo.get_name2fakeid()
    except Exception:
        configured = {}

    for account_name, biz in configured.items():
        current = record_status.get(biz)
        if current:
            linked.add(biz)
            accounts.append(
                {
                    **current,
                    "account_name": account_name,
                    "state": "valid" if current["valid"] else "expired",
                }
            )
        else:
            accounts.append(
                {
                    "biz": biz,
                    "account_name": account_name,
                    "captured_name": "",
                    "captured_at": "",
                    "expires_at": "",
                    "seconds_remaining": 0,
                    "valid": False,
                    "state": "missing",
                }
            )

    for biz, current in record_status.items():
        if biz in linked:
            continue
        accounts.append(
            {
                **current,
                "account_name": current.get("captured_name") or "未匹配公众号",
                "state": "valid" if current["valid"] else "expired",
            }
        )

    accounts.sort(key=lambda item: (item["state"] != "valid", item["account_name"]))
    return {
        "ttl_minutes": 0,
        "valid_count": sum(1 for item in accounts if item["state"] == "valid"),
        "expired_count": sum(1 for item in accounts if item["state"] == "expired"),
        "missing_count": sum(1 for item in accounts if item["state"] == "missing"),
        "items": accounts,
    }


@router.get("/status")
def get_profile_credentials_status() -> dict[str, Any]:
    return _build_status()


@router.get("/route-status")
def get_wxdown_route_status() -> dict[str, Any]:
    """返回 Clash 当前是否把微信文章流量交给 wxdown。"""
    return clash_wxdown_route.status()


@router.post("/route-activate")
def activate_wxdown_route() -> dict[str, Any]:
    """立即把 WXDOWN-AUTO 切回 WXDOWN-LOCAL。"""
    try:
        return clash_wxdown_route.activate()
    except ClashRouteError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("")
def ingest_profile_credentials(body: CredentialIngestRequest) -> dict[str, Any]:
    if not body.items:
        raise HTTPException(status_code=400, detail="Credential 列表为空")
    saved, rejected = profile_credential_store.save_wxdown_items(body.items)
    if not saved and rejected:
        raise HTTPException(status_code=422, detail="；".join(rejected))
    return {
        "saved": len(saved),
        "rejected": rejected,
        "status": _build_status(),
    }


@router.post("/refresh")
def refresh_profile_credential(body: CredentialRefreshRequest) -> dict[str, Any]:
    """从知识库前端唤起 Mac 微信，触发指定公众号的 Credential 捕获。"""
    try:
        from src.db.repository import ArticleRepository

        with ArticleRepository() as repo:
            configured = repo.get_name2fakeid()
    except Exception as exc:
        raise HTTPException(status_code=503, detail="无法读取公众号配置") from exc

    account_name = next(
        (name for name, biz in configured.items() if str(biz) == body.biz),
        "",
    )
    if not account_name:
        raise HTTPException(status_code=404, detail="未找到该公众号配置")

    try:
        result = trigger_wechat_credential_refresh(account_name)
    except WeChatRefreshError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {**result.as_dict(), "status": _build_status()}


@router.delete("")
def clear_profile_credentials() -> dict[str, Any]:
    profile_credential_store.clear()
    return {"ok": True, "status": _build_status()}
