#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""实体页「近期动态」综合段(它是什么 / 近期发生了什么 / 观点与分歧)。

写入实体页 marker 区,重跑只替换 marker 内。增量条件(select_targets):
14 天窗口内 >=3 篇,且(页面还没有综合段 或 窗口篇数比上次综合时多 >=growth)。

被 wiki_daily.py 调用;也可单独跑:
  python3 wiki_entity_synthesis.py --entity "Claude Code" --force
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wikilib as W  # noqa: E402

PROMPT = """你是知识库维护者。为实体「{name}」写一段「近期动态」综合,只依据下面 {n} 篇公众号文章的摘要与开头节选(时间范围 {window})。

输出 markdown(不要围栏、不要一级/二级标题),三个部分,各自一小段或几个要点:

**它是什么**:1~2 句定位。
**近期发生了什么**:按时间先后把具体事件串起来。
**观点与分歧**:各来源的评价是否一致?有无矛盾、争议或需要注意的偏差?口径一致就一句话说明一致在哪。

规则:
- 严格只用给定材料,禁止编造;具体日期/数字必须出自材料。
- 提到下列有 wiki 页的名字时,首次出现写成双链如 [[Codex]](不含「{name}」自己):{whitelist}
- 不要出现"文章1""来源2"这类编号,直接说"量子位的文章""InfoQ 的报道"。
- 总长 150~300 字。

材料:
{items}
"""


def body_excerpt(p: str, limit: int = 500) -> str:
    try:
        text = (W.VAULT / p).read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    body = text[m.end():] if m else text
    body = body.split(W.GRAPH_MARK)[0]                       # 去图谱尾区
    body = re.sub(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]", r"\2", body)
    body = re.sub(r"\[\[([^\[\]|]+)\]\]", r"\1", body)       # 去双链语法
    body = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", body)
    return re.sub(r"\n{2,}", "\n", body).strip()[:limit]


def call_claude(prompt: str, model: str, timeout: int = 240) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model, "--output-format", "text"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI {proc.returncode}: {proc.stderr[:300]}")
    out = proc.stdout.strip()
    return re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out).strip()


def select_targets(u: W.Universe, window_days: int = 14, min_docs: int = 3,
                   growth: int = 3, only: str | None = None,
                   force: bool = False) -> list[dict]:
    cutoff = max((date.today() - timedelta(days=window_days)).isoformat(), W.EPOCH)
    out = []
    for k in u.page_keys:
        sub, name = u.page_file[k]
        if only and name != only:
            continue
        win = sorted((p for p in u.ent_docs[k]
                      if u.docs[p]["published"][:10] >= cutoff),
                     key=lambda p: u.docs[p]["published"], reverse=True)
        if len(win) < min_docs:
            continue
        page = W.VAULT / "wiki" / sub / f"{name}.md"
        if not page.exists():
            continue
        if not force:
            last_n = W.synthesis_doc_count(page.read_text(encoding="utf-8"))
            if last_n is not None and len(win) - last_n < growth:
                continue
        out.append({"k": k, "name": name, "page": page, "win": win, "cutoff": cutoff})
    out.sort(key=lambda t: -len(t["win"]))
    return out


def run_targets(u: W.Universe, targets: list[dict], model: str = W.SONNET,
                workers: int = 3) -> list[str]:
    if not targets:
        return []
    wl_all = [t["name"] for t in targets] + \
             [u.page_file[k][1] for k in u.page_keys[:40]]

    def work(t):
        picked = t["win"][:6]
        items = "\n\n".join(
            f"- {u.docs[p]['published'][:10]} [{u.docs[p]['account']}]{u.docs[p]['title']}\n"
            f"  摘要: {u.docs[p]['summary'] or '无'}\n  开头节选: {body_excerpt(p)}"
            for p in picked)
        wl = "、".join(dict.fromkeys(n for n in wl_all if n != t["name"]))
        try:
            text = call_claude(PROMPT.format(
                name=t["name"], n=len(picked),
                window=f"{t['cutoff']} 起", whitelist=wl, items=items), model)
            page_text = t["page"].read_text(encoding="utf-8")
            t["page"].write_text(
                W.upsert_marker_section(page_text, text, len(t["win"])),
                encoding="utf-8")
            return t["name"]
        except Exception as e:
            print(f"  FAIL {t['name']}: {e}", file=sys.stderr)
            return None

    with ThreadPoolExecutor(workers) as ex:
        done = [r for r in ex.map(work, targets) if r]
    if done:
        W.append_log(W.VAULT, "synthesis", f"{len(done)} 个实体页近期动态({model})")
    return done


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--entity", help="只处理这个实体(页面名)")
    ap.add_argument("--min-docs", type=int, default=3)
    ap.add_argument("--window-days", type=int, default=14)
    ap.add_argument("--growth", type=int, default=3)
    ap.add_argument("--force", action="store_true", help="忽略增量条件,强制重写")
    ap.add_argument("--model", default=W.SONNET)
    ap.add_argument("--workers", type=int, default=3)
    a = ap.parse_args()
    u = W.Universe(W.connect_ro())
    targets = select_targets(u, a.window_days, a.min_docs, a.growth,
                             only=a.entity, force=a.force)
    print(f"待综合 {len(targets)} 个实体 (model={a.model})")
    done = run_targets(u, targets, a.model, a.workers)
    for n in done:
        print(f"  ok {n}")


if __name__ == "__main__":
    main()
