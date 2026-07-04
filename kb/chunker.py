# -*- coding: utf-8 -*-
"""Markdown → 检索用 chunk。

策略:按二级/三级标题切段落组,过小的合并、过大的按段落再切。
图片引用、frontmatter 已在上游去掉;这里再剥掉 md 语法噪音,
让 FTS 和 embedding 都吃干净文本。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# 单 chunk 目标大小(字符)。中文 ~1 字符 1 token 出头,
# 800-1200 字对 bge-m3(8k 窗口)和引用展示都合适。
CHUNK_TARGET = 1000
CHUNK_MAX = 1600


@dataclass
class Chunk:
    seq: int
    heading: str   # 所属小节标题("" = 文首)
    text: str


_IMG_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_HEADING_RE = re.compile(r"^(#{1,6})\s*(.+?)\s*$", re.M)


def _clean(text: str) -> str:
    text = _IMG_RE.sub("", text)
    text = _LINK_RE.sub(r"\1", text)       # 链接保留文字
    text = re.sub(r"[*_`>#]+", "", text)   # md 标记剥掉
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def split_chunks(body: str) -> list[Chunk]:
    """按标题切,合并小节、拆大节。"""
    # 先按标题行分段:[(heading, section_text), ...]
    sections: list[tuple[str, str]] = []
    last_end = 0
    last_heading = ""
    for m in _HEADING_RE.finditer(body):
        seg = body[last_end:m.start()]
        if seg.strip():
            sections.append((last_heading, seg))
        last_heading = m.group(2).strip()
        last_end = m.end()
    tail = body[last_end:]
    if tail.strip():
        sections.append((last_heading, tail))
    if not sections:
        return []

    chunks: list[Chunk] = []
    buf_head, buf = sections[0][0], ""

    def flush():
        nonlocal buf
        cleaned = _clean(buf)
        if cleaned:
            chunks.append(Chunk(seq=len(chunks), heading=buf_head, text=cleaned))
        buf = ""

    for heading, seg in sections:
        # 大节:先清空 buffer,再按段落拆
        if len(seg) > CHUNK_MAX:
            flush()
            buf_head = heading
            for para in re.split(r"\n{2,}", seg):
                if len(buf) + len(para) > CHUNK_MAX and buf:
                    flush()
                    buf_head = heading
                buf += para + "\n\n"
            flush()
            continue
        # 正常节:凑到目标大小再切
        if len(buf) + len(seg) > CHUNK_TARGET and buf:
            flush()
            buf_head = heading
        if not buf:
            buf_head = heading
        buf += seg + "\n\n"
    flush()
    return chunks
