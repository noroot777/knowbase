"""把 profile_ext 通道安装到现有 WechatRequest，不侵入旧爬虫文件。"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

from src.crawler.profile_ext import (
    ProfileCredentialUnavailableError,
    fetch_profile_ext_page,
    profile_credential_store,
)
from src.utils.data_manager import data_manager
from src.utils.helpers import time_delta, time_now


_MAX_PAGES = 10
_PAGE_MIN_SECONDS, _PAGE_MAX_SECONDS = 2.5, 4.0


def _canonical_link(link: str) -> str:
    if not link:
        return ""
    try:
        from src.utils.article_import import canonical_article_link

        return canonical_article_link(link)
    except Exception:
        return link.split("#", 1)[0]


def _profile_aware_update(
    self: Any,
    fakeid: str,
    message_exist: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """通用增量抓取：同时适配 profile_ext 和旧列表返回结构。"""
    existing = message_exist or []
    existing_ids = {str(item.get("id") or "") for item in existing if item.get("id")}
    existing_links = {
        _canonical_link(str(item.get("link") or ""))
        for item in existing
        if item.get("link")
    }

    from src.db.repository import ArticleRepository

    with ArticleRepository() as repo:
        rows = repo.conn.execute("SELECT article_id FROM deleted_articles").fetchall()
        skipped_by_user = {str(row["article_id"]) for row in rows}

    known_deleted = set(data_manager.issues_message.get("is_delete") or [])
    collected: list[dict[str, Any]] = []
    page_size = 20
    offset = 0

    for _ in range(_MAX_PAGES):
        page = self.fetch_appmsg_preview_list(
            fakeid,
            begin=offset,
            count=page_size,
        )
        if not page:
            break
        hit_boundary = False
        for item in page:
            article_id = str(item.get("id") or "")
            canonical = _canonical_link(str(item.get("link") or ""))
            if article_id in skipped_by_user:
                continue
            if article_id in existing_ids or (canonical and canonical in existing_links):
                hit_boundary = True
                continue
            if item.get("is_deleted"):
                if article_id and article_id not in known_deleted:
                    data_manager.issues_message["is_delete"].append(article_id)
                continue
            if int(item.get("item_show_type") or 0) in (5, 8, 10):
                continue
            create_time = str(item.get("create_time") or "")
            if create_time and time_delta(time_now(), create_time).days > 30:
                hit_boundary = True
                continue
            collected.append(dict(item))

        if hit_boundary or len(page) < page_size:
            break
        offset += page_size
        time.sleep(random.uniform(_PAGE_MIN_SECONDS, _PAGE_MAX_SECONDS))

    collected.sort(key=lambda item: str(item.get("create_time") or ""))
    return collected


def install_profile_ext_support() -> None:
    """幂等安装适配器；仅在 web_app 组合入口中调用。"""
    from src.crawler.wechat_request import WechatRequest

    if getattr(WechatRequest, "_profile_ext_installed", False):
        return
    legacy_fetch: Callable[..., list[dict[str, Any]]] = (
        WechatRequest.fetch_appmsg_preview_list
    )

    def profile_aware_fetch(
        self: Any,
        fakeid: str,
        *,
        count: int = 20,
        begin: int = 0,
    ) -> list[dict[str, Any]]:
        if fakeid in profile_credential_store.records():
            # 已被标记失效的旧值也再向服务端试一次；若微信恢复接受，
            # 会自动清除失效状态，无需用户手工重置本地 JSON。
            credential = profile_credential_store.get(fakeid, allow_invalid=True)
            try:
                page = fetch_profile_ext_page(
                    credential,
                    offset=max(0, int(begin)),
                    count=min(20, max(1, int(count))),
                )
            except ProfileCredentialUnavailableError:
                profile_credential_store.mark_invalid(fakeid)
                raise
            profile_credential_store.mark_valid(fakeid)
            return page
        return legacy_fetch(self, fakeid, count=count, begin=begin)

    WechatRequest.fetch_appmsg_preview_list = profile_aware_fetch
    WechatRequest.fakeid2message_update = _profile_aware_update
    WechatRequest._profile_ext_installed = True
