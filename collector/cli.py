#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""公众号采集器命令行入口 —— 一次性任务,跑完就退,不常驻。

    python -m cli status                           健康状况(JSON)
    python -m cli collect                          日常增量采集
    python -m cli collect --since 2026-08-01 --until 2026-08-26 [--targets a,b]
    python -m cli export [--no-images]             导出到 obsidian
    python -m cli accounts list                    看订阅的公众号
    python -m cli accounts discover [关键词]       从本机已关注的号里找候选
    python -m cli accounts add <名字> [--fakeid X] 加入订阅
    python -m cli accounts remove <名字>
    python -m cli accounts enable|disable <名字>   是否参与采集

文章从本机绿泡泡数据库里已同步的推送发现,正文按文章链接抓。不需要登录
公众平台,也不需要任何代理或逐号凭证。

输出协议:stdout 只出 NDJSON(每行一个事件),给上游程序解析;
人类可读的提示一律走 stderr。这样同一个命令既能给知识库管道调,也能手敲。

退出码:0 正常 / 1 一般错误 / 2 本机数据库读不了
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

EXIT_OK, EXIT_ERROR = 0, 1


def emit(event: dict) -> None:
    """一个事件一行,立即 flush——上游是边读边显示进度的。"""
    print(json.dumps(event, ensure_ascii=False), flush=True)


def info(*parts) -> None:
    print(*parts, file=sys.stderr, flush=True)


def _pad(text: str, width: int) -> str:
    """按显示宽度补空格。中日韩字符占两格,用 len() 对齐会歪。"""
    shown = sum(2 if ord(c) > 0x2E80 else 1 for c in text)
    return text + " " * max(1, width - shown)


# ── status ──────────────────────────────────────────────────────────────────────

def cmd_status(args) -> int:
    from src.collect.local_runner import local_status

    emit(local_status())
    return EXIT_OK


# ── collect ─────────────────────────────────────────────────────────────────────

def cmd_collect(args) -> int:
    from src.collect.local_runner import collect

    since, until = args.since or "", args.until or ""
    if bool(since) != bool(until):
        info("错误:--since 和 --until 必须同时给")
        return EXIT_ERROR
    if since:
        from datetime import date
        try:
            dates = [date.fromisoformat(d) for d in (since, until)]
            if dates[0] > dates[1] or [d.isoformat() for d in dates] != [since, until]:
                raise ValueError
        except ValueError:
            info("错误:日期须为 YYYY-MM-DD，且起始日期不能晚于结束日期")
            return EXIT_ERROR
    targets = [t.strip() for t in (args.targets or "").split(",") if t.strip()]

    result = collect(emit, targets=targets or None, since=since, until=until)
    info(f"采集完成:新增 {result['new_articles']} 篇"
         + (f",{len(result['errors'])} 个号失败" if result["errors"] else ""))
    return EXIT_ERROR if result["errors"] else EXIT_OK


# ── export ──────────────────────────────────────────────────────────────────────

def cmd_export(args) -> int:
    """导出到 obsidian。转调现成脚本,它自己会打进度。"""
    argv = [sys.executable, str(ROOT / "scripts" / "export_obsidian.py")]
    if args.no_images:
        argv.append("--no-images")
    proc = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True)
    # 脚本的输出是人类可读的行,原样转到 stderr;结构化结果自己 emit
    if proc.stdout:
        info(proc.stdout.rstrip())
    if proc.returncode != 0:
        info(proc.stderr.rstrip()[-500:])
        emit({"stage": "export", "status": "error",
              "detail": (proc.stderr or proc.stdout).rstrip()[-300:]})
        return EXIT_ERROR
    fails = []
    for line in proc.stdout.splitlines():
        if "无正文,跳过" in line:
            fails.append(line.split("无正文,跳过", 1)[1].strip())
    tail = [l for l in proc.stdout.splitlines() if "完成" in l]
    emit({"stage": "export", "status": "ok",
          "detail": tail[-1].replace("[export] ", "") if tail else "完成",
          "no_body": fails})
    return EXIT_OK


# ── accounts ────────────────────────────────────────────────────────────────────

