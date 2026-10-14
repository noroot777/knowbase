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
     转发、代充值广告同一个号发了 177 次。**喂给模型的那三样(账号、标题、
     正文)逐字相同**的归成一组,每组只判一篇,结论广播给同组其余篇。

判定口径改了之后,存量要跟上也走这里 —— `--recheck` 连已判过的一起重判。
它覆盖已有判定,所以必须同时用 `--account` 圈定范围(见 `_scope_args`)。

用法:
  uv run python scripts/promo_backfill.py scan            # 规模 + token/费用预估
  uv run python scripts/promo_backfill.py run [--limit N] # 实际补判
  uv run python scripts/promo_backfill.py report          # 判出来的软广清单
  uv run python scripts/promo_backfill.py sync-tags       # 修「标记没写进 md」的待办
  # 口径改了,重判某个号的存量(先 scan 看规模和花费):
  uv run python scripts/promo_backfill.py scan --account GitHub猿 --recheck
  uv run python scripts/promo_backfill.py run  --account GitHub猿 --recheck
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb import store                                            # noqa: E402
from kb.extract import (PROMO_HEAD, PROMO_TAIL, PROMO_TAG,       # noqa: E402
                        align_promo_tag, body_fingerprint, commit_promo,
                        judge_promo, pending_promo_docs,
                        promo_tag_pending_docs)

# 软广只在公众号来源里成气候。dr(英文博客/HN 汇总)与 pre_tagged 的视频系
# 不进这个补判:前者没有投放稿,后者是衍生条目,源头文章判过就够了。
SOURCES = ["dr-gzh", "gzh"]
WORKERS = 6          # 与 pipeline_stages.EXTRACT_WORKERS 同口径

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
    """待补判的文档 + 它的那组输入。正文为空的跳过(判不了,也没内容可污染)。

    **喂给模型的三样(标题、账号、正文)和它们所属的那一版 `content_hash`
    从 `store.doc_input_snapshot` 一条语句里读。** 拆成两句的话,两句之间另一
    条连接的索引就可能把这一篇换版:带回来的是旧标题配新正文、或者新版本号
    指着一份我们没读过的正文,而后面那道条件写照旧放行 —— 判的是一份没人写过
    的东西(理由同 `kb.extract.mark_no_body`)。上一轮这里是「名单一次查询、
    正文另一次查询」,缺口就在这。
    """
    rows = pending_promo_docs(conn, SOURCES, limit, accounts, recheck)
    out = []
    for r in rows:
        snap = store.doc_input_snapshot(conn, r["path"])
        if snap is None or not snap["body"].strip():
            continue
        out.append(snap)
    return out


def _group(cands: list[dict]) -> list[list[dict]]:
    """把同一份稿子的多个副本归成一组,组内只需判一次。

    **归组键是「喂给模型的那三样逐字相同」:账号 + 标题 + 正文指纹。** 组内任
    一篇的 prompt 和代表篇的 prompt 一个字节都不差,所以广播结论不是推断、
    也不需要证明「这两篇是不是同一份稿子」—— 问的是同一个问题,答案当然共用。

    原来的键是「账号 + 标题 + 正文长度桶(500 字一档)」。那是个**猜**:一个号
    把「每日资讯」当标题反复用是合法输入,广告段有没有也可能只差几十个字,于
    是两篇不同的文章落进同一个桶,长的那篇(常常正是带交易段的那篇)判出的
    hard 被广播给另一篇正常教程 —— 那篇连同它的实体一起从检索和图鉴里消失,
    而脚本报的是「软广 ×2」。反过来的组合一样坏:代表篇是较长的正常稿时,
    另一篇的已有 hard 会被盖成 none。审查用两篇同题「每日资讯」(910 字开源
    教程 + 960 字代充值广告)确定性复现过,末态是教程被隐藏、理由写着
    「同稿判定:…/group-ad.md」(2026-10-14)。

    **正文不做空白规范化、长度也不再入键。** 折叠空白能多并掉几组,但那就又
    回到「我们认为这点差异不影响判定」的猜测上了 —— 而 `clip_body` 的截断点
    会跟着空白数漂,喂进去的内容未必还相同。宁可多调几次模型:漏并一组的代价
    是一次调用(约 $0.004),并错一组的代价是一篇正常文章被永久隐藏。

    账号和标题留在键里不是为了「证明同稿」,它们本来就是 prompt 的一部分
    (判定要看「这个号在卖什么」),逐字相同才谈得上共用答案。
    """
    buckets: dict[tuple, list[dict]] = {}
    for c in cands:
        key = (c["account"], c["title"], body_fingerprint(c["body"]))
        buckets.setdefault(key, []).append(c)
    # 组内顺序只决定谁当代表、谁的 path 写进广播理由里;正文逐字相同,取谁都
    # 判得出同一个结论。按 path 定序是为了让同一批输入每次跑出同样的归属。
    return [sorted(g, key=lambda c: c["path"]) for g in buckets.values()]


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


