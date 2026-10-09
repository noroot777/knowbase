#!/usr/bin/env python3
"""kb extract 的并发版:LLM 调用(kb.llm API 通道)并发跑,DB 写入在主线程串行。

与 `kb extract --writeback` 结果等价;可随时中断,断点由 extracted_at IS NULL 天然接续。
放在 Knowbase 项目里用 uv run python 跑。
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from kb import store
from kb.extract import (NO_BODY_NO_TEXT, body_gap, extract_with_llm,
                        mark_no_body, pending_docs, save_extraction,
                        writeback_extraction)

WORKERS = 6


def main():
    conn = store.connect()
    todo = pending_docs(conn, 0)
    print(f"[extract-p] 待提取 {len(todo)} 篇, {WORKERS} 并发", flush=True)

    # 预取正文,worker 不碰数据库。空正文在这儿就筛掉:拿空串去调 LLM 必然
    # 产出一份只凭标题编的判定,还要按 token 付钱。两档判定与另外两条路共用
    # kb.extract 那一份(这个脚本自称与 `kb extract --writeback` 等价)。
    jobs = []
    empty = no_text = 0
    for row in todo:
        body = store.doc_body(conn, row["path"])
        gap = body_gap(row["path"], body)
        if gap == NO_BODY_NO_TEXT:
            mark_no_body(conn, row["path"])
            no_text += 1
            continue
        if gap:
            empty += 1
            continue
        jobs.append((row["path"], row["title"], body))
    if no_text:
        conn.commit()
        print(f"[extract-p] 无正文可提取 {no_text} 篇(仅图片等,已记录)", flush=True)
    if empty:
        print(f"[extract-p] 正文为空 {empty} 篇(待回填,本轮跳过)", flush=True)

    ok = failed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(extract_with_llm, t, b): (p, t) for p, t, b in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            path, title = futs[fut]
            try:
                data = fut.result()
            except Exception as e:
                failed += 1
                print(f"[{i}/{len(jobs)}] FAIL {title[:28]}: {str(e)[:80]}", flush=True)
                continue
            stale = save_extraction(conn, path, data)
            # 先把判定落盘,再去回写 —— 和另外两条路同一个时序。回写整段要取
            # kb.maintenance 那把维护锁,攥着一个没提交的 SQLite 写事务去等它的话,
            # 持锁的清理会卡在自己的 DELETE 上(SQLite 的写锁不认那把锁),两边
            # 互相等到我们超时放弃为止。
            conn.commit()
            try:
                writeback_extraction(conn, path, data, stale)
            except Exception as e:
                print(f"    [writeback-fail] {path}: {e}", flush=True)
            conn.commit()
            ok += 1
            if i % 10 == 0 or i == len(jobs):
                print(f"[{i}/{len(jobs)}] ok={ok} fail={failed}", flush=True)

    print(f"[extract-p] 完成: 成功 {ok}, 失败 {failed}", flush=True)
    conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
