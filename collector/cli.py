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
    python -m cli articles drop --id <文章 id> [--account <名字>]
                                                   这一篇不再采集(拉黑身份)

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
import tempfile
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
    with tempfile.TemporaryDirectory(prefix="gzh-export-") as tmp:
        report = Path(tmp) / "report.json"
        argv = [sys.executable, str(ROOT / "scripts" / "export_obsidian.py"),
                "--report", str(report)]
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
        fails = _no_body_items(report, proc.stdout)
    tail = [l for l in proc.stdout.splitlines() if "完成" in l]
    emit({"stage": "export", "status": "ok",
          "detail": tail[-1].replace("[export] ", "") if tail else "完成",
          "no_body": fails})
    return EXIT_OK


def _no_body_items(report: Path, stdout: str) -> list[dict]:
    """这一轮因为没正文而没导出的那几篇。每条 `{name, id, account, title, link}`。

    **先读结构化报告,读不出来才退回解析 stdout 那行**「无正文,跳过 账号 · 标题」。
    退回那一档只有 `name`:标题会重名、`·` 也可能出现在号名或标题里,拿它反查是
    哪一篇一定出错,所以宁可**不给身份**——上游据此把那一条显示成不可点、不可删,
    而不是指着另一篇文章让人删。
    **报告写不出来不许让这几条整个消失**:那样面板上会显示「导出完成」且明细为空,
    而确实有文章没进知识库(元规则:漏报是静默丢数据)。
    """
    try:
        items = json.loads(report.read_text(encoding="utf-8"))["no_body"]
    except (OSError, ValueError, KeyError, TypeError):
        items = None
    if isinstance(items, list):
        out = []
        for one in items:
            if not isinstance(one, dict):
                continue
            account, title = str(one.get("account") or ""), str(one.get("title") or "")
            out.append({"name": f"{account} · {title}".strip(" ·"),
                        "id": str(one.get("id") or ""), "account": account,
                        "title": title, "link": str(one.get("link") or "")})
        return out
    return [{"name": line.split("无正文,跳过", 1)[1].strip(),
             "id": "", "account": "", "title": "", "link": ""}
            for line in stdout.splitlines() if "无正文,跳过" in line]


# ── articles ────────────────────────────────────────────────────────────────────

def cmd_articles(args) -> int:
    """这一篇不再采集:删掉它,并把它的身份记进黑名单。

    **幂等**:重复点一次、或者它刚好被另一条路径删掉了,都不该报红让人以为没删掉。
    但**「库里没有这一行」不等于「它已经被永久排除」** —— 见 `_drop_missing`。
    """
    from src.db.repository import ArticleRepository
    from src.utils.article_identity import exclusion_keys, existing_article_id

    if args.action != "drop":
        return _fail(f"不认识的操作「{args.action}」", args.json)
    if not args.id:
        return _fail("需要给文章 id(--id)", args.json)
    link = getattr(args, "link", "") or ""
    with ArticleRepository() as repo:
        row = repo.conn.execute(
            "SELECT id, account_name, title FROM articles WHERE id=?", (args.id,)
        ).fetchone()
        if row is None and link:
            # **「按这个 ID 查不到」不等于「库里没有这一篇」。** 同一原文换个通道、
            # 或者换一份采集库之后会以另一个档案 ID 保存(`prepare_article` 沿用旧
            # ID 就是这么来的)。漏掉这一步就只把请求方给的那组键记进名单、库里那
            # 一行照旧在,导出下一趟把它写进知识库 —— 第 2 轮审查实测:删除报
            # `ok=true, excluded=true`,**删除之后才启动**的导出仍写出 1 篇 md。
            # 找到了就当成常规删除往下走(归属照旧核对),不走 `_drop_missing`。
            alias = existing_article_id(repo, link)
            if alias:
                row = repo.conn.execute(
                    "SELECT id, account_name, title FROM articles WHERE id=?",
                    (alias,),
                ).fetchone()
        if row is None:
            return _drop_missing(repo, args.id, link, args.json)
        # 归属对不上一律拒:调用方拿的是面板上那一条的身份,对不上说明两边
        # 说的不是同一篇,删下去就是删错文章。
        if args.account and row["account_name"] != args.account:
            return _fail(
                f"这一篇属于「{row['account_name']}」，与给定的"
                f"「{args.account}」对不上，没有改动", args.json)
        repo.remove_article(row["id"], row["account_name"])
        # 请求方给的那组键一并记进名单。`remove_article` 只认它删掉的那个档案 ID
        # (外加从那一行读出的链接身份),而上面可能删的是**另一个** ID;旧页面手里
        # 那个 ID、以及按链接算的稳定 ID 不记上,换个通道再发现同一篇时发现侧那一道
        # 早检查就挡不住。名单里存的键 = `exclusion_keys` 那一组,与写入/发布侧同一份。
        repo.exclude_identities(*exclusion_keys(args.id, link))
        repo.commit()
    detail = f"已移除,以后不再采集:{row['account_name']} · {row['title']}"
    if args.json:
        emit({"ok": True, "detail": detail, "removed": True})
    else:
        info(detail)
    return EXIT_OK


