# -*- coding: utf-8 -*-
"""公众号文章链接的规范化与去重候选。

只剩链接这一件事:同一篇文章在推送、分享卡片、浏览器地址栏里带的查询参数
各不相同(scene/clicktime/pass_ticket 这类是会话痕迹,不属于文章身份),
入库去重和按链接回查都得先把它们抹平。

原来这里还有「按链接手工导入单篇」的整条实现(抓页面、解析元数据、图片
消息分支),那是旧 Vue 后端 `/api/import` 的后端;前端和路由删掉之后它
再没有调用者,连带把 wechat_picture / wechat_preview 两个模块吊在依赖图上。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

_MP_HOST = "mp.weixin.qq.com"


class ArticleImportError(ValueError):
    """链接不是一条可用的公众号文章链接(文案可直接展示给用户)。"""

_SKIP_QUERY_PREFIXES = ("utm_", "from_", "scene", "clicktime", "ascene")
_SKIP_QUERY_KEYS = frozenset(
    {
        "scene",
        "clicktime",
        "ascene",
        "devicetype",
        "version",
        "lang",
        "pass_ticket",
        "wx_header",
    }
)


def normalize_mp_article_url(url: str) -> str:
    """校验并补全协议/域名，用于发起请求。"""
    raw = (url or "").strip()
    if not raw:
        raise ArticleImportError("链接不能为空")
    if not re.match(r"^https?://", raw, re.I):
        raw = f"https://{raw}"
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    if host != _MP_HOST:
        raise ArticleImportError("请输入有效的绿泡泡公众号文章链接（mp.weixin.qq.com）")
    path = parsed.path or ""
    if "/s/" not in path and "appmsg" not in path:
        raise ArticleImportError("该链接不是公众号文章页")
    return raw.split("#")[0]


def extract_mp_article_slug(url: str) -> str:
    """提取 /s/{slug} 中的 slug，用于模糊去重。"""
    try:
        parsed = urlparse(normalize_mp_article_url(url))
    except ArticleImportError:
        return ""
    m = re.match(r"^/s/([A-Za-z0-9_-]+)/?$", parsed.path or "")
    return m.group(1) if m else ""


def canonical_article_link(url: str) -> str:
    """
    归一化文章链接，便于入库与去重（https、去 fragment、短链去 query 等）。
    """
    raw = normalize_mp_article_url(url)
    parsed = urlparse(raw)
    path = parsed.path or ""

    m = re.match(r"^/s/([A-Za-z0-9_-]+)/?$", path)
    if m:
        return f"https://{_MP_HOST}/s/{m.group(1)}"

    qs = parse_qs(parsed.query, keep_blank_values=False)
    kept: list[tuple[str, str]] = []
    for key in sorted(qs.keys()):
        kl = key.lower()
        if kl in _SKIP_QUERY_KEYS or any(kl.startswith(p) for p in _SKIP_QUERY_PREFIXES):
            continue
        vals = qs[key]
        if vals and str(vals[0]).strip():
            kept.append((key, str(vals[0]).strip()))

    query = urlencode(kept, doseq=False) if kept else ""
    return urlunparse(("https", _MP_HOST, path, "", query, ""))


def link_lookup_variants(url: str) -> list[str]:
    """生成用于数据库匹配的链接候选（顺序：原文 → 归一化）。"""
    out: list[str] = []
    seen: set[str] = set()

    def add(u: str) -> None:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)

    raw = (url or "").strip()
    if raw:
        add(raw.split("#")[0])
    try:
        add(normalize_mp_article_url(url))
    except ArticleImportError:
        pass
    try:
        add(canonical_article_link(url))
    except ArticleImportError:
        pass
    return out
