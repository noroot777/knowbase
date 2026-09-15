"""公众号文章跨通道身份；忽略推送时间及链接跟踪参数。"""

from __future__ import annotations

import hashlib
from html import unescape
from urllib.parse import parse_qs, urlencode, urlparse


def article_identity(link: str) -> str:
    parsed = urlparse(unescape(link or ""))
    if parsed.hostname != "mp.weixin.qq.com":
        return ""
    query = parse_qs(parsed.query)
    values = [query.get(k, [""])[0] for k in ("__biz", "mid", "idx")]
    if all(values):
        return "wechat:" + urlencode(dict(zip(("biz", "mid", "idx"), values)))
    if parsed.path.startswith("/s/"):
        return "wechat:" + parsed.path.rstrip("/")
    return ""


def stable_article_id(link: str) -> str:
    identity = article_identity(link)
    return "wx-" + hashlib.sha256(identity.encode()).hexdigest()[:32] if identity else ""


def has_readable_body(repo, article_id: str) -> bool:
    text = repo.get_body_text(article_id)
    paragraphs = text if isinstance(text, list) else [text]
    return any(str(p or "").strip() for p in paragraphs) or repo.has_body_html(article_id)


def prepare_article(repo, account_name: str, article: dict) -> tuple[str, dict, bool] | None:
    """沿用已有档案 ID/归属，不复活删除项。返回是否新文章。"""
    article = dict(article)
    identity = article_identity(article.get("link", ""))
    if not hasattr(repo, "_article_identity_index"):
        repo._article_identity_index = {
            key: row["id"]
            for row in repo.conn.execute("SELECT id, link FROM articles")
            if (key := article_identity(row["link"]))
        }
    aid = str(article.get("id") or "")
    for key in (aid, identity, stable_article_id(article.get("link", ""))):
        if key and repo.is_deleted_blacklist(key):
            return None
    existing_id = repo._article_identity_index.get(identity, aid)
    row = repo.conn.execute(
        "SELECT id, account_name, is_user_deleted, is_wx_deleted FROM articles WHERE id=?",
        (existing_id,),
    ).fetchone()
    if row:
        if row["is_user_deleted"] or row["is_wx_deleted"]:
            return None
        article["id"] = row["id"]
        account_name = row["account_name"]
    if identity:
        repo._article_identity_index[identity] = article["id"]
    return account_name, article, row is None


def remember_deleted_identity(repo, article_id: str) -> None:
    row = repo.conn.execute("SELECT link FROM articles WHERE id=?", (article_id,)).fetchone()
    if row and (identity := article_identity(row["link"])):
        from src.utils.helpers import time_now

        repo.conn.execute(
            "INSERT OR IGNORE INTO deleted_articles (article_id, deleted_at) VALUES (?, ?)",
            (identity, time_now()),
        )
