# -*- coding: utf-8 -*-
"""补齐 dr-youtube-dig 文档的 summary(一次性回填,幂等可重跑)。

dr-youtube-dig 是 pre_tagged 来源:summary 走 frontmatter 的 summary 字段,
但管线历史上没写过这个字段 → 库里 244 篇全空,问答/枚举兜底展示拿不到摘要。

这些文档的正文第一段就是标准的导语摘要(dig 管线的输出格式),
所以直接抽第一段回填,零 LLM 成本:
- 写进 md frontmatter(pre_tagged 的真源头,以后重建索引不丢)
- 同步 UPDATE documents.summary
- 登记 content_hash,防止下次 index 因 frontmatter 变化无谓重建 chunk/向量

**整段走 `kb.doc_rewrite.rewriting()`**(逐篇一把维护锁:读 → 抽导语 → 写回 →
登记已索引 → 在锁里提交)。原来它在锁外,「写完文件 → 刷 hash」之间别人的一次
导出就能让那句刷新把一份**未索引**的新正文认证成「已索引」—— 末态是磁盘上新
正文、chunks 还是旧的,新正文一个字都搜不到,再同步也不恢复(2026-10-14 审查跨进程复现)。「手工一次性脚本,人在边上看着」挡不住另一个进程里的定时同步。
顺带钉住的还有一件:`summary` 那一列现在和写回去的是**同一份快照**。

用法: uv run python scripts/backfill_youtube_summaries.py [--dry-run]
"""

from __future__ import annotations

import json
import re
import sys

from kb import store
from kb.doc_rewrite import rewriting
from kb.maintenance import LockBusy

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
    """逐篇回填。**dry-run 也走协调入口**(它只是不写):两条读法分开写的话,
    预演看到的快照和真跑那一次不是同一份,而这个脚本存在的意义就是让人先看一眼。

    **等不到维护锁退 2,已经补上的那几篇照旧留着**:这个脚本幂等可重跑,下一趟
    从「summary 还空着」的那些接着补,所以「补了一半」是个合法中间态,回滚掉反
    而白干。退出码不许还是 0 —— 它是一次性运维脚本,人看完那行就走了,报 0 等于
    说「244 篇都补齐了」,而实际可能一篇没补。
    """
    dry = "--dry-run" in sys.argv
    conn = store.connect()
    rows = conn.execute(
        "SELECT path FROM documents WHERE source='dr-youtube-dig' AND summary=''"
    ).fetchall()
    print(f"待补 {len(rows)} 篇" + ("(dry-run)" if dry else ""))
    done = skipped = stale = 0
    try:
        for r in rows:
            path = r["path"]
            with rewriting(conn, path) as doc:
                if doc.text is None:
                    print(f"  跳过({doc.reason}): {path}")
                    skipped += 1
                    continue
                m = re.match(r"\A---\n.*?\n---\n?", doc.text, re.S)
                body = doc.text[m.end():] if m else doc.text
                summ = first_paragraph(body)
                if not summ:
                    print(f"  跳过(抽不出导语): {path}")
                    skipped += 1
                    continue
                if dry:
                    print(f"  {path[-60:]} → {summ[:80]}")
                    done += 1
                    continue
                new_text = add_summary_to_frontmatter(doc.text, summ)
                if new_text is not None:
                    # 登记那一步自己会证两样(写回的还在磁盘上、那组索引输入还是
                    # 索引里那一组),证不出来就保留待索引 —— 不在这儿替它判。
                    doc.write(new_text)
                # frontmatter 本来就有 summary 时上面返回 None(文件不必动),库里
                # 那一列照旧要同步 —— 这个脚本就是为了它。和写回去的是同一份快照。
                conn.execute("UPDATE documents SET summary=? WHERE path=?",
                             (summ, path))
                done += 1
            if doc.changed and not doc.indexed:
                stale += 1
    except LockBusy as e:
        conn.commit()          # 已经补上的那几篇落盘,下一趟从剩下的接着补
        print(f"完成: 补齐 {done}, 跳过 {skipped}")
        print(f"中止: 等不到维护锁({e}) —— 剩下的重跑一次就接上")
        return 2
    conn.commit()
    print(f"完成: 补齐 {done}, 跳过 {skipped}")
    if stale:
        print(f"  其中 {stale} 篇改完没登记已索引(磁盘领先于索引),留待下一轮重建")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
