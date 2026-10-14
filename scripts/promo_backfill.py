#!/usr/bin/env python3
"""存量文章的软广补判。

软广判定是搭在提取阶段做的(kb/extract.py),新文章进管道时顺带就判了。
这个脚本只为一件事存在:把判定上线之前就提取过的那批存量补上。补完一次
`promo_level=''` 就再也不会有新增,脚本自然退休。

省钱的两件事:
  1. 只判软广,不重跑整套 extract —— 输入砍到头 1600 + 尾 800 字符
     (软广信号密集在开头的钩子和结尾的转化引导,中间的实测细节没有增量),
     输出从一整份元数据缩成一个小 JSON。
  2. 同稿去重 —— 库里 8400 篇公众号文里有近 3000 篇是同一份稿子:克隆号互相
     转发、代充值广告同一个号发了 177 次。按「账号 + 标题 + 正文长度桶」归组,
     每组只判一篇,结论广播给同组其余篇,省掉约四分之一的调用。

判定口径改了之后,存量要跟上也走这里 —— `--recheck` 连已判过的一起重判。
它覆盖已有判定,所以必须同时用 `--account` 圈定范围(见 `_scope_args`)。

用法:
  uv run python scripts/promo_backfill.py scan            # 规模 + token/费用预估
  uv run python scripts/promo_backfill.py run [--limit N] # 实际补判
  uv run python scripts/promo_backfill.py report          # 判出来的软广清单
  # 口径改了,重判某个号的存量(先 scan 看规模和花费):
  uv run python scripts/promo_backfill.py scan --account GitHub猿 --recheck
  uv run python scripts/promo_backfill.py run  --account GitHub猿 --recheck
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import store                                            # noqa: E402
from kb.extract import (PROMO_HEAD, PROMO_TAIL, PROMO_TAG,       # noqa: E402
                        body_fingerprint, judge_promo, pending_promo_docs,
                        save_promo, sync_promo_tag)

# 软广只在公众号来源里成气候。dr(英文博客/HN 汇总)与 pre_tagged 的视频系
# 不进这个补判:前者没有投放稿,后者是衍生条目,源头文章判过就够了。
SOURCES = ["dr-gzh", "gzh"]
WORKERS = 6          # 与 pipeline_stages.EXTRACT_WORKERS 同口径
LENGTH_BUCKET = 500  # 正文长度按这个粒度分桶,同标题不同长度的稿子分开判

# 估算用的两个常量,拿 50 篇真实文章实测校准过:单次调用输入平均 2696 token,
# 其中喂进去的正文平均 1929 字符 —— 中文正文基本是 1 字符 1 token,剩下的是
# prompt 模板本身(判定口径那几段)。模板那部分原先是 ~770 token,判定口径加上
# 「借壳」那条和它的反向清单之后长了 554 字符,这里跟着抬到 1324。
# **改 `_PROMO_RULES` 要顺手核一次这个数** —— 它只用于 scan 的费用预估,偏小
# 不会让任何一次判定出错,但会让人按一个虚低的报价决定要不要跑几千篇。
CHARS_PER_TOKEN = 1.0
PROMPT_OVERHEAD = 1324
OUTPUT_TOKENS = 95   # 实测平均,一个 {"level","reason"} 小 JSON


def _scope_args(args) -> tuple[list[str], bool]:
    """把「判哪些篇」这两个参数解析成 (账号名, 要不要重判),顺带落守卫。

    `--recheck` 覆盖已有判定,无目标地跑一次是 9000 多篇、把所有历史结论
    (含人工核对过的)一起盖掉,所以要求它必须配 `--account`。守卫搭在参数
    解析这一层而不是 SQL 里:越早拦住,越不会出现「跑了一半才发现范围错了」。

    两个都按 `getattr` 兜底,**不要求调用方的 args 必须带上这两个属性** ——
    `cmd_run` 有手工拼 `argparse.Namespace` 的调用方(回写时序那组回归),
    直接写 `args.recheck` 会把它们打成 AttributeError。解析收在这一处也是
    为了别在 scan/run 两边各读一次、各漂一次。
    """
    accounts = [a.strip() for a in getattr(args, "account", None) or []
                if a.strip()]
    recheck = bool(getattr(args, "recheck", False))
    if recheck and not accounts:
        raise SystemExit("--recheck 会覆盖已有判定,必须用 --account 指定账号范围")
    return accounts, recheck


def _candidates(conn, limit: int = 0, accounts: list[str] | None = None,
                recheck: bool = False) -> list[dict]:
    """待补判的文档 + 正文。正文为空的跳过(判不了,也没内容可污染)。

    正文和**它所属的那一版** `content_hash` 从 `store.doc_body_version` 一条
    语句里读:拆成两句的话,两句之间另一条连接的索引就可能把这一篇换版,于是
    带回来的版本号指着一份我们没读过的正文,后面那道条件写反而会放行
    (理由同 `kb.extract.mark_no_body` / `store.doc_body_version`)。
    """
    rows = pending_promo_docs(conn, SOURCES, limit, accounts, recheck)
    out = []
    for r in rows:
        version, body = store.doc_body_version(conn, r["path"])
        if not body.strip():
            continue
        out.append({"path": r["path"], "title": r["title"],
                    "account": r["account"], "body": body,
                    "version": version, "seen": body_fingerprint(body)})
    return out


def _group(cands: list[dict]) -> list[list[dict]]:
    """把同一份稿子的多个副本归成一组,组内只需判一次。

    归组键是「账号 + 标题 + 正文长度桶」。

    带长度桶是因为同名但长度差一截的多半是两篇不同的文章(「量子位编辑作者
    招聘」这种通用标题尤其容易撞),归错组会把一篇的结论安到另一篇头上。

    **带账号**防的是另一头:搬运号把原创号的稿子整篇抄走、长度自然落在同一个
    桶里,于是一个结论广播给两个号。这个库里有 300 组是这样跨号的,最大的两组
    是「谷歌Gemini 4开跑了」(新智元 1 篇 + 搬运号 53 篇)和「OpenAI大神教你
    如何榨干Codex」(量子位 1 篇 + 搬运号 57 篇)——判定口径认「借壳」之后,
    搬运版判 hard,不带账号就会把原创那篇一起广播成 hard,而 hard 是连实体
    带条目一起从检索和图鉴里摘掉。代价是 7230 组变 7570 组,多 340 次调用。
    """
    buckets: dict[tuple, list[dict]] = {}
    for c in cands:
        key = (c["account"], c["title"], len(c["body"]) // LENGTH_BUCKET)
        buckets.setdefault(key, []).append(c)
    # 组内取最长的一篇当代表:转发版常常被砍掉尾部的推广段,而那正是判定依据
    return [sorted(g, key=lambda c: len(c["body"]), reverse=True)
            for g in buckets.values()]


def _estimate(groups: list[list[dict]]) -> dict:
    calls = len(groups)
    in_tok = sum(
        min(len(g[0]["body"]), PROMO_HEAD + PROMO_TAIL) / CHARS_PER_TOKEN
        + PROMPT_OVERHEAD for g in groups)
    return {"calls": calls, "docs": sum(len(g) for g in groups),
            "in_tokens": int(in_tok), "out_tokens": calls * OUTPUT_TOKENS}


def _fmt_cost(est: dict) -> str:
    """按两档常见单价给个区间。中转站价格各不相同,这里只作数量级参考。"""
    mtok_in, mtok_out = est["in_tokens"] / 1e6, est["out_tokens"] / 1e6
    rows = []
    for name, pin, pout in (("Haiku 4.5 ($1/$5)", 1.0, 5.0),
                            ("Sonnet 4.6 ($3/$15)", 3.0, 15.0)):
        rows.append(f"    {name}: ${mtok_in * pin + mtok_out * pout:.2f}")
    return "\n".join(rows)


def cmd_scan(args) -> int:
    accounts, recheck = _scope_args(args)
    conn = store.connect()
    try:
        cands = _candidates(conn, 0, accounts, recheck)
    finally:
        conn.close()
    groups = _group(cands)
    est = _estimate(groups)
    scope = f"来源 {'/'.join(SOURCES)}"
    if accounts:
        scope += f",账号 {'/'.join(accounts)}"
    if recheck:
        scope += ",含已判过的(会覆盖原判定)"
    print(f"待补判文档: {est['docs']}({scope})")
    print(f"同稿归组后实际调用: {est['calls']} 次"
          f"(省掉 {est['docs'] - est['calls']} 次重复判定)")
    print(f"预估输入 {est['in_tokens']:,} token,输出 {est['out_tokens']:,} token")
    print("预估花费:")
    print(_fmt_cost(est))
    return 0


def _judge_group(group: list[dict],
                 require_verdict: bool = False) -> tuple[list[dict], dict]:
    """判一组里的代表篇。重判时要求回包带明确判定,见 `judge_promo`。"""
    rep = group[0]
    return group, judge_promo(rep["title"], rep["body"], rep["account"],
                              require_verdict=require_verdict)


def _flag_in_vault(conn, path: str, level: str, stale: bool,
                   body_seen: str | None = None) -> None:
    """把 flag/软广 在 md frontmatter 里对齐到当前判定,跟提取流水线一个做法。

    只写 doc_tags 是留不住的:下次这篇文档内容有变动、走到 upsert_document 时,
    doc_tags 会被整表删掉再按 frontmatter 重建,补判打的标记当场消失
    (promo_level 还在,检索过滤不受影响,漂的是 obs/面板上的 tag 视图)。
    反过来,重跑把 hard 改判成 none 时那行也得摘掉,否则重索引又把它带回来。
    回写改了文件,所以同步刷新 content_hash,免得下次 index 白白重建这批文档。

    单篇回写失败(文件被删、没有 frontmatter)不该让整组判定作废——判定结果
    已经落库了,这里只是补一个展示用的标记。

    **调用它之前整组判定必须已经提交**(见 `cmd_run`):回写整段在维护锁里,
    攥着写事务等锁会把持锁的清理卡死。等不到锁时 `LockBusy` 照常抛出去、整轮
    中止 —— 那说明另一头正在做维护,这个脚本本来就按 `promo_level=''` 可重跑,
    每篇再各等 `KB_MAINT_LOCK_WAIT` 才是更坏的一种。
    """
    try:
        sync_promo_tag(conn, path, level, stale, body_seen)
    except OSError as e:
        print(f"      (同步 {path} 的 {PROMO_TAG} 失败: {e})", flush=True)


def cmd_run(args) -> int:
    conn = store.connect()
    try:
        accounts, recheck = _scope_args(args)
        cands = _candidates(conn, args.limit * 4 if args.limit else 0,
                            accounts, recheck)
        groups = _group(cands)
        if args.limit:
            groups = groups[: args.limit]
        est = _estimate(groups)
        print(f"[promo] 待判 {est['docs']} 篇 → {est['calls']} 次调用,"
              f"{WORKERS} 并发", flush=True)
        if args.dry_run:
            print("[promo] --dry-run,不实际调用")
            return 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        done = failed = hard = soft = skipped = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = [pool.submit(_judge_group, g, recheck) for g in groups]
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    group, promo = fut.result()
                except Exception as e:
                    # 重判时「回包里没有合法 level」也走到这里(judge_promo 的
                    # require_verdict):**这一篇的旧结论一个字没动**,下次重跑。
                    failed += 1
                    print(f"[{i}/{len(groups)}] FAIL {str(e)[:120]}", flush=True)
                    continue
                reason = promo["reason"]
                flags = []
                wrote = 0
                for j, c in enumerate(group):
                    p = dict(promo)
                    if j:   # 广播给同稿副本,注明结论不是它自己判的
                        p["reason"] = f"{reason}(同稿判定:{group[0]['path']})"
                    written, stale = save_promo(conn, c["path"], p, now,
                                                c["version"])
                    if not written:
                        # 读走正文到现在这一篇换版了(补抓 → 导出 → 索引)。
                        # 新那版多半已由正常提取判过,旧结论不许盖上去;如实
                        # 记成跳过,下一轮按新版重判(见 save_promo 的条件写)。
                        skipped += 1
                        print(f"[{i}/{len(groups)}] 跳过(正文已换版) "
                              f"{c['path']}", flush=True)
                        continue
                    flags.append((c["path"], p["level"], stale, c["seen"]))
                    done += 1
                    wrote += 1
                # 整组判定先落盘,**再**逐篇回写 —— 和另外三条提取路径同一个时序。
                # 回写要取 kb.maintenance 那把维护锁,而 SQLite 的写锁不认它:
                # 攥着这一组没提交的写事务去等锁,持锁的清理会卡在自己的 DELETE
                # 上,两边互等到清理报 `database is locked`(2026-10-01 第 3 轮
                # 审查实测)。共用入口自己也会 flush,这里写明顺序是为了在调用
                # 点上就看得出来。
                conn.commit()
                for path, level, stale, seen in flags:
                    _flag_in_vault(conn, path, level, stale, seen)
                conn.commit()
                # 计数按**实际写进去的**篇数,不按组的长度 —— 换版跳过的那几篇
                # 什么都没写,算进去就是报一个没发生的结果。
                if promo["level"] == "hard" and wrote:
                    hard += wrote
                    print(f"[{i}/{len(groups)}] 软广 ×{wrote} "
                          f"{group[0]['title'][:34]} — {reason[:40]}", flush=True)
                elif promo["level"] == "soft":
                    soft += wrote
                if i % 50 == 0:
                    print(f"[{i}/{len(groups)}] 已判 {done} 篇", flush=True)
        print(f"[promo] 完成: 判定 {done} 篇,hard {hard},soft {soft},"
              f"失败组 {failed},跳过 {skipped} 篇(失败和跳过的下次重跑)",
              flush=True)
    finally:
        conn.close()
    return 0


def cmd_report(args) -> int:
    conn = store.connect()
    try:
        rows = conn.execute(
            "SELECT path, title, account, published, promo_level, promo_reason"
            " FROM documents WHERE promo_level IN ('hard','soft')"
            " ORDER BY promo_level, published DESC"
        ).fetchall()
        hard = [r for r in rows if r["promo_level"] == "hard"]
        soft = [r for r in rows if r["promo_level"] == "soft"]
        done = conn.execute(
            "SELECT count(*) c FROM documents WHERE promo_level <> ''"
        ).fetchone()["c"]
        print(f"已判定 {done} 篇:hard {len(hard)},soft {len(soft)}\n")
        print(f"## 软广 hard({len(hard)})\n")
        for r in hard:
            print(f"- [{r['account']}] {r['title']}\n  {r['promo_reason']} · {r['path']}")
        if args.soft:
            print(f"\n## 疑似 soft({len(soft)})\n")
            for r in soft:
                print(f"- [{r['account']}] {r['title']}\n  {r['promo_reason']}")
    finally:
        conn.close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def _scope(p):
        p.add_argument("--account", action="append", metavar="名字",
                       help="只判这个号(可重复给)")
        p.add_argument("--recheck", action="store_true",
                       help="连已判过的一起重判(覆盖原判定,必须配 --account)")
    _scope(sub.add_parser("scan"))
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, default=0, help="只处理前 N 组(试跑用)")
    r.add_argument("--dry-run", action="store_true", help="只打印规模,不调模型")
    _scope(r)
    rp = sub.add_parser("report")
    rp.add_argument("--soft", action="store_true", help="连 soft 一档也列出来")
    args = ap.parse_args()
    return {"scan": cmd_scan, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
