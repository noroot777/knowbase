#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""采集器的公共底层:抓正文、下封面、搜号、查扫码凭证。

这个文件原来是那个跑在 :8000 的 FastAPI 后端(53 个路由 + 一个 `app`),给
`collector/frontend` 那套 web UI 用。前端和常驻服务都已经撤掉——公众号管理
搬进了 kb-web 的面板,凭证改成直接读 wxdown 落盘的 JSON——路由于是一个都没
人调了。留着 2840 行没有入口的代码只会让人以为它还在跑,所以删掉,只留下
CLI 和采集流程真正 import 的这些东西。

文件名保持 `api`:cli.py / src/collect/{runner,bootstrap}.py 里的 `import api`
都还指着它,改名只是给 9 处调用点添乱。要看被删掉的那套服务翻 git 历史。
"""

import json
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel

# ── 数据目录和文件路径 ──────────────────────────────────────────────────────────
DATA_DIR = Path(__file__).parent / "data"
COVERS_DIR = DATA_DIR / "covers"


class SearchCandidate(BaseModel):
    """搜索结果中单个公众号候选项。"""
    fakeid: str        # 微信内部 ID，用于后续爬取
    nickname: str      # 公众号名称
    alias: str         # 微信号（英文 ID，可能为空）
    avatar: str        # 头像图片 URL（round_head_img）
    signature: str     # 简介
    service_type: int  # 0=订阅号, 1=服务号, 2=其他


# 下载封面图时使用的 HTTP 请求头，模拟浏览器访问，避免微信服务器拒绝请求
_DOWNLOAD_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0.4240.198 Safari/537.36',
    'Referer': 'https://mp.weixin.qq.com/',
}


def download_cover(article_id: str, cover_url: str, covers_dir: Path, request_headers: dict[str, str]) -> None:
    """下载封面图到 data/covers；失败时仅告警不阻断流程。"""
    if not article_id or not cover_url:
        return
    covers_dir.mkdir(parents=True, exist_ok=True)
    filename = article_id.replace("/", "_") + ".jpg"
    dest = covers_dir / filename
    if dest.exists():
        return
    try:
        import io
        import requests
        from PIL import Image

        resp = requests.get(cover_url, timeout=15, headers=request_headers)
        resp.raise_for_status()
        img = Image.open(io.BytesIO(resp.content))
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        width, height = img.size
        if width > 640:
            new_height = int(height * 640.0 / width)
            img = img.resize((640, new_height), Image.Resampling.LANCZOS)
        img.save(dest, "JPEG", quality=85)
    except Exception as e:
        print(f"[cover] 下载失败 {article_id}: {e}")


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


def _ingest_crawl_article(repo, account_name: str, article: dict) -> None:
    """入库单篇爬取文章：元数据、封面、正文、字数、全文索引。"""
    from src.db import fts as fts_mod

    article_id = str(article.get("id") or "")
    repo.upsert_article_from_blog(account_name, article, update_fts=False)
    download_cover(
        article_id,
        str(article.get("cover") or ""),
        COVERS_DIR,
        _DOWNLOAD_HEADERS,
    )
    repo.update_cover_path(article_id)
    repo.commit()
    _fetch_article_detail_text(article_id, str(article.get("link") or ""), repo=repo)
    body_raw = repo.get_body_text(article_id) or ""
    if isinstance(body_raw, list):
        body_for_fts = "\n".join(str(x) for x in body_raw)
    else:
        body_for_fts = str(body_raw)
    fts_mod.upsert_article_fts(
        repo.conn,
        article_id,
        str(article.get("title") or ""),
        str(article.get("digest") or ""),
        body_for_fts,
    )
    wc = _word_count_from_body_raw(body_raw, str(article.get("title") or ""))
    repo.set_word_count(article_id, wc)
    repo.commit()


def _fetch_article_detail_text(
    article_id: str,
    link: str,
    *,
    repo=None,
) -> bool:
    """
    根据文章链接拉取正文：纯文本（LLM/检索）+ HTML（预览排版），写入 article_bodies。
    传入 repo 时复用同一连接；调用方应在网络抓取前 commit 元数据，避免长事务占锁。
    """
    if not link or not article_id:
        return False
    from src.db.connection import db_write_with_retry
    from src.db.repository import ArticleRepository
    from src.utils.wechat_body import fetch_article_body

    own_repo = repo is None
    if own_repo:
        repo = ArticleRepository()
    try:
        if repo.has_body(article_id):
            return False
        text, html = fetch_article_body(link)
        if not text:
            return False
        if isinstance(text, str) and text in ("已删除", "请求错误"):
            return False

        def _persist() -> None:
            repo.set_body_text(article_id, text)
            # HTML 一并落库：Markdown 导出和预览都依赖它，抓取时不存后面就得重抓
            if html:
                repo.set_body_html(article_id, html)
            repo.commit()

        db_write_with_retry(_persist)
        return True
    except Exception as e:
        print(f"[detail] 抓取正文失败 {article_id}: {e}")
        return False
    finally:
        if own_repo:
            repo.close()


def _search_biz(query: str) -> list[SearchCandidate]:
    """
    调用微信 searchbiz API 一次，返回候选列表（最多 5 条）。

    调用方负责先校验凭证。空关键词抛 400，网络失败抛 503，
    微信业务错误抛 502 —— 由调用方决定如何处理。
    """
    query = query.strip()
    if not query:
        raise HTTPException(status_code=400, detail="搜索关键词不能为空")

    try:
        import requests as req
        from src.utils.data_manager import data_manager, headers as wechat_headers

        # 复制全局 headers 并注入当前 Cookie
        h = dict(wechat_headers)
        h['Cookie'] = data_manager.id_info['cookie']
        params = {
            'action': 'search_biz',
            'begin': 0,
            'count': 5,
            'query': query,
            'token': data_manager.id_info['token'],
            'lang': 'zh_CN',
            'f': 'json',
            'ajax': 1,
        }
        response = req.get(
            'https://mp.weixin.qq.com/cgi-bin/searchbiz?',
            params=params,
            headers=h,
            timeout=15,
        ).json()
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"微信接口请求失败：{e}")

    # 微信接口正常时 ret=0，非 0 表示 token/cookie 问题
    if response.get('base_resp', {}).get('ret') != 0:
        raise HTTPException(
            status_code=502,
            detail=f"微信接口返回错误：{response.get('base_resp', {}).get('err_msg', '未知')}，请检查 token/cookie 是否过期",
        )

    # 将微信返回的字段映射到前端友好的结构
    candidates: list[SearchCandidate] = []
    for item in response.get('list', []):
        candidates.append(SearchCandidate(
            fakeid=item.get('fakeid', ''),
            nickname=item.get('nickname', ''),
            alias=item.get('alias', ''),
            avatar=item.get('round_head_img', ''),
            signature=item.get('signature', ''),
            service_type=item.get('service_type', 0),
        ))
    return candidates


# ── 凭证状态 ────────────────────────────────────────────────────────────────────
# 进程内存里的最近一次凭证检测结果。原来由常驻服务的爬取任务和 /api/auth/check
# 写入；现在采集是一次性 CLI，一个进程只跑一趟，所以它实际上一直是下面这组初值,
# 真正的判断落在 _check_auth_valid()（现问现答）。保留是因为 AuthStatus 的三个
# 字段还从这里取，前端横幅认那个结构。
_auth_state: dict = {
    "valid": True,    # 凭证是否有效（True 也可能是"尚未检测"的默认值）
    "checked_at": "", # 上次检测时间（空字符串表示本次启动后尚未检测）
    "error": "",      # 失败原因，如 "invalid csrf token"
}

# 微信 API 返回这些 err_msg 时说明 token/cookie 已失效
_AUTH_FAIL_MSGS = {"invalid session", "invalid csrf token", "csrf token invalid"}


def _check_auth_valid() -> tuple[bool, str]:
    """
    向微信 API 发送一次轻量探测请求，检测 token/cookie 是否有效。
    直接用 requests 发请求，只检测不登录。

    返回：(is_valid, reason)
      - is_valid=True, reason=""        → 凭证正常
      - is_valid=False, reason="..."    → 凭证失效，reason 说明原因
      - is_valid=True, reason="警告..."  → 网络异常，不确定，让主流程自行处理
    """
    try:
        import requests as req
        from src.utils.data_manager import data_manager

        id_info = data_manager.id_info
        token = id_info.get("token", "").strip()
        cookie = id_info.get("cookie", "").strip()
        if not token or not cookie:
            return False, "id_info.json 中 token 或 cookie 为空"

        h = dict(_DOWNLOAD_HEADERS)
        h["Cookie"] = cookie
        # 用 search_biz 接口做探测（参数随意，只看返回的 err_msg）
        params = {
            "action": "search_biz",
            "begin": 0,
            "count": 1,
            "query": "test",
            "token": token,
            "lang": "zh_CN",
            "f": "json",
            "ajax": 1,
        }
        resp = req.get(
            "https://mp.weixin.qq.com/cgi-bin/searchbiz?",
            params=params,
            headers=h,
            timeout=10,
        ).json()
        err_msg = resp.get("base_resp", {}).get("err_msg", "ok")
        if err_msg in _AUTH_FAIL_MSGS:
            return False, f"凭证已失效（{err_msg}）"
        return True, ""
    except Exception as e:
        # 网络超时等异常不等于凭证失效，返回 True 让主流程自行尝试
        return True, f"检测时出现异常（将尝试继续爬取）: {e}"


# ── 抓取节流参数（src/collect/runner.py 按这组值控制翻页与换号的节奏）──────────
# 每页拉的文章数（微信最多支持 40，取 20 更接近正常用户浏览节奏）
_RANGE_PAGE_SIZE = 20
# 单个账号最多翻多少页（20 * 15 = 300 篇兜底，防止死循环）
_RANGE_MAX_PAGES_PER_ACCOUNT = 15
# 页与页之间随机等待 [_RANGE_PAGE_MIN, _RANGE_PAGE_MAX] 秒
_RANGE_PAGE_MIN, _RANGE_PAGE_MAX = 2.5, 4.0
# 账号与账号之间随机等待 [_RANGE_ACCT_MIN, _RANGE_ACCT_MAX] 秒
# (范围抓取与全量增量抓取共用这组节流参数)
_RANGE_ACCT_MIN, _RANGE_ACCT_MAX = 4.0, 7.0


class AuthStatus(BaseModel):
    """凭证状态的完整信息，供前端状态横幅展示。"""
    has_credentials: bool  # id_info.json 中是否有非空的 token 和 cookie
    valid: bool            # 上次检测/爬取是否成功（False 表示凭证失效）
    checked_at: str        # 上次检测时间（空 = 本次启动后尚未检测）
    error: str             # 失败原因（空 = 正常）
    token_hint: str        # token 前 8 位，供用户确认是否是最新的值
    id_info_mtime: str     # id_info.json 文件的最后修改时间
    credential_saved_at: str = ""
    credential_expires_at: str = ""
    credential_days_remaining: float | None = None
    credential_expired: bool = False
    credential_expires_soon: bool = False
    credential_ttl_days: int = 4
    credential_expiry_label: str = ""


def _build_auth_status() -> AuthStatus:
    """
    组合内存中的 _auth_state 和磁盘上的 id_info.json 元数据，
    构建完整的 AuthStatus 对象。
    """
    from src.utils.credential_expiry import (
        credential_expiry_from_id_info,
        format_days_remaining_label,
    )

    id_info_file = DATA_DIR / "id_info.json"
    has_credentials = False
    token_hint = ""
    mtime = ""
    info: dict = {}
    file_mtime_ts: float | None = None

    if id_info_file.exists():
        try:
            file_mtime_ts = id_info_file.stat().st_mtime
            with open(id_info_file, encoding="utf-8") as f:
                info = json.load(f)
            token = info.get("token", "").strip()
            cookie = info.get("cookie", "").strip()
            has_credentials = bool(token and cookie)
            # 只展示 token 前 8 位，防止完整 token 被截图泄露
            token_hint = token[:8] + "..." if len(token) > 8 else token
            mtime = datetime.fromtimestamp(file_mtime_ts).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

    expiry = credential_expiry_from_id_info(info, file_mtime=file_mtime_ts)
    expiry_label = format_days_remaining_label(
        expiry.get("days_remaining")  # type: ignore[arg-type]
    )

    return AuthStatus(
        has_credentials=has_credentials,
        valid=_auth_state["valid"],
        checked_at=_auth_state["checked_at"],
        error=_auth_state["error"],
        token_hint=token_hint,
        id_info_mtime=mtime,
        credential_saved_at=str(expiry.get("saved_at") or ""),
        credential_expires_at=str(expiry.get("expires_at") or ""),
        credential_days_remaining=expiry.get("days_remaining"),  # type: ignore[arg-type]
        credential_expired=bool(expiry.get("expired")),
        credential_expires_soon=bool(expiry.get("expires_soon")),
        credential_ttl_days=int(expiry.get("ttl_days") or 4),
        credential_expiry_label=expiry_label,
    )
