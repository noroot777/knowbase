"""Mac 微信 Credential 存储与 ``profile_ext`` 文章列表客户端。"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests


ROOT_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_CREDENTIAL_FILE = ROOT_DIR / "data" / "profile_credentials.json"
PROFILE_ENDPOINT = "https://mp.weixin.qq.com/mp/profile_ext"

# 凭证截下来多久之内算"新鲜"。这不是微信的真实 TTL——实测同一批 30 分钟前
# 截的凭证,三个里两个还能用、一个已经 no session,微信那边没有整齐的过期点。
# 它的用途只有一个:别再把昨天的存货显示成绿点。超过这个时长只是"没把握",
# 采集照样会拿它去试(fetch_article_page 用的是 allow_invalid=True),
# 试通了会自动标回有效。嫌误报就调 WXDOWN_CRED_FRESH_MINUTES。
def _fresh_seconds() -> int:
    raw = os.getenv("WXDOWN_CRED_FRESH_MINUTES", "").strip()
    try:
        minutes = int(raw) if raw else 20
    except ValueError:
        minutes = 20
    return max(1, minutes) * 60


# 凭证的三种上报状态(还有个 "missing" 是压根没这条记录,由调用方给):
#   valid   截得新,大概率能用
#   stale   超过 _fresh_seconds() 了,没实测过,可能还行
#   expired 服务端明确回过 no session
CRED_VALID, CRED_STALE, CRED_EXPIRED = "valid", "stale", "expired"

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


class ProfileCredentialUnavailableError(RuntimeError):
    """当前公众号没有可用的 Mac 微信 Credential。"""


@dataclass
class ProfileCredential:
    biz: str
    uin: str
    key: str
    pass_ticket: str
    appmsg_token: str
    cookie: str
    captured_at: int
    name: str = ""
    avatar: str = ""
    invalidated_at: int = 0

    def is_valid(self, now: int | None = None) -> bool:
        """只采用服务端结论；不再按本地固定时长提前判过期。

        别在这里加时长判断:它是 ``get(allow_invalid=False)`` 的闸门,
        收紧了就等于"过了 20 分钟不许再采集",而实测超时的凭证多半还能用。
        年龄是另一回事,看 :meth:`is_fresh`。
        """
        return self.invalidated_at <= 0

    def age_seconds(self, now: int | None = None) -> int:
        return max(0, int(now if now is not None else time.time()) - self.captured_at)

    def is_fresh(self, now: int | None = None) -> bool:
        """截下来还没多久——只用来上报"有没有把握",不参与放行。"""
        return self.age_seconds(now) < _fresh_seconds()


def credential_state(credential: ProfileCredential, now: int | None = None) -> str:
    """给一条凭证定性:CRED_EXPIRED / CRED_STALE / CRED_VALID。"""
    if not credential.is_valid(now):
        return CRED_EXPIRED
    return CRED_VALID if credential.is_fresh(now) else CRED_STALE


def _first(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name)
    return str(values[0]) if values and values[0] else ""


def _parse_cookie(raw: str) -> str:
    """把 Cookie 或合并 Set-Cookie 整理成请求 Cookie。"""
    if not raw:
        return ""
    pairs: list[str] = []
    seen: set[str] = set()
    pattern = re.compile(
        r"(?:^|[,;]\s*)([!#$%&'*+.^_`|~0-9A-Za-z-]+)=([^;,\r\n]*)"
    )
    for match in pattern.finditer(raw):
        name = match.group(1).strip()
        value = match.group(2).strip()
        if not value or name.lower() in COOKIE_ATTRIBUTES or name in seen:
            continue
        seen.add(name)
        pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def _cookie_map(cookie: str) -> dict[str, str]:
    return dict(pair.split("=", 1) for pair in cookie.split("; ") if "=" in pair)


def _captured_at_seconds(value: Any) -> int:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return int(time.time())
    if timestamp > 10_000_000_000:
        timestamp /= 1000
    return int(timestamp)


def parse_wxdown_item(item: dict[str, Any]) -> ProfileCredential:
    """解析 wxdown-service WebSocket 推送的单条 Credential。"""
    url = str(item.get("url") or "")
    parsed = urlparse(url)
    if parsed.hostname != "mp.weixin.qq.com":
        raise ValueError("Credential URL 不是 mp.weixin.qq.com")
    query = parse_qs(parsed.query, keep_blank_values=True)
    cookie = _parse_cookie(str(item.get("set_cookie") or item.get("cookie") or ""))
    cookies = _cookie_map(cookie)
    credential = ProfileCredential(
        biz=_first(query, "__biz") or str(item.get("biz") or ""),
        uin=_first(query, "uin") or str(item.get("uin") or ""),
        key=_first(query, "key") or str(item.get("key") or ""),
        pass_ticket=(
            _first(query, "pass_ticket")
            or str(item.get("pass_ticket") or "")
            or cookies.get("pass_ticket", "")
        ),
        appmsg_token=unquote(
            _first(query, "appmsg_token")
            or str(item.get("appmsg_token") or "")
            or cookies.get("appmsg_token", "")
        ),
        cookie=cookie,
        captured_at=_captured_at_seconds(item.get("timestamp")),
        name=str(item.get("name") or item.get("nickname") or ""),
        avatar=str(item.get("avatar") or ""),
    )
    missing = [
        field
        for field in ("biz", "uin", "key", "pass_ticket", "cookie")
        if not getattr(credential, field)
    ]
    if missing:
        raise ValueError(f"Credential 缺少字段: {', '.join(missing)}")
    return credential


class ProfileCredentialStore:
    """以 0600 JSON 文件保存按公众号区分的短期 Credential。"""

    def __init__(self, path: Path = DEFAULT_CREDENTIAL_FILE) -> None:
        self.path = path
        self._lock = threading.RLock()

    def _load_unlocked(self) -> dict[str, ProfileCredential]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        items = raw.get("items", []) if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return {}
        result: dict[str, ProfileCredential] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                credential = ProfileCredential(**item)
            except (TypeError, ValueError):
                continue
            if credential.biz:
                result[credential.biz] = credential
        return result

    def _write_unlocked(self, records: dict[str, ProfileCredential]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = {
            "version": 1,
            "items": [asdict(item) for item in records.values()],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)

    def save_wxdown_items(
        self, items: list[dict[str, Any]]
    ) -> tuple[list[ProfileCredential], list[str]]:
        saved: list[ProfileCredential] = []
        rejected: list[str] = []
        with self._lock:
            records = self._load_unlocked()
            for index, item in enumerate(items, start=1):
                try:
                    credential = parse_wxdown_item(item)
                except ValueError as exc:
                    rejected.append(f"第 {index} 条: {exc}")
                    continue
                previous = records.get(credential.biz)
                # 用 >= 而不是 >:时间戳一样就是同一次抓包,重新导入它没有任何新信息,
                # 却会把记录整个换掉——包括把 invalidated_at 清零。采集时服务端返回
                # no session 会 mark_invalid,而 wxdown 的 JSON 文件里那条旧凭证还在,
                # 下一次导入(面板每 5 秒就导一次)就把它刷回"就绪",绿点是假的,
                # 一采集又失败。只有真正更新的抓包才该覆盖。
                if previous and previous.captured_at >= credential.captured_at:
                    continue
                records[credential.biz] = credential
                saved.append(credential)
            if saved:
                self._write_unlocked(records)
        return saved, rejected

    def records(self) -> dict[str, ProfileCredential]:
        with self._lock:
            return self._load_unlocked()

    def has_records(self) -> bool:
        return bool(self.records())

    def has_valid_records(self) -> bool:
        return any(item.is_valid() for item in self.records().values())

    def get(self, biz: str, *, allow_invalid: bool = False) -> ProfileCredential:
        credential = self.records().get(str(biz).strip())
        if credential is None:
            raise ProfileCredentialUnavailableError(
                "该公众号缺少 Mac 微信 Credential，请启动 wxdown-service 并在微信中打开该号文章"
            )
        if not credential.is_valid() and not allow_invalid:
            raise ProfileCredentialUnavailableError(
                "该公众号的 Mac 微信 Credential 已被服务端判定失效，请在微信中重新打开一篇文章"
            )
        return credential

    def mark_invalid(self, biz: str) -> None:
        with self._lock:
            records = self._load_unlocked()
            credential = records.get(str(biz).strip())
            if credential is None or credential.invalidated_at > 0:
                return
            credential.invalidated_at = int(time.time())
            self._write_unlocked(records)

    def mark_valid(self, biz: str) -> None:
        with self._lock:
            records = self._load_unlocked()
            credential = records.get(str(biz).strip())
            if credential is None or credential.invalidated_at <= 0:
                return
            credential.invalidated_at = 0
            self._write_unlocked(records)

    def clear(self) -> None:
        with self._lock:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass


def _format_time(timestamp: int) -> str:
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _format_article_time(timestamp: int) -> str:
    """保持与旧爬虫文章结构一致：文章发布时间精确到分钟。"""
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")


def credential_public_status(credential: ProfileCredential) -> dict[str, Any]:
    now = int(time.time())
    valid = credential.is_valid(now)
    return {
        "biz": credential.biz,
        "captured_name": credential.name,
        "captured_at": _format_time(credential.captured_at),
        "expires_at": "",
        "seconds_remaining": -1 if valid else 0,
        "valid": valid,
        # state 比 valid 细一档:valid 只说"服务端还没否掉它",state 会把
        # 放了太久、没人验证过的那批单拎出来标成 stale(见 credential_state)
        "state": credential_state(credential, now),
        "age_seconds": credential.age_seconds(now),
        "invalidated_at": _format_time(credential.invalidated_at),
    }


def _article_id(
    ext: dict[str, Any], comm: dict[str, Any], link: str, timestamp: int, index: int
) -> str:
    query = parse_qs(urlparse(link).query)
    mid = _first(query, "mid")
    link_index = _first(query, "idx")
    msgid = str(ext.get("appmsgid") or comm.get("id") or mid or "profile")
    link_identity = f"{mid}_{link_index}" if mid and link_index else mid
    aid = str(ext.get("aid") or link_identity or ext.get("itemidx") or index)
    return f"{msgid}-{aid}-{timestamp}"


def _article_from_ext(
    ext: dict[str, Any],
    comm: dict[str, Any],
    index: int,
    parent: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    parent = parent or {}
    title = html.unescape(str(ext.get("title") or "")).strip()
    link = html.unescape(str(ext.get("content_url") or "")).strip()
    if not title and not link:
        return None
    timestamp = int(comm.get("datetime") or ext.get("datetime") or 0)
    identity = dict(parent)
    identity.update({key: value for key, value in ext.items() if value is not None})
    return {
        "id": _article_id(identity, comm, link, timestamp, index),
        "title": title,
        "digest": html.unescape(str(ext.get("digest") or "")).strip(),
        "link": link,
        "cover": html.unescape(str(ext.get("cover") or "")).strip(),
        "create_time": _format_article_time(timestamp),
        "is_deleted": False,
        "item_show_type": int(ext.get("item_show_type") or 0),
    }


def parse_general_msg_list(raw: str | dict[str, Any]) -> list[dict[str, Any]]:
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    messages = parsed.get("list") if isinstance(parsed, dict) else None
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
        main = _article_from_ext(ext, comm, 1)
        if main:
            articles.append(main)
        multi = ext.get("multi_app_msg_item_list") or []
        if isinstance(multi, list):
            for index, child in enumerate(multi, start=2):
                if not isinstance(child, dict):
                    continue
                article = _article_from_ext(child, comm, index, parent=ext)
                if article:
                    articles.append(article)
    articles.sort(key=lambda item: str(item.get("create_time") or ""), reverse=True)
    return articles


def fetch_profile_ext_page(
    credential: ProfileCredential,
    *,
    offset: int = 0,
    count: int = 20,
    timeout: float = 30,
) -> list[dict[str, Any]]:
    params: dict[str, str | int] = {
        "action": "getmsg",
        "__biz": credential.biz,
        "offset": max(0, int(offset)),
        "count": max(1, min(int(count), 20)),
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
    session.trust_env = False
    response = session.get(
        PROFILE_ENDPOINT,
        params=params,
        headers={
            "User-Agent": WECHAT_USER_AGENT,
            "Referer": "https://mp.weixin.qq.com/",
            "Accept": "application/json, text/plain, */*",
            "Cookie": credential.cookie,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("微信 profile_ext 返回了异常数据")
    ret = int(payload.get("ret") or 0)
    if ret == -3:
        raise ProfileCredentialUnavailableError(
            "Mac 微信 Credential 已失效（no session），请重新打开该公众号文章"
        )
    if ret != 0:
        raise RuntimeError(
            f"profile_ext 请求失败: {payload.get('errmsg') or 'unknown'} (ret={ret})"
        )
    raw_list = payload.get("general_msg_list")
    if not raw_list:
        return []
    return parse_general_msg_list(raw_list)


profile_credential_store = ProfileCredentialStore()