def _deliver(conn, cand: dict, promo: dict) -> tuple[str, bool]:
    """交付一篇的判定 —— 落库、`doc_tags`、md 那行,整段经 `commit_promo`。
    返回 `(为什么什么都没写, md 那行对齐了没有)`,三档的含义见 `commit_promo`。

    只写 doc_tags 是留不住的:下次这篇文档内容有变动、走到 upsert_document 时,
    doc_tags 会被整表删掉再按 frontmatter 重建,补判打的标记当场消失
    (promo_level 还在,检索过滤不受影响,漂的是 obs/面板上的 tag 视图)。
    反过来,重跑把 hard 改判成 none 时那行也得摘掉,否则重索引又把它带回来。
    回写改了文件,所以同步刷新 content_hash,免得下次 index 白白重建这批文档。

    **这三件事必须在同一个协调区间里成立,所以整段挪进了 `commit_promo`**:
    原来落库在锁外、只有摘 md 那一下在锁内,中间插进一次合法的索引就会按还
    没摘的 frontmatter 把标记重新插回 `doc_tags`,而且之后再也不会自己纠正
    (完整形态见 `kb.extract_promo.commit_promo`)。

    单篇**回写**失败(文件被删、没权限、frontmatter 被重排成摘不掉的写法)不该
    让整组判定作废——判定结果已经落库了。但它**也不是一次成功的交付**:库里
    是 none、md 里那行还挂着,而索引下一轮会按 frontmatter 把它读回
    `doc_tags`(那一侧由 `store._align_promo_tag` 挡住)。所以 `commit_promo`
    给这一档留一条持久化待办、返回 `("", False)`,这里只负责**如实打印和
    计数** —— 判什么算「交付失败」是交付层的口径,脚本不放宽也不收紧。

    等不到维护锁时 `LockBusy` 照常抛、整轮中止 —— 那说明另一头正在做维护,
    每篇再各等 `KB_MAINT_LOCK_WAIT` 才是更坏的一种。**这一档是干净的**:取锁
    在落库之前,等不到锁就一个字都没写,下一轮按 `promo_level=''` 照旧捞得到
    这一篇;原先取锁在落库之后,`LockBusy` 时判定已经落了库、`promo_level`
    不再是空的,那一篇从此就掉出默认候选了。
    """
    def note(e):
        print(f"      (同步 {cand['path']} 的 {PROMO_TAG} 失败: {e};"
              f" 已记待办,跑 sync-tags 修)", flush=True)

    return commit_promo(conn, cand["path"], promo, cand["version"],
                        body_fingerprint(cand["body"]), on_error=note)


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
        done = failed = hard = soft = skipped = unsynced = 0
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
                wrote = 0
                for j, c in enumerate(group):
                    p = dict(promo)
                    if j:   # 广播给同稿副本,注明结论不是它自己判的
                        p["reason"] = f"{reason}(同稿判定:{group[0]['path']})"
                    # 逐篇一个协调区间:落库、doc_tags、md 那行在同一把维护锁
                    # 里对齐。粒度按篇和另外三条提取路径一致 —— 整趟几千篇要
                    # 跑几十分钟,圈起来等于攥着锁等几十次模型往返。
                    why, synced = _deliver(conn, c, p)
                    if why:
                        # 什么都没写:正文换版了(新那版多半已由正常提取判过,
                        # 旧结论不许盖上去)、磁盘上这一篇还没进索引,或者等锁
                        # 期间被清理掉了。如实记成跳过,下一轮重判一次就对了。
                        skipped += 1
                        print(f"[{i}/{len(groups)}] 跳过({why}) {c['path']}",
                              flush=True)
                        continue
                    done += 1
                    wrote += 1
                    if not synced:
                        # 判定落库了、md 那行没对齐(待办已记)。**和成功分开
                        # 报** —— 混在 done 里等于说末态是一致的。
                        unsynced += 1
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
        if unsynced:
            print(f"[promo] 其中 {unsynced} 篇的 {PROMO_TAG} 没写进 md"
                  f"(判定已落库):跑 `sync-tags` 补,不花钱", flush=True)
    finally:
        conn.close()
    # 全组都失败时退出码要能看出来:自动化按 0 判成功,而「一篇都没判成」和
    # 「判完了」是两件事(模型挂了、配额用尽、口径里的 JSON 约定被改坏都长这样)。
    return 1 if failed and not done else 0


def cmd_sync_tags(args) -> int:
    """把留着待办的那几篇的 md 那行 flag 重新对齐到库里的判定。**不调模型。**

    交付跨两个存储(kb.db 的事务管不着 vault 文件),第二步失败时
    `writeback_extraction` 会留下 `promo_tag_pending`。这条命令就是那条待办的
    出口:目标状态是库里那份判定,所以**不必再问一次模型、也不花钱**;
    `align_promo_tag` 按目标状态幂等对齐,对上了就把待办清掉。

    仍然对齐不了的(文件被删、frontmatter 被重排成按行删不掉的 flow-style)
    如实记成失败、待办留在库里 —— 不许清掉一条没修好的待办。
    """
    from kb.maintenance import maintenance_lock

    accounts, _ = _scope_args(args)
    conn = store.connect()
    try:
        paths = promo_tag_pending_docs(conn, accounts)
        print(f"[promo] 待对齐 {len(paths)} 篇", flush=True)
        fixed = failed = 0
        for path in paths:
            try:
                # 一篇一把维护锁:改的是 vault 文件 + kb.db,口径同回写那条路
                # (`docs/decisions/maintenance-lock.md`)。取锁之前先 flush ——
                # 锁序是维护锁 → SQLite 写锁,攥着没提交的写事务去等维护锁,
                # 持锁的清理会卡在它自己的 DELETE 上。
                if conn.in_transaction:
                    conn.commit()
                with maintenance_lock():
                    align_promo_tag(conn, path)
                fixed += 1
            except Exception as e:
                failed += 1
                print(f"  仍对不齐 {path}: {str(e)[:120]}", flush=True)
        print(f"[promo] 完成: 对齐 {fixed} 篇,仍失败 {failed} 篇", flush=True)
    finally:
        conn.close()
    return 1 if failed else 0


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
    # sync-tags 不调模型、不覆盖判定,所以只收 --account 这一个范围参数
    sub.add_parser("sync-tags").add_argument(
        "--account", action="append", metavar="名字", help="只修这个号的")
    args = ap.parse_args()
    return {"scan": cmd_scan, "run": cmd_run, "report": cmd_report,
            "sync-tags": cmd_sync_tags}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
