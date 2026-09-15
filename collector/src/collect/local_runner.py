"""本机微信推送 → 共用文章库。身份映射缓存只含公众号 ID，不存原始消息。"""

from __future__ import annotations

import json
import os
import tempfile

from src.crawler.local_wechat import LocalReader, LocalReaderError, resolve_accounts


def local_status() -> dict:
    from src.db.repository import ArticleRepository

    with ArticleRepository() as repo:
        targets = sorted(repo.get_name2fakeid())
    try:
        LocalReader().check()
    except LocalReaderError as e:
        return {"ready": False, "message": "微信数据库不可读", "hint": str(e), "targets": targets}
    return {"ready": True, "message": "微信数据库可读",
            "hint": "只采集本机已同步的公众号推送；完整正文仍通过原文链接获取", "targets": targets}


def _cache_path():
    from src.db.connection import DATA_DIR

    return DATA_DIR / "local_wechat_accounts.json"


def _load_mapping() -> dict:
    try:
        data = json.loads(_cache_path().read_text())
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_mapping(mapping: dict) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".local-wechat-")
    try:
        with os.fdopen(fd, "w") as out:
            json.dump(mapping, out, ensure_ascii=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def collect(emit, *, targets=None, since="", until="", reader=None) -> dict:
    import api
    from src.db.repository import ArticleRepository
    from src.utils.article_identity import has_readable_body, prepare_article
    from src.utils.helpers import time_now

    result = {"new_articles": 0, "errors": [], "auth_error": False, "done": 0, "total": 0}
    reader = reader or LocalReader()
    try:
        reader.check()
        with ArticleRepository() as repo:
            accounts = repo.list_accounts()
            known = {a["name"] for a in accounts}
            for name in set(targets or []) - known:
                result["errors"].append(f"{name}: 未启用或不在订阅列表中")
            if targets:
                accounts = [a for a in accounts if a["name"] in targets]
            result["total"] = len(accounts)

            cached = _load_mapping()
            # 静默匹配身份；进度只显示本次实际采集的目标，读取错误仍照常报告。
            mapping = resolve_accounts(reader, accounts, cached, on_error=result["errors"].append)
            _save_mapping({**cached, **mapping})
            for account in accounts:
                name, biz = account["name"], account["fakeid"]
                emit({"stage": "crawl", "status": "running", **result, "current": name})
                try:
                    username = mapping.get(biz)
                    if not username:
                        raise LocalReaderError("未找到该公众号的本机图文推送；请订阅并同步到 Mac 微信")
                    matched = False
                    for article in reader.articles(username):
                        if article["biz"] != biz:
                            continue
                        matched = True
                        if since and not since <= article["create_time"][:10] <= until:
                            continue
                        prepared = prepare_article(repo, name, article)
                        if prepared is None:
                            continue
                        owner, normalized, is_new = prepared
                        if not is_new and has_readable_body(repo, normalized["id"]):
                            continue
                        api._ingest_crawl_article(repo, owner, normalized)
                        result["new_articles"] += int(is_new)
                        if not has_readable_body(repo, normalized["id"]):
                            result["errors"].append(f"{name} · {article['title']}: 正文获取失败，重试将补抓")
                    if not matched:
                        raise LocalReaderError("本机尚无可识别的该公众号图文推送")
                    repo.set_account_latest_crawl(name, time_now())
                    repo.commit()
                except Exception as e:
                    result["errors"].append(f"{name}: {e}")
                finally:
                    result["done"] += 1
    except LocalReaderError as e:
        result["errors"].append(str(e))
    emit({"stage": "crawl", "status": "done", **result})
    return result
