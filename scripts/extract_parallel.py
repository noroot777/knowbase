#!/usr/bin/env python3
"""kb extract 的并发版:LLM 调用(kb.llm API 通道)并发跑,DB 写入在主线程串行。

与 `kb extract --writeback` 结果等价;可随时中断,断点由 extracted_at IS NULL 天然接续。
放在 Knowbase 项目里用 uv run python 跑。
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from kb import store
from kb.extract import (StaleBody, body_fingerprint, commit_extraction,
                        extract_with_llm, pending_docs)

WORKERS = 6


def main():
    conn = store.connect()
    todo = pending_docs(conn, 0)
    print(f"[extract-p] 待提取 {len(todo)} 篇, {WORKERS} 并发", flush=True)

    # 预取正文,worker 不碰数据库。连指纹一起记下:这一趟几千篇要跑几十分钟,
    # 期间某一篇的正文完全可能被整篇换掉(补抓把跳转壳换成全文),落库前比一次。
    jobs = []
    for row in todo:
        body = store.doc_body(conn, row["path"])
        jobs.append((row["path"], row["title"], body, body_fingerprint(body)))

    ok = failed = stale_body = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(extract_with_llm, t, b): (p, t, seen)
                for p, t, b, seen in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            path, title, seen = futs[fut]
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
