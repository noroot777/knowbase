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


def exclusion_keys(article_id: str, link: str) -> tuple[str, ...]:
    """一篇文章的「永久排除」键:档案 ID、链接身份、按链接算出的稳定 ID。

    **发现那一道和写入那一道查的是同一组** —— 各写一遍的话,发现侧拦得住、写入侧
    拦不住(或者反过来),而两边只差一个键这件事在任何输出上都看不出来。
    去掉空串:`deleted_articles` 里不该有空身份,拿空串去匹配等于给自己留一个
    "谁都命中"的键。
    """
    keys = [str(article_id or ""), article_identity(link or ""),
            stable_article_id(link or "")]
    return tuple(dict.fromkeys(k for k in keys if k))


def has_readable_body(repo, article_id: str) -> bool:
    text = repo.get_body_text(article_id)
    paragraphs = text if isinstance(text, list) else [text]
    return any(str(p or "").strip() for p in paragraphs) or repo.has_body_html(article_id)


def identity_index(repo) -> dict[str, str]:
    """`article_identity(link)` → 库里保存它的那个**档案 ID**,全库一份(按连接缓存)。

    SQL 那边算不出链接身份(规范化在 Python 里),所以只能把 `(id, link)` 拉回来
    逐行算。**口径只有这一份**:沿用旧 ID(`prepare_article`)和「按 ID 查不到时
    再按链接找一次」(`existing_article_id`)问的是同一件事,各写一遍的话两边只差
    一个大小写/参数处理这件事在任何输出上都看不出来。

    缓存挂在 repo 上:一轮采集要问几千次,每次全表扫一遍太贵。**所以它只在
    「这一趟里库没被别人改过」的前提下可信** —— 拿到 ID 之后该不该用,由调用方
    自己再查一次那一行(`cmd_articles` 就是这么做的)。
    """
    if not hasattr(repo, "_article_identity_index"):
        repo._article_identity_index = {
            key: row["id"]
            for row in repo.conn.execute("SELECT id, link FROM articles")
            if (key := article_identity(row["link"]))
        }
    return repo._article_identity_index


def existing_article_id(repo, link: str) -> str:
    """库里保存着同一原文的那个档案 ID(算不出身份、或者库里没有 → 空串)。

    **「按这个 ID 查不到」不等于「库里没有这一篇」。** 同一篇文章在不同通道、
    或者换一份采集库之后,会以另一个档案 ID 保存 —— `prepare_article` 沿用旧 ID
    这件事本身就是这种情形的来源。永久移除时漏掉这一步的后果见
    `docs/decisions/failure-drop.md`:请求方给的那组键记进了名单、库里那一行却
    还在,导出下一趟照旧把它写进知识库。
    """
    identity = article_identity(link or "")
    return str(identity_index(repo).get(identity) or "") if identity else ""


def prepare_article(repo, account_name: str, article: dict) -> tuple[str, dict, bool] | None:
    """沿用已有档案 ID/归属，尊重用户删除，允许自动退休项重试。"""
    article = dict(article)
    identity = article_identity(article.get("link", ""))
    index = identity_index(repo)
    aid = str(article.get("id") or "")
    # **这只是早一道,不是互斥** —— 查过之后到真正写入之间,用户完全可能把这一篇
    # 删掉(面板上那颗「不再抓取」)。真正的不变量在写语句自己的 WHERE 里
    # (`repository.upsert_article_from_blog`),查的是同一组键。
    for key in exclusion_keys(aid, article.get("link", "")):
        if repo.is_deleted_blacklist(key):
            return None
    existing_id = index.get(identity, aid)
    row = repo.conn.execute(
        "SELECT id, account_name, is_user_deleted, is_wx_deleted FROM articles WHERE id=?",
        (existing_id,),
    ).fetchone()
    if row:
        if row["is_user_deleted"] or repo.is_deleted_blacklist(row["id"]):
            return None
        article["id"] = row["id"]
        account_name = row["account_name"]
    if identity:
        index[identity] = article["id"]
    return account_name, article, row is None


def remember_deleted_identity(repo, article_id: str) -> None:
    row = repo.conn.execute("SELECT link FROM articles WHERE id=?", (article_id,)).fetchone()
    if row and (identity := article_identity(row["link"])):
        from src.utils.helpers import time_now

        repo.conn.execute(
            "INSERT OR IGNORE INTO deleted_articles (article_id, deleted_at) VALUES (?, ?)",
            (identity, time_now()),
        )
