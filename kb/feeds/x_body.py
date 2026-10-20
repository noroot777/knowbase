"""Shared X body rendering with producer-recorded, body-bound fragment provenance."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

PROVENANCE = 'body_provenance'


class SourceUnknown(ValueError):
    def __init__(self):
        super().__init__('正文来源待核:缺少或无法验证原有片段来源,保留原行;需可靠重新采集补证后再补全')


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


@dataclass(frozen=True)
class Body:
    prefix: str
    text: str
    media: str = ''
    fallback: str = ''
    source_known: bool = False

    def require_source(self) -> Body:
        if not self.source_known:
            raise SourceUnknown()
        return self

    def render(self, text: str) -> str:
        content = text or self.fallback
        body = self.prefix + content
        return body + ('\n\n' if content else '') + self.media if self.media else body


def _fingerprint(body: str) -> str:
    return hashlib.sha256(body.rstrip().encode()).hexdigest()


def record(entry: dict, text: str, *, prefix='', media='', fallback='') -> dict:
    fragments = Body(prefix, text, media, fallback)
    if fragments.render(text).rstrip() != entry['body'].rstrip():
        raise ValueError('X body does not match its producer-recorded fragments')
    evidence = dict(version=1, fingerprint=_fingerprint(entry['body']),
                    prefix=prefix, media=media, fallback=fallback)
    return {**entry, PROVENANCE: json.dumps(evidence, ensure_ascii=False, sort_keys=True)}


def parts(entry: dict) -> Body:
    body = entry['body']
    unknown = Body('', body)
    try:
        evidence = json.loads(entry.get(PROVENANCE) or '')
    except (TypeError, ValueError, RecursionError):
        return unknown
    if (not isinstance(evidence, dict) or evidence.get('version') != 1
            or evidence.get('fingerprint') != _fingerprint(body)
            or not all(isinstance(evidence.get(key), str) for key in ('prefix', 'media', 'fallback'))):
        return unknown
    prefix, media, fallback = (evidence[key] for key in ('prefix', 'media', 'fallback'))
    raw = body.rstrip()
    if prefix and raw == prefix.rstrip():
        raw = ''
    elif raw.startswith(prefix):
        raw = raw[len(prefix):]
    else:
        return unknown
    if media:
        if raw == media:
            raw = ''
        elif raw.endswith('\n\n' + media):
            raw = raw[:-len(media) - 2]
        else:
            return unknown
    if fallback:
        if raw != fallback:
            return unknown
        raw = ''
    fragments = Body(prefix, raw, media, fallback, source_known=True)
    return fragments if fragments.render(raw).rstrip() == body.rstrip() else unknown


def with_text(entry: dict, text: str) -> dict:
    fragments = parts(entry).require_source()
    body = Body(fragments.prefix, text, fragments.media)
    return record({**entry, 'body': body.render(text)}, text,
                  prefix=body.prefix, media=body.media)
