"""本机绿泡泡推送 → 增量文章索引 → 按范围下载正文。"""

from __future__ import annotations

from src.crawler.local_wechat import LocalReader, LocalReaderError


def local_status() -> dict:
    from src.db.repository import ArticleRepository

    with ArticleRepository() as repo:
        targets = sorted(repo.get_name2fakeid())
    try:
        LocalReader().check()
    except LocalReaderError as e:
        return {"ready": False, "message": "绿泡泡数据库不可读", "hint": str(e), "targets": targets}
    return {"ready": True, "message": "绿泡泡数据库可读",
            "hint": "只采集本机已同步的公众号推送；完整正文仍通过原文链接获取", "targets": targets}


def collect(emit, *, targets=None, since="", until="", reader=None,
            round_key="") -> dict:
    from src.collect.local_accounts import refresh_display_names
    from src.collect.local_index import ArticleIndex
    from src.db.repository import ArticleRepository
    from src.utils.helpers import time_now

    result = {"new_articles": 0, "errors": [], "done": 0, "total": 0,
              "missing_accounts": [], "deferred_articles": 0, "unavailable_articles": 0,
              "matched_articles": 0, "download_done": 0, "download_total": 0,
              "download_succeeded": 0, "download_failed": 0, "existing_articles": 0,
              "excluded_articles": 0,
              "body_failures": [], "article_failures": [],
              "unreadable_messages": 0, "unreadable_accounts": [],
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
                # 改名核对:会话列表上的名字就是绿泡泡此刻显示的那个,零请求。
                # 放在发现之后,这样新订阅的号在同一轮里就能解出归属、对上名字。
                try:
                    refresh_display_names(repo, reader=reader)
                except LocalReaderError as e:
                    result["errors"].append(f"核对公众号名字: {e}")
                covered = index.covered_accounts(accounts, active)
                result.update(index.discovery_issues(accounts, active))
                # 已有覆盖的账号统一记录最近完成发现检查的时间；坏推送单独诊断。
                incomplete = index.failed_accounts(accounts)
                for name in covered - incomplete:
                    repo.set_account_latest_crawl(name, time_now())
                repo.commit()
                articles = index.query(accounts, active, since, until)
                result["matched_articles"] = len(articles)
                _download(repo, articles, result, progress, round_key)
                repo.commit()
                result["done"] = len(accounts)
    except LocalReaderError as exc:
        result["errors"].append(str(exc))
    result["errors"] = list(dict.fromkeys(result["errors"]))
    emit({"stage": "crawl", "status": "done", **result})
    return result


def _download(repo, articles, result, progress, round_key=""):
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
        item = {"name": current, "id": article["id"], "account": owner,
                "title": article["title"], "link": article["link"]}
        progress({"phase": "download", "current": current})
        succeeded = excluded = False
        try:
            pacer.wait()
            # 「这一篇是知识库哪一次业务抓取发现的」只能由仓储在**首次插入这一篇
            # 的那个事务里**一起写掉,这里只负责把批次号递下去。队列里既有首次发现
            # 的、也有前几天抓到、这次只是来补正文的(`has_readable_body` 那道筛放
            # 进来的就是后者),而补正文不构成新的首次来源 —— 这一层分不出来谁是谁,
            # 也不该分:仓储那边 `rowcount` 说了算。
            api._ingest_crawl_article(repo, owner, article, fetch_round=round_key)
            repo.commit()
            # **按实际写入结果报,不按计划长度报。** 队列是发现阶段排好的,而发现
            # 到这一刻之间用户完全可能在面板上按下「不再抓取」—— 写入层会拒掉这
            # 一篇(`repository._excluded_clause`)。那时既不该记成新增(它没进库),
            # 也不该记成失败、更不该说一句「已安排自动重试」:它不会再被抓了。
            if not repo.has_article(article["id"]):
                excluded = True
                result["excluded_articles"] += 1
                continue
            result["new_articles"] += int(is_new)
            succeeded = has_readable_body(repo, article["id"])
            if not succeeded:
                state = body_retry.state(repo, article["id"])
                if state and state["reason"] == "unavailable":
                    result["unavailable_articles"] += 1
                else:
                    result["body_failures"].append({**item,
                        "reason": "正文暂未获取，已安排自动重试"})
        except Exception as exc:
            message = f"{current}: {exc}"
            result["errors"].append(message)
            result["article_failures"].append({**item, "error": message,
                                              "reason": f"文章发现或入库失败：{exc}"})
        finally:
            # **一篇一个事务单元,出口一定要把它收掉。** 那几句受限写入即使影响 0 行
            # 也已经开着一个 SQLite 写事务(实测 `rowcount=0` 时 `in_transaction`
            # 仍为 True、另一个连接当场 `database is locked`),而下一圈开头就是
            # `pacer.wait()` 的退避和一次网络请求 —— 不收尾就等于攥着整库写锁去等
            # 网络,用户那一刻按「不再抓取」会被堵到超时(见 `failure-drop.md`)。
            # 异常路径尤其要走到这里:它压根没有自己的 commit。
            if repo.conn.in_transaction:
                repo.conn.rollback()
            result["download_done"] += 1
            if not excluded:
                result["download_succeeded" if succeeded else "download_failed"] += 1
            progress({"phase": "download", "current": current})
