#!/usr/bin/env python3
"""kb extract 的并发版:LLM 调用(kb.llm API 通道)并发跑,DB 写入在主线程串行。

与 `kb extract --writeback` 结果等价;可随时中断,断点由 extracted_at IS NULL 天然接续。
放在 Knowbase 项目里用 uv run python 跑。
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from kb import store
from kb.extract import (extract_with_llm, pending_docs, save_extraction,
                        writeback_extraction)

WORKERS = 6


def main():
    conn = store.connect()
    todo = pending_docs(conn, 0)
    print(f"[extract-p] 待提取 {len(todo)} 篇, {WORKERS} 并发", flush=True)

    # 预取正文,worker 不碰数据库
    jobs = []
    for row in todo:
        jobs.append((row["path"], row["title"], store.doc_body(conn, row["path"])))

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
