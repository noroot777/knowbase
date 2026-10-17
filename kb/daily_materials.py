"""日报素材清单的只读展示模型，保留落盘原文与素材身份。"""

from __future__ import annotations

import re

from kb.daily_page import REF_SEP, _HEAD_LINE, _MATERIAL_LINK, _rows, _split

_SOURCE_TAIL = re.compile(rf'{re.escape(REF_SEP)}(?:(?!{re.escape(REF_SEP)})[^\n·])+·')


def _entry(raw: str, number: int) -> tuple[dict, int] | None:
    link = _MATERIAL_LINK.match(raw)
    if link and raw[link.end() - len(REF_SEP):link.end()] != REF_SEP:
        link = None
    if link:
        title = re.sub(r'\\([\\\[\]])', r'\1', raw[1:link.start(1) - 2])
        url = link.group(1).strip()
        if url.startswith('<') and url.endswith('>'):
            url = url[1:-1]
        source_start = link.end()
    else:
        tails = list(_SOURCE_TAIL.finditer(raw))
        if len(tails) != 1:
            return None
        tail = tails[0]
        title, url = raw[:tail.start()], ''
        source_start = tail.start() + len(REF_SEP)
    end = raw.find('\n', source_start)
    end = len(raw) if end < 0 else end
    if raw[end + 1:].partition('\n')[0].strip():
        return None
    source = raw[source_start:end]
    if '·' not in source:
        return None
    lines = [line.strip() for line in title.splitlines() if line.strip()]
    short = ' '.join((lines[0] if lines else '未命名素材').split())
    if len(short) > 72:
        short = short[:71].rstrip() + '…'
    return {
        'number': number, 'title': short, 'full_title': ' '.join(title.split()),
        'url': url, 'source': ' '.join(source.split()),
    }, end


def material_view(body: str) -> dict | None:
    """从日报自己的清单取展示数据，不查库、不重写文件，也不改变引用编号。"""
    rows = _rows(body)
    head = next((candidate for candidate in reversed(list(_HEAD_LINE.finditer(body)))
                 if int(candidate.group(1)) == len(rows)
                 and _split(body[candidate.end():]) == rows), None)
    if head is None:
        return None
    items = []
    cursor = head.end()
    section_end = body.find('\n', head.start())
    section_end = len(body) if section_end < 0 else section_end
    for number, raw in enumerate(rows, 1):
        parsed = _entry(raw, number)
        if parsed is None:
            return None
        item, end = parsed
        if number < len(rows) and raw[end:].strip():
            return None
        start = body.find(raw, cursor)
        if start < 0:
            return None
        item['span'] = [start, start + end]
        items.append(item)
        section_end = start + end
        cursor = start + len(raw)
    line_end = body.find('\n', head.start())
    line_end = len(body) if line_end < 0 else line_end
    return {
        'before': body[:head.start()], 'heading': body[head.start() + 3:line_end],
        'items': items, 'after': body[section_end:],
        'span': [head.start(), section_end],
    }
