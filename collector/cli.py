#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""公众号采集器命令行入口 —— 一次性任务,跑完就退,不常驻。

    python -m cli status                          健康状况(JSON)
    python -m cli collect                          日常增量采集
    python -m cli collect --since 2026-08-01 --until 2026-08-26 [--targets a,b]
    python -m cli export [--no-images]             导出到 obsidian
    python -m cli creds [--missing-only]           看每个号的文章凭证够不够
    python -m cli accounts list                    看订阅的公众号
    python -m cli accounts search <关键词>         搜候选(需要扫码凭证)
    python -m cli accounts add <名字> [--fakeid X] 加入订阅;给了 fakeid 就不再搜
    python -m cli accounts remove <名字>
    python -m cli accounts enable|disable <名字>   是否参与采集

输出协议:stdout 只出 NDJSON(每行一个事件),给上游程序解析;
人类可读的提示一律走 stderr。这样同一个命令既能给知识库管道调,也能手敲。

退出码:0 正常 / 1 一般错误 / 2 凭证失效 / 3 微信频率限制
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

EXIT_OK, EXIT_ERROR, EXIT_AUTH, EXIT_FREQ = 0, 1, 2, 3


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
    import api
    from src.db.repository import ArticleRepository

    auth = api._build_auth_status().model_dump()
    with ArticleRepository() as repo:
        targets = sorted(repo.get_name2fakeid().keys())

    # 一次性任务,每次实测,不吃 _auth_state 的缓存:那个 dict 在没探测过时
    # valid 恒为 true,读它会把"没查过"说成"有效"。
    # credential_expired 是另一回事——它按 saved_at + 4 天 TTL 本地估算,
    # 偏保守,实测有效时以实测为准(实测过 8-26:估算已超期 4 天但 cookie 仍活)。
    ok, reason = api._check_auth_valid()
    expired = bool(auth.get("credential_expired"))
    label = auth.get("credential_expiry_label") or ""
    if ok:
        message = "凭证有效" + (f"(本地估算{label},实测仍可用)" if expired else "")
    else:
        message = f"凭证失效:{reason}"
    emit({
        "ready": ok,
        "message": message,
        "hint": "" if ok else "跑 `python -m cli login` 重新扫码",
        "credential_expiry_label": label,
        "credential_days_remaining": auth.get("credential_days_remaining"),
        "targets": targets,
    })
    return EXIT_OK


# ── collect ─────────────────────────────────────────────────────────────────────

def cmd_collect(args) -> int:
    from src.collect.runner import collect

    since, until = args.since or "", args.until or ""
    if bool(since) != bool(until):
        info("错误:--since 和 --until 必须同时给")
        return EXIT_ERROR
    targets = [t.strip() for t in (args.targets or "").split(",") if t.strip()]

    result = collect(emit, targets=targets or None, since=since, until=until)
    if result["auth_error"]:
        info("凭证失效,采集中止。跑 `python -m cli login` 续期后重试")
        return EXIT_AUTH
    if result["freq_control"]:
        info("触发微信频率限制,已中止。等 30-60 分钟再跑")
        return EXIT_FREQ
    info(f"采集完成:新增 {result['new_articles']} 篇"
         + (f",{len(result['errors'])} 个号失败" if result["errors"] else ""))
    return EXIT_OK


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


# ── creds:每个公众号的文章凭证 ──────────────────────────────────────────────────
#
# 这套凭证跟 `login` 那个扫码 token 是两回事:扫码 token 只用来搜索/翻列表,
# 抓正文要的是每个号各自的 profile 凭证,由 Mac 微信里打开一篇文章时经
# wxdown-service 的 mitm 抓下来。
#
# wxdown 把抓到的凭证写成一个普通 JSON 文件,所以这里直接读它——原来那条
# "wxdown WebSocket → 前端 :5173 → 后端 :8000 POST" 的转发链路,两头的常驻
# 服务都只是为了搬运这个文件,去掉即可。

