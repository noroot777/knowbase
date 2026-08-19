# -*- coding: utf-8 -*-
"""kb 命令行入口。

  kb index [--reembed] [--no-vector]   扫 vault → 增量索引 → 补向量
  kb extract [--limit N] [--writeback] LLM 提取 entity/topic/summary
  kb search "query" [过滤参数]          混合检索
  kb ask "意图"                         检索 + Claude 综合回答(带引用)
  kb stats                              索引统计
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime

from kb import store
from kb.chunker import split_chunks
from kb.scanner import scan_all
from kb.search import Filters, search


def cmd_index(args) -> int:
    conn = store.connect()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    seen: set[str] = set()
    new = unchanged = 0

    from kb.extract import parse_pretagged, save_extraction

    for doc in scan_all():
        seen.add(doc.path)
        if not args.reembed and not store.doc_needs_reindex(conn, doc.path, doc.content_hash):
            unchanged += 1
            continue
        chunks = split_chunks(doc.body)
        store.upsert_document(conn, doc, chunks, now)
        # pre_tagged 来源(youtube)的 entity/topic 顺手入库,不占 LLM 额度
        pre = parse_pretagged(doc)
        if pre is not None:
            save_extraction(conn, doc.path, pre)
        new += 1
        if new % 50 == 0:
            conn.commit()
            print(f"[index] {new} 篇...")
    removed = store.remove_missing_documents(conn, seen)
    conn.commit()
    print(f"[index] 完成: 新增/更新 {new}, 未变 {unchanged}, 移除 {removed}")

    if not args.no_vector:
        rows = store.chunks_without_vectors(conn)
        if rows:
            print(f"[embed] {len(rows)} 个 chunk 待编码(首次运行会下载 bge-m3 模型,~2GB)")
            try:
                from kb.embedder import embed_texts
            except Exception as e:
                print(f"[embed] 跳过: {e}")
                return 0
            B = 128
            for i in range(0, len(rows), B):
                batch = rows[i:i + B]
                vecs = embed_texts([r["text"] for r in batch])
                store.insert_vectors(conn, [r["id"] for r in batch], vecs)
                conn.commit()
                print(f"[embed] {min(i + B, len(rows))}/{len(rows)}")
        else:
            print("[embed] 向量已是最新")
    conn.close()
    return 0


def cmd_extract(args) -> int:
    from kb.extract import (extract_with_claude, pending_docs,
                            save_extraction, writeback_tags)
    conn = store.connect()
    todo = pending_docs(conn, args.limit)
    print(f"[extract] 待提取 {len(todo)} 篇 (model=haiku)")
    ok = failed = empty = 0
    for i, row in enumerate(todo, 1):
        body_row = conn.execute(
            "SELECT group_concat(text, char(10)) AS body FROM chunks WHERE path=? ORDER BY seq",
            (row["path"],),
        ).fetchone()
        body = body_row["body"] or ""
        if not body.strip():
            empty += 1
            print(f"[{i}/{len(todo)}] SKIP {row['title'][:28]}: 正文为空"
                  f"(先跑 gzh_refill_body.py 回填,再 kb index)")
            continue
        try:
            data = extract_with_claude(row["title"], body)
            save_extraction(conn, row["path"], data)
            conn.commit()
            ok += 1
            ents = ", ".join(e["name"] for e in data["entities"][:4])
            print(f"[{i}/{len(todo)}] OK {row['title'][:28]} → [{ents}]")
            if args.writeback:
                tags = [f"topic/{t}" for t in data["topics"]] + \
                       [f"entity/{e['name']}" for e in data["entities"]]
                from kb.extract import _tag_slug
                tags = [t.split("/")[0] + "/" + _tag_slug(t.split("/", 1)[1]) for t in tags]
                writeback_tags(row["path"], tags)
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(todo)}] FAIL {row['title'][:28]}: {e}")
    line = f"[extract] 完成: 成功 {ok}, 失败 {failed}"
    if empty:
        line += f", 跳过 {empty}(正文空)"
    print(line)
    conn.close()
    return 0


def _build_filters(args) -> Filters:
    return Filters(
        source=args.source,
        account=args.account,
        tags=args.tag or [],
        entities=args.entity or [],
    )


def cmd_search(args) -> int:
    conn = store.connect()
    hits = search(conn, args.query, _build_filters(args),
                  top_k=args.top, use_vector=not args.no_vector)
    if args.json:
        print(json.dumps([h.__dict__ for h in hits], ensure_ascii=False, indent=1))
        conn.close()
        return 0
    if not hits:
        print("(没有结果)")
        conn.close()
        return 0
    for i, h in enumerate(hits, 1):
        legs = "+".join(h.matched_by)
        print(f"\n{i}. {h.title}")
        print(f"   {h.account} · {h.published[:10]} · {h.source} · [{legs}]")
        summary_row = conn.execute(
            "SELECT summary FROM documents WHERE path=?", (h.path,)
        ).fetchone()
        if summary_row and summary_row["summary"]:
            print(f"   摘要: {summary_row['summary']}")
        snippet = h.best_chunk[:150].replace("\n", " ")
        print(f"   {snippet}...")
        print(f"   {h.path}")
    conn.close()
    return 0


def cmd_ask(args) -> int:
    """意图问答:检索 top-N → 塞给 Claude 综合回答,强制引用来源。"""
    conn = store.connect()
    hits = search(conn, args.query, _build_filters(args), top_k=8)
    if not hits:
        print("(知识库里没有相关内容)")
        conn.close()
        return 0
    ctx_parts = []
    for i, h in enumerate(hits, 1):
        full = conn.execute(
            "SELECT group_concat(text, char(10)) AS body FROM chunks WHERE path=?",
            (h.path,),
        ).fetchone()
        body = (full["body"] or "")[:2500]
        ctx_parts.append(
            f"[{i}] {h.title} ({h.account}, {h.published[:10]})\n路径: {h.path}\n{body}"
        )
    prompt = (
        "根据下面知识库文章回答用户问题。要求:\n"
        "- 只基于给出的文章,不要编造;文章里没有就说没有\n"
        "- 引用时标注 [编号],结尾列出用到的文章标题和路径\n\n"
        f"用户问题: {args.query}\n\n" + "\n\n---\n\n".join(ctx_parts)
    )
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", args.model],
        capture_output=True, text=True, timeout=180,
    )
    print(proc.stdout if proc.returncode == 0 else f"claude 调用失败: {proc.stderr[:300]}")
    conn.close()
    return 0


def cmd_stats(args) -> int:
    conn = store.connect()
    q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    print(f"文档:   {q('SELECT COUNT(*) FROM documents')}")
    for r in conn.execute(
        "SELECT source, COUNT(*) c FROM documents GROUP BY source ORDER BY c DESC"
    ):
        print(f"  {r['source']}: {r['c']}")
    print(f"chunks: {q('SELECT COUNT(*) FROM chunks')}")
    print(f"向量:   {q('SELECT COUNT(*) FROM chunk_vec')}")
    print(f"实体:   {q('SELECT COUNT(DISTINCT norm) FROM entities')}")
    print(f"已提取: {q('SELECT COUNT(*) FROM documents WHERE extracted_at IS NOT NULL')}"
          f" / 待提取: {len(__import__('kb.extract', fromlist=['pending_docs']).pending_docs(conn))}")
    conn.close()
    return 0


def main() -> int:
    p = argparse.ArgumentParser(prog="kb", description="个人知识库")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("index", help="扫 vault 建/更新索引")
    sp.add_argument("--reembed", action="store_true", help="强制全量重建(换模型后用)")
    sp.add_argument("--no-vector", action="store_true", help="只建 FTS,不算向量")
    sp.set_defaults(func=cmd_index)

    sp = sub.add_parser("extract", help="LLM 提取 entity/topic/summary")
    sp.add_argument("--limit", type=int, default=0)
    sp.add_argument("--writeback", action="store_true",
                    help="把提取的 tag 回写到 obs md frontmatter")
    sp.set_defaults(func=cmd_extract)

    for name in ("search", "ask"):
        sp = sub.add_parser(name, help="混合检索" if name == "search" else "意图问答")
        sp.add_argument("query")
        sp.add_argument("--source", default=None)
        sp.add_argument("--account", default=None)
        sp.add_argument("--tag", action="append", help="可多次,AND 关系(前缀匹配)")
        sp.add_argument("--entity", action="append")
        if name == "search":
            sp.add_argument("--top", type=int, default=10)
            sp.add_argument("--no-vector", action="store_true")
            sp.add_argument("--json", action="store_true")
            sp.set_defaults(func=cmd_search)
        else:
            sp.add_argument("--model", default="sonnet")
            sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("stats", help="索引统计")
    sp.set_defaults(func=cmd_stats)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
