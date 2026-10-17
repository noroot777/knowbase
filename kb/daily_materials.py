"""日报素材清单的只读展示模型，保留落盘原文与素材身份。"""

from __future__ import annotations

import re

from kb.daily_page import REF_SEP

_HEAD = re.compile(r'^## 素材清单[(（](\d+)[ \t]*条[)）][ \t]*$', re.M)
_ROW_START = re.compile(r'\s*(\d+)\.[ \t]+')
_ROW_LINE = re.compile(r'^[ \t]*(\d+)\.[ \t]+', re.M)
_SOURCE_TAIL = re.compile(rf'{re.escape(REF_SEP)}(?:(?!{re.escape(REF_SEP)})[^\n·])+·')
_LINK = re.compile(r'\[((?:\\[\\\[\]]|[^\[\]\\])*)\]\(<([^<>\n]+)>\)', re.S)


def _number_matches(raw: str, expected: int) -> bool:
    return raw == str(expected)


def _entry(title: str, source: str, number: int) -> dict | None:
    link = _LINK.fullmatch(title)
    if link:
        url = link.group(2)
        if ' '.join(url.split()) != url:
            return None
        title = re.sub(r'\\([\\\[\]])', r'\1', link.group(1))
    else:
        if '](' in title:
            return None
        url = ''
    lines = [line.strip() for line in title.splitlines() if line.strip()]
    short = ' '.join((lines[0] if lines else '未命名素材').split())
    if len(short) > 72:
        short = short[:71].rstrip() + '…'
    return {
        'number': number, 'title': short, 'full_title': ' '.join(title.split()),
        'url': url, 'source': ' '.join(source.split()),
    }


def _section(body: str, head: re.Match) -> dict | None:
    tails = list(_SOURCE_TAIL.finditer(body, head.end()))
    count = len(tails)
    if not _number_matches(head.group(1), count):
        return None
    items = []
    cursor = head.end()
    for number, tail in enumerate(tails, 1):
        row = _ROW_START.match(body, cursor, tail.start())
        if row is None or not _number_matches(row.group(1), number):
            return None
        end = body.find('\n', tail.end())
        end = len(body) if end < 0 else end
        source = body[tail.start() + len(REF_SEP):end]
        if number > 1 and any(
            _number_matches(alternative.group(1), number)
            and _entry(body[alternative.end():tail.start()], source, number) is not None
            for alternative in _ROW_LINE.finditer(body, row.end(), tail.start())
        ):
            return None
        items.append({
            'number': number, 'title': body[row.end():tail.start()],
            'source': source, 'span': [row.end(), end],
        })
        cursor = end
    if count and body[cursor:].strip():
        return None
    return {
        'before': body[:head.start()], 'heading': head.group()[3:],
        'items': items, 'after': body[cursor:],
        'span': [head.start(), cursor],
    }


def material_view(body: str) -> dict | None:
    """只投影唯一且完整的展示结构；歧义原文不借归属容错解析来猜边界。"""
    candidates = [_section(body, head) for head in _HEAD.finditer(body)]
    candidates = [candidate for candidate in candidates if candidate is not None]
    if len(candidates) != 1:
        return None
    view = candidates[0]
    items = []
    for row in view['items']:
        item = _entry(row['title'], row['source'], row['number'])
        if item is None:
            return None
        items.append({**item, 'span': row['span']})
    view['items'] = items
    return view