def cmd_accounts(args) -> int:
    from src.db.repository import ArticleRepository

    action = args.action
    if action == "list":
        with ArticleRepository() as repo:
            rows = repo.conn.execute(
                "SELECT name, fakeid, display_name, in_crawl_config, latest_crawl_at "
                "FROM accounts WHERE removed=0 ORDER BY in_crawl_config DESC, name"
            ).fetchall()
        out = [dict(r) for r in rows]
        if args.json:
            emit({"accounts": out})
        else:
            active = sum(1 for a in out if a["in_crawl_config"])
            info(f"共 {len(out)} 个公众号,{active} 个参与采集(✓):")
            for a in out:
                mark = "✓" if a["in_crawl_config"] else "·"
                # 微信上已经改了名的,把新名字跟在后面——库里的键还是旧的那个
                shown = a["name"]
                if a["display_name"] and a["display_name"] != a["name"]:
                    shown = f"{a['name']} →{a['display_name']}"
                info(f"  {mark} {_pad(shown, 32)}最近采集 {a['latest_crawl_at'] or '-'}")
        return EXIT_OK

    if action == "discover":
        return _accounts_discover(args.name or "", args.json)

    if not args.name:
        info(f"错误:{action} 需要给公众号名字")
        return EXIT_ERROR

    if action == "add":
        return _accounts_add(args.name, args.fakeid, args.json)

    with ArticleRepository() as repo:
        exists = repo.conn.execute(
            "SELECT 1 FROM accounts WHERE name=? AND removed=0",
            (args.name,)).fetchone()
        if not exists:
            return _fail(f"订阅列表里没有「{args.name}」", args.json)
        if action == "remove":
            repo.remove_account_from_list(args.name)
            detail = f"已移出订阅列表(已采集的文章保留):{args.name}"
        else:
            flag = 1 if action == "enable" else 0
            repo.conn.execute(
                "UPDATE accounts SET in_crawl_config=? WHERE name=? AND removed=0",
                (flag, args.name))
            detail = f"{'启用' if flag else '停用'}采集:{args.name}"
        repo.commit()
    if args.json:
        emit({"ok": True, "detail": detail})
    else:
        info(detail)
    return EXIT_OK


def _fail(detail: str, as_json: bool, code: int = EXIT_ERROR) -> int:
    """失败的统一出口。detail 是成品文案,上游原样显示,不再自己拼错误话术。"""
    if as_json:
        emit({"ok": False, "detail": detail})
    else:
        info(f"错误:{detail}")
    return code


def _accounts_discover(query: str, as_json: bool) -> int:
    """从本机绿泡泡会话里找候选。不需要扫码凭证,也不走网络。"""
    from src.collect.local_accounts import discover
    from src.crawler.local_wechat import LocalReaderError
    from src.db.repository import ArticleRepository

    try:
        with ArticleRepository() as repo:
            rows = discover(repo, query)
    except LocalReaderError as e:
        return _fail(str(e), as_json, EXIT_ERROR)

    candidates = [{"name": r["name"], "fakeid": r["fakeid"],
                   "alias": r["username"],
                   "signature": _discover_note(r),
                   "subscribed": r["subscribed"],
                   "stored_name": r["stored_name"]}
                  for r in rows if r["name"]]
    if as_json:
        emit({"candidates": candidates})
    elif not candidates:
        info(f"本机没有名字含「{query}」的公众号会话" if query
             else "本机还没有任何公众号会话")
    else:
        addable = sum(1 for c in candidates if c["fakeid"] and not c["subscribed"])
        info(f"本机 {len(candidates)} 个公众号会话,其中 {addable} 个可以添加:")
        for c in candidates:
            mark = "已订阅" if c["subscribed"] else ("可添加" if c["fakeid"] else "无图文")
            info(f"  {mark}  {_pad(c['name'], 26)}{c['signature']}")
    return EXIT_OK


def _discover_note(row: dict) -> str:
    if not row["fakeid"]:
        return "本机暂无可解析的图文推送"
    if row["subscribed"] and row["stored_name"] != row["name"]:
        return f"已订阅,库里存作「{row['stored_name']}」"
    if row["subscribed"]:
        return "已订阅"
    return "最近推送 " + (
        datetime.fromtimestamp(row["last_at"]).strftime("%Y-%m-%d")
        if row["last_at"] else "—")