def _wxdown_cred_file() -> Path:
    """wxdown-service 的凭证文件位置。装到别处就设 WXDOWN_DIR 或 WXDOWN_CRED_FILE。"""
    import os

    explicit = os.getenv("WXDOWN_CRED_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = os.getenv("WXDOWN_DIR", "").strip()
    root = Path(base).expanduser() if base else \
        Path.home() / "code" / "gongzhonghao" / "wxdown-service"
    return root / "resources" / "data" / "credentials.json"


def _import_wxdown_creds() -> tuple[int, str]:
    """把 wxdown 文件里的凭证并进本地库。返回 (真正新增/更新的条数, 说明)。"""
    import json as _json

    from src.crawler.profile_ext import profile_credential_store

    path = _wxdown_cred_file()
    if not path.exists():
        return 0, f"没找到 wxdown 凭证文件({path})"
    try:
        items = _json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return 0, f"读不了 wxdown 凭证文件:{e}"
    if not isinstance(items, list):
        return 0, "wxdown 凭证文件格式不对(顶层不是数组)"

    # save_wxdown_items 对"时间戳一样"的条目也算保存,直接用它的返回数,
    # 每次重跑都报同一个数字——边点文章边刷新时就看不出这轮新增了几个。
    # 自己比对前后快照,只数真正变新的。
    before = {biz: c.captured_at for biz, c in profile_credential_store.records().items()}
    profile_credential_store.save_wxdown_items(items)
    after = profile_credential_store.records()
    fresh = sum(1 for biz, c in after.items()
                if biz not in before or c.captured_at > before[biz])
    return fresh, ""


def cmd_creds(args) -> int:
    from src.crawler.profile_ext import (
        credential_public_status,
        profile_credential_store,
    )
    from src.db.repository import ArticleRepository

    imported, warn = (0, "") if args.no_import else _import_wxdown_creds()

    records = profile_credential_store.records()
    status = {biz: credential_public_status(c) for biz, c in records.items()}
    with ArticleRepository() as repo:
        rows = repo.conn.execute(
            "SELECT name, fakeid FROM accounts "
            "WHERE removed=0 AND in_crawl_config=1 ORDER BY name"
        ).fetchall()

    accounts = []
    for r in rows:
        cur = status.get(r["fakeid"])
        if cur is None:
            state, captured = "missing", ""
        else:
            state = "valid" if cur["valid"] else "expired"
            captured = cur["captured_at"]
        accounts.append({"name": r["name"], "fakeid": r["fakeid"],
                         "state": state, "captured_at": captured})

    ready = sum(1 for a in accounts if a["state"] == "valid")
    if args.json:
        emit({"ready": ready, "total": len(accounts), "imported": imported,
              "warning": warn, "source": str(_wxdown_cred_file()),
              "accounts": accounts})
        return EXIT_OK

    if warn:
        info(f"注意:{warn}")
    elif imported:
        info(f"从 wxdown 导入 {imported} 条新凭证")
    info(f"{ready}/{len(accounts)} 个公众号已就绪:")
    label = {"valid": "✓ 有效", "expired": "✗ 已失效", "missing": "· 没凭证"}
    for a in accounts:
        if args.missing_only and a["state"] == "valid":
            continue
        info(f"  {label[a['state']]}  {_pad(a['name'], 26)}{a['captured_at']}")
    return EXIT_OK


# ── route:让微信文章流量走 wxdown 的代理 ─────────────────────────────────────────

def cmd_route(args) -> int:
    """看/切 Clash 上那条把微信文章流量交给 wxdown 的规则。

    不切的话,在微信里点文章 wxdown 截不到,凭证就一直是"没有"。
    """
    from src.services.clash_wxdown_route import ClashRouteError, clash_wxdown_route

    try:
        data = clash_wxdown_route.activate() if args.activate \
            else clash_wxdown_route.status()
    except ClashRouteError as e:
        if args.json:
            emit({"ok": False, "detail": str(e)})
        else:
            info(f"错误:{e}")
        return EXIT_ERROR
    if args.json:
        emit({"ok": True, **data})
    else:
        info(json.dumps(data, ensure_ascii=False, indent=2))
    return EXIT_OK


# ── accounts ────────────────────────────────────────────────────────────────────

def cmd_accounts(args) -> int:
    from src.db.repository import ArticleRepository

    action = args.action
    if action == "list":
        # 顺带把每个号的文章凭证状态带上:面板要按"哪些还没就绪"来提示,
        # 分两条命令拿就得起两个子进程。这里只读本地凭证文件,不碰 wxdown、
        # 不走网络,够便宜——从 wxdown 导入是 `creds` 的活。
        from src.crawler.profile_ext import profile_credential_store

        creds = {biz: c.is_valid() for biz, c in profile_credential_store.records().items()}
        with ArticleRepository() as repo:
            rows = repo.conn.execute(
                "SELECT name, fakeid, in_crawl_config, latest_crawl_at "
                "FROM accounts WHERE removed=0 ORDER BY in_crawl_config DESC, name"
            ).fetchall()
        out = []
        for r in rows:
            valid = creds.get(r["fakeid"])
            out.append({**dict(r),
                        "cred_state": "missing" if valid is None
                        else ("valid" if valid else "expired")})
        if args.json:
            emit({"accounts": out})
        else:
            ready = sum(1 for a in out if a["cred_state"] == "valid")
            info(f"共 {len(out)} 个公众号(✓ = 参与采集),{ready} 个凭证就绪:")
            mark_cred = {"valid": "凭证✓", "expired": "凭证✗", "missing": "无凭证"}
            for a in out:
                mark = "✓" if a["in_crawl_config"] else "·"
                info(f"  {mark} {_pad(a['name'], 26)}{mark_cred[a['cred_state']]}"
                     f"  最近采集 {a['latest_crawl_at'] or '-'}")
        return EXIT_OK

    if not args.name:
        info(f"错误:{action} 需要给公众号名字")
        return EXIT_ERROR

    if action == "search":
        return _accounts_search(args.name, args.json)
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


def _accounts_search(query: str, as_json: bool) -> int:
    """搜公众号候选。走公众平台接口,需要扫码凭证。"""
    import api

    try:
        candidates = api._search_biz(query)
    except Exception as e:
        return _fail(f"搜索失败,凭证可能已过期(跑 `python -m cli login` 续期):"
                     f"{str(e)[:120]}", as_json, EXIT_AUTH)
    rows = [{"name": c.nickname, "fakeid": c.fakeid,
             "alias": c.alias, "signature": c.signature}
            for c in candidates]
    if as_json:
        emit({"candidates": rows})
    elif not rows:
        info(f"没搜到「{query}」")
    else:
        info(f"搜到 {len(rows)} 个:")
        for r in rows:
            info(f"  {_pad(r['name'], 26)}{r['alias'] or '-'}")
    return EXIT_OK


def _accounts_add(name: str, fakeid: str = "", as_json: bool = False) -> int:
    """写入订阅列表。给了 fakeid 就直接落库——面板上是先搜后点,那一步已经
    定死了是哪个号,再搜一次既慢又可能因为微信排序变化选中另一个同名号。"""
    from src.db.repository import ArticleRepository

    if not fakeid:
        import api
        try:
            candidates = api._search_biz(name)
        except Exception as e:
            return _fail(f"搜索失败,凭证可能已过期(跑 `python -m cli login` 续期):"
                         f"{str(e)[:120]}", as_json, EXIT_AUTH)
        exact = [c for c in candidates if c.nickname == name]
        if not exact:
            near = "、".join(c.nickname for c in candidates[:5])
            return _fail(f"没有完全同名的公众号" + (f",搜到:{near}" if near else ""),
                         as_json)
        name, fakeid = exact[0].nickname, exact[0].fakeid

    with ArticleRepository() as repo:
        # 走 enable_crawl_account 而不是 upsert_account:add 是"用户主动订阅",
        # 得把 removed 标记一并清掉。之前用 upsert_account,重新订阅一个移除过、
        # 且有历史文章的号会提示"已添加",但它 removed 还是 1——列表、同步目标、
        # 默认采集里统统看不到它。upsert_account 留给采集过程更新用,那条路上
        # 不该有把已移除的号悄悄复活的能力。
        repo.enable_crawl_account(name, fakeid)
        repo.commit()
    detail = f"已添加:{name} ({fakeid})"
    if as_json:
        emit({"ok": True, "detail": detail, "name": name, "fakeid": fakeid})
    else:
        info(detail)
    return EXIT_OK


# ── login ───────────────────────────────────────────────────────────────────────

def cmd_login(args) -> int:
    """扫码登录公众平台:二维码存成图片,路径打在 stderr 上,自己打开扫。

    过期会自动换一张(微信的二维码大约两分钟就废),整体等 5 分钟不确认就放弃。
    """
    import time
    from datetime import datetime

    from src.auth.mp_scan_login import (
        SCAN_STATUS_CONFIRMED,
        SCAN_STATUS_EXPIRED,
        MpScanLogin,
        scan_status_message,
    )
    from src.utils.data_manager import data_manager

    qr_path = ROOT / "data" / "login_qrcode.png"
    login = MpScanLogin()
    try:
        login.start_session()
        qr_path.write_bytes(login.fetch_qrcode_bytes())
    except Exception as e:
        info(f"取二维码失败:{e}")
        return EXIT_ERROR
    info(f"二维码已存到 {qr_path}\n用微信扫它,然后在手机上确认登录。")

    deadline = time.time() + 300
    last_msg = ""
    while time.time() < deadline:
        try:
            result = login.poll_scan()
        except Exception as e:
            info(f"轮询扫码状态失败:{e}")
            return EXIT_ERROR
        if result.status == SCAN_STATUS_CONFIRMED:
            break
        if result.status in SCAN_STATUS_EXPIRED:
            try:
                qr_path.write_bytes(login.fetch_qrcode_bytes())
                info("二维码过期,已换一张,重新扫")
            except Exception as e:
                info(f"刷新二维码失败:{e}")
                return EXIT_ERROR
        else:
            msg = scan_status_message(result.status, result.raw)
            if msg != last_msg:            # 同一状态别刷屏
                info(msg)
                last_msg = msg
        time.sleep(2)
    else:
        info("等待扫码超时(5 分钟),放弃")
        return EXIT_ERROR

    try:
        token, cookie_str = login.complete_login()
    except Exception as e:
        info(f"完成登录失败:{e}")
        return EXIT_ERROR
    data_manager.id_info["token"] = token
    data_manager.id_info["cookie"] = cookie_str
    data_manager.id_info["saved_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    data_manager.write("id_info")
    qr_path.unlink(missing_ok=True)
    info("登录成功,凭证已写入 data/id_info.json")
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

    r = sub.add_parser("creds", help="每个公众号的文章凭证状态(自动从 wxdown 导入)")
    r.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")
    r.add_argument("--no-import", action="store_true", help="只看现状,不从 wxdown 导入")
    r.add_argument("--missing-only", action="store_true", help="只列还没就绪的")

    rt = sub.add_parser("route", help="微信文章流量是否走 wxdown 代理")
    rt.add_argument("--activate", action="store_true", help="立即切过去")
    rt.add_argument("--json", action="store_true", help="输出 JSON")

    a = sub.add_parser("accounts", help="维护订阅的公众号")
    a.add_argument("action",
                   choices=["list", "search", "add", "remove", "enable", "disable"])
    a.add_argument("name", nargs="?", help="公众号名字(search 时是关键词)")
    a.add_argument("--fakeid", default="", help="add 时直接指定,跳过搜索")
    a.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")

    sub.add_parser("login", help="扫码登录公众平台(添加公众号时需要)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from src.collect.bootstrap import install

    install()          # profile_ext 通道 + 微信域名绕过代理,不装会静默走错路
    handlers = {
        "status": cmd_status, "collect": cmd_collect, "export": cmd_export,
        "accounts": cmd_accounts, "login": cmd_login, "creds": cmd_creds,
        "route": cmd_route,
    }
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        info("\n已中断")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
