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
     转发、代充值广告同一个号发了 177 次。按「标题 + 正文长度桶」归组,每组
     只判一篇,结论广播给同组其余篇,省掉约四分之一的调用。

用法:
  uv run python scripts/promo_backfill.py scan            # 规模 + token/费用预估
  uv run python scripts/promo_backfill.py run [--limit N] # 实际补判
  uv run python scripts/promo_backfill.py report          # 判出来的软广清单
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
                        judge_promo, pending_promo_docs,
                        save_promo, sync_promo_tag)

# 软广只在公众号来源里成气候。dr(英文博客/HN 汇总)与 pre_tagged 的视频系
# 不进这个补判:前者没有投放稿,后者是衍生条目,源头文章判过就够了。
SOURCES = ["dr-gzh", "gzh"]
WORKERS = 6          # 与 pipeline_stages.EXTRACT_WORKERS 同口径
LENGTH_BUCKET = 500  # 正文长度按这个粒度分桶,同标题不同长度的稿子分开判

# 估算用的两个常量,2026-09-16 拿 50 篇真实文章实测校准过:
# 单次调用输入平均 2696 token,其中喂进去的正文平均 1929 字符 —— 中文正文
# 基本是 1 字符 1 token,剩下的 ~770 token 是 prompt 模板本身(判定口径那几段)。
CHARS_PER_TOKEN = 1.0
PROMPT_OVERHEAD = 770
OUTPUT_TOKENS = 95   # 实测平均,一个 {"level","reason"} 小 JSON


def _candidates(conn, limit: int = 0) -> list[dict]:
    """待补判的文档 + 正文。正文为空的跳过(判不了,也没内容可污染)。"""
    rows = pending_promo_docs(conn, SOURCES, limit)
    out = []
    for r in rows:
        body = store.doc_body(conn, r["path"])
        if not body.strip():
            continue
        out.append({"path": r["path"], "title": r["title"],
                    "account": r["account"], "body": body})
    return out


def _group(cands: list[dict]) -> list[list[dict]]:
    """把同一份稿子的多个副本归成一组,组内只需判一次。

    归组键是「标题 + 正文长度桶」而不是光看标题:同名但长度差一截的多半是
    两篇不同的文章(「量子位编辑作者招聘」这种通用标题尤其容易撞),归错组
    会把一篇的结论安到另一篇头上。长度桶把它们分开,代价只是多判几次。
    """
    buckets: dict[tuple, list[dict]] = {}
    for c in cands:
        key = (c["title"], len(c["body"]) // LENGTH_BUCKET)
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
    conn = store.connect()
    try:
        cands = _candidates(conn)
    finally:
        conn.close()
    groups = _group(cands)
    est = _estimate(groups)
    print(f"待补判文档: {est['docs']}(来源 {'/'.join(SOURCES)})")
    print(f"同稿归组后实际调用: {est['calls']} 次"
          f"(省掉 {est['docs'] - est['calls']} 次重复判定)")
    print(f"预估输入 {est['in_tokens']:,} token,输出 {est['out_tokens']:,} token")
    print("预估花费:")
    print(_fmt_cost(est))
    return 0


def _judge_group(group: list[dict]) -> tuple[list[dict], dict]:
    rep = group[0]
    return group, judge_promo(rep["title"], rep["body"], rep["account"])


def _flag_in_vault(conn, path: str, level: str, stale: bool) -> None:
    """把 flag/软广 在 md frontmatter 里对齐到当前判定,跟提取流水线一个做法。

    只写 doc_tags 是留不住的:下次这篇文档内容有变动、走到 upsert_document 时,
    doc_tags 会被整表删掉再按 frontmatter 重建,补判打的标记当场消失
    (promo_level 还在,检索过滤不受影响,漂的是 obs/面板上的 tag 视图)。
    反过来,重跑把 hard 改判成 none 时那行也得摘掉,否则重索引又把它带回来。
    回写改了文件,所以同步刷新 content_hash,免得下次 index 白白重建这批文档。

    单篇回写失败(文件被删、没有 frontmatter)不该让整组判定作废——判定结果
    已经落库了,这里只是补一个展示用的标记。
    """
    try:
        sync_promo_tag(conn, path, level, stale)
    except OSError as e:
        print(f"      (同步 {path} 的 {PROMO_TAG} 失败: {e})", flush=True)


def cmd_run(args) -> int:
    conn = store.connect()
    try:
        cands = _candidates(conn, args.limit * 4 if args.limit else 0)
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
        done = failed = hard = soft = 0
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = [pool.submit(_judge_group, g) for g in groups]
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    group, promo = fut.result()
                except Exception as e:
                    failed += 1
                    print(f"[{i}/{len(groups)}] FAIL {str(e)[:120]}", flush=True)
                    continue
                reason = promo["reason"]
                for j, c in enumerate(group):
                    p = dict(promo)
                    if j:   # 广播给同稿副本,注明结论不是它自己判的
                        p["reason"] = f"{reason}(同稿判定:{group[0]['path']})"
                    stale = save_promo(conn, c["path"], p, now)
                    _flag_in_vault(conn, c["path"], p["level"], stale)
                    done += 1
                conn.commit()
                if promo["level"] == "hard":
                    hard += len(group)
                    print(f"[{i}/{len(groups)}] 软广 ×{len(group)} "
                          f"{group[0]['title'][:34]} — {reason[:40]}", flush=True)
                elif promo["level"] == "soft":
                    soft += len(group)
                if i % 50 == 0:
                    print(f"[{i}/{len(groups)}] 已判 {done} 篇", flush=True)
        print(f"[promo] 完成: 判定 {done} 篇,hard {hard},soft {soft},"
              f"失败组 {failed}(失败的下次重跑)", flush=True)
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
    sub.add_parser("scan")
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int, default=0, help="只处理前 N 组(试跑用)")
    r.add_argument("--dry-run", action="store_true", help="只打印规模,不调模型")
    rp = sub.add_parser("report")
    rp.add_argument("--soft", action="store_true", help="连 soft 一档也列出来")
    args = ap.parse_args()
    return {"scan": cmd_scan, "run": cmd_run, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