def _drop_missing(repo, article_id: str, link: str, as_json: bool) -> int:
    """`articles` 里没有这一行时,那句「以后也不会再采集」怎么兑现。

    走到这里时,调用方已经先按链接身份找过一遍同一原文的**其他档案 ID**
    (`existing_article_id`)—— 那一步找到了就是常规删除,不会进来。所以这里面对的
    是「库里真的没有这一篇」。

    **缺行不是排除凭据。** 同步记录和没关掉的旧页面会一直持有这一篇的身份,而采集
    库完全可能被清理、重建或回退到没有这一行的版本 —— 那时照旧返回成功,主系统会
    把它从各轮明细里隐藏,而下一轮发现时它照样入库、照样抓正文、照样导出(第 1 轮
    审查实测:`row_exists_after_drop_and_ingest=true`,`body_fetch_calls=1`)。
    明细一旦隐藏,用户**再也看不到这一条**,也就不会知道承诺没兑现。

    所以分三档,只有拿得出凭据的两档算成功:

    1. **名单里已经有它**(`exclusion_keys` 里任一个命中)—— 真的已经排除过了,
       幂等成功。这是「连点两次」「两个标签页各点一次」的正常形态。
    2. **算得出身份**(调用方给了 `--link`)—— 当场把这组键补写进名单
       (`exclude_identities`),再报成功。`add_deleted_blacklist` 走不通:它要从
       `articles` 那一行里读规范链接,而那一行已经没了。
    3. **两样都没有** —— 拒。只写一个旧档案 ID 挡不住换个通道再发现同一篇
       (`stable_article_id` 和 `article_identity` 都算不出来),宣称「永久排除」
       就是报一个没做到的结论。拒掉的代价只是那一条留在明细里(下一轮同步会补上
       `link`,或者等它再次入库之后再删),比静默失约轻得多。
    """
    from src.utils.article_identity import exclusion_keys

    keys = exclusion_keys(article_id, link)
    if any(repo.is_deleted_blacklist(k) for k in keys):
        detail = "这一篇已经不在库里了,此前已记为永久不再采集"
        if as_json:
            emit({"ok": True, "detail": detail, "removed": False, "excluded": True})
        else:
            info(detail)
        return EXIT_OK
    # 名单里没有它。能算出链接身份就当场补上——那是跨通道也认的那个键。
    identity_keys = [k for k in keys if k != article_id]
    if identity_keys:
        repo.exclude_identities(*keys)
        repo.commit()
        detail = "这一篇已经不在库里了,已记为永久不再采集"
        if as_json:
            emit({"ok": True, "detail": detail, "removed": False, "excluded": True})
        else:
            info(detail)
        return EXIT_OK
    return _fail(
        "这一篇已经不在采集库里,而且没有原文链接可以算出它的身份,"
        "没法承诺「以后不再采集」(换个通道发现同一篇时挡不住)。"
        "下一轮同步会重新带上链接,那时再移除", as_json)


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
                # 绿泡泡上已经改了名的,把新名字跟在后面——库里的键还是旧的那个
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
                   "probe": r["probe"],
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
            mark = ("已订阅" if c["subscribed"] else "可添加" if c["fakeid"]
                    else "无图文" if c["probe"] == "none" else "待核验")
            info(f"  {mark}  {_pad(c['name'], 26)}{c['signature']}")
    return EXIT_OK


