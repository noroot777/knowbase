"""Shared X ingestion body conventions and conservative legacy provenance recovery."""
from __future__ import annotations

import re
from dataclasses import dataclass

_MEDIA_LINE = re.compile(r'(?:!\[(?:图片|视频封面)\]|\[视频\])\(https?://[^\s)]+\)')


def placeholder(author: str) -> str:
    return f'{author} 的动态'[:500]


def context_body(context: str, text: str) -> str:
    return f'> {context}\n\n{text}' if context else text


def feed_body(text: str, title: str) -> str:
    return text or title


def media_lines(media) -> str:
    if not isinstance(media, dict):
        return ''
    lines = []
    photos = media.get('photos') if isinstance(media.get('photos'), list) else []
    videos = media.get('videos') if isinstance(media.get('videos'), list) else []
    for photo in photos[:8]:
        if isinstance(photo, dict) and isinstance(photo.get('url'), str):
            lines.append(f"![图片]({photo['url']})")
    for video in videos[:4]:
        if not isinstance(video, dict):
            continue
        link = video.get('url') if isinstance(video.get('url'), str) else ''
        thumb = video.get('thumbnail_url') if isinstance(video.get('thumbnail_url'), str) else ''
        if thumb:
            lines.append(f'![视频封面]({thumb})')
        if link:
            lines.append(f'[视频]({link})')
    return '\n'.join(lines)


def _only_media(text: str) -> bool:
    return bool(text.strip()) and all(_MEDIA_LINE.fullmatch(line) for line in text.strip().splitlines())


def _has_title(text: str, title: str) -> bool:
    return bool(title.strip()) and re.sub(r'\s+', ' ', text).startswith(re.sub(r'\s+', ' ', title))


@dataclass(frozen=True)
class Body:
    prefix: str
    text: str
    media: str = ''

    def render(self, text: str) -> str:
        body = self.prefix + text
        return body + ('\n\n' if text else '') + self.media if self.media else body


def parts(entry: dict, *, prefix: str | None = None) -> Body:
    body = entry['body']
    title = entry.get('title', '')
    generated = title == placeholder(entry.get('author', ''))
    if prefix is None:
        head, separator, tail = body.partition('\n\n')
        attribution = head and all(line.startswith('> ') for line in head.splitlines())
        recognized = (separator and _has_title(tail, title)) or (
            generated and (not tail.strip() or _only_media(tail)))
        prefix = head + '\n\n' if attribution and recognized else ''
    raw = body.removeprefix(prefix).strip()
    if prefix and body.rstrip() == prefix.rstrip():
        raw = ''
    if generated and raw == title:
        raw = ''
    if generated and _only_media(raw):
        return Body(prefix, '', raw)
    text, separator, media = raw.rpartition('\n\n')
    if separator and _has_title(text, title) and _only_media(media):
        return Body(prefix, text, media)
    return Body(prefix, raw)
