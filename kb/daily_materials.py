"""日报素材清单的只读展示模型，保留落盘原文与素材身份。"""

from __future__ import annotations

import re

from kb.daily_page import REF_SEP, _ENTRY_TAIL, _HEAD_LINE, _MATERIAL_LINK, _rows, _split


def _entry(raw: str, number: int) -> tuple[dict, str] | None:
    link = _MATERIAL_LINK.match(raw)
    if link:
        title = re.sub(r'\\([\\\[\]])', r'\1', raw[1:link.start(1) - 2])
        url = link.group(1).strip()
        if url.startswith('<') and url.endswith('>'):
            url = url[1:-1]
        source = raw[link.end():]
    else:
        tails = [tail for tail in re.finditer(re.escape(REF_SEP), raw)
                 if _ENTRY_TAIL.match(raw, tail.start())]
        if not tails:
            return None
        tail = tails[-1]
        title, url = raw[:tail.start()], ''
        source = raw[tail.start() + len(REF_SEP):]
    source, separator, after = source.partition('\n')
    if '·' not in source:
        return None
    lines = [line.strip() for line in title.splitlines() if line.strip()]
    short = ' '.join((lines[0] if lines else '未命名素材').split())
    if len(short) > 72:
        short = short[:71].rstrip() + '…'
    return {
        'number': number, 'title': short, 'full_title': ' '.join(title.split()),
        'url': url, 'source': ' '.join(source.split()),
    }, separator + after


def material_view(body: str) -> dict | None:
    """从日报自己的清单取展示数据，不查库、不重写文件，也不改变引用编号。"""
    rows = _rows(body)
    head = next((candidate for candidate in reversed(list(_HEAD_LINE.finditer(body)))
                 if int(candidate.group(1)) == len(rows)
                 and _split(body[candidate.end():]) == rows), None)
    if head is None:
        return None
    items = []
    after = ''
    for number, raw in enumerate(rows, 1):
        parsed = _entry(raw, number)
        if parsed is None:
            return None
        item, extra = parsed
        if number < len(rows) and extra.strip():
            return None
        items.append(item)
        after = extra
    line_end = body.find('\n', head.start())
    line_end = len(body) if line_end < 0 else line_end
    if not rows:
        after = body[line_end:]
    return {
        'before': body[:head.start()], 'heading': body[head.start() + 3:line_end],
        'items': items, 'after': after,
    }
