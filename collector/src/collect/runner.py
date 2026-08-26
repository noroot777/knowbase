# -*- coding: utf-8 -*-
"""采集循环:一次性任务的实现,不依赖任何常驻状态。

与 api.py 里那两个后台线程版本(_run_crawl_sqlite / _run_ranged_crawl)的区别:

- 进度不写模块级 _crawl_state,而是逐条 emit 出去,由调用方决定怎么呈现
  (CLI 打成 NDJSON 给上游读)。没有全局状态 = 可以放心并发跑多个实例。
- 不做 LLM 打标/摘要。那是给 WeChatOA 自家阅读器用的,知识库侧另有提取阶段。
- 不写 operation_logs.jsonl。日志归调用方。

入库单篇仍复用 api.py 里久经验证的 _ingest_crawl_article(封面下载、正文抓取、
FTS、字数一条龙),那部分逻辑与是不是常驻服务无关。
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable

Emit = Callable[[dict[str, Any]], None]


def _sleep_between_accounts() -> None:
    """号间随机等待。固定 1.2s 曾在 2026-07-30 触发账号级 freq control。"""
    import api

    time.sleep(random.uniform(api._RANGE_ACCT_MIN, api._RANGE_ACCT_MAX))


def _new_result() -> dict[str, Any]:
    return {
        "new_articles": 0,
        "errors": [],
        "auth_error": False,      # 凭证失效,整轮中止
        "freq_control": False,    # 微信频率限制,整轮中止
        "done": 0,
        "total": 0,
    }


def _resolve_targets(repo, targets: list[str] | None) -> tuple[list[tuple[str, str]], list[str]]:
    """把目标名解析成 [(name, fakeid)],并返回不在订阅列表里的名字。"""
    n2f = repo.get_name2fakeid()
    if not targets:
        return list(n2f.items()), []
    pairs = [(n, n2f[n]) for n in targets if n in n2f]
    missing = [n for n in targets if n not in n2f]
    return pairs, missing


def collect(
    emit: Emit,
    *,
    targets: list[str] | None = None,
    since: str = "",
    until: str = "",
) -> dict[str, Any]:
    """采集入口。给了 since+until 走区间补抓,否则走日常增量。

    返回汇总 dict;致命中断(凭证/限流)通过 auth_error / freq_control 标记,
    不抛异常——已经抓到的部分仍然有效,调用方该让它进入后续阶段。
    """
    import api
    from src.crawler.wechat_request import (
        CredentialExpiredError,
        FreqControlError,
        WechatRequest,
    )
    from src.db.repository import ArticleRepository
    from src.utils.data_manager import data_manager
    from src.utils.helpers import time_now

    ranged = bool(since and until)
    data_manager.reload("id_info")
    data_manager.reload("issues_message")

    result = _new_result()
    with ArticleRepository() as repo:
        pairs, missing = _resolve_targets(repo, targets)
        for name in missing:
            result["errors"].append(f"{name}: 不在订阅列表中,已跳过")
        result["total"] = len(pairs)
        emit({"stage": "crawl", "status": "running", "done": 0,
              "total": len(pairs), "current": "",
              "mode": "range" if ranged else "incremental"})

        wechat = WechatRequest()
        for name, fakeid in pairs:
            emit({"stage": "crawl", "status": "running", "done": result["done"],
                  "total": len(pairs), "current": name})
            try:
                repo.upsert_account(name, fakeid)
                if ranged:
                    got = _collect_one_ranged(repo, wechat, name, fakeid, since, until)
                else:
                    got = _collect_one_incremental(repo, wechat, name, fakeid)
                result["new_articles"] += got
                repo.set_account_latest_crawl(name, time_now())
                repo.commit()
            except CredentialExpiredError as e:
                result["auth_error"] = True
                result["errors"].append(str(e))
                break
            except FreqControlError as e:
                # 账号级限流:继续打剩下的号只会延长封禁窗口,立即收工
                result["freq_control"] = True
                result["errors"].append(str(e))
                break
            except Exception as e:
                result["errors"].append(f"{name}: {e}")
            finally:
                result["done"] += 1
            _sleep_between_accounts()

    emit({"stage": "crawl", "status": "done", **result})
    return result


def _collect_one_incremental(repo, wechat, name: str, fakeid: str) -> int:
    """日常增量:拉最新几页,与库里已有的比对后只入新的。"""
    import api

    existing = repo.get_blogs_for_account(name)
    fresh = wechat.fakeid2message_update(fakeid, existing)
    for article in fresh:
        api._ingest_crawl_article(repo, name, article)
    return len(fresh)


def _collect_one_ranged(repo, wechat, name: str, fakeid: str,
                        since: str, until: str) -> int:
    """区间补抓:分页往回翻,把 [since, until] 内的文章入库。

    停止条件是"本页最老一篇已早于 since"——再翻只会更早。
    """
    import api

    got = 0
    for page in range(api._RANGE_MAX_PAGES_PER_ACCOUNT):
        articles = wechat.fetch_appmsg_preview_list(
            fakeid, begin=page * api._RANGE_PAGE_SIZE, count=api._RANGE_PAGE_SIZE)
        if not articles:
            break

        # item_show_type 5/8/10 不是普通图文;is_deleted 的正文可能已 404
        candidates = [
            a for a in articles
            if not a.get("is_deleted")
            and int(a.get("item_show_type") or 0) not in (5, 8, 10)
        ]
        # create_time 是 "YYYY-MM-DD HH:MM",since/until 只有日期。整串比大小会把
        # 结束日当天全漏掉——"2026-08-26 12:00" 字符串上大于 "2026-08-26"。面板上
        # 「今天」「近 7 天」「自定义到今天」抓回来的都会静默少一天,只比日期部分。
        for article in candidates:
            if since <= str(article.get("create_time") or "")[:10] <= until:
                api._ingest_crawl_article(repo, name, article)
                got += 1

        oldest = min(str(a.get("create_time") or "")[:10] for a in articles)
        if oldest and oldest < since:
            break
        if page < api._RANGE_MAX_PAGES_PER_ACCOUNT - 1:
            time.sleep(random.uniform(api._RANGE_PAGE_MIN, api._RANGE_PAGE_MAX))
    return got
