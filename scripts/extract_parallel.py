#!/usr/bin/env python3
"""kb extract 的并发版:LLM 调用(kb.llm API 通道)并发跑,DB 写入在主线程串行。

与 `kb extract --writeback` 结果等价;可随时中断,断点由 extracted_at IS NULL 天然接续。
放在 Knowbase 项目里用 uv run python 跑。
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from kb import store
from kb.extract import (NO_BODY_NO_TEXT, StaleBody, body_gap,
                        commit_extraction, extract_with_llm, mark_no_body,
                        model_input, pending_docs)

WORKERS = 6


def main():
    conn = store.connect()
    todo = pending_docs(conn, 0)
    print(f"[extract-p] 待提取 {len(todo)} 篇, {WORKERS} 并发", flush=True)

    # 预取模型输入,worker 不碰数据库。空正文在这儿就筛掉:拿空串去调 LLM 必然
    # 产出一份只凭标题编的判定,还要按 token 付钱。两档判定与另外两条路共用
    # kb.extract 那一份(这个脚本自称与 `kb extract --writeback` 等价)。
    #
    # **标题、账号、正文、版本号四样取自同一个快照**(model_input 一条语句),
    # 回答的不是一件事:版本号给 mark_no_body 做条件 UPDATE,指纹给
    # commit_extraction 比对 ——这一趟几千篇要跑几十分钟,期间某一篇完全可能被
    # 整篇换掉(补抓把跳转壳换成全文),落库前拿它比一次。拆成几句读就会把
    # 旧标题配上新正文喂给模型(见 extract.input_fingerprint)。
    jobs = []
    empty = no_text = tried = 0
    for row in todo:
        snap = model_input(conn, row["path"])
        gap = body_gap(row["path"], snap["body"])
        if gap == NO_BODY_NO_TEXT:
            # 条件 UPDATE:判定期间内容换版了就一行都不写(见 mark_no_body)
            tried += 1
            if mark_no_body(conn, row["path"], snap["version"]):
                no_text += 1
            continue
        if gap:
            empty += 1
            continue
        jobs.append(snap)
    if tried:
        # 0 行的那次 UPDATE 也把事务开着了,所以按「试过几次」提交、不按
        # 「写进去几条」:后面的 tag 回写要先取维护锁,攥着没提交的写事务
        # 去等锁会把持锁那边卡死(见 `docs/decisions/maintenance-lock.md` 锁序那条)
        conn.commit()
    if no_text:
        print(f"[extract-p] 无正文可提取 {no_text} 篇(仅图片等,已记录)", flush=True)
    if empty:
        print(f"[extract-p] 正文为空 {empty} 篇(待回填,本轮跳过)", flush=True)

    ok = failed = stale_body = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        # **account 也要传** —— 软广判定按「这个号在卖什么」读文末那段,账号名
        # 是判据的一部分(模板见 kb.extract_llm)。三条提取路径的输入口径必须
        # 一样,不然同一篇文章在这个脚本和管道上判出不同结果。
        futs = {pool.submit(extract_with_llm, s["title"], s["body"],
                            account=s["account"]): s
                for s in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            snap = futs[fut]
            path, title, seen = snap["path"], snap["title"], snap["seen"]
            try:
                data = fut.result()
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(jobs)}] FAIL {title[:28]}: {str(e)[:80]}", flush=True)
                continue
            try:
                # 校验 → 落库 → 提交 → 回写整段在「维护锁 → kb.db 写锁」里,
                # 和另外两条路同一个入口:这中间没有别人能把正文换掉,而攥着一个
                # 没提交的写事务去等维护锁会把持锁的清理卡死在它自己的 DELETE 上
                # (SQLite 的写锁不认那把锁)。见 extract.commit_extraction。
                commit_extraction(
                    conn, path, data, seen,
                    on_error=lambda e, p=path: print(f"    [writeback-fail] {p}: {e}",
                                                     flush=True))
            except StaleBody:
                # 不是失败:这一篇还在待提取集合里,下一轮按新正文重提。
                stale_body += 1
                print(f"[{i}/{len(jobs)}] SKIP {title[:28]}: 正文已更新,这次结果作废",
                      flush=True)
                continue
            ok += 1
            if i % 10 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] ok={ok} fail={failed}", flush=True)

    line = f"[extract-p] 完成: 成功 {ok}, 失败 {failed}"
    if stale_body:
        line += f", 跳过 {stale_body}(正文已更新)"
    print(line, flush=True)
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
