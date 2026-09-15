"""本机微信推送 → 增量文章索引 → 按范围下载正文。"""

from __future__ import annotations

from src.crawler.local_wechat import LocalReader, LocalReaderError


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


def collect(emit, *, targets=None, since="", until="", reader=None) -> dict:
    from src.collect.local_index import ArticleIndex
    from src.db.repository import ArticleRepository
    from src.utils.helpers import time_now

    result = {"new_articles": 0, "errors": [], "auth_error": False, "done": 0, "total": 0,
              "missing_accounts": [], "deferred_articles": 0, "unavailable_articles": 0,
              "matched_articles": 0, "download_done": 0, "download_total": 0,
              "download_succeeded": 0, "download_failed": 0, "existing_articles": 0,
              "body_failures": [], "unreadable_messages": 0, "unreadable_accounts": [],
              "unattributed_messages": 0, "coverage_unknown_accounts": []}
    reader = reader or LocalReader()

    def progress(event):
        emit({"stage": "crawl", "status": "running", **result, **event})

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
            if accounts:
                index = ArticleIndex(repo)
                active = index.refresh(reader, progress, result["errors"].append)
                covered = index.covered_accounts(accounts, active)
                result.update(index.discovery_issues(accounts, active))
                unreadable = {a["name"] for a in result["unreadable_accounts"]}
                # 最近采集记录文章发现；正文独立重试，不能成为所有账号的全局闸门。
                incomplete = index.covered_accounts(accounts, list(index.failed_sources))
                for name in covered - incomplete - unreadable:
                    repo.set_account_latest_crawl(name, time_now())
                repo.commit()
                articles = index.query(accounts, active, since, until)
                result["matched_articles"] = len(articles)
                _download(repo, articles, result, progress)
                repo.commit()
                result["done"] = len(accounts)
    except LocalReaderError as exc:
        result["errors"].append(str(exc))
    result["errors"] = list(dict.fromkeys(result["errors"]))
    emit({"stage": "crawl", "status": "done", **result})
    return result


def _download(repo, articles, result, progress):
    import api
    from src.utils.article_identity import has_readable_body, prepare_article
    from src.utils import body_retry
    from src.utils.request_pacing import RequestPacer

    queue = []
    for name, article in articles:
        prepared = prepare_article(repo, name, article)
        if prepared is None:
            continue
        owner, normalized, is_new = prepared
        if not is_new and has_readable_body(repo, normalized["id"]):
            result["existing_articles"] += 1
            continue
        if body_retry.deferred(repo, normalized["id"], normalized["link"]):
            result["deferred_articles"] += 1
            continue
        queue.append((owner, normalized, is_new))
    result["download_total"] = len(queue)
    progress({"phase": "download", "current": ""})
    pacer = RequestPacer()
    for owner, article, is_new in queue:
        current = f"{owner} · {article['title']}"
        progress({"phase": "download", "current": current})
        succeeded = False
        try:
            pacer.wait()
            api._ingest_crawl_article(repo, owner, article)
            repo.commit()
            result["new_articles"] += int(is_new)
            succeeded = has_readable_body(repo, article["id"])
            if not succeeded:
                state = body_retry.state(repo, article["id"])
                if state and state["reason"] == "unavailable":
                    result["unavailable_articles"] += 1
                else:
                    result["body_failures"].append({"title": current,
                        "reason": "正文暂未获取，已安排自动重试"})
        except Exception as exc:
            result["errors"].append(f"{current}: {exc}")
        finally:
            result["download_done"] += 1
            result["download_succeeded" if succeeded else "download_failed"] += 1
            progress({"phase": "download", "current": current})
