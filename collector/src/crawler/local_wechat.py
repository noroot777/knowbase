"""通过 rion-wechat-cli 只读公众号推送；不接触密钥、私聊或微信界面。"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from html import unescape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from xml.etree import ElementTree as ET

from src.utils.article_identity import stable_article_id


class LocalReaderError(RuntimeError):
    pass


def reader_command() -> str:
    explicit = os.getenv("RION_WECHAT_CLI_BIN") or os.getenv("RION_WECHAT_READER_BIN")
    if explicit:
        return explicit
    root = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex")
    candidates = [shutil.which("rion-wechat-cli"),
                  root / "skills/wechat-cli/scripts/reader.sh", root / "bin/rion-wechat-cli"]
    for candidate in candidates:
        if candidate and Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    raise LocalReaderError("未安装微信数据库读取器，请配置 wechat-cli 或 RION_WECHAT_CLI_BIN")


class LocalReader:
    def call(self, *args: str) -> dict:
        try:
            proc = subprocess.run([reader_command(), *args], capture_output=True,
                                  text=True, timeout=60)
        except subprocess.TimeoutExpired:
            raise LocalReaderError("微信数据库读取超时，请检查 wechat-cli doctor") from None
        except OSError:
            raise LocalReaderError("无法启动微信读取器，请检查安装和执行权限") from None
        try:
            payload = json.loads(proc.stdout)
        except (ValueError, TypeError):
            raise LocalReaderError("微信读取器未返回有效 JSON，请检查 wechat-cli doctor") from None
        if not isinstance(payload, dict) or proc.returncode or not payload.get("ok"):
            # 不把包含原始 XML、文件路径或访问材料的 stderr 送进管道日志。
            raise LocalReaderError("微信数据库读取失败，请运行 wechat-cli doctor 检查本机接入")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise LocalReaderError("微信读取器数据格式不兼容")
        return data

    def check(self) -> None:
        data = self.call("doctor")
        status = data.get("status", {})
        status = status.get("status", status)
        if status.get("live_database_read_ok") is not True:
            raise LocalReaderError("微信数据库尚未就绪；请用 wechat-cli access-plan 检查，通知预览不能采集文章")

    def accounts(self) -> list[dict]:
        # 足够大的 limit 避免 Reader 先截会话、再按类型过滤导致漏掉不活跃公众号。
        rows = self.call("sessions", "--type-filter", "official_account", "--limit", "100000").get("sessions")
        if not isinstance(rows, list):
            raise LocalReaderError("微信读取器缺少公众号会话列表")
        return [r for r in rows if re.fullmatch(r"gh_[A-Za-z0-9_]+", r.get("username", ""))]

    def messages(self, username: str, *, limit: int = 500, offset: int = 0) -> list[dict]:
        if not re.fullmatch(r"gh_[A-Za-z0-9_]+", username):
            raise LocalReaderError("仅允许读取公众号推送")
        rows = self.call("timeline", username, "--limit", str(limit), "--offset", str(offset),
                         "--display-order", "desc", "--include-media-paths", "false").get("messages")
        if not isinstance(rows, list):
            raise LocalReaderError("微信读取器缺少推送记录")
        return rows

    def articles(self, username: str):
        # 时间范围按文章 pub_time 筛选，不能用消息接收时间排除补同步的旧文章。
        seen = set()
        for offset in range(0, 100000, 500):
            rows = self.messages(username, offset=offset)
            for row in rows:
                for article in parse_push(row):
                    if article["id"] not in seen:
                        seen.add(article["id"])
                        yield article
            if len(rows) < 500:
                return
        raise LocalReaderError("公众号推送超过单次读取上限，未标记本次采集成功")


def parse_push(message: dict) -> list[dict]:
    """只解析公众号 mmreader 图文项；服务通知及转发卡片不作文章。"""
    raw = message.get("text") or message.get("content") or ""
    if not isinstance(raw, str) or "<mmreader" not in raw:
        return []
    if "<!DOCTYPE" in raw.upper() or "<!ENTITY" in raw.upper():
        raise LocalReaderError("公众号推送 XML 含不支持的声明")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        raise LocalReaderError("公众号图文推送格式无法解析，未跳过此错误") from None
    result = []
    for item in root.findall(".//mmreader/category/item"):
        url = unescape(item.findtext("url") or "").strip()
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        if parsed.scheme not in ("http", "https") or parsed.hostname != "mp.weixin.qq.com":
            continue
        if not all(q.get(k, [""])[0] for k in ("__biz", "mid", "idx")):
            continue
        try:
            stamp = int(item.findtext("pub_time") or 0)
            kind = int(item.findtext("itemshowtype") or 0)
            deleted = int(item.findtext("del_flag") or 0)
            published = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")
        except (ValueError, OverflowError, OSError):
            raise LocalReaderError("公众号推送的发布时间或类型无效") from None
        if stamp <= 0 or deleted or kind in (5, 8, 10):
            continue
        # 原文所需的公开文章身份保留；不导出消息里的追踪参数或客户端访问材料。
        public_q = {k: q[k][0] for k in ("__biz", "mid", "idx", "sn") if q.get(k)}
        link = "https://mp.weixin.qq.com/s?" + urlencode(public_q)
        title = unescape(item.findtext("title") or "").strip()
        if title:
            result.append({"id": stable_article_id(link), "biz": public_q["__biz"],
                           "title": title, "digest": unescape(item.findtext("digest") or ""),
                           "link": link, "cover": item.findtext("cover") or "",
                           "create_time": published, "item_show_type": kind})
    return result


def resolve_accounts(reader: LocalReader, accounts: list[dict], cached: dict,
                     progress=lambda name: None, on_error=None) -> dict[str, str]:
    """名字只用于加速找候选；最终始终用推送 URL 的 __biz 核实。"""
    sessions = reader.accounts()
    by_username = {s["username"]: s for s in sessions}
    wanted = {a["fakeid"] for a in accounts}
    names = {a.get("display_name") or a["name"] for a in accounts} | {a["name"] for a in accounts}
    ordered = sorted(sessions, key=lambda s: (
        s["username"] not in set(cached.values()), s.get("display_name") not in names))
    found = {biz: username for biz, username in cached.items()
             if biz in wanted and username in by_username}
    for session in ordered:
        if wanted <= found.keys():
            break
        username = session["username"]
        progress(session.get("display_name") or "公众号")
        try:
            for row in reader.messages(username, limit=20):
                for article in parse_push(row):
                    # 不能仅因同名认号，也不能从公众号的普通分享卡片认号。
                    if article["biz"] in wanted:
                        found[article["biz"]] = username
        except LocalReaderError as e:
            if on_error is None:
                raise
            on_error(f"匹配公众号 {session.get('display_name') or username}: {e}")
    return found
