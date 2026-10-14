# -*- coding: utf-8 -*-
"""补齐 dr-youtube-dig 文档的 summary(一次性回填,幂等可重跑)。

dr-youtube-dig 是 pre_tagged 来源:summary 走 frontmatter 的 summary 字段,
但管线历史上没写过这个字段 → 库里 244 篇全空,问答/枚举兜底展示拿不到摘要。

这些文档的正文第一段就是标准的导语摘要(dig 管线的输出格式),
所以直接抽第一段回填,零 LLM 成本:
- 写进 md frontmatter(pre_tagged 的真源头,以后重建索引不丢)
- 同步 UPDATE documents.summary
- refresh_content_hash 防止下次 index 因 frontmatter 变化无谓重建 chunk/向量

用法: uv run python scripts/backfill_youtube_summaries.py [--dry-run]
"""

from __future__ import annotations

import json
import re
import sys

from kb import store
from kb.config import VAULT_ROOT
from kb.extract import refresh_content_hash
from kb.scanner import file_hash

MAX_LEN = 160

_MD_NOISE = [
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),          # 加粗
    (re.compile(r"\[(.+?)\]\([^)]*\)"), r"\1"),     # 链接
    (re.compile(r"`(.+?)`"), r"\1"),                # 行内代码
]


def first_paragraph(body: str) -> str:
    """正文第一段(跳过空行/标题/分隔线),压成一行纯文本。"""
    lines: list[str] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if lines:
                break
            continue
        if line.startswith("#") or set(line) <= {"-", "—"}:
            if lines:
                break
            continue
        lines.append(line)
    text = " ".join(lines)
    for pat, rep in _MD_NOISE:
        text = pat.sub(rep, text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_LEN]


def add_summary_to_frontmatter(text: str, summary: str) -> str | None:
    """把 summary 键插到 frontmatter 尾部(closing --- 之前)。
    已有 summary 键或没有 frontmatter 时返回 None(跳过)。"""
    m = re.match(r"\A(---\n)(.*?)(\n---\n)", text, re.S)
    if not m:
        return None
    if re.search(r"^summary\s*:", m.group(2), re.M):
        return None
    # json.dumps 产出带转义的双引号字符串,是合法 YAML 标量
    line = f"\nsummary: {json.dumps(summary, ensure_ascii=False)}"
    return text[:m.end(2)] + line + text[m.end(2):]


def main() -> int:
    dry = "--dry-run" in sys.argv
    conn = store.connect()
    rows = conn.execute(
        "SELECT path FROM documents WHERE source='dr-youtube-dig' AND summary=''"
    ).fetchall()
    print(f"待补 {len(rows)} 篇" + ("(dry-run)" if dry else ""))
    done = skipped = 0
    for r in rows:
        path = r["path"]
        full = VAULT_ROOT / path
        try:
            text = full.read_text(encoding="utf-8")
        except OSError as e:
            print(f"  跳过(读不到): {path} ({e})")
            skipped += 1
            continue
        m = re.match(r"\A---\n.*?\n---\n?", text, re.S)
        body = text[m.end():] if m else text
        summ = first_paragraph(body)
        if not summ:
            print(f"  跳过(抽不出导语): {path}")
            skipped += 1
            continue
        if dry:
            print(f"  {path[-60:]} → {summ[:80]}")
            done += 1
            continue
        new_text = add_summary_to_frontmatter(text, summ)
        if new_text is not None:
            full.write_text(new_text, encoding="utf-8")
            # 刷新只认「库里记的还是我动手之前那一份」(refresh_content_hash
            # 没有无条件分支了)。`text` 就是上面读出来的那份原文 —— 库里记的
            # 要是别的值,说明磁盘已经领先于索引,这一行就不该写:那等于替别人
            # 的改动签一张已索引的收据。0 行是对的末态,下一轮重建一次。
            refresh_content_hash(conn, path, file_hash(text))
        conn.execute("UPDATE documents SET summary=? WHERE path=?", (summ, path))
        done += 1
    conn.commit()
    print(f"完成: 补齐 {done}, 跳过 {skipped}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
