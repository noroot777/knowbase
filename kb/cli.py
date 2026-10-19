# -*- coding: utf-8 -*-
"""kb 命令行入口。

  kb index [--reembed] [--no-vector]   扫 vault → 增量索引 → 补向量
  kb extract [--limit N] [--writeback] LLM 提取 entity/topic/summary
  kb search "query" [过滤参数]          混合检索
  kb ask "意图"                         意图问答(与 web 同一条链路,带引用)
  kb stats                              索引统计
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime

from kb import store
from kb.chunker import split_chunks
from kb.scanner import ScanReport, scan_all
from kb.search import Filters, search


def cmd_index(args) -> int:
    """整趟在维护锁里:扫描读到的是那一刻磁盘上的内容,而删文件和 documents
    行是另一个进程的事(存量清理 `scripts/feed_trim_history.py`)。不互斥的话
    这一趟会拿删除**之前**读到的内容把刚清掉的文档写回去,库里留下一行指向不
    存在文件的 documents(搜得到、点开没有原文)。原委见 `kb.maintenance`。
    """
    from kb.maintenance import LockBusy, maintenance_lock

    def waiting(waited: float) -> None:
        print(f'[index] 等维护锁:另一个 kb.db 维护任务正在跑(已等 {int(waited)} 秒)',
              flush=True)

    try:
        with maintenance_lock(on_wait=waiting):
            return _index_pass(args)
    except LockBusy as e:
        print(f'[index] {e}', file=sys.stderr)
        return 2


def _index_pass(args) -> int:
    conn = store.connect()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    report = ScanReport()
    new = unchanged = 0

    from kb.extract import parse_pretagged, save_extraction

    for doc in scan_all(report=report):
        if not args.reembed and store.observed_document_is_current(conn, doc):
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
    removed = store.remove_missing_documents(conn, report)
    conn.commit()
    print(f"[index] 完成: 新增/更新 {new}, 未变 {unchanged}, 移除 {removed}{report.warning}")

    if not args.no_vector:
        try:
            from kb.embedder import embed_pending_chunks
        except Exception as e:
            print(f"[embed] 跳过: {e}")
            conn.close()
            return int(bool(report.errors))

        def _report(done: int, total: int) -> None:
            if done == 0:
                print(f"[embed] {total} 个 chunk 待编码"
                      f"(首次运行会下载 bge-m3 模型,~2GB)")
            else:
                print(f"[embed] {done}/{total}")

        if embed_pending_chunks(conn, _report) == 0:
            print("[embed] 向量已是最新")
    conn.close()
    return int(bool(report.errors))


def cmd_extract(args) -> int:
    from kb.extract import (NO_BODY_NO_TEXT, StaleBody, body_gap,
                            commit_extraction, extract_with_llm, mark_no_body,
                            model_input, pending_docs)
    from kb.llm_stages import stage_model
    conn = store.connect()
    todo = pending_docs(conn, args.limit)
    print(f"[extract] 待提取 {len(todo)} 篇 (model={stage_model('extract')})")
    ok = failed = empty = no_text = stale_body = 0
    for i, row in enumerate(todo, 1):
        # 标题、账号、正文、版本号一条语句读出来(见 extract.model_input):拆成
        # 几句的话,「这一版没有文字」那个终态会落到另一版正文上,而模型会收到
        # 一份跨版拼出来的输入。顺带这也是 commit_extraction 那道校验的取样点
        # —— 两个机制问的不是一件事(version 是"索引里哪一版文件",指纹是"我
        # 读到的那组输入长什么样"),但都该取自同一个快照。
        snap = model_input(conn, row["path"])
        version, body = snap["version"], snap["body"]
        # 空正文的两档判定与管道共用 kb.extract 那一份,别在这儿另写一套 ——
        # 两处各判各的话,命令行跑完不落终态,下一轮管道又把它报成「需回填」
        gap = body_gap(row["path"], body)
        if gap == NO_BODY_NO_TEXT:
            # 终态按判定时那一版写(条件 UPDATE):这期间别的进程把正文索引进来了
            # 就一行都不写,留给下一轮按新版重判(见 extract.mark_no_body)
            if not mark_no_body(conn, row["path"], version):
                conn.commit()
                print(f"[{i}/{len(todo)}] SKIP {row['title'][:28]}: 正文在判定"
                      f"期间更新,留待下一轮")
                continue
            conn.commit()
            no_text += 1
            print(f"[{i}/{len(todo)}] SKIP {row['title'][:28]}: 无正文可提取"
                  f"(仅图片等,已记录;正文补回来会自动重进队列)")
            continue
        if gap:
            empty += 1
            print(f"[{i}/{len(todo)}] SKIP {row['title'][:28]}: 正文为空"
                  f"(先把正文补回来再 kb index)")
            continue
        # 读到的这一组输入的指纹:落库前 save_extraction 拿它比一次,正文或
        # 标题在模型调用期间被换过(补抓把跳转壳换成全文)就丢掉这次结果。
        seen = snap["seen"]
        try:
            # **account 也要传** —— 软广判定按「这个号在卖什么」读文末那段,
            # 账号名是判据的一部分(模板见 kb.extract_llm)。三条提取路径的
            # 输入口径必须一样,不然同一篇文章在命令行和管道上判出不同结果。
            data = extract_with_llm(snap["title"], body,
                                    account=snap["account"])
            # 校验 → 落库 → 提交 → 回写整段在「维护锁 → kb.db 写锁」里:这中间
            # 没有别人能把正文换掉(见 commit_extraction)。回写失败只打一行 ——
            # 判定已经落库了,不该把这一篇记成 FAIL。
            commit_extraction(
                conn, row["path"], data, seen, writeback=args.writeback,
                on_error=lambda e, p=row["path"]: print(f"    [writeback-fail] {p}: {e}"))
            ok += 1
            ents = ", ".join(e["name"] for e in data["entities"][:4])
            print(f"[{i}/{len(todo)}] OK {row['title'][:28]} → [{ents}]")
        except StaleBody:
            # 不是失败:这一篇还在待提取集合里,下一轮按新正文重提。
            stale_body += 1
            print(f"[{i}/{len(todo)}] SKIP {row['title'][:28]}: 正文已更新,"
                  f"这次结果作废(下轮按新正文重提)")
            continue
        except Exception as e:
            failed += 1
            print(f"[{i}/{len(todo)}] FAIL {row['title'][:28]}: {e}")
            continue
    line = f"[extract] 完成: 成功 {ok}, 失败 {failed}"
    if empty:
        line += f", 跳过 {empty}(正文空)"
    if no_text:
        line += f", 无正文 {no_text}(仅图片等)"
    if stale_body:
        line += f", 跳过 {stale_body}(正文已更新)"
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
    """意图问答:直接复用 Web 端那条链路(kb.askflow.run_ask)。

    旧实现是自己 top-8 检索 + 拼 prompt 调本机 claude CLI,既吃不到 agentic/
    枚举(“都有哪些”会被 top-K 截断),也用不上设置面板里配的 API key。
    --source/--account/--tag/--entity 作为硬过滤原样传下去(和 kb search 同语义),
    run_ask 收到它就走规划管线把过滤编进 SQL,不靠提示词求模型遵守。
    """
    from kb.askflow import filter_refs_to_cited, run_ask

    f = _build_filters(args)
    hard = f if (f.source or f.account or f.tags or f.entities) else None

    conn = store.connect()
    try:
        res = run_ask(conn, args.query, model=args.model, filters=hard)
    except RuntimeError as e:
        print(f"问答失败: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    print(res["answer"].strip())
    refs = filter_refs_to_cited(res["answer"], res.get("refs") or [])
    if refs:
        print("\n参考:")
        for r in refs:
            acct = f" · {r['account']}" if r.get("account") else ""
            print(f"  [{r['n']}] {r['title']}{acct}\n      {r['path']}")
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
    p = argparse.ArgumentParser(prog="kb", description="Knowbase · 个人知识库")
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
        # ask 的过滤参数与 search 同语义(硬过滤),给了就走规划管线而非 agent
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
            sp.add_argument("--model", default=None,
                            help="缺省用设置里的默认模型(与 web 下拉一致)")
            sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("stats", help="索引统计")
    sp.set_defaults(func=cmd_stats)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
