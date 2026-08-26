#!/usr/bin/env python3
"""kb extract 的并发版:LLM 调用(kb.llm API 通道)并发跑,DB 写入在主线程串行。

与 `kb extract --writeback` 结果等价;可随时中断,断点由 extracted_at IS NULL 天然接续。
放在 knowledge-base 项目里用 uv run python 跑。
"""
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

from kb import store
from kb.extract import (_tag_slug, extract_with_llm, pending_docs,
                        refresh_content_hash, save_extraction, writeback_tags)

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
            save_extraction(conn, path, data)
            tags = [f"topic/{_tag_slug(t)}" for t in data["topics"]] + \
                   [f"entity/{_tag_slug(e['name'])}" for e in data["entities"] if e.get("name")]
            try:
                if writeback_tags(path, tags):
                    refresh_content_hash(conn, path)
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
