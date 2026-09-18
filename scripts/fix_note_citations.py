#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 wiki/notes/ 里存量笔记正文的 `[n]` 补成可点的 Obsidian 内部链接。

背景:`/api/notes` 以前落盘的是模型原文,正文里的 `[1][2][3]` 是纯文本
(网页端那些引用链接是 chat.js 渲染时现拼的),只有末尾「## 引用」列表是双链。
新存的笔记已经在服务端补好了(kb/web/chat_api.linkify_citations),这个脚本
只管此前存下来的那些。

来源就是笔记自己末尾的「## 引用」列表(`n. [[路径|标题]] — 号`),不查库。

用法:
    python scripts/fix_note_citations.py           # dry-run,只报告
    python scripts/fix_note_citations.py --write   # 就地改写

notes 是手写资产区,所以默认不写盘;--write 时只改正文段(「## 引用」之前),
引用列表本身一个字不动,已经是链接的 `[\\[n\\]](...)` 也不会被二次处理。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from kb.config import VAULT_ROOT                        # noqa: E402
from kb.web.chat_api import linkify_citations           # noqa: E402

# 引用列表的一行:`12. [[source/xxx/yyy|标题]] — 号 · [原文](...)`
_REF_LINE = re.compile(r"^(\d+)\.\s+\[\[([^\]|]+)(?:\|([^\]]*))?\]\]")


def parse_refs(tail: str) -> list[dict]:
    refs = []
    for line in tail.splitlines():
        m = _REF_LINE.match(line.strip())
        if m:
            path = m.group(1 + 1)
            refs.append({"n": int(m.group(1)),
                         "path": path if path.endswith(".md") else path + ".md",
                         "title": (m.group(3) or "").strip()})
    return refs


def fix(text: str) -> tuple[str, int]:
    """返回 (新正文, 补上的引用数)。没有引用列表就原样返回。"""
    head, sep, tail = text.partition("\n## 引用\n")
    if not sep:
        return text, 0
    refs = parse_refs(tail)
    if not refs:
        return text, 0
    new = linkify_citations(head, refs)
    return new + sep + tail, len(re.findall(r"\[\\\[\d+\\\]\]\(", new))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="就地改写(默认只报告)")
    ap.add_argument("--vault", type=Path, default=VAULT_ROOT)
    args = ap.parse_args()

    notes = sorted((args.vault / "wiki" / "notes").glob("*.md"))
    if not notes:
        print("没找到笔记:", args.vault / "wiki" / "notes")
        return 1

    changed = 0
    for p in notes:
        old = p.read_text(encoding="utf-8")
        new, n = fix(old)
        if new == old:
            print(f"  跳过  {p.name}" + ("" if n else "  (没有引用列表或无 [n])"))
            continue
        changed += 1
        print(f"{'改写' if args.write else 'dry '}  {p.name}  补上 {n} 处引用链接")
        if args.write:
            p.write_text(new, encoding="utf-8")
    print(f"\n{changed} 篇" + ("已改写。" if args.write else "可改写(加 --write 生效)。"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
