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


def _split_long(para: str) -> list[str]:
    """把超过 CHUNK_MAX 的单个段落再切开。

    技术文里的代码块常常整块没有空行,按 \\n{2,} 拆段拆不动它;不切开的话
    这一段会原样成为一个 chunk,把 embedding 的注意力显存按长度平方顶爆。
    优先按行切,单行还超长(压缩过的 json/minified 代码)就硬切。
    """
    if len(para) <= CHUNK_MAX:
        return [para]
    out: list[str] = []
    buf = ""
    for line in para.split("\n"):
        while len(line) > CHUNK_MAX:
            if buf:
                out.append(buf)
                buf = ""
            out.append(line[:CHUNK_MAX])
            line = line[CHUNK_MAX:]
        if buf and len(buf) + len(line) + 1 > CHUNK_MAX:
            out.append(buf)
            buf = ""
        buf += line + "\n"
    if buf.strip():
        out.append(buf)
    return out


def strip_markup(text: str) -> str:
    """剥掉图片引用和 md 语法噪音,只留可检索的文字。

    **「这一段有没有字」的唯一口径**:chunk 清理完是空的就不产生 chunk,所以
    「为什么一个 chunk 都没有」也只能问它(见 kb/extract.py 的 body_gap)。
    """
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
        cleaned = strip_markup(buf)
        if cleaned:
            chunks.append(Chunk(seq=len(chunks), heading=buf_head, text=cleaned))
        buf = ""

    for heading, seg in sections:
        # 大节:先清空 buffer,再按段落拆
        if len(seg) > CHUNK_MAX:
            flush()
            buf_head = heading
            for para in re.split(r"\n{2,}", seg):
                for piece in _split_long(para):
                    if len(buf) + len(piece) > CHUNK_MAX and buf:
                        flush()
                        buf_head = heading
                    buf += piece + "\n\n"
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