# 解不出归属时该说什么。按 `discover` 记下的 probe 分档 —— 「翻过、确实没有」和
# 「这一次没翻」不是一件事:后者说成没有图文就是在报一个没做过的结论,而用户看到
# 它就不会再试了。实测 15 个都有图文推送的候选,宽泛搜索下排在解析预算之外的 3 个
# 被说成没有图文,缩小关键词再搜立刻解出来、加得进去。
# 拆成「状态」和「怎么办」两半,是因为加号失败那条提示要把它们重新组合:
# 列表里一行放得下 `状态(怎么办)`,一句错误提示里还得接着说别的,原样塞进去
# 就是括号套括号。`_NO_BIZ_NOTE` 照旧由这两半拼出来,字面值一个字没变。
_NO_BIZ_STATE = {
    "none": "本机暂无可解析的图文推送",
    "unchecked": "这次没核验",
    "error": "本机这条会话读不动",
}

# 下一步怎么办。只有「真的没有图文」那档该去绿泡泡里同步一篇;另两档是这一轮
# 没解析到(排在预算之外/这条会话读不动),号本身可能早就有图文了,叫人再去
# 同步一遍是白跑。
_NO_BIZ_FIX = {
    "none": "可能只收到过服务通知;在绿泡泡里打开它、同步一篇图文之后再试",
    "unchecked": "缩小关键词再搜",
    "error": "缩小关键词再搜",
}

# 列表那一列只放得下一句,所以「怎么办」收在括号里;「没有图文」那档的处置话太长,
# 列表里不写,留给加号失败时那条提示。
_NO_BIZ_NOTE = {
    key: state + ("" if key == "none" else f"({_NO_BIZ_FIX[key]})")
    for key, state in _NO_BIZ_STATE.items()
}


def _worst_probe(rows: list[dict]) -> str:
    """一组同名候选里取最该说出来的那一档:读不动 > 没核验 > 确实没有图文。

    排在前面的两档都是「这一轮没查清」,照它们给建议最多是多搜一次;反过来把
    读不动说成「没有图文」,用户就不会再试了。
    """
    probes = {r.get("probe", "") for r in rows}
    for probe in ("error", "unchecked", "none"):
        if probe in probes:
            return probe
    return "unchecked"


def _discover_note(row: dict) -> str:
    if not row["fakeid"]:
        return _NO_BIZ_NOTE.get(row.get("probe", ""), _NO_BIZ_NOTE["unchecked"])
    if row["subscribed"] and row["stored_name"] != row["name"]:
        return f"已订阅,库里存作「{row['stored_name']}」"
    if row["subscribed"]:
        return "已订阅"
    return "最近推送 " + (
        datetime.fromtimestamp(row["last_at"]).strftime("%Y-%m-%d")
        if row["last_at"] else "—")


