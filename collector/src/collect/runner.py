# -*- coding: utf-8 -*-
"""采集循环:一次性任务的实现,不依赖任何常驻状态。

进度不写模块级状态,而是逐条 emit 出去,由调用方决定怎么呈现(CLI 打成 NDJSON
给上游读)。没有全局状态 = 可以放心并发跑多个实例。不做 LLM 打标/摘要,那是
WeChatOA 自家阅读器的事,知识库侧另有提取阶段。

入库单篇复用 api.py 里久经验证的 _ingest_crawl_article(封面下载、正文抓取、
FTS、字数一条龙)。
"""

from __future__ import annotations

import os
import random
import time
from datetime import datetime
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
        "auth_error": False,      # 一份文章凭证都没有,整轮没必要跑
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

    返回汇总 dict;一份文章凭证都没有时通过 auth_error 标记并直接收工,
    单个号的失败只记进 errors,不影响其余的号。
    """
    import api
    from src.crawler.profile_ext import profile_credential_store
    from src.crawler.wechat_request import WechatRequest
    from src.db.repository import ArticleRepository
    from src.utils.data_manager import data_manager
    from src.utils.helpers import time_now

    ranged = bool(since and until)
    data_manager.reload("id_info")
    data_manager.reload("issues_message")

    result = _new_result()
    if not profile_credential_store.has_records():
        # 文章列表和正文都靠 wxdown 截下来的 profile_ext 凭证。一份都没有时照常
        # 开跑的话,44 个号会挨个失败,中间还各睡 2.5-4 秒——白打一遍微信接口,
        # 换来一屏一模一样的报错。
        result["auth_error"] = True
        result["errors"].append("一份文章凭证都没有,wxdown 没在跑或还没截到")
        emit({"stage": "crawl", "status": "done", **result})
        return result

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
                _refresh_display_name(repo, name)
                repo.set_account_latest_crawl(name, time_now())
                repo.commit()
            except Exception as e:
                result["errors"].append(f"{name}: {e}")
            finally:
                result["done"] += 1
            _sleep_between_accounts()

    emit({"stage": "crawl", "status": "done", **result})
    return result


def _display_recheck_seconds() -> int:
    """隔多久重新核对一次昵称。调小=改名发现得快,调大=省请求。"""
    raw = os.getenv("GZH_DISPLAY_RECHECK_DAYS", "").strip()
    try:
        days = int(raw) if raw else 7
    except ValueError:
        days = 7
    return max(1, days) * 86400


def _checked_recently(stamp: str, now: float) -> bool:
    if not stamp:
        return False
    try:
        checked = datetime.strptime(stamp, "%Y-%m-%d %H:%M").timestamp()
    except ValueError:
        return False                # 时间戳读不懂就当没核对过,重抓一次不亏
    return 0 <= now - checked < _display_recheck_seconds()


def _refresh_display_name(repo, name: str) -> None:
    """核对一次这个号在微信上的昵称,失败就算了。

    改名本来只搭在"抓新文章正文"这趟车上,可"改了名"和"不发新文章"常常
    是同一批号——而那恰恰是用户在微信里搜不到、最需要看见新名字的那批。
    所以隔一阵子补一次:从库里挑一篇已有文章,单独抓一次正文页只读
    `var nickname`(见 extract_account_nickname),不写正文、不动文章表。

    节流看的是 `display_checked_at` 而不是 display_name 空不空:后者只能
    回答"当前叫什么",补上一次之后就再也不会看第二眼,这个号以后再改名
    就永远发现不了。核对完无论名字变没变都记一笔时间,所以稳定状态下
    每个号 `GZH_DISPLAY_RECHECK_DAYS` 天才多花一次请求。
    """
    if _checked_recently(repo.display_name_checked_at(name), time.time()):
        return
    article = repo.conn.execute(
        "SELECT link FROM articles WHERE account_name=? AND is_user_deleted=0 "
        "AND link<>'' ORDER BY create_time DESC LIMIT 1", (name,)
    ).fetchone()
    if article is None:
        return
    from src.utils.wechat_body import fetch_article_body_full

    try:
        _text, _html, nickname = fetch_article_body_full(str(article["link"]))
    except Exception:
        return                      # 网络/凭证的问题不该拖累这一轮采集
    if not nickname:
        return                      # 没读到就不算核对过,下一轮再来
    repo.set_account_display_name(name, nickname)
    repo.mark_display_name_checked(name)


def _collect_one_incremental(repo, wechat, name: str, fakeid: str) -> int:
    """日常增量:拉最新几页,与库里已有的比对后只入新的。"""
    import api

    existing = repo.get_blogs_for_account(name)
    fresh = wechat.fetch_new_articles(fakeid, existing)
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
        articles = wechat.fetch_article_page(
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
