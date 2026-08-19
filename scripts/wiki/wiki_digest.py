#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日动态摘要(digest):读 kb.db 某天文章元数据,claude CLI 综合成
「当日要闻 + 趋势信号」,写 <vault>/wiki/digest/YYYY-MM-DD.md。

被 wiki_daily.py 调用(index 由它统一重渲染);也可单独跑:
  python3 wiki_digest.py --date 2026-07-04
  python3 wiki_digest.py --since 2026-06-30 --until 2026-07-04
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import wikilib as W  # noqa: E402

PROMPT = """你是 AI 行业情报分析师。下面是 {date} 当天入库的 {n} 篇公众号文章的元数据。\
写一份当日动态摘要,输出 markdown(不要代码围栏),不要逐条罗列所有文章。

结构严格如下:

## 当日要闻

挑 3~6 条真正重要的事,每条一个列表项:**一句话点出事件**:再用 2~3 句展开(是什么、为什么值得注意)。\
句末用来源编号标注,如 [3][7]。工具使用教程、旧闻盘点、招聘、纯营销类内容不算要闻。

## 趋势信号

1~3 条跨文章的观察:多篇文章共同指向的方向、与此前认知不同的变化。当天素材撑不起就只写一行:今日无明显新信号。

规则:
- 只依据下面给定的摘要写,禁止编造事实、数字、评价。
- 下列名字在知识库里有 wiki 页,正文首次提到时写成双链,例如 [[Claude Code]]:
{whitelist}

文章列表:
{items}
"""


def entity_whitelist(vault: Path) -> list[str]:
    names = []
    for sub in ("tools", "models", "orgs", "concepts", "topics"):
        d = vault / "wiki" / sub
        if d.exists():
            names += [p.stem for p in d.glob("*.md") if not p.stem.startswith("_")]
    return sorted(set(names))


def docs_of_day(con, dt: str):
    rows = con.execute(
        "SELECT path, title, account, published, summary FROM documents "
        "WHERE source=? AND published LIKE ? AND extracted_at IS NOT NULL "
        "ORDER BY published", (W.SOURCE, dt + "%")).fetchall()
    out = []
    for r in rows:
        ents = [e[0] for e in con.execute(
            "SELECT entity FROM entities WHERE path=?", (r["path"],)).fetchall()]
        tops = [t[0].split("/", 1)[1] for t in con.execute(
            "SELECT tag FROM doc_tags WHERE path=? AND tag LIKE 'topic/%'",
            (r["path"],)).fetchall()]
        out.append({**dict(r), "entities": ents, "topics": tops})
    return out


def call_claude(prompt: str, model: str, timeout: int = 240) -> str:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--model", model, "--output-format", "text"],
        capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"claude CLI 退出码 {proc.returncode}: {proc.stderr[:300]}")
    out = proc.stdout.strip()
    return re.sub(r"^```(?:markdown)?\s*|\s*```$", "", out).strip()


def build_digest(con, vault: Path, dt: str, model: str) -> bool:
    docs = docs_of_day(con, dt)
    if not docs:
        print(f"[{dt}] 无文章,跳过")
        return False
    items = "\n".join(
        f"{i+1}. [{d['account']}]{d['title']} — {d['summary'] or '(无摘要)'}"
        f"(实体: {', '.join(d['entities'][:6]) or '无'};主题: {', '.join(d['topics']) or '无'})"
        for i, d in enumerate(docs))
    prompt = PROMPT.format(date=dt, n=len(docs),
                           whitelist="、".join(entity_whitelist(vault)), items=items)
    body = call_claude(prompt, model)

    ref = "\n".join(f"{i+1}. [[{d['path'][:-3]}|{d['title']}]] — {d['account']}"
                    for i, d in enumerate(docs))
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    page = (f"---\ndate: {dt}\ndocs: {len(docs)}\nmodel: {model}\n"
            f"generated: \"{now}\"\n---\n\n# 动态摘要 · {dt}\n\n"
            f"{body}\n\n## 当日文章({len(docs)} 篇)\n\n{ref}\n")
    out = vault / "wiki" / "digest" / f"{dt}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    W.append_log(vault, "digest", f"{dt}({len(docs)} 篇,{model})")
    print(f"[{dt}] {len(docs)} 篇 -> {out}")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()
    if a.date:
        dates = [a.date]
    elif a.since and a.until:
        from datetime import date as D, timedelta
        d0, d1 = D.fromisoformat(a.since), D.fromisoformat(a.until)
        dates = [(d0 + timedelta(i)).isoformat() for i in range((d1 - d0).days + 1)]
    else:
        ap.error("需要 --date 或 --since/--until")
    con = W.connect_ro()
    made = [d for d in dates if build_digest(con, W.VAULT, d, a.model)]
    print(f"完成 {len(made)}/{len(dates)}(index 由 wiki_daily 统一重渲染)")


if __name__ == "__main__":
    main()
