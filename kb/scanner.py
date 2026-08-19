# -*- coding: utf-8 -*-
"""扫描 obs vault,解析每篇 md 的 frontmatter 与正文。

产出 Doc 对象流,供索引层消费。增量逻辑靠 content_hash:
文件内容没变的文档,索引层直接跳过。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import yaml

from kb.config import SOURCES, VAULT_ROOT, Source


@dataclass
class Doc:
    path: str                # 相对 VAULT_ROOT 的路径(索引主键)
    source: str              # 来源标识
    title: str
    account: str             # 公众号名/频道名
    published: str
    url: str
    tags: list[str] = field(default_factory=list)
    body: str = ""           # 去 frontmatter 后的正文
    content_hash: str = ""
    meta: dict = field(default_factory=dict)  # 完整 frontmatter(project/kind/summary 等)


_FM_RE = re.compile(r"\A---\n(.*?)\n---\n?", re.S)

# wiki 双链与图谱尾区只是阅读层装饰(linkify 后处理产物),索引/向量必须
# 看到与 linkify 前完全一致的正文:[[页|原文]]→原文,[[页]]→页,
# <!-- kb-graph --> 之后整段丢弃。这保证 linkify 不改变 chunk 与 embedding。
_GRAPH_FOOTER_RE = re.compile(r"\n<!-- kb-graph -->.*\Z", re.S)
_WIKILINK_RE = re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]|\[\[([^\[\]|]+)\]\]")


def normalize_body(body: str) -> str:
    """索引视角的正文:剥掉双链语法与图谱尾区。"""
    body = _GRAPH_FOOTER_RE.sub("", body)
    return _WIKILINK_RE.sub(lambda m: m.group(2) or m.group(3), body)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """拆 frontmatter 和正文。没有 frontmatter 时返回 ({}, 全文)。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}
    return fm, text[m.end():]


def _doc_from_file(md_path: Path, src: Source) -> Doc | None:
    try:
        text = md_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    fm, body = parse_frontmatter(text)

    tags = fm.get("tags") or []
    if not isinstance(tags, list):
        tags = [str(tags)]
    tags = [str(t).strip() for t in tags if str(t).strip()]

    # channel(youtube) 和 account(gzh) 语义相同,归一到 account
    account = str(fm.get("account") or fm.get("channel") or "").strip()
    title = str(fm.get("title") or "").strip() or md_path.stem
    url = str(fm.get("url") or fm.get("youtube") or "").strip()
    published = str(fm.get("published") or fm.get("publish_time") or "").strip()

    return Doc(
        path=str(md_path.relative_to(VAULT_ROOT)),
        source=str(fm.get("source") or src.name),
        title=title,
        account=account,
        published=published,
        url=url,
        tags=tags,
        body=normalize_body(body),
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        meta=fm,
    )


def scan_all() -> Iterator[Doc]:
    """遍历所有来源,产出 Doc。"""
    for src in SOURCES:
        if not src.root.is_dir():
            continue
        for md_path in sorted(src.root.rglob("*.md")):
            rel_parts = md_path.relative_to(src.root).parts
            if rel_parts and rel_parts[0] in src.skip_dirs:
                continue
            doc = _doc_from_file(md_path, src)
            if doc is not None:
                yield doc
