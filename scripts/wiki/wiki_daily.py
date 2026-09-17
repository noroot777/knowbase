#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日 wiki 同步(kb-web 管道第五阶段"wiki";也可手动跑)。

顺序:
1. linkify   已提取、未打标记的 dr-gzh 文章正文双链化(含历史导入,纯机械,零 LLM)
2. pages     全量重渲染实体页/主题页(幂等,保留「近期动态」marker 区)
3. index     重渲染 index.md
4. digest    最近 N 天 ∩ >= EPOCH,无摘要或有新文章的日期(LLM,~1 次/天)
5. synthesis 14 天窗口 >=3 篇且(无综合 或 新增 >=3 篇)的实体(LLM,增量)
6. graph     重生成 graph.html(近 14 天窗口)

历史导入保护:步骤 1/2 机械操作,欢迎全量;4/5 有日期门,老日期永不触发 LLM。
--skip-linkify:kb 管道 extract 正在跑时只做读 DB 的部分,不写 source 文件。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))          # repo 根,import kb 用

import wikilib as W  # noqa: E402


def step_linkify(u: W.Universe) -> int:
    from kb import store
    from kb.extract import refresh_content_hash

    changed = []
    for p in u.docs:
        new_text = W.linkify_doc(u, p)
        if new_text is None:
            continue
        (W.VAULT / p).write_text(new_text, encoding="utf-8")
        changed.append(p)
    if changed:
        conn = store.connect()
        try:
            for p in changed:
                refresh_content_hash(conn, p)
            conn.commit()
        finally:
            conn.close()
    return len(changed)


def step_pages(u: W.Universe) -> tuple[int, int]:
    for sub in set(W.KIND_DIR.values()) | {"concepts", "topics", "digest", "notes"}:
        (W.VAULT / "wiki" / sub).mkdir(parents=True, exist_ok=True)
    n_e = 0
    for k in u.page_keys:
        sub, name = u.page_file[k]
        page = W.VAULT / "wiki" / sub / f"{name}.md"
        existing = page.read_text(encoding="utf-8") if page.exists() else None
        text = W.render_entity_page(u, k, existing)
        if existing != text:
            page.write_text(text, encoding="utf-8")
            n_e += 1
    n_t = 0
    for t, fname in u.topic_file.items():
        page = W.VAULT / "wiki" / "topics" / f"{fname}.md"
        text = W.render_topic_page(u, t)
        if not page.exists() or page.read_text(encoding="utf-8") != text:
            page.write_text(text, encoding="utf-8")
            n_t += 1
    return n_e, n_t


def step_digest(con, recent_days: int, model) -> list[str]:
    import wiki_digest as D
    made = []
    today = date.today()
    for i in range(recent_days + 1):
        dt = (today - timedelta(days=i)).isoformat()
        if dt < W.EPOCH:
            continue
        n_docs = con.execute(
            "SELECT COUNT(*) FROM documents WHERE source=? AND published LIKE ? "
            "AND extracted_at IS NOT NULL", (W.SOURCE, dt + "%")).fetchone()[0]
        if n_docs == 0:
            continue
        page = W.VAULT / "wiki" / "digest" / f"{dt}.md"
        if page.exists():
            import re
            m = re.search(r"^docs: (\d+)$", page.read_text(encoding="utf-8"), re.M)
            if m and int(m.group(1)) >= n_docs:
                continue          # 无新文章,不重做
        if D.build_digest(con, W.VAULT, dt, model):
            made.append(dt)
    return made


def step_synthesis(u: W.Universe, model, workers: int) -> list[str]:
    import wiki_entity_synthesis as S
    targets = S.select_targets(u, window_days=14, min_docs=3, growth=3)
    return S.run_targets(u, targets, model=model, workers=workers)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent-days", type=int, default=2, help="digest 回看天数")
    ap.add_argument("--model", default=None, help="临时覆盖模型 id;默认用设置面板的「管道 · wiki 摘要与综述」阶段")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--skip-linkify", action="store_true")
    ap.add_argument("--skip-llm", action="store_true", help="只做机械部分(1/2/3/6)")
    a = ap.parse_args()

    con = W.connect_ro()
    u = W.Universe(con)
    print(f"universe: {len(u.docs)} 篇已提取文章,{len(u.page_keys)} 个实体页")

    if a.skip_linkify:
        print("linkify: 跳过(--skip-linkify)")
        n_link = 0
    else:
        n_link = step_linkify(u)
        print(f"linkify: 新双链化 {n_link} 篇")

    n_e, n_t = step_pages(u)
    print(f"pages: 实体页更新 {n_e},主题页更新 {n_t}")

    made, syn = [], []
    if not a.skip_llm:
        model = a.model or W.llm_model()   # 整轮定一次,digest 和 synthesis 用同一个
        print(f"llm: 本轮用 {model}")
        made = step_digest(con, a.recent_days, model)
        print(f"digest: 生成 {len(made)} 天 {made}")
        syn = step_synthesis(u, model, a.workers)
        print(f"synthesis: 更新 {len(syn)} 个实体 {syn[:8]}")

    (W.VAULT / "index.md").write_text(W.render_index(u, W.VAULT), encoding="utf-8")
    print("index: 已重渲染")

    since = (date.today() - timedelta(days=14)).isoformat()
    r = subprocess.run([sys.executable, str(HERE / "build_graph_html.py"),
                        "--since", since],
                       capture_output=True, text=True, timeout=300)
    print(f"graph: {'ok' if r.returncode == 0 else 'FAIL ' + r.stderr[:200]}")

    if n_link or made or syn:
        W.append_log(W.VAULT, "wiki",
                     f"双链化 {n_link} 篇,digest {len(made)} 天,综合 {len(syn)} 实体")
    print("WIKI_DAILY_DONE")


if __name__ == "__main__":
    main()
