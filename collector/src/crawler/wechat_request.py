# -*- coding: utf-8 -*-
"""公众号文章列表抓取。

这里原来是公众平台后台的 appmsgpublish 接口(扫码拿的 token + cookie)。微信关掉
它之后,列表只能从 Mac 微信客户端的 /mp/profile_ext?action=getmsg 拿——凭证由
wxdown 的 mitm 截下来,每个号各一份。

旧接口的代码删干净了,不是"顺手清理":它留着的时候是有害的。原来的降级是"这个号
没有 profile_ext 凭证 → 退回 appmsgpublish",而那条路今天必然返回 invalid session,
于是一个号缺凭证就把整轮采集掐断,还告诉用户去跑 login——login 续的是另一套凭证,
对这个错误毫无作用。现在缺凭证就是缺凭证,报在这个号头上,其余的号照抓。

扫码 token 只剩搜号一个用途,在 api._search_biz 里。
"""

from __future__ import annotations

import random
import time
from typing import Any

from src.crawler.profile_ext import (
    ProfileCredentialUnavailableError,
    fetch_profile_ext_page,
    profile_credential_store,
)
from src.utils.data_manager import data_manager
from src.utils.helpers import time_delta, time_now

# 增量抓取时单个号最多往前翻多少页,兜底防止失控
_MAX_PAGES = 10
# 翻页之间的随机等待秒数
_PAGE_MIN_SECONDS, _PAGE_MAX_SECONDS = 2.5, 4.0
# profile_ext 单页上限就是 20,给大了服务端也只给 20
_PAGE_SIZE = 20


def _canonical_link(link: str) -> str:
    if not link:
        return ""
    try:
        from src.utils.article_identity import article_identity
        from src.utils.article_links import canonical_article_link

        return article_identity(link) or canonical_article_link(link)
    except Exception:
        return link.split("#", 1)[0]


class WechatRequest:
    """公众号文章列表客户端。

    无状态:凭证每次现读 profile_credential_store,不在 __init__ 里缓存——抓取
    过程中 wxdown 随时可能截到新的一份,缓存会让这一轮一直用旧的。
    """

    def fetch_article_page(
        self,
        fakeid: str,
        *,
        count: int = _PAGE_SIZE,
        begin: int = 0,
    ) -> list[dict[str, Any]]:
        """取一页文章列表。缺凭证/凭证失效时抛 ProfileCredentialUnavailableError。"""
        # 已被标记失效的旧值也再向服务端试一次:若微信恢复接受,会自动清除失效
        # 状态,不用用户手工去改本地 JSON。
        credential = profile_credential_store.get(fakeid, allow_invalid=True)
        try:
            page = fetch_profile_ext_page(
                credential,
                offset=max(0, int(begin)),
                count=min(_PAGE_SIZE, max(1, int(count))),
            )
        except ProfileCredentialUnavailableError:
            profile_credential_store.mark_invalid(fakeid)
            raise
        profile_credential_store.mark_valid(fakeid)
        return page

    def fetch_new_articles(
        self,
        fakeid: str,
        message_exist: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """增量抓取:往前翻到撞上已有文章或超过 30 天为止。"""
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
        offset = 0

        for _ in range(_MAX_PAGES):
            page = self.fetch_article_page(fakeid, begin=offset, count=_PAGE_SIZE)
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

            if hit_boundary or len(page) < _PAGE_SIZE:
                break
            offset += _PAGE_SIZE
            time.sleep(random.uniform(_PAGE_MIN_SECONDS, _PAGE_MAX_SECONDS))

        collected.sort(key=lambda item: str(item.get("create_time") or ""))
        return collected
