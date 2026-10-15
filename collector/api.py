#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采集器的公共底层:入库单篇、按链接抓正文、数字数。

这个文件原来是那个跑在 :8000 的 FastAPI 后端(53 个路由 + 一个 `app`),给
`collector/frontend` 那套 web UI 用。前端和常驻服务都已经撤掉——公众号管理
搬进了 kb-web 的面板——路由于是一个都没人调了,连同搜号、扫码凭证检测、
封面下载一起删掉,只留下采集流程真正 import 的这些东西。

文件名保持 `api`:cli.py / src/collect/{local_runner,bootstrap}.py 里的
`import api` 都还指着它,改名只是给调用点添乱。要看被删掉的那套服务翻
git 历史。
"""

from pathlib import Path

# ── 数据目录和文件路径 ──────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"


def _word_count_from_body_raw(raw: object, title: str = "") -> int:
    """同事务内统计字数，避免未提交正文在新连接中不可见。"""
    import re

    if isinstance(raw, list):
        text = "".join(str(x) for x in raw)
    else:
        text = str(raw or "")
    cleaned = re.sub(r"\s+", "", text)
    if cleaned:
        return len(cleaned)
    return len(re.sub(r"\s+", "", str(title or "")))


def _ingest_crawl_article(repo, account_name: str, article: dict,
                          *, fetch_round: str = "") -> bool:
    """入库单篇爬取文章：元数据、正文、字数。

    `fetch_round` 是知识库这一次业务抓取的批次号，只透传给仓储；它只在这一篇
    **首次**落库时才被记下来，而且跟首次元数据写在同一个 savepoint 里——要么一起
    落地，要么连文章那行一起撤回（为什么「同一个事务」还不够见
    `ArticleRepository.upsert_article_from_blog` 的说明）。下面这次 commit
    **在走网络抓正文之前**——元数据和身份一起落地，正文失败不影响它们。
    """
    from src.utils.article_identity import prepare_article

    prepared = prepare_article(repo, account_name, article)
    if prepared is None:
        return False
    account_name, article, _is_new = prepared

    article_id = str(article.get("id") or "")
    # 写不进去 = 这个身份在上面那次检查之后被永久排除了(面板上那颗「不再抓取」)。
    # **到此为止**:不抓正文、不记字数 —— 判据在写语句自己的 WHERE 里,这里只是
    # 读它的结论(见 `repository.upsert_article_from_blog`)。
    # **拒写也要 commit**,口径同下面 `_persist` 的早退:那句受限写入即使影响 0 行
    # 也已经开着一个写事务。这一处眼下还有上游兜着(`local_runner._download` 紧跟
    # 一句 commit),但收尾该待在开事务的这一层 —— 不然换个调用方就漏。
    if not repo.upsert_article_from_blog(account_name, article,
                                         fetch_round=fetch_round):
        repo.commit()
        return False
    repo.commit()
    nickname = _fetch_article_detail_text(
        article_id, str(article.get("link") or ""), repo=repo
    )
    # 公众号改名后,只有正文页带着新名字。这里顺手记下来,面板才不会一直显示
    # 一个在绿泡泡里已经搜不到的旧名字。改的是 display_name,不动主键 name。
    if nickname and repo.set_account_display_name(account_name, nickname):
        repo.commit()
    body_raw = repo.get_body_text(article_id) or ""
    wc = _word_count_from_body_raw(body_raw, str(article.get("title") or ""))
    repo.set_word_count(article_id, wc)
    repo.commit()
    return _is_new


def _fetch_article_detail_text(
    article_id: str,
    link: str,
    *,
    repo=None,
) -> str:
    """
    根据文章链接拉取正文：纯文本（LLM/检索）+ HTML（预览排版），写入 article_bodies。
    传入 repo 时复用同一连接；调用方应在网络抓取前 commit 元数据，避免长事务占锁。

    返回这篇文章所属公众号的当前昵称（读不到、或本来就没抓时为空串）——它是
    这一趟请求顺手捡的，调用方拿去更新订阅列表上的显示名。
    """
    if not link or not article_id:
        return ""
    from src.db.connection import db_write_with_retry
    from src.db.repository import ArticleRepository
    from src.utils.wechat_body import body_unavailable, fetch_article_body_full
    from src.utils.article_identity import has_readable_body
    from src.utils import body_retry

    own_repo = repo is None
    if own_repo:
        repo = ArticleRepository()
    try:
        if has_readable_body(repo, article_id):
            return ""
        if body_retry.deferred(repo, article_id, link):
            return ""
        text, html, nickname = fetch_article_body_full(link)
        # 下面这三条失败路径写的都是**挂在这篇文章上**的退避状态。它们各自的判据
        # 都在 `repository.set_fetch_state` 的 WHERE 里:这一篇在抓取期间被永久
        # 移除了就写 0 行、如实返回 False,不抛外键错误 —— 抛出来会被下载循环当成
        # 一次同步失败,把用户主动的移除记成红(见 `failure-drop.md`)。
        if isinstance(text, str):
            # 作者删除/平台违规下架/投诉侵权 → 一周复查一次;其余算临时故障退避重试
            body_retry.failed(repo, article_id, link,
                              unavailable=bool(body_unavailable(text)))
            return nickname
        paragraphs = text if isinstance(text, list) else [text]
        if not any(str(p or "").strip() for p in paragraphs) and not (html or "").strip():
            body_retry.failed(repo, article_id, link)
            return nickname

        def _persist() -> None:
            # 抓一篇正文要几秒到几十秒,用户完全可能在这中间按下「不再抓取」。
            # 排除判据在写语句自己的 WHERE 里,这里只读它的结论:一个字节都没写
            # 就别再往下记重试状态了(那张表也挂在这篇文章上)。
            # **拒写也要 commit**:那句受限写入即使影响 0 行也已经开着写事务,
            # 直接 return 会把整库写锁攥到下一篇的网络请求里去。
            if not repo.set_body_text(article_id, text, link=link):
                repo.commit()
                return
            # HTML 一并落库：Markdown 导出和预览都依赖它，抓取时不存后面就得重抓
            if html:
                repo.set_body_html(article_id, html, link=link)
            body_retry.succeeded(repo, article_id)
            repo.commit()

        db_write_with_retry(_persist)
        return nickname
    except Exception as e:
        # 走到这里说明上面那几处**一处都没来得及记**(网络抛、解析抛)。补一笔,
        # 判据同上 —— 文章已经被移除时它写 0 行,不会在异常处理里再抛一次。
        body_retry.failed(repo, article_id, link)
        print(f"[detail] 抓取正文失败 {article_id}: {e}")
        return ""
    finally:
        if own_repo:
            repo.close()