def _accounts_add(name: str, fakeid: str = "", as_json: bool = False) -> int:
    """写入订阅列表。给了 fakeid 就直接落库——面板上是先搜后点,那一步已经
    定死了是哪个号,再搜一次既慢又可能因为绿泡泡排序变化选中另一个同名号。"""
    from src.db.repository import AccountNameTaken, ArticleRepository

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
        blind = [r for r in same if not r["fakeid"]]
        ids = {r["fakeid"] for r in exact}
        if same and not ids:
            # 名字对上了、只是没拿到 __biz。别说成"没关注"——那是另一回事,
            # 照着去关注一遍也不会变好。**为什么没拿到有三档,处置也不同**:
            # 原来一律按"确实没有图文"报,于是本机这条会话读不动、或者这次压根
            # 没核验到它的时候,提示也在建议"去同步一篇图文",而它早就同步过了
            # (第 2 轮审查实测:搜索那一行显示的是「本机这条会话读不动」,点添加
            # 却说成「没有能解析出归属的图文推送」)。文案口径仍是 _NO_BIZ_NOTE。
            probe = _worst_probe(same)
            return _fail(f"「{name}」在本机有会话,但{_NO_BIZ_STATE[probe]}。"
                         f"{_NO_BIZ_FIX[probe]},或者用 --fakeid 直接指定是哪一个",
                         as_json)
        if not ids:
            near = "、".join(dict.fromkeys(r["name"] for r in rows[:5]))
            return _fail(f"本机没有名叫「{name}」的公众号会话"
                         + (f",相近的有:{near}" if near else "")
                         + "。请先在绿泡泡里关注并同步该号", as_json)
        if len(ids) > 1:
            return _fail(f"本机有 {len(ids)} 个同名公众号会话,"
                         f"请用 --fakeid 指定是哪一个", as_json)
        if blind:
            # **同名会话里还有没解析出归属的,一样是歧义。** 原来只拿「已经解析出
            # __biz 的那几个」判歧义,于是:两个同名会话、一个解析出来了、另一个
            # 读不动(或排在预算之外),按名字添加会**把解析出来的那个拿去入库**
            # —— 退出码 0、提示「已添加」,订阅的却是另一个号(第 1 轮审查复现:
            # 面板上点那条写着「本机这条会话读不动」的候选,前端发的正是空 id,
            # 落库的是另一条候选的 biz)。归属只认 `__biz`,同名不是线索:一个
            # 名字对上两个会话时,少一个归属就等于不知道用户要的是哪一个。
            probe = _worst_probe(blind)
            return _fail(f"本机有 {len(same)} 个名叫「{name}」的会话,其中 "
                         f"{len(blind)} 个{_NO_BIZ_STATE[probe]},没法确定你要的是"
                         f"哪一个。{_NO_BIZ_FIX[probe]},或者用 --fakeid 指定",
                         as_json)
        fakeid = exact[0]["fakeid"]

    with ArticleRepository() as repo:
        # 走 enable_crawl_account 而不是 upsert_account:add 是"用户主动订阅",
        # 得把 removed 标记一并清掉。之前用 upsert_account,重新订阅一个移除过、
        # 且有历史文章的号会提示"已添加",但它 removed 还是 1——列表、同步目标、
        # 默认采集里统统看不到它。upsert_account 留给采集过程更新用,那条路上
        # 不该有把已移除的号悄悄复活的能力。
        try:
            stored = repo.enable_crawl_account(name, fakeid)
        except AccountNameTaken as e:
            # **同名、不同号:拒掉,一个字都不写。** 档案名是主键,旧号的文章全挂
            # 在它名下、导出目录也按它归档,覆盖等于把那些历史接到这个新号的身份
            # 上(第 2 轮审查实测:两条同名候选都解析出归属之后点第二条,报的是
            # 「已添加」、订阅列表仍只有一条,第一个号连它的历史文章一起变成了第二
            # 个号的 __biz)。目前不支持同时订阅两个同名号——要支持就得给第二个号
            # 另起一个档案键,而那个键会成为新的 vault 目录名,不是这里能顺手定的。
            return _fail(
                f"「{name}」这个档案名在库里已经是另一个号的(__biz {e.held}),"
                f"历史文章都挂在它名下、导出目录也按它归档,不能改挂到 {e.wanted} "
                f"上。目前不支持同时订阅两个同名公众号;如果是刚才加错了号,把那条"
                f"订阅移除(名下还没有文章的号移除时整行就删掉了,名字跟着空出来)"
                f"再加这一个", as_json)
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

    r = sub.add_parser("articles", help="维护已采集的文章")
    r.add_argument("action", choices=["drop"])
    r.add_argument("--id", default="", help="文章 id")
    r.add_argument("--account", default="", help="它该属于哪个公众号(对不上就拒)")
    r.add_argument("--link", default="",
                   help="它的原文链接。库里已经没有这一行时,永久排除只能靠它算身份")
    r.add_argument("--json", action="store_true", help="输出 JSON 而不是人话")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from src.collect.bootstrap import install

    install()          # 绿泡泡域名绕过系统代理,不装会在有 MITM 代理时静默失败
    handlers = {
        "status": cmd_status, "collect": cmd_collect, "export": cmd_export,
        "accounts": cmd_accounts, "articles": cmd_articles,
    }
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        info("\n已中断")
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
