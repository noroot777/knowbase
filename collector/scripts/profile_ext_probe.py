#!/usr/bin/env python3
"""只读探测微信客户端 profile_ext 文章列表接口。

该脚本用于验证 ``/mp/profile_ext?action=getmsg`` 是否仍可使用，不会写数据库。
Credential 可以来自一条微信客户端历史消息请求，或来自包含 ``url`` 与
``set_cookie`` 字段的 JSON 文件。
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests


ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT_DIR / "data" / "wechatoa.db"
PROFILE_ENDPOINT = "https://mp.weixin.qq.com/mp/profile_ext"
WECHAT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 "
    "Safari/537.36 MicroMessenger/6.8.0(0x16080000) "
    "NetType/WIFI MiniProgramEnv/Mac MacWechat/WECHAT/WeChatBrowser XWEB/1191"
)
COOKIE_ATTRIBUTES = {
    "path",
    "expires",
    "max-age",
    "domain",
    "secure",
    "httponly",
    "samesite",
}


@dataclass
class ProfileCredential:
    biz: str = ""
    uin: str = ""
    key: str = ""
    pass_ticket: str = ""
    appmsg_token: str = ""
    cookie: str = ""

    def missing_required(self) -> list[str]:
        return [
            name
            for name in ("biz", "uin", "key", "pass_ticket")
            if not getattr(self, name)
        ]

    def presence(self) -> dict[str, bool]:
        return {
            "biz": bool(self.biz),
            "uin": bool(self.uin),
            "key": bool(self.key),
            "pass_ticket": bool(self.pass_ticket),
            "appmsg_token": bool(self.appmsg_token),
            "cookie": bool(self.cookie),
        }


def _first(query: dict[str, list[str]], *names: str) -> str:
    for name in names:
        values = query.get(name)
        if values and values[0]:
            return values[0]
    return ""


def _parse_url_fields(url: str) -> dict[str, str]:
    if not url:
        return {}
    query = parse_qs(urlparse(url).query, keep_blank_values=True)
    return {
        "biz": _first(query, "__biz", "biz"),
        "uin": _first(query, "uin"),
        "key": _first(query, "key"),
        "pass_ticket": _first(query, "pass_ticket"),
        "appmsg_token": _first(query, "appmsg_token"),
    }


def _parse_cookie(raw: str) -> str:
    """把 Cookie 或合并后的 Set-Cookie 文本整理成请求 Cookie。"""
    if not raw:
        return ""
    pairs: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(r"(?:^|[,;]\s*)([!#$%&'*+.^_`|~0-9A-Za-z-]+)=([^;,\r\n]*)")
    for match in pattern.finditer(raw):
        name = match.group(1).strip()
        value = match.group(2).strip()
        if not value or name.lower() in COOKIE_ATTRIBUTES or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _load_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        if not data:
            raise ValueError("Credential JSON 列表为空")
        data = data[0]
    if not isinstance(data, dict):
        raise ValueError("Credential JSON 必须是对象或非空对象列表")
    return data


def _account_biz(database: Path, account: str) -> str:
    if not account or not database.exists():
        return ""
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT fakeid FROM accounts WHERE name = ? LIMIT 1",
            (account,),
        ).fetchone()
    return str(row[0] or "") if row else ""


def load_credential(args: argparse.Namespace) -> ProfileCredential:
    data: dict[str, Any] = {}
    if args.credential_file:
        data = _load_json(args.credential_file)

    source_url = str(args.url or data.get("url") or data.get("profile_url") or "")
    url_fields = _parse_url_fields(source_url)
    biz = str(
        url_fields.get("biz")
        or data.get("__biz")
        or data.get("biz")
        or _account_biz(args.database, args.account)
        or ""
    )
    raw_cookie = str(
        data.get("cookie")
        or data.get("set_cookie")
        or os.environ.get("WECHAT_PROFILE_COOKIE")
        or ""
    )
    cookie = _parse_cookie(raw_cookie)
    cookie_map = dict(
        pair.split("=", 1) for pair in cookie.split("; ") if "=" in pair
    )

    return ProfileCredential(
        biz=biz,
        uin=str(url_fields.get("uin") or data.get("uin") or ""),
        key=str(url_fields.get("key") or data.get("key") or ""),
        pass_ticket=str(
            url_fields.get("pass_ticket")
            or data.get("pass_ticket")
            or cookie_map.get("pass_ticket")
            or ""
        ),
        appmsg_token=str(
            url_fields.get("appmsg_token")
            or data.get("appmsg_token")
            or cookie_map.get("appmsg_token")
            or ""
        ),
        cookie=cookie,
    )


def _article_from_ext(
    ext: dict[str, Any],
    comm: dict[str, Any],
    biz: str,
    item_index: int,
) -> dict[str, Any] | None:
    title = html.unescape(str(ext.get("title") or "")).strip()
    link = html.unescape(str(ext.get("content_url") or "")).strip()
    if not title and not link:
        return None
    timestamp = int(comm.get("datetime") or ext.get("datetime") or 0)
    published_at = (
        datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        if timestamp > 0
        else ""
    )
    appmsgid = str(ext.get("appmsgid") or comm.get("id") or "")
    article_id = f"{biz}:{appmsgid}:{item_index}:{timestamp}"
    return {
        "id": article_id,
        "title": title,
        "digest": html.unescape(str(ext.get("digest") or "")).strip(),
        "link": link,
        "cover": html.unescape(str(ext.get("cover") or "")).strip(),
        "author": html.unescape(str(ext.get("author") or "")).strip(),
        "published_at": published_at,
    }


def parse_articles(general_msg_list: str, biz: str) -> list[dict[str, Any]]:
    parsed = json.loads(general_msg_list)
    messages = parsed.get("list") if isinstance(parsed, dict) else parsed
    if not isinstance(messages, list):
        raise ValueError("general_msg_list 中没有文章列表")

    articles: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        comm = message.get("comm_msg_info") or {}
        ext = message.get("app_msg_ext_info") or {}
        if not isinstance(comm, dict) or not isinstance(ext, dict):
            continue
        main = _article_from_ext(ext, comm, biz, 1)
        if main:
            articles.append(main)
        multi = ext.get("multi_app_msg_item_list") or []
        if isinstance(multi, list):
            for index, child in enumerate(multi, start=2):
                if not isinstance(child, dict):
                    continue
                article = _article_from_ext(child, comm, biz, index)
                if article:
                    articles.append(article)
    return articles


def probe(
    credential: ProfileCredential,
    *,
    offset: int,
    count: int,
    timeout: float,
    use_env_proxy: bool,
) -> dict[str, Any]:
    params: dict[str, str | int] = {
        "action": "getmsg",
        "__biz": credential.biz,
        "offset": offset,
        "count": count,
        "uin": credential.uin,
        "key": credential.key,
        "pass_ticket": credential.pass_ticket,
        "f": "json",
        "is_ok": "1",
        "scene": "124",
    }
    if credential.appmsg_token:
        params["appmsg_token"] = credential.appmsg_token

    session = requests.Session()
    session.trust_env = use_env_proxy
    headers = {
        "User-Agent": WECHAT_USER_AGENT,
        "Referer": "https://mp.weixin.qq.com/",
        "Accept": "application/json, text/plain, */*",
    }
    if credential.cookie:
        headers["Cookie"] = credential.cookie

    response = session.get(
        PROFILE_ENDPOINT,
        params=params,
        headers=headers,
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"绿泡泡返回了异常 JSON 类型：{type(payload).__name__}")

    ret = int(payload.get("ret") or 0)
    result: dict[str, Any] = {
        "ok": ret == 0,
        "http_status": response.status_code,
        "ret": ret,
        "errmsg": str(payload.get("errmsg") or ""),
        "credential_fields": credential.presence(),
        "missing_required": credential.missing_required(),
        "can_continue": bool(payload.get("can_msg_continue")),
        "next_offset": payload.get("next_offset"),
        "message_count": payload.get("msg_count"),
        "articles": [],
    }
    raw_list = payload.get("general_msg_list")
    if ret == 0 and isinstance(raw_list, str) and raw_list:
        result["articles"] = parse_articles(raw_list, credential.biz)
    if ret == -3:
        result["hint"] = "绿泡泡返回 no session，需要有效的 uin/key/pass_ticket Credential"
    elif ret != 0:
        result["hint"] = "Credential 可能已过期，或该绿泡泡接口触发了风控"
    elif not result["articles"]:
        result["hint"] = "接口成功，但本页没有解析到图文文章"
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="只读验证绿泡泡客户端 profile_ext 文章列表通道",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Credential JSON 示例（文件不要提交到 Git）：\n"
            '  {"url":"https://mp.weixin.qq.com/mp/profile_ext?...", '
            '"set_cookie":"wap_sid2=...; ..."}\n\n'
            "无 Credential 时也可运行，用于确认接口返回 no session：\n"
            "  uv run python scripts/profile_ext_probe.py --account GitHubDaily"
        ),
    )
    parser.add_argument("--account", default="", help="从 SQLite 查询公众号 __biz")
    parser.add_argument("--url", default="", help="绿泡泡客户端捕获的完整 profile_ext URL")
    parser.add_argument(
        "--credential-file",
        type=Path,
        help="包含 url/set_cookie 或各 Credential 字段的 JSON 文件",
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DB)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--use-env-proxy",
        action="store_true",
        help="允许 requests 使用系统代理；默认强制直连绿泡泡",
    )
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        credential = load_credential(args)
        if not credential.biz:
            raise ValueError("缺少 __biz：请提供 --account、--url 或 Credential JSON")
        result = probe(
            credential,
            offset=max(0, args.offset),
            count=min(20, max(1, args.count)),
            timeout=args.timeout,
            use_env_proxy=args.use_env_proxy,
        )
    except (OSError, ValueError, requests.RequestException, json.JSONDecodeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(
            f"profile_ext: ret={result['ret']} errmsg={result['errmsg']!r} "
            f"articles={len(result['articles'])}"
        )
        if result.get("hint"):
            print(f"提示：{result['hint']}")
        for index, article in enumerate(result["articles"], start=1):
            print(f"{index:>2}. {article['published_at']}  {article['title']}")
            print(f"    {article['link']}")
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
