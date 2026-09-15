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


def _cache_path(kind="accounts"):
    from src.db.connection import DATA_DIR

    return DATA_DIR / f"local_wechat_{kind}.json"


def _load_mapping() -> dict:
    try:
        data = json.loads(_cache_path().read_text())
        return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)}
    except (OSError, ValueError, AttributeError):
        return {}


def _load_discovery() -> dict:
    try:
        data = json.loads(_cache_path("discovery").read_text())
        return {k: v for k, v in data.items() if isinstance(v, dict)
                and isinstance(v.get("checked_at"), (int, float))
                and (v.get("biz") is None or isinstance(v["biz"], str))}
    except (OSError, ValueError, AttributeError):
        return {}


def _save_mapping(mapping: dict, kind="accounts") -> None:
    path = _cache_path(kind)
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
    from src.utils import body_retry
    from src.utils.request_pacing import RequestPacer
    from src.utils.helpers import time_now

    result = {"new_articles": 0, "errors": [], "auth_error": False, "done": 0, "total": 0,
              "missing_accounts": [], "deferred_articles": 0, "unavailable_articles": 0}
    reader = reader or LocalReader()
    pacer = RequestPacer()
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
            discovery = _load_discovery()
            # 静默匹配身份；进度只显示本次实际采集的目标，读取错误仍照常报告。
            mapping = resolve_accounts(reader, accounts, cached, on_error=result["errors"].append,
                                       discovery=discovery)
            _save_mapping({**cached, **mapping})
            _save_mapping(discovery, "discovery")
            for account in accounts:
                name, biz = account["name"], account["fakeid"]
                emit({"stage": "crawl", "status": "running", **result, "current": name})
                try:
                    username = mapping.get(biz)
                    if not username:
                        result["missing_accounts"].append(name)
                        continue
                    matched = False
                    for article in reader.articles(username, on_error=lambda error:
                                                   result["errors"].append(f"{name}: {error}")):
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
                        if body_retry.deferred(repo, normalized["id"], normalized["link"]):
                            result["deferred_articles"] += 1
                            continue
                        pacer.wait()
                        api._ingest_crawl_article(repo, owner, normalized)
                        result["new_articles"] += int(is_new)
                        if not has_readable_body(repo, normalized["id"]):
                            state = body_retry.state(repo, normalized["id"])
                            if state and state["reason"] == "unavailable":
                                result["unavailable_articles"] += 1
                            else:
                                result["errors"].append(f"{name} · {article['title']}: 正文获取失败，稍后自动重试")
                    if not matched:
                        result["missing_accounts"].append(name)
                        continue
                    repo.set_account_latest_crawl(name, time_now())
                    repo.commit()
                except Exception as e:
                    result["errors"].append(f"{name}: {e}")
                finally:
                    result["done"] += 1
    except LocalReaderError as e:
        result["errors"].append(str(e))
    result["errors"] = list(dict.fromkeys(result["errors"]))
    emit({"stage": "crawl", "status": "done", **result})
    return result