def _accounts_add(name: str, fakeid: str = "", as_json: bool = False) -> int:
    """写入订阅列表。给了 fakeid 就直接落库——面板上是先搜后点,那一步已经
    定死了是哪个号,再搜一次既慢又可能因为微信排序变化选中另一个同名号。"""
    from src.db.repository import ArticleRepository

    if not fakeid:
        # 面板上是先 discover 后点,fakeid 早就定死了;这条是终端里直接按名字
        # 加的路。从本机会话里按**完全同名**认,不做模糊匹配——加错号之后
        # 文章会按它的档案键归档,再改回来要连 vault 目录一起搬。
        from src.collect.local_accounts import discover
        from src.crawler.local_wechat import LocalReaderError

        try:
            with ArticleRepository() as repo:
                rows = discover(repo, name)
        except LocalReaderError as e:
            return _fail(str(e), as_json, EXIT_ERROR)
        same = [r for r in rows if r["name"] == name]
        exact = [r for r in same if r["fakeid"]]
        ids = {r["fakeid"] for r in exact}
        if same and not ids:
            # 名字对上了、只是这个号本机还没有可解析的图文:别说成"没关注",
            # 那是另一回事,照着去关注一遍也不会变好。
            return _fail(f"「{name}」在本机有会话,但还没有能解析出归属的图文推送"
                         "(可能只收到过服务通知)。在绿泡泡里打开它、"
                         "同步一篇图文之后再试", as_json)
        if not ids:
            near = "、".join(dict.fromkeys(r["name"] for r in rows[:5]))
            return _fail(f"本机没有名叫「{name}」的公众号会话"
                         + (f",相近的有:{near}" if near else "")
                         + "。请先在绿泡泡里关注并同步该号", as_json)
        if len(ids) > 1:
            return _fail(f"本机有 {len(ids)} 个同名公众号会话,"
                         f"请用 --fakeid 指定是哪一个", as_json)
        fakeid = exact[0]["fakeid"]

    with ArticleRepository() as repo:
        # 走 enable_crawl_account 而不是 upsert_account:add 是"用户主动订阅",
        # 得把 removed 标记一并清掉。之前用 upsert_account,重新订阅一个移除过、
        # 且有历史文章的号会提示"已添加",但它 removed 还是 1——列表、同步目标、
        # 默认采集里统统看不到它。upsert_account 留给采集过程更新用,那条路上
        # 不该有把已移除的号悄悄复活的能力。
        stored = repo.enable_crawl_account(name, fakeid)
        repo.commit()
    if stored != name:
        # 同一个号改了名:认出来了,没有再插一行
        detail = f"「{stored}」就是这个号(已改名为「{name}」),已更新显示名并确保在采集列表里"
    else:
        detail = f"已添加:{name} ({fakeid})"
    if as_json:
        emit({"ok": True, "detail": detail, "name": stored, "fakeid": fakeid})
    else:
        info(detail)
    return EXIT_OK


# ── 入口 ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cli", description="公众号采集器(一次性任务,不常驻)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="健康状况与订阅列表(JSON)")

    c = sub.add_parser("collect", help="采集文章到本地库")
    c.add_argument("--since", help="起始日期 YYYY-MM-DD(与 --until 同时给)")
    c.add_argument("--until", help="结束日期 YYYY-MM-DD")
    c.add_argument("--targets", help="逗号分隔的公众号名,默认全部订阅号")

    e = sub.add_parser("export", help="把本地库导出成 obsidian md")
    e.add_argument("--no-images", action="store_true", help="不下图,保留远程 URL")

    a = sub.add_parser("accounts", help="维护订阅的公众号")
    a.add_argument("action",
                   choices=["list", "discover", "add", "remove", "enable", "disable"])
    a.add_argument("name", nargs="?",
                   help="公众号名字(discover 时是关键词,可省略)")
    a.add_argument("--fakeid", default="", help="add 时直接指定,跳过查找")
    a.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from src.collect.bootstrap import install

    install()          # 微信域名绕过系统代理,不装会在有 MITM 代理时静默失败
    handlers = {
        "status": cmd_status, "collect": cmd_collect, "export": cmd_export,
        "accounts": cmd_accounts,
    }
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        info("\n已中断")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
