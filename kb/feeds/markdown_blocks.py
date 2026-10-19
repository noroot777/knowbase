"""Keep source code and ASCII diagrams out of model translation requests."""
from __future__ import annotations

import re
from uuid import uuid4

from markdown_it import MarkdownIt


def prepare(body: str, limit: int) -> tuple[str, dict[str, str], int]:
    offsets = [0]
    for boundary in re.finditer(r'\r\n|\r|\n|$', body):
        offsets.append(boundary.end())
    spans = [(offsets[token.map[0]], offsets[token.map[1]])
             for token in MarkdownIt('commonmark').parse(body)
             if token.type in ('fence', 'code_block') and token.map]
    cutoff = min(len(body), limit)
    for start, end in spans:
        if start < cutoff < end:
            cutoff = start
            break
    prefix = 'KB_PRESERVE_' + uuid4().hex + '_'
    protected = {}
    text = body[:cutoff]
    for start, end in reversed(spans):
        if end > cutoff:
            continue
        marker = prefix + str(len(protected)) + '_END'
        protected[marker] = body[start:end]
        text = text[:start] + '\n\n' + marker + '\n\n' + text[end:]
    return text, protected, cutoff


def restore(text: str, protected: dict[str, str]) -> str:
    positions = [text.find(marker) for marker in reversed(protected)]
    if positions != sorted(positions):
        raise RuntimeError('译文打乱了代码块顺序，本次结果不保存，请重试')
    for marker, original in protected.items():
        pattern = re.compile(r'(?m)^[ \t]*' + re.escape(marker) + r'[ \t]*$')
        if text.count(marker) != 1 or len(pattern.findall(text)) != 1:
            raise RuntimeError('译文未完整保留代码块标记，本次结果不保存，请重试')
        text = pattern.sub(lambda match: original.rstrip('\n'), text)
    return text
