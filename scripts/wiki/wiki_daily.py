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
7. catgraph  重生成 graph-projects.html / graph-skills.html(全量;新条目才判归类)

历史导入保护:步骤 1/2 机械操作,欢迎全量;4/5 有日期门,老日期永不触发 LLM。
--skip-linkify:kb 管道 extract 正在跑时只做读 DB 的部分,不写 source 文件。
6/7 任一失败都会让本脚本非零退出(前面的步骤照常完成),管道那头才看得见红态。
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
    """逐篇走协调入口双链化:读 → 装饰 → 写回 → 登记已索引,每篇一把维护锁。

    **这一步写的是 vault 里的文章文件,所以它必须认那把维护锁**(判据见
    `docs/decisions/maintenance-lock.md`:按「写不写 vault 里的文章文件 + 写不写
    kb.db」问)。原来它整段在锁外,而且是**两份快照**:`linkify_doc` 自己读一次
    算新文本,外面再读一次算 `before` —— 两次读之间别人的一次写入就让 `before`
    指向一份我们压根没变换过的内容,随后那次刷新把一份**未索引**的新正文认证成
    「已索引」。末态实测过:磁盘上是新正文、chunks 还是旧的,新正文一个字都搜
    不到,点开文章却是新的,再同步也不恢复(2026-10-14 审查跨进程复现)。

    粒度是**每篇各取一次**,不是整趟:整趟圈起来等于攥着锁把 vault 里每一篇都
    读一遍,而管道的同步和索引都在等同一把锁。代价是每篇多一次取放锁,实测
    9000 次共 311 ms —— 这一趟本来就要把这 9000 篇各读一遍。

    **等不到锁只收住这一步,不许带崩整趟 wiki**:`LockBusy` 的语义是「这一篇一个
    字节都没动」,而双链化纯机械、下一轮照旧补得回来;后面的 pages / index / 两张
    图谱压根不碰这把锁,因为 linkify 等不到锁就一张图都不重建是赔本的。所以这里
    `break` 出去照常往下走 —— 这一步取锁本身是这次新加的,不收住的话等于给整条
    wiki 阶段新开了一个失败口子。
    """
    from kb import store
    from kb.doc_rewrite import rewriting
    from kb.maintenance import LockBusy

    changed = stale = 0
    conn = store.connect()
    try:
        for p in u.docs:
            try:
                with rewriting(conn, p) as doc:
                    if doc.text is None:
                        continue          # 这一篇已经不在了,或者读不出来
                    new_text = W.linkify_text(u, p, doc.text)
                    if new_text is None:
                        continue          # 已经双链化过了
                    doc.write(new_text)
            except LockBusy as e:
                print(f"linkify: 等不到维护锁,这一步到此为止({e})")
                break
            if doc.changed:
                changed += 1
                stale += 0 if doc.indexed else 1
    finally:
        conn.close()
    if stale:
        # 登记不上不是失败,正是想要的末态:这几篇继续带着「待索引」,下一轮索引
        # 重建一次就对了。出声是为了让「磁盘长期领先于索引」看得见。
        print(f"linkify: {stale} 篇改完没登记已索引(磁盘领先于索引),留待下一轮重建")
    return changed


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


def run_step(label: str, cmd: list[str], timeout: int) -> str:
    """跑一个图谱子进程,成功返回空串,失败返回一句原因(调用方据此非零退出)。

    子步骤失败**必须带着非零退出码传上去**:管道那头的 `_run_wiki()` 只看
    wiki_daily 的总退出码,这里只打印一行 `FAIL` 的话,图谱质量验收拒绝写页面、
    模型配置错、写盘失败全都会被报成 `wiki: ok`——两张图长期停在旧版本,面板上
    却一路绿灯(2026-09-22 第 8 轮审查发现)。stdout 那行给面板看(它按
    `graph:`/`catgraph:` 前缀捞),完整 stderr 原样转出去进日志。
    """
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        print(f"{label}: FAIL 超时({timeout}s)")
        return f"{label} 超时({timeout}s)"
    if r.returncode == 0:
        print(f"{label}: ok")
        return ""
    err = (r.stderr or r.stdout or "").strip()
    print(f"{label}: FAIL {err[:200]}")
    if r.stderr:
        sys.stderr.write(r.stderr if r.stderr.endswith("\n") else r.stderr + "\n")
    return f"{label} 退出码 {r.returncode}"


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
    cmd = [sys.executable, str(HERE / "build_catgraph.py")]
    if a.skip_llm:
        # 分类图谱的归类结果长期缓存,日常只判当天新增的那几条;--skip-llm 时
        # 完全不判,用已有缓存重生成(新条目暂时落「待归类」)。
        cmd.append("--no-llm")
    fails = [x for x in (
        run_step("graph", [sys.executable, str(HERE / "build_graph_html.py"),
                           "--since", since], 300),
        run_step("catgraph", cmd, 1800),
    ) if x]

    if n_link or made or syn:
        W.append_log(W.VAULT, "wiki",
                     f"双链化 {n_link} 篇,digest {len(made)} 天,综合 {len(syn)} 实体")
    if fails:
        raise SystemExit("wiki_daily: " + ";".join(fails) + "(图谱未更新)")
    print("WIKI_DAILY_DONE")


if __name__ == "__main__":
    main()
